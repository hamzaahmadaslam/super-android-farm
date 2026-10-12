"""Run a command on one Android or on many, and the plumbing every control module shares.

A command is always an argument list. `adb shell` joins its arguments into one line for
the device's shell, so each argument is quoted for that shell first; nothing typed by a
user is ever spliced into a line that this code wrote.

Three bounds apply to every command, because one misbehaving device must not be able to
hold up or bloat a fleet operation:

    time     the command is killed at its deadline;
    output   reading stops (and the command is killed) once the cap is reached, so
             `cat /dev/zero` costs a few hundred KB, not the daemon's memory;
    reach    a device that does not answer ADB fails fast with a message that says so.

The denylist is a seat belt against typos and pasted one-liners, not a security
boundary: anyone allowed to run shell commands on a device can do harm in ways no list
will catch. It exists because a single `rm -rf /sdcard` sent to `all` is unrecoverable,
and because Androids share their network namespace with the VPN sidecar, so flushing
iptables inside an Android would silently open the kill switch.
"""
from __future__ import annotations

import os
import posixpath
import re
import selectors
import shlex
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from collections.abc import Callable, Iterable, Sequence

from ..core.adb import Adb, AdbError, quote
from ..core.models import check_id
from ..core.tasks import Task, TaskManager, run_on_instances
from ..util.proc import kill_tree, spawn

if TYPE_CHECKING:   # the instance manager is another area's module; only its interface is used
    from ..core.instances import InstanceManager

MAX_COMMAND_BYTES = 64 * 1024


class Refused(ValueError):
    """A command the denylist stopped. Pass force=True to run it anyway."""


# --------------------------------------------------------------------------- shared plumbing

@dataclass(frozen=True)
class ControlSettings:
    concurrency: int = 8
    install_timeout: int = 900
    shell_timeout: int = 60
    shell_output_kb: int = 256
    shell_denylist: bool = True
    max_app_mb: int = 8192
    max_transfer_mb: int = 2048


def settings(config: Any) -> ControlSettings:
    """The `control` section of the farm config. Falls back to the built-in defaults for
    any field a (possibly older) config object does not have."""
    section = getattr(config, "control", None)
    defaults = ControlSettings()
    if section is None:
        return defaults
    return ControlSettings(**{k: getattr(section, k, v) for k, v in asdict(defaults).items()})


def selector_text(selector: str | int | Iterable[int]) -> str:
    """Callers may pass a selector string, one id or a list of ids; the manager only
    speaks selectors, and ids are validated on the way."""
    if isinstance(selector, str):
        return selector
    if isinstance(selector, int):
        return str(check_id(selector))
    return ",".join(str(check_id(i)) for i in selector)


def pick(mgr: InstanceManager, selector: str | int | Iterable[int]) -> list[int]:
    text = selector_text(selector)
    ids = mgr.select(text)
    if not ids:
        raise ValueError(f"selector {text!r} matches no Android")
    return ids


def reach(mgr: InstanceManager, instance_id: int) -> Adb:
    """An ADB handle that is known to answer. The ADB server forgets TCP devices when it
    restarts, so a device that looks offline gets one reconnect before it is given up."""
    adb = mgr.adb(check_id(instance_id))
    if adb.state() != "device":
        # A stale "offline" entry blocks `adb connect` ("already connected"); drop it first.
        adb.disconnect()
        if not adb.connect(attempts=2, delay=0.5):
            raise AdbError(f"Android {instance_id} does not answer on ADB ({adb.serial}); it is stopped or still booting")
    return adb


def fleet(mgr: InstanceManager, tasks: TaskManager, kind: str, title: str, selector: str | int | Iterable[int],
          fn: Callable[[int, Adb, Task], str | None], *, heavy: bool = False) -> Task:
    """One Task that runs fn(id, adb, task) on every selected Android, `control.concurrency`
    at a time. Each device's outcome is its own; a failure never stops the others.

    heavy marks work that restarts the Android framework: booting is the most expensive
    moment of an Android's life, so those also respect `performance.start_concurrency`."""
    ids = pick(mgr, selector)
    limit = settings(mgr.config).concurrency
    if heavy:
        limit = min(limit, int(getattr(getattr(mgr.config, "performance", None), "start_concurrency", limit)))

    def one(instance_id: int, task: Task) -> str | None:
        return fn(instance_id, reach(mgr, instance_id), task)

    return run_on_instances(tasks, kind, title, ids, one, concurrency=max(1, limit))


@dataclass
class Out:
    """What a device command said and how it ended. Android's tools are inconsistent:
    `pm grant` reports failure only on stderr with exit 255, `am start` prints `Error:` on
    stderr and exits 0, `pm uninstall` prints `Failure [...]` on stdout. Callers need all
    three to tell success from failure."""
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return "\n".join(t for t in (self.stdout.strip(), self.stderr.strip()) if t)

    @property
    def last(self) -> str:
        """The line worth showing a person: the last one that is not a Java stack frame."""
        lines = [ln.strip() for ln in self.text.splitlines() if ln.strip() and not ln.strip().startswith("at ")]
        return (lines[-1] if lines else "no output")[:240]


def sh(adb: Adb, command: str | Sequence[str], *, timeout: float = 30) -> Out:
    """Run a device command and keep exit code, stdout and stderr apart. A list is quoted
    argument by argument; a string must be a fixed line written in this code base."""
    line = command if isinstance(command, str) else " ".join(quote(a) for a in command)
    r = adb.raw(["shell", line], timeout=timeout, check=False)
    return Out(r.code, r.stdout.replace("\r\n", "\n"), r.stderr.replace("\r\n", "\n"))


def is_root(adb: Adb) -> bool:
    return adb.shell("id -u", timeout=10, check=False) == "0"


def root_argv(adb: Adb, argv: Sequence[str]) -> list[str]:
    """argv, prefixed so it runs as root. Some images run `adb shell` as root, others as
    the unprivileged `shell` user with a `su` binary beside it; both must work."""
    return list(argv) if is_root(adb) else ["su", "0", *argv]


def root_sh(adb: Adb, argv: Sequence[str], *, timeout: float = 30) -> Out:
    return sh(adb, root_argv(adb, argv), timeout=timeout)


# --------------------------------------------------------------------------- bounded execution

@dataclass
class ExecResult:
    code: int | None        # None when the command was killed (deadline or output cap)
    output: str             # stdout and stderr interleaved, as a terminal would show them
    truncated: bool
    timed_out: bool
    seconds: float

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def to_dict(self) -> dict:
        return asdict(self)


def run_bounded(argv: Sequence[str], *, timeout: float, max_bytes: int) -> ExecResult:
    """Run a host command, keeping at most max_bytes of output and at most `timeout`
    seconds of patience. Unlike subprocess.run(capture_output=True) the output is never
    buffered beyond the cap."""
    started = time.monotonic()
    try:
        p = spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        return ExecResult(127, f"{argv[0]}: command not found", False, False, 0.0)
    assert p.stdout is not None
    fd = p.stdout.fileno()
    os.set_blocking(fd, False)
    buf = bytearray()
    truncated = timed_out = False
    deadline = started + timeout
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not sel.select(min(remaining, 0.5)):
                # No data. If the child is gone but something it started still holds the
                # pipe open, waiting for EOF would last until the deadline for nothing.
                if p.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                break
            room = max_bytes - len(buf)
            buf += chunk[:room]
            if len(chunk) > room:
                truncated = True
                break
    if timed_out or truncated:
        kill_tree(p.pid, signal.SIGKILL)
    try:
        code: int | None = p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        kill_tree(p.pid, signal.SIGKILL)
        code = None
    p.stdout.close()
    if timed_out or truncated:
        code = None
    return ExecResult(code, buf.decode("utf-8", "replace").replace("\r\n", "\n"), truncated, timed_out,
                      round(time.monotonic() - started, 2))


def check_command(command: Sequence[str]) -> list[str]:
    if isinstance(command, (str, bytes)) or not command:
        raise TypeError("command must be a non-empty list of arguments (split a command line with shlex.split first)")
    argv = [str(a) for a in command]
    if any("\x00" in a for a in argv):
        raise ValueError("command contains a NUL byte")
    if sum(len(a.encode()) + 1 for a in argv) > MAX_COMMAND_BYTES:
        raise ValueError(f"command is longer than {MAX_COMMAND_BYTES} bytes")
    return argv


def device_exec(adb: Adb, command: Sequence[str], *, timeout: float = 60, max_bytes: int = 256 * 1024,
                root: bool = False) -> ExecResult:
    """Run one argv on one device with the time and output bounds. The exit code is the
    command's own (ADB's shell protocol carries it back)."""
    argv = check_command(command)
    if root:
        argv = root_argv(adb, argv)
    line = " ".join(quote(a) for a in argv)
    return run_bounded([adb.binary, "-s", adb.serial, "shell", line], timeout=timeout, max_bytes=max_bytes)


# --------------------------------------------------------------------------- denylist

_SHELLS = {"sh", "bash", "ash", "mksh", "dash"}
_WRAPPERS = {"nohup", "time", "exec", "toybox", "busybox", "nice", "setsid", "env", "command", "builtin", "xargs", "strace"}
_POWER = {"reboot", "halt", "poweroff", "shutdown"}
_FORMAT = re.compile(r"^(mkfs(\..+)?|mke2fs|make_ext4fs|make_f2fs|newfs_msdos|wipefs|blkdiscard|sgdisk|parted|fdisk)\Z")
_IPTABLES = {"iptables", "ip6tables", "iptables-legacy", "ip6tables-legacy", "iptables-nft", "ip6tables-nft"}
_FIREWALL_ALWAYS = {"iptables-restore", "ip6tables-restore", "ndc"}
# Listing rules is harmless and is how one checks the kill switch; anything else changes them.
_IPTABLES_READ_ONLY = re.compile(r"^(-[LSnvx]+|--list(-rules)?|--line-numbers|--numeric|--verbose|-t|-w|[a-z]+|[A-Z_]+)\Z")
_IP_CHANGE = {"add", "del", "delete", "flush", "set", "replace", "change", "append", "prepend", "up", "down"}
_VITAL_PROCESSES = {"init", "zygote", "zygote64", "system_server", "surfaceflinger", "servicemanager", "adbd",
                    "netd", "vold", "lmkd", "installd"}
_VITAL_PACKAGES = {"android", "com.android.systemui", "com.android.shell", "com.android.settings",
                   "com.android.providers.settings", "com.android.providers.media.module", "com.android.permissioncontroller"}
# Directories whose removal (or whose every child's removal) leaves the Android or its
# user data unusable. Deeper paths are the user's business.
_VITAL_DIRS = {
    "/", "/data", "/data/data", "/data/app", "/data/user", "/data/user/0", "/data/user_de", "/data/user_de/0",
    "/data/system", "/data/misc", "/data/media", "/data/media/0", "/data/local", "/sdcard", "/storage",
    "/storage/emulated", "/storage/emulated/0", "/mnt", "/mnt/sdcard", "/system", "/vendor", "/product",
    "/system_ext", "/apex", "/dev", "/proc", "/sys", "/etc", "/bin", "/sbin", "/odm",
    "/sdcard/Android", "/sdcard/Android/data", "/sdcard/Android/obb",
}
_FIND_NARROWING = {"-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-newer", "-mtime", "-mmin", "-size"}
_DANGEROUS_SINK = re.compile(r"^(/dev/block|/dev/[sv]d[a-z]|/dev/nvme|/dev/mmcblk|/proc/sysrq-trigger|/sys/power/state)")
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{")
_WIPE_ACTIONS = ("MASTER_CLEAR", "FACTORY_RESET")


def _vital_path(path: str) -> str | None:
    """The vital directory a path names, also when written as `dir/`, `dir/*` or `dir/.`."""
    if not path.startswith("/"):
        return None
    for suffix in ("/*", "/.*", "/."):
        if path.endswith(suffix):
            path = path[: -len(suffix)] or "/"
            break
    norm = posixpath.normpath(path)
    norm = "/" if norm.startswith("//") else norm
    return norm if norm in _VITAL_DIRS else None


def _split_script(script: str) -> list[list[str]]:
    """Break a shell script into its simple commands (best effort: enough to see through
    `sh -c 'a; b && c'`, not a shell parser)."""
    script = script.replace("\n", " ; ")     # shlex treats a newline as plain whitespace
    try:
        lexer = shlex.shlex(script, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = re.split(r"\s+|(?=[;&|])|(?<=[;&|])", script)
    commands: list[list[str]] = [[]]
    for tok in tokens:
        if tok and set(tok) <= set(";&|()"):
            commands.append([])
        elif tok:
            commands[-1].append(tok)
    return [c for c in commands if c]


def _expand(argv: list[str], depth: int = 0) -> Iterable[list[str]]:
    """The command itself and every command it would start through a wrapper or `sh -c`."""
    if not argv:
        return
    yield argv
    if depth >= 4:
        return
    name, rest = posixpath.basename(argv[0]), argv[1:]
    if name == "su":
        while rest and (rest[0] in ("-", "-l", "--login", "root", "shell", "system") or rest[0].isdigit()):
            rest = rest[1:]
        if rest[:1] == ["-c"]:
            name, rest = "sh", rest
        else:
            yield from _expand(rest, depth + 1)
            return
    if name in _SHELLS:
        if "-c" in rest and rest.index("-c") + 1 < len(rest):
            for cmd in _split_script(rest[rest.index("-c") + 1]):
                yield from _expand(cmd, depth + 1)
    elif name == "timeout":
        tail = [a for a in rest if not a.startswith("-")]
        yield from _expand(tail[1:], depth + 1)
    elif name in _WRAPPERS:
        tail = list(rest)
        while tail and (tail[0].startswith("-") or (name == "env" and "=" in tail[0])):
            tail = tail[1:]
        yield from _expand(tail, depth + 1)


def _reason(argv: list[str]) -> str | None:
    name, args = posixpath.basename(argv[0]), argv[1:]
    words = [a for a in args if not a.startswith("-")]
    flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--"))
    long_flags = {a for a in args if a.startswith("--")}
    recursive = "r" in flags or "R" in flags or "--recursive" in long_flags

    if name in _POWER or (name == "svc" and words[:1] == ["power"] and set(words) & {"reboot", "shutdown"}):
        return "it powers the Android off or reboots it (use the instance restart instead)"
    if name == "setprop" and words[:1] and (words[0] == "sys.powerctl" or words[0] == "ctl.stop"):
        return "it stops system services or powers the Android off"
    if name == "stop":
        return "it stops the Android framework"
    if _FORMAT.match(name):
        return "it formats or repartitions storage"
    if name == "rm":
        for target in words:
            vital = _vital_path(target)
            if vital and (recursive or target.endswith(("*", "/"))):
                return f"it deletes {vital}"
    if name == "find" and words and ("-delete" in args or "-exec" in args or "-execdir" in args):
        vital = _vital_path(words[0])
        # `find /sdcard -name '*.tmp' -delete` is ordinary housekeeping; an unfiltered delete is a wipe.
        unfiltered = not (set(args) & _FIND_NARROWING)
        if vital and unfiltered and ("-delete" in args or any(posixpath.basename(a) in ("rm", "shred") for a in args)):
            return f"it deletes files under {vital}"
    if name in ("mv", "shred") and words:
        vital = next((v for v in map(_vital_path, words[:-1] if name == "mv" else words) if v), None)
        if vital:
            return f"it moves or destroys {vital}"
    if name in ("chmod", "chown", "chgrp", "chcon") and recursive:
        vital = next((v for v in map(_vital_path, words[1:]) if v), None)
        if vital:
            return f"it changes ownership or permissions of everything under {vital}"
    if name == "dd":
        sink = next((a[3:] for a in args if a.startswith("of=")), "")
        if _DANGEROUS_SINK.match(sink):
            return f"it overwrites {sink}"
    if any(a in (">", ">>") and i + 1 < len(argv) and _DANGEROUS_SINK.match(argv[i + 1]) for i, a in enumerate(argv)):
        return "it writes to a raw device or a kernel trigger"
    changes_firewall = (name in _FIREWALL_ALWAYS
                        or (name in _IPTABLES and not all(_IPTABLES_READ_ONLY.match(a) for a in args))
                        or (name == "nft" and words[:1] != ["list"])
                        or (name == "tc" and bool(set(words) & _IP_CHANGE)))
    if changes_firewall:
        return ("it changes firewall or routing rules; the Android shares its network namespace with the "
                "network sidecar, so this can open the VPN kill switch")
    if name in ("ip", "ifconfig", "route") and (set(words) & _IP_CHANGE):
        return "it changes network interfaces or routes that the network sidecar manages"
    if name in ("kill", "killall", "pkill"):
        if name == "kill" and "1" in words:
            return "it kills init (the whole Android)"
        if name != "kill" and set(words) & _VITAL_PROCESSES:
            return "it kills a vital system process"
    if name in ("pm", "cmd"):
        sub = words[1:] if name == "cmd" and words[:1] == ["package"] else words if name == "pm" else []
        if sub[:1] and sub[0] in ("uninstall", "disable", "disable-user", "hide", "suspend", "clear"):
            pkgs = [w for w in sub[1:] if w in _VITAL_PACKAGES or (sub[0] == "uninstall" and w.startswith("com.android."))]
            if pkgs:
                return f"it removes or disables the system package {pkgs[0]}"
        if name == "cmd" and words[:2] == ["testharness", "enable"]:
            return "it factory-resets the Android"
    if name == "locksettings" and words[:1] and words[0].startswith("set-"):
        return "it sets a lock-screen credential, which locks automation out of the device"
    if name in ("am", "cmd") and any(w in a for a in args for w in _WIPE_ACTIONS):
        return "it factory-resets the Android"
    if name == "recovery" or (name == "wipe" and words):
        return "it wipes user data"
    return None


def destructive_reason(command: Sequence[str]) -> str | None:
    """Why this command is refused without force, or None when it may run."""
    argv = [str(a) for a in command]
    if any(_FORK_BOMB.search(a) for a in argv):
        return "it is a fork bomb"
    for simple in _expand(argv):
        reason = _reason(simple)
        if reason:
            return reason
    return None


# --------------------------------------------------------------------------- the fleet API

class Shell:
    """Shell commands across the fleet.

    A Task's per-device result only has room for a one-line summary, so full outputs are
    kept here, per task, and read with outputs(task_id). Only the most recent tasks are
    kept: output of a finished command is a convenience, not a log.
    """

    def __init__(self, mgr: InstanceManager, tasks: TaskManager, *, keep: int = 50):
        self.mgr, self.tasks = mgr, tasks
        self._keep = keep
        self._outputs: OrderedDict[str, dict[int, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def _limits(self, timeout: float | None, max_output_kb: int | None) -> tuple[float, int]:
        s = settings(self.mgr.config)
        t = float(timeout) if timeout else float(s.shell_timeout)
        if not 0 < t <= 3600:
            raise ValueError("timeout must be between 1 second and 1 hour")
        kb = int(max_output_kb) if max_output_kb else s.shell_output_kb
        return t, max(1, min(kb, 16384)) * 1024

    def _guard(self, argv: list[str], force: bool) -> None:
        if force or not settings(self.mgr.config).shell_denylist:
            return
        reason = destructive_reason(argv)
        if reason:
            raise Refused(f"refused: {reason}. Run it with force if that is really what you want.")

    def exec(self, id: int, command: Sequence[str], *, timeout: float | None = None, force: bool = False,
             root: bool = False, max_output_kb: int | None = None) -> dict:
        """Run a command on one Android now and return {code, output, truncated, timed_out, seconds}."""
        argv = check_command(command)
        self._guard(argv, force)
        t, cap = self._limits(timeout, max_output_kb)
        return device_exec(reach(self.mgr, id), argv, timeout=t, max_bytes=cap, root=root).to_dict()

    def run(self, selector: str | int | Iterable[int], command: Sequence[str], *, timeout: float | None = None,
            force: bool = False, root: bool = False, max_output_kb: int | None = None) -> Task:
        """Run a command on every selected Android. Per device: ok when it exits 0."""
        argv = check_command(command)
        self._guard(argv, force)
        t, cap = self._limits(timeout, max_output_kb)

        def one(instance_id: int, adb: Adb, task: Task) -> str:
            result = device_exec(adb, argv, timeout=t, max_bytes=cap, root=root)
            self._remember(task.id, instance_id, result.to_dict())
            last = (result.output.strip().splitlines() or [""])[-1][:300]
            if result.timed_out:
                raise TimeoutError(f"no result after {t:g}s; command stopped")
            if result.truncated:
                raise RuntimeError(f"printed more than {cap // 1024} KB; command stopped")
            if result.code != 0:
                raise RuntimeError(f"exit {result.code}: {last}")
            return last or "ok"

        title = "shell: " + " ".join(argv)[:80]
        return fleet(self.mgr, self.tasks, "shell", title, selector, one)

    def _remember(self, task_id: str, instance_id: int, result: dict) -> None:
        with self._lock:
            self._outputs.setdefault(task_id, {})[instance_id] = result
            self._outputs.move_to_end(task_id)
            while len(self._outputs) > self._keep:
                self._outputs.popitem(last=False)

    def outputs(self, task_id: str) -> dict[int, dict]:
        """Full per-device results of a run() task: {instance id: {code, output, ...}}."""
        with self._lock:
            return {k: dict(v) for k, v in self._outputs.get(task_id, {}).items()}

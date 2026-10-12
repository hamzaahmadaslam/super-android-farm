"""One-time (and every-boot) host preparation. The only part of the farm that needs root.

    plan()    works out what this machine needs, as a list of steps made of small actions
    apply()   carries the steps out; with dry_run=True it runs nothing and returns the same
              steps with every command and file spelled out

Design rules:

* Idempotent. Every step can be repeated: files are rewritten only when their content
  differs, packages already on PATH are not reinstalled, devices that exist are not
  re-created.
* One failed step never aborts the rest; each step reports its own status.
* Two modes. The full run (install time) writes configuration files and installs
  packages. The boot run (`--boot`, from superfarm-host.service) only re-establishes what
  does not survive a reboot: loaded modules, zram, KSM, camera devices, /run folders.
* `only` limits a run to named steps (`--only packages`). The installer test uses it to
  install the distribution packages inside a container, where kernel modules, sysctl and
  systemd do not exist.
* Root executes fixed commands with validated values. Nothing from the farm's YAML is
  ever passed to a shell; the only free-form values (user, group, compression algorithm)
  are checked against strict patterns first.

Everything here is root-only and therefore exercised only through --dry-run and unit
tests with a fake root on the development machine.
"""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterable

from ..config import Config, load_config
from ..paths import Paths, get_paths
from ..resources import resource_path
from ..util import proc
from ..util.files import atomic_write
from . import capacity, distro as distro_mod, modules

RUN_DIR = "/run/superfarm"
SYSCTL_FILE = "/etc/sysctl.d/90-superfarm.conf"
TMPFILES_FILE = "/etc/tmpfiles.d/superfarm.conf"
UNIT_DIR = "/etc/systemd/system"
# The only unit root installs. The daemon itself is a *user* unit, installed without root
# by `sf service install` (see superfarm.host.service).
UNITS = ("superfarm-host.service",)
DEFAULT_GROUP = "docker"
# Every step id plan() can produce, in order.
STEP_IDS = ("packages", "docker-service", "groups", "modules", "sysctl", "run-dirs", "zram", "ksm", "camera", "systemd")

_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\$?$")
_ALGO = re.compile(r"^[a-z0-9-]{1,20}$")


@dataclass(frozen=True)
class Sysctl:
    key: str
    minimum: int
    why: str


# Kernel limits that are fine for a desktop and far too small for hundreds of containers.
# Values are floors: a machine that already has more is left alone.
SYSCTLS: tuple[Sysctl, ...] = (
    Sysctl("fs.inotify.max_user_instances", 8192,
           "Android opens inotify instances freely and all Androids' system processes share uid 0 "
           "and uid 1000; the default of 128 is exhausted by a handful of them."),
    Sysctl("fs.inotify.max_user_watches", 1048576, "Same reason, for watches."),
    Sysctl("kernel.pid_max", 4194304,
           "One Android is roughly 80 processes and well over 1000 threads; the classic limit of 32768 "
           "ids is reached at about 30 Androids."),
    Sysctl("fs.file-max", 2097152, "Open-file ceiling for the whole machine."),
    Sysctl("net.ipv4.neigh.default.gc_thresh1", 4096,
           "Every container is a neighbour-table entry; at the default 128/512/1024 the kernel starts "
           "dropping entries ('neighbour table overflow') and containers lose connectivity at random."),
    Sysctl("net.ipv4.neigh.default.gc_thresh2", 8192, "See gc_thresh1."),
    Sysctl("net.ipv4.neigh.default.gc_thresh3", 16384, "See gc_thresh1."),
    Sysctl("net.ipv6.neigh.default.gc_thresh1", 4096, "See net.ipv4 gc_thresh1."),
    Sysctl("net.ipv6.neigh.default.gc_thresh2", 8192, "See net.ipv4 gc_thresh1."),
    Sysctl("net.ipv6.neigh.default.gc_thresh3", 16384, "See net.ipv4 gc_thresh1."),
    Sysctl("net.ipv4.ip_forward", 1, "Containers reach the network through the host's forwarding."),
)

# package key -> program that proves it is already installed. Docker in particular must not
# be "installed" again: a machine with Docker's own packages would get a conflicting one.
_PROBE = {"docker": "docker", "adb": "adb", "scrcpy": "scrcpy", "ffmpeg": "ffmpeg", "v4l-utils": "v4l2-ctl",
          "patchelf": "patchelf", "zstd": "zstd", "pigz": "pigz", "mokutil": "mokutil", "dkms": "dkms",
          "git": "git", "curl": "curl", "kmod": "modprobe", "iproute": "ip", "pciutils": "lspci",
          "build-tools": "make", "openvpn": "openvpn", "wireguard-tools": "wg"}
REQUIRED_PACKAGES = ("docker", "adb", "ffmpeg", "kmod", "iproute", "curl")
OPTIONAL_PACKAGES = ("scrcpy", "pciutils", "zstd", "pigz", "patchelf", "git")
CAMERA_PACKAGES = ("v4l-utils", "build-tools", "kernel-headers", "dkms", "mokutil")


def sysctl_path(key: str, root: Path | str = "/") -> Path:
    return Path(root) / "proc/sys" / key.replace(".", "/")


def read_sysctl(key: str, root: Path | str = "/") -> int | None:
    try:
        return int(sysctl_path(key, root).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def sysctl_conf(config: Config) -> str:
    lines = ["# Written by `sf host apply`. Limits sized for hundreds of Android containers.",
             "# Each value is a floor chosen by the farm; raise freely, lowering brings the problems back."]
    for s in SYSCTLS:
        lines += [f"# {s.why}", f"{s.key} = {s.minimum}"]
    if config.performance.swappiness >= 0:
        lines += ["# performance.swappiness", f"vm.swappiness = {config.performance.swappiness}"]
    return "\n".join(lines) + "\n"


def tmpfiles_conf(group: str, extra_dirs: list[str]) -> str:
    lines = ["# Written by `sf host apply`. Runtime folders of the Android farm, recreated at every boot.",
             "# setgid + group-writable: the daemon runs as a member of the group, not as root."]
    lines += [f"d {d} 2770 root {group} -" for d in (RUN_DIR, *extra_dirs)]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ actions

def _run(argv: list[str], *, optional: bool = False, timeout: int = 120, capture: str = "") -> dict:
    return {"kind": "run", "argv": [str(a) for a in argv], "optional": optional, "timeout": timeout, "capture": capture}


def _write(path: str, content: str, mode: int = 0o644) -> dict:
    return {"kind": "write", "path": path, "content": content, "mode": mode}


def _mkdir(path: str, mode: int, group: str) -> dict:
    return {"kind": "mkdir", "path": path, "mode": mode, "owner": "root", "group": group}


def _value(path: str, value: str) -> dict:
    """Write one value into /sys or /proc."""
    return {"kind": "value", "path": path, "value": value}


def render(action: dict) -> list[str]:
    """The action as text a person could run or review. Used for --dry-run output."""
    kind = action["kind"]
    if kind == "run":
        # {NAME} arguments are filled from an earlier command's output; show them as shell variables.
        line = " ".join(f'"${a[1:-1]}"' if re.fullmatch(r"\{[A-Z]+\}", a) else shlex.quote(a) for a in action["argv"])
        if action.get("capture"):
            line = f"{action['capture']}=$({line})"
        return [line + ("   # optional: failure is tolerated" if action.get("optional") else "")]
    if kind == "write":
        body = [f"    {line}" for line in action["content"].splitlines()]
        return [f"write {action['path']} (mode {action['mode']:04o}):", *body]
    if kind == "mkdir":
        return [f"install -d -m {action['mode']:04o} -o {action['owner']} -g {action['group']} {shlex.quote(action['path'])}"]
    if kind == "value":
        return [f"echo {shlex.quote(action['value'])} > {shlex.quote(action['path'])}"]
    raise ValueError(f"unknown action kind {kind!r}")


def _step(sid: str, title: str, why: str, actions: list[dict], *, boot: bool, note: str = "") -> dict:
    return {"id": sid, "title": title, "why": why, "boot": boot, "actions": actions,
            "status": "pending" if actions else "ok", "detail": note or ("" if actions else "nothing to do")}


# ------------------------------------------------------------------ who

def resolve_user(user: str | None = None) -> str:
    """The desktop user the farm will run as: explicit, else whoever called sudo."""
    name = user or os.environ.get("SUPERFARM_USER") or os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    if not _NAME.match(name):
        raise ValueError(f"bad user name {name!r}")
    return name


def resolve_group(group: str | None = None) -> str:
    name = group or os.environ.get("SUPERFARM_GROUP") or DEFAULT_GROUP
    if not _NAME.match(name):
        raise ValueError(f"bad group name {name!r}")
    return name


def sudo_home() -> str | None:
    """Farm home of the user who called sudo. Under sudo `~` is root's home, so without
    this the setup would read (and the units would point at) an empty farm in /root."""
    if os.environ.get("SUPERFARM_HOME") or os.geteuid() != 0:
        return None
    caller = os.environ.get("SUPERFARM_USER") or os.environ.get("SUDO_USER")
    if not caller or not _NAME.match(caller):
        return None
    try:
        return str(Path(pwd.getpwnam(caller).pw_dir) / ".local" / "share" / "superfarm")
    except KeyError:
        return None


def _groups_file(root: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    try:
        for line in (root / "etc/group").read_text().splitlines():
            parts = line.split(":")
            if len(parts) >= 4:
                out[parts[0]] = [m for m in parts[3].split(",") if m]
    except OSError:
        pass
    return out


def _is_swap_zram(root: Path) -> bool:
    return capacity.zram_active(root)


def _under(path: str, parent: str) -> bool:
    p = os.path.normpath(path)
    return p == parent or p.startswith(parent.rstrip("/") + "/")


# ------------------------------------------------------------------ plan

def plan(config: Config, paths: Paths, *, boot: bool | None = None, user: str | None = None,
         group: str | None = None, root: Path | str = "/",
         which: Callable[[str], str | None] = proc.which, distro: distro_mod.Distro | None = None,
         has_nvidia: bool | None = None) -> list[dict]:
    """Steps this machine needs. Reads state (under `root`) but changes nothing.

    boot=True limits the plan to what must be redone after every reboot. When boot is
    None it follows the SUPERFARM_BOOT environment variable set by the boot unit.
    """
    root = Path(root)
    if boot is None:
        boot = os.environ.get("SUPERFARM_BOOT") == "1"
    user, group = resolve_user(user), resolve_group(group)
    dist = distro or distro_mod.detect(root)
    features = modules.features_of(config)
    steps: list[dict] = []

    # ---- packages
    if not boot:
        actions: list[dict] = []
        notes: list[str] = []
        if dist.supported:
            need = [k for k in REQUIRED_PACKAGES if not which(_PROBE[k])]
            optional = [k for k in OPTIONAL_PACKAGES if not which(_PROBE[k])]
            if "camera" in features:
                optional += [k for k in CAMERA_PACKAGES if k == "kernel-headers" or not which(_PROBE[k])]
            if has_nvidia if has_nvidia is not None else _has_nvidia(root):
                optional.append("relay-runtime")
            if need or optional:
                if dist.refresh_argv():
                    actions.append(_run(dist.refresh_argv(), timeout=600, optional=True))
                ps = dist.packages(*need)
                if ps.names:
                    actions.append(_run(dist.install_argv(ps.names), timeout=1800))
                notes += [f"{k}: {h}" for k, h in ps.hints.items()]
                # Optional packages one at a time: scrcpy is missing from several
                # distributions and must not take the others down with it.
                for key in optional:
                    ps = dist.packages(key)
                    if ps.names:
                        # The hint ("your release may not have it; get it from ...") is shown
                        # only if this install does not succeed.
                        actions.append({**_run(dist.install_argv(ps.names), timeout=1800, optional=True),
                                        "hint": ps.hints.get(key, "")})
                    notes += [f"{k}: {h}" for k, h in ps.hints.items() if not ps.names]
        else:
            notes.append(f"{dist.name} is not a supported package family; install docker, adb, ffmpeg and "
                         "scrcpy with your package manager")
        steps.append(_step("packages", "Install packages", "Docker, adb, ffmpeg and helpers.", actions,
                           boot=False, note="; ".join(notes)))

        steps.append(_step("docker-service", "Enable Docker", "Docker must run at boot before any Android can.",
                           [_run(["systemctl", "enable", "--now", "docker.service"], timeout=120)], boot=False))

        # ---- groups
        known = _groups_file(root)
        # docker: use Docker without sudo. video/render: the GPU render nodes and camera
        # devices (the NVIDIA relay and the camera feeders run as this user, not as root).
        wanted_groups = [g for g in dict.fromkeys((group, "docker", "video", "render")) if g in known]
        missing_groups = [g for g in wanted_groups if user not in known.get(g, [])]
        actions = [_run(["usermod", "-aG", ",".join(missing_groups), user])] if missing_groups else []
        steps.append(_step("groups", f"Add {user} to groups", "The daemon runs as the desktop user, never as root.",
                           actions, boot=False,
                           note=f"log out and in again for the new groups ({', '.join(missing_groups)}) to apply"
                           if missing_groups else ""))

    # ---- modules
    actions = []
    if not boot:
        actions += [_write(modules.MODULES_LOAD_FILE, modules.modules_load_conf(features, root=root)),
                    _write(modules.MODPROBE_FILE, modules.modprobe_conf(features))]
    cache: dict = {}
    for m in modules.wanted(features):
        if m.name == "v4l2loopback":
            continue            # handled by the camera step, which knows whether it is installed
        st = modules.state(m.name, root=root, _cache=cache)
        if st in ("loaded", "builtin"):
            continue
        actions.append(_run(modules.load_argv(m), optional=not m.required or st == "missing", timeout=30))
    steps.append(_step("modules", "Load kernel modules",
                       "Module auto-loading cannot be relied on, so everything is loaded by name.", actions, boot=True))

    # ---- sysctl
    if not boot:
        actions = [_write(SYSCTL_FILE, sysctl_conf(config)), _run(["sysctl", "-q", "-p", SYSCTL_FILE], timeout=30)]
        low = [s.key for s in SYSCTLS if (read_sysctl(s.key, root) or 0) < s.minimum]
        steps.append(_step("sysctl", "Raise kernel limits", "Defaults are sized for a desktop, not for hundreds "
                           "of containers.", actions, boot=False,
                           note=f"below the farm's floor now: {', '.join(low)}" if low else "all limits already sufficient"))

    # ---- /run folders
    extra = []
    sock = os.path.normpath(config.gpu.nvidia.socket_dir)
    # Root only ever creates folders below RUN_DIR. A socket folder configured elsewhere
    # belongs to the user and is created by the relay code as that user.
    if _under(sock, RUN_DIR) and sock != RUN_DIR:
        extra.append(sock)
    actions = [] if boot else [_write(TMPFILES_FILE, tmpfiles_conf(group, extra))]
    actions += [_mkdir(d, 0o2770, group) for d in (RUN_DIR, *extra)]
    steps.append(_step("run-dirs", "Create runtime folders",
                       "Sockets for the NVIDIA relay and camera FIFOs live in /run, which is empty after boot.",
                       actions, boot=True))

    # ---- zram
    if config.performance.zram.enabled:
        algo = config.performance.zram.algorithm
        if not _ALGO.match(algo):
            raise ValueError(f"bad performance.zram.algorithm {algo!r}")
        if _is_swap_zram(root):
            steps.append(_step("zram", "Compressed swap (zram)", "", [], boot=True,
                               note="a zram swap device is already active; left as it is"))
        else:
            total = capacity.meminfo(root).get("MemTotal", 0)
            size_mb = max(256, total * config.performance.zram.size_percent // 100)
            # The zram module itself is loaded by the modules step above.
            actions = [
                _run(["zramctl", "--find", "--size", f"{size_mb}M", "--algorithm", algo], capture="ZRAM", timeout=30),
                _run(["mkswap", "{ZRAM}"], timeout=30),
                # Higher priority than disk swap: compressed RAM is used first, the disk last.
                _run(["swapon", "--priority", "100", "{ZRAM}"], timeout=30),
            ]
            steps.append(_step("zram", "Compressed swap (zram)",
                               "Idle Androids compress well; more of them fit before the machine swaps to disk.",
                               actions, boot=True,
                               note=f"{size_mb} MB, {algo}" + (
                                   "" if config.performance.swappiness >= 0 else
                                   f"; the measured setup also ran with vm.swappiness={capacity.ZRAM_SWAPPINESS} "
                                   "(performance.swappiness), which is left alone unless you set it")))

    # ---- ksm
    if config.performance.ksm:
        run_file = "/sys/kernel/mm/ksm/run"
        current = ""
        try:
            current = (root / run_file.lstrip("/")).read_text().strip()
        except OSError:
            pass
        steps.append(_step("ksm", "Kernel same-page merging",
                           "Identical memory pages are stored once. Only processes that opt in are merged, so "
                           "the gain for Androids is unproven (experimental).",
                           [] if current == "1" else [_value(run_file, "1")], boot=True))

    # ---- cameras
    if "camera" in features:
        steps.append(_camera_step(config, root, which, cache))

    # ---- systemd units
    if not boot:
        steps.append(_units_step(config, paths, user, group, root))
    return steps


def _has_nvidia(root: Path) -> bool:
    from . import gpu
    return any(g.vendor == "nvidia" for g in gpu.detect_gpus(root / "sys", dev=root / "dev", namer=None))


def _camera_step(config: Config, root: Path, which: Callable[[str], str | None], cache: dict) -> dict:
    cam = config.media.camera
    why = "One virtual camera device per Android, created by root because the control device is root-only."
    actions: list[dict] = []
    if modules.state("videodev", root=root, _cache=cache) not in ("loaded", "builtin"):
        actions.append(_run(["modprobe", "videodev"], timeout=30))
    st = modules.state("v4l2loopback", root=root, _cache=cache)
    if st == "missing":
        return _step("camera", "Virtual cameras", why, [], boot=True,
                     note="the v4l2loopback module is not installed for this kernel: build it with "
                          f"`sudo {resource_path('native', 'v4l2loopback', 'build.sh')} install` (and sign it "
                          "with `sf host sign-module -- v4l2loopback` under Secure Boot), then run `sf host apply` again")
    if st != "loaded":
        # devices=0: no camera exists until one is created for a specific Android below, so
        # device numbers always match Android ids. max_buffers=8 is what the Android HAL expects.
        actions.append(_run(modules.load_argv(next(m for m in modules.MODULES if m.name == "v4l2loopback")), timeout=30))
    ctl = which("v4l2loopback-ctl") or "v4l2loopback-ctl"
    first = cam.device_base + 1
    for n in range(first, first + cam.slots):
        dev = f"/dev/video{n}"
        if (root / dev.lstrip("/")).exists():
            continue
        # -x 1 (exclusive_caps): the device looks like a plain capture camera to Android.
        actions.append(_run([ctl, "add", "-n", f"Super Farm Camera {n - cam.device_base}", "-x", "1", "-b", "8", dev],
                            timeout=20, optional=True))
    return _step("camera", "Virtual cameras", why, actions, boot=True,
                 note=f"{cam.slots} devices, /dev/video{first} .. /dev/video{first + cam.slots - 1}")


def render_unit(template: str, **values: str | os.PathLike) -> str:
    """Fill the @PLACEHOLDER@ fields of a unit template from deploy/systemd.

    Keyword names are the placeholder names in lower case (python="/x" fills @PYTHON@).
    Every placeholder in the template must be given: a field left unfilled would reach
    systemd as a literal @NAME@."""
    filled = {k.upper(): str(v) for k, v in values.items()}
    for key, v in filled.items():
        # Unit files are line-based; a newline or quote in a value could add directives,
        # and % starts a systemd specifier.
        if re.search(r"[\n\r\"'\\%]", v):
            raise ValueError(f"value not allowed in a unit file: {v!r}")
        # Unquoted Environment= and ExecStart= values split on spaces.
        if re.search(r"\s", v):
            raise ValueError(f"{key} contains whitespace ({v!r}); systemd units need a value without spaces "
                             "(move the farm home or the virtual environment)")
    out = template
    for k, v in filled.items():
        out = out.replace(f"@{k}@", v)
    left = re.findall(r"@[A-Z_]+@", out)
    if left:
        raise ValueError(f"unknown placeholders in unit template: {sorted(set(left))}")
    return out


def _units_step(config: Config, paths: Paths, user: str, group: str, root: Path) -> dict:
    actions: list[dict] = []
    notes: list[str] = []
    for unit in UNITS:
        src = resource_path("deploy", "systemd", unit)
        try:
            template = src.read_text()
        except OSError:
            notes.append(f"{src} not found; unit {unit} not installed")
            continue
        actions.append(_write(f"{UNIT_DIR}/{unit}", render_unit(
            # abspath, not resolve(): a virtual environment's python is a link to the system
            # interpreter, and the unit must start the environment's one.
            template, user=user, run_group=group, superfarm_home=paths.home,
            python=os.path.abspath(sys.executable))))
    if actions:
        actions += [_run(["systemctl", "daemon-reload"], timeout=60),
                    _run(["systemctl", "enable", "superfarm-host.service"], timeout=60)]
        notes.append(f"the daemon is a user service of {user}, installed without root: sf service install")
    return _step("systemd", "Install the boot unit",
                 "superfarm-host redoes the boot-time host setup (modules, cameras, /run folders) as root. "
                 "The daemon is not installed here: it runs as the desktop user's own service.",
                 actions, boot=False, note="; ".join(notes))


# ------------------------------------------------------------------ apply

def _execute(action: dict, root: Path, runner: Callable[..., proc.Result], variables: dict[str, str]) -> str:
    """Carry out one action. Returns a short note; raises on failure."""
    kind = action["kind"]
    if kind == "run":
        argv = [variables.get(a[1:-1], a) if a.startswith("{") and a.endswith("}") else a for a in action["argv"]]
        r = runner(argv, timeout=action["timeout"], check=True)
        if action.get("capture"):
            value = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
            if not re.match(r"^/dev/[A-Za-z0-9_/-]+$", value):
                raise RuntimeError(f"{argv[0]} printed no device name")
            variables[action["capture"]] = value
        return ""
    target = root / action["path"].lstrip("/")
    if kind == "write":
        try:
            if target.read_text() == action["content"]:
                return "unchanged"
        except OSError:
            pass
        atomic_write(target, action["content"], mode=action["mode"])
        return "written"
    if kind == "mkdir":
        target.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(target, 0, grp.getgrnam(action["group"]).gr_gid)
        os.chmod(target, action["mode"])
        return ""
    if kind == "value":
        target.write_text(action["value"])
        return ""
    raise ValueError(f"unknown action kind {kind!r}")


def _brief(error: Exception) -> str:
    """A failed command as one line: what was run and the last thing it said."""
    if isinstance(error, proc.CommandError):
        said = (error.stderr or error.stdout or "").strip().splitlines()
        why = "timed out" if error.timed_out else f"exit {error.code}"
        return f"{shlex.join(error.argv)} ({why}" + (f": {said[-1].strip()[:200]})" if said else ")")
    return str(error)


def apply(config: Config, paths: Paths, *, dry_run: bool = False, boot: bool | None = None,
          user: str | None = None, group: str | None = None, root: Path | str = "/",
          runner: Callable[..., proc.Result] | None = None, steps: list[dict] | None = None,
          only: Iterable[str] | None = None) -> list[dict]:
    """Run the plan. Returns the steps with status ok | failed | dry-run and, per step,
    `commands`: every command and file exactly as executed (or as it would be).

    only   step ids to run (see STEP_IDS); every other step is left out of the result.

    A real run must be root. A dry run needs no privileges and touches nothing."""
    root = Path(root)
    if steps is None:
        steps = plan(config, paths, boot=boot, user=user, group=group, root=root)
    if only is not None:
        wanted = set(only)
        unknown = wanted - set(STEP_IDS)
        if unknown:
            raise ValueError(f"unknown step(s) {', '.join(sorted(unknown))}; steps are: {', '.join(STEP_IDS)}")
        steps = [s for s in steps if s["id"] in wanted]
    if not dry_run and runner is None and os.geteuid() != 0:
        raise PermissionError("host setup needs root: run `sf host apply`, which asks for sudo itself, or "
                              f"`sudo {sys.executable} -m superfarm.host.setup apply` "
                              "(add --dry-run to see every command first)")
    runner = runner or proc.run
    variables: dict[str, str] = {}
    for step in steps:
        step["commands"] = [line for a in step["actions"] for line in render(a)]
        if dry_run:
            if step["actions"]:
                step["status"] = "dry-run"
            continue
        failures: list[str] = []
        tolerated: list[str] = []
        for action in step["actions"]:
            try:
                _execute(action, root, runner, variables)
            except (proc.CommandError, OSError, RuntimeError, KeyError, ValueError) as e:
                if action.get("optional"):
                    # Tolerated, but never silently: an optional package that did not
                    # install is a feature the operator will miss later.
                    tolerated.append(f"optional step did not succeed: {_brief(e)}"
                                     + (f". {action['hint']}" if action.get("hint") else ""))
                    continue
                failures.append(str(e))
                # Later actions of a step build on earlier ones (mkswap needs the device).
                break
        step["status"] = "failed" if failures else "ok"
        step["detail"] = "; ".join(filter(None, [step["detail"], *failures, *tolerated]))
    return steps


def format_steps(steps: list[dict]) -> str:
    """Plain-text report for the terminal."""
    out: list[str] = []
    for s in steps:
        out.append(f"[{s['status']}] {s['title']}" + (f" - {s['detail']}" if s["detail"] else ""))
        out += [f"    {line}" for line in s.get("commands", [])]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """`python -m superfarm.host.setup apply [--boot] [--dry-run]`: what `sf host apply` runs.

    The boot unit calls this module directly so it keeps working whatever the CLI looks like."""
    ap = argparse.ArgumentParser(prog="superfarm.host.setup", description="Prepare this machine for the Android farm.")
    ap.add_argument("command", choices=["plan", "apply"])
    ap.add_argument("--boot", action="store_true", help="only what must be redone after a reboot")
    ap.add_argument("--dry-run", action="store_true", help="print every command, change nothing")
    ap.add_argument("--user", default=None, help="desktop user the farm runs as (default: the sudo caller)")
    ap.add_argument("--group", default=None, help=f"group that owns {RUN_DIR} (default: {DEFAULT_GROUP})")
    ap.add_argument("--home", default=None, help="farm home (default: $SUPERFARM_HOME)")
    ap.add_argument("--only", default=None, metavar="STEP[,STEP]",
                    help=f"run only these steps ({', '.join(STEP_IDS)})")
    args = ap.parse_args(argv)
    paths = get_paths(args.home or sudo_home())
    try:
        config = load_config(paths)
        steps = apply(config, paths, dry_run=args.dry_run or args.command == "plan", boot=args.boot or None,
                      user=args.user, group=args.group,
                      only=[s for s in args.only.split(",") if s] if args.only else None)
    except (PermissionError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(format_steps(steps))
    return 1 if any(s["status"] == "failed" for s in steps) else 0


if __name__ == "__main__":
    raise SystemExit(main())

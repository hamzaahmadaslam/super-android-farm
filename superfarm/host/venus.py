"""The NVIDIA relay: one patched `virgl_test_server --venus` per NVIDIA card.

Android's graphics stack is Mesa, and Mesa cannot drive NVIDIA's proprietary driver, so
an NVIDIA Android draws like this instead:

    Android GLES -> ANGLE -> Vulkan -> Mesa Venus driver (vulkan.virtio.so)
        -> unix socket /venus/vtest.sock            (bind-mounted from the host)
    host: virgl_test_server --venus (patched) -> NVIDIA Vulkan driver -> the card

Rules this module exists to enforce (each one was learned the hard way):

* The relay must be running *before* an NVIDIA Android starts.
* A running relay is never restarted. Every NVIDIA Android holds connections to it;
  restarting it takes the graphics of all of them down until they are restarted too.
* Relay processes are recognised by exact process name (the kernel's 15-character
  `comm`, "virgl_test_serv") and by the socket path on their command line. Matching the
  whole command line by substring (`pkill -f`) also matches the caller's own shell.
* The relay runs as the desktop user. The card needs no root.

Sources, patches and the build script live in native/venus/.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from pathlib import Path

from ..config import Config
from ..paths import Paths
from ..util import proc
from ..util.files import locked

# File in the runtime folder -> where the Android container sees it. The 32-bit gralloc is
# needed because Android 13's camera service is a 32-bit program.
RUNTIME_FILES: dict[str, str] = {
    "gpu_config.sh": "/vendor/bin/gpu_config.sh",
    "vulkan.virtio.so": "/vendor/lib64/hw/vulkan.virtio.so",
    "gralloc.venus.so": "/vendor/lib64/hw/gralloc.venus.so",
    "gralloc.venus.x86.so": "/vendor/lib/hw/gralloc.venus.so",
}
CONTAINER_SOCKET_DIR = "/venus"          # gpu_config.sh sets mesa.vtest.socket.name=/venus/vtest.sock
SOCKET_NAME = "vtest.sock"
RELAY_BINARY = "bin/virgl_test_server"
RELAY_HELPER = "libexec/virgl_render_server"
RELAY_LIBRARY = "libvirglrenderer.so.1"
RELAY_COMM = "virgl_test_serv"           # "virgl_test_server" cut to the kernel's 15 characters
BUILD_HINT = "build it with native/venus/build.sh (see native/venus/README.md)"

_NODE = re.compile(r"^/dev/dri/(renderD\d+)$")
# Children this process started, kept so they are reaped instead of lingering as zombies.
_children: dict[str, subprocess.Popen] = {}


class RelayError(RuntimeError):
    """The relay is not available; the message says what to do about it."""


def runtime_dir(config: Config, paths: Paths) -> Path:
    return Path(config.gpu.nvidia.runtime_dir).expanduser() if config.gpu.nvidia.runtime_dir else paths.overlays / "venus"


def relay_dir(config: Config, paths: Paths) -> Path:
    return Path(config.gpu.nvidia.relay_dir).expanduser() if config.gpu.nvidia.relay_dir else paths.overlays / "venus-relay"


def node_name(node: str) -> str:
    m = _NODE.match(node or "")
    if not m:
        raise ValueError(f"bad render node {node!r}: expected /dev/dri/renderD<number>")
    return m.group(1)


def socket_dir(config: Config, node: str) -> Path:
    """Host folder holding one card's socket: <socket_dir>/<renderDNNN>. This folder (not
    its parent) is what gets mounted at /venus, so an Android only ever sees its own card."""
    return Path(config.gpu.nvidia.socket_dir) / node_name(node)


def _library_dir(relay: Path) -> Path | None:
    # Debian multiarch, plain lib, and the lib64 used by Fedora/SUSE builds.
    for sub in ("lib/x86_64-linux-gnu", "lib", "lib64"):
        if (relay / sub / RELAY_LIBRARY).exists():
            return relay / sub
    return None


def runtime_status(config: Config, paths: Paths) -> dict:
    """Which runtime and relay files exist. Pure file checks; starts nothing."""
    rt, rl = runtime_dir(config, paths), relay_dir(config, paths)
    files = {name: (rt / name).is_file() for name in RUNTIME_FILES}
    lib = _library_dir(rl)
    relay = {
        "binary": os.access(rl / RELAY_BINARY, os.X_OK),
        "helper": os.access(rl / RELAY_HELPER, os.X_OK),
        "library": str(lib) if lib else "",
    }
    icd = Path(config.gpu.nvidia.icd).is_file()
    missing = [str(rt / n) for n, ok in files.items() if not ok]
    missing += [str(rl / p) for p, ok in ((RELAY_BINARY, relay["binary"]), (RELAY_HELPER, relay["helper"])) if not ok]
    if not lib:
        missing.append(str(rl / "lib" / RELAY_LIBRARY))
    runtime_ready = all(files.values())
    relay_ready = bool(relay["binary"] and relay["helper"] and lib)
    return {
        "runtime_dir": str(rt), "relay_dir": str(rl), "files": files, "relay": relay,
        "icd": icd, "icd_path": config.gpu.nvidia.icd, "missing": missing,
        "runtime_ready": runtime_ready, "relay_ready": relay_ready,
        "ready": runtime_ready and relay_ready,
        "built_for": "Android 13 x86_64 (API 33)",
    }


# ------------------------------------------------------------------ finding relay processes

def find_relays(proc_root: Path | str = "/proc") -> list[dict]:
    """Every relay process on the machine: {pid, socket, node}.

    Same semantics as `pgrep -x virgl_test_serv`: the kernel's process name must match
    exactly. The command line is then read only to learn which socket it serves."""
    out: list[dict] = []
    try:
        entries = sorted((e for e in os.listdir(proc_root) if e.isdigit()), key=int)
    except OSError:
        return out
    for entry in entries:
        base = Path(proc_root) / entry
        try:
            if (base / "comm").read_text().strip() != RELAY_COMM:
                continue
            argv = (base / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue        # the process went away while we were looking
        out.append({"pid": int(entry), "socket": _arg(argv, "--socket-path"), "node": _arg(argv, "--rendernode")})
    return out


def _arg(argv: list[str], flag: str) -> str:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    return ""


def _is_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def _relay_for(sock: Path, proc_root: Path | str = "/proc") -> dict | None:
    for p in find_relays(proc_root):
        if p["socket"] == str(sock):
            return p
    return None


def relay_status(config: Config, *, nodes: list[str] | None = None, proc_root: Path | str = "/proc") -> list[dict]:
    """Per NVIDIA render node: socket path, pid, running. `nodes` defaults to the detected
    NVIDIA cards. Relays that belong to something else (another socket path) are ignored."""
    if nodes is None:
        from . import gpu           # local import: gpu imports this module
        nodes = [g.node for g in gpu.detect_gpus() if g.vendor == "nvidia"]
    relays = find_relays(proc_root)
    out = []
    for node in nodes:
        sock = socket_dir(config, node) / SOCKET_NAME
        match = next((p for p in relays if p["socket"] == str(sock)), None)
        has_socket = _is_socket(sock)
        out.append({"node": node, "socket_dir": str(sock.parent), "socket": str(sock),
                    "pid": match["pid"] if match else 0, "socket_present": has_socket,
                    "running": bool(match) and has_socket})
    return out


# ------------------------------------------------------------------ starting

def relay_command(config: Config, paths: Paths, node: str) -> tuple[list[str], dict[str, str]]:
    """argv and extra environment of the relay for one card."""
    rl = relay_dir(config, paths)
    sock = socket_dir(config, node) / SOCKET_NAME
    argv = [str(rl / RELAY_BINARY), "--venus", "--use-egl-surfaceless",
            "--rendernode", node, "--socket-path", str(sock)]
    lib = _library_dir(rl)
    env = {
        # Pin Vulkan to the NVIDIA driver: on a machine that also has an Intel/AMD GPU the
        # loader would otherwise offer both and the relay may pick the wrong one.
        "VK_ICD_FILENAMES": config.gpu.nvidia.icd,
        "VK_DRIVER_FILES": config.gpu.nvidia.icd,
        # The relay starts one helper per client. Its location is compiled in at build
        # time; naming it here keeps a relay folder that was moved or copied working.
        "RENDER_SERVER_EXEC_PATH": str(rl / RELAY_HELPER),
    }
    if lib:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(p for p in (str(lib), os.environ.get("LD_LIBRARY_PATH", "")) if p)
    return argv, env


def _log_tail(path: Path, lines: int = 5) -> str:
    try:
        return " | ".join(path.read_text(errors="replace").strip().splitlines()[-lines:])[:600]
    except OSError:
        return ""


def ensure_relay(config: Config, paths: Paths, node: str, *, timeout: float = 10.0,
                 proc_root: Path | str = "/proc") -> str:
    """Make sure the relay for `node` is running and return the folder to mount at /venus.

    A relay that is already serving this card's socket is left exactly as it is. Otherwise
    one is started as the current user, in its own session so it outlives the caller."""
    name = node_name(node)
    st = runtime_status(config, paths)
    if not st["relay_ready"]:
        missing = [m for m in st["missing"] if m.startswith(st["relay_dir"])]
        raise RelayError(f"NVIDIA relay is not built ({', '.join(missing)} missing); {BUILD_HINT}")
    if not st["icd"]:
        raise RelayError(f"NVIDIA Vulkan driver manifest {st['icd_path']} not found; install the NVIDIA driver "
                         "or point gpu.nvidia.icd at nvidia_icd.json")
    if not os.access(node, os.R_OK | os.W_OK):
        raise RelayError(f"no access to {node} for this user; add yourself to the group that owns it "
                         f"(`sudo usermod -aG render,video $USER`, then log in again)")
    sdir = socket_dir(config, node)
    sock = sdir / SOCKET_NAME
    try:
        sdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RelayError(
            f"cannot create {sdir} ({e.strerror}); run `sudo sf host apply` once (it creates "
            f"{config.gpu.nvidia.socket_dir} for the farm user), or set gpu.nvidia.socket_dir to a folder you own"
        ) from None

    # Bulk starts boot several Androids at once; without the lock two of them would both
    # find no relay and start two servers fighting over one socket path.
    with locked(sdir.parent / f".{name}"):
        if _is_socket(sock) and _relay_for(sock, proc_root):
            return str(sdir)
        old = _children.pop(name, None)
        if old is not None:
            old.poll()
        # A socket file without a server behind it is left over from a crash or reboot.
        try:
            sock.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            raise RelayError(f"cannot remove stale socket {sock}: {e.strerror}") from None
        paths.logs.mkdir(parents=True, exist_ok=True)
        log_path = paths.logs / f"venus-relay-{name}.log"
        argv, env = relay_command(config, paths, node)
        with open(log_path, "ab") as log:
            log.write(f"--- starting relay for {node} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
            log.flush()
            child = proc.spawn(argv, env=env, stdout=log, stderr=log)
        _children[name] = child
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _is_socket(sock):
                break
            if child.poll() is not None:
                _children.pop(name, None)
                raise RelayError(f"relay for {node} exited with code {child.returncode}: "
                                 f"{_log_tail(log_path) or 'no output'} (log: {log_path})")
            time.sleep(0.1)
        else:
            # Only ever signals the child started a few lines above, never a pre-existing relay.
            proc.kill_tree(child.pid)
            _children.pop(name, None)
            raise RelayError(f"relay for {node} did not create {sock} within {timeout:.0f}s: "
                             f"{_log_tail(log_path) or 'no output'} (log: {log_path})")
        # Android processes connect under many different uids (system, every app), so the
        # socket is open to all of them. The folder above it stays private to the farm.
        os.chmod(sock, 0o666)   # noqa: S103 - see comment above: every Android uid must connect
        os.chmod(sdir, 0o755)   # noqa: S103 - containers traverse it as arbitrary uids
    return str(sdir)


def container_mounts(config: Config, paths: Paths, node: str) -> list[str]:
    """`docker run` arguments that give one Android the relay socket and the runtime files."""
    rt = runtime_dir(config, paths)
    args = ["-v", f"{socket_dir(config, node)}:{CONTAINER_SOCKET_DIR}"]
    for name, target in RUNTIME_FILES.items():
        args += ["-v", f"{rt / name}:{target}:ro"]
    return args

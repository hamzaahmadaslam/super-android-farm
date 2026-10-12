"""Native scrcpy windows on the desktop the daemon runs on.

A scrcpy window is the smoothest way to watch and drive one device, and many of
them tiled over the screen is the classic "wall of phones". It only works when the
daemon belongs to a desktop session: the old farm ran its panels as root, which has
no display and no sound, and every window silently failed to appear. Here that
situation is detected up front and refused with a message that says what to do.

Each window is its own scrcpy process, on its own free local port (scrcpy's default
range holds 16; a farm has more devices than that), tracked by instance id and
stopped as a process group.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any, Protocol

from ..core.adb import Adb
from ..core.models import check_id
from ..util.proc import CommandError, kill_tree, run, spawn, which

DEFAULT_SCREEN = (1920, 1080)
MAX_WINDOWS = 200          # one decoder process each; beyond this the desktop is the bottleneck, not the farm
_TITLE_BAR = 30            # pixels a window manager typically adds above each window
_GAP = 8
_MIN_WIDTH = 96            # below this a phone screen is unreadable, so windows overlap instead of shrinking
_STAGGER = 0.15            # pause between launches: each start pushes a server and opens an ADB tunnel
_SETTLE = 0.5              # how long a new process is watched for an immediate exit (bad option, no display)


class ScrcpyError(RuntimeError):
    pass


class NoDesktopError(ScrcpyError):
    """The daemon has no display to put a window on."""


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Layout:
    rects: list[Rect]
    cols: int
    rows: int
    overlap: bool          # True when there are too many windows to show side by side at a usable size


# --------------------------------------------------------------------------- pure helpers

def tile(count: int, screen: tuple[int, int], *, aspect: float = 480 / 854, gap: int = _GAP,
         title: int = _TITLE_BAR, min_width: int = _MIN_WIDTH) -> Layout:
    """Place `count` equally sized windows of the given width/height ratio on a screen.

    Picks the column count that gives the largest windows which still all fit, in as
    many rows as that takes. `title` pixels are reserved above every window for the
    window manager's title bar. If the windows would become narrower than `min_width`
    they keep that width and overlap evenly instead; all of them stay fully on screen.
    """
    if count < 1:
        return Layout([], 0, 0, False)
    width, height = int(screen[0]), int(screen[1])
    if width < 64 or height < 64 or aspect <= 0:
        raise ValueError(f"cannot tile windows on a {width}x{height} screen")
    best: tuple[float, int, int] | None = None
    for cols in range(1, count + 1):
        rows = math.ceil(count / cols)
        cell_w = (width - gap * (cols + 1)) / cols
        cell_h = (height - gap * (rows + 1)) / rows - title
        w = min(cell_w, cell_h * aspect)
        if best is None or w > best[0] + 0.5:   # on a tie the earlier (fewer columns, taller) layout stays
            best = (w, cols, rows)
    assert best is not None
    fit_w, cols, rows = best
    overlap = fit_w < min_width
    if overlap:
        # Largest size that is still usable and fits the screen at all, then spread the
        # windows evenly over the free space so each one shows at least a strip.
        w = int(min(min_width, width - 2 * gap, (height - 2 * gap - title) * aspect))
        cols = min(count, max(cols, math.ceil(math.sqrt(count * (width / height) / aspect))))
        rows = math.ceil(count / cols)
    else:
        w = int(fit_w)
    w = max(w, 1)
    h = max(int(w / aspect), 1)
    free_x, free_y = width - 2 * gap - w, height - 2 * gap - title - h
    step_x = min(w + gap, free_x / (cols - 1)) if cols > 1 else 0
    step_y = min(h + title + gap, free_y / (rows - 1)) if rows > 1 else 0
    rects = [Rect(int(gap + (i % cols) * step_x), int(gap + title + (i // cols) * step_y), w, h)
             for i in range(count)]
    return Layout(rects, cols, rows, overlap)


def parse_version(text: str) -> tuple[int, ...] | None:
    """(3, 3, 4) from `scrcpy 3.3.4 <https://...>` (first line of `scrcpy --version`)."""
    m = re.search(r"\bscrcpy\s+v?(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(g) for g in m.groups() if g is not None) if m else None


def parse_xrandr(text: str) -> tuple[int, int] | None:
    """Size of the primary (else first) connected output from `xrandr --current`."""
    outputs = re.findall(r"^\S+ connected (primary )?(\d+)x(\d+)\+\d+\+\d+", text, re.M)
    if not outputs:
        return None
    primary = [o for o in outputs if o[0]]
    _, w, h = (primary or outputs)[0]
    return int(w), int(h)


def parse_wlr_randr(text: str) -> tuple[int, int] | None:
    """Size of the first enabled output from `wlr-randr` (the mode marked `current`)."""
    m = re.search(r"^\s+(\d+)x(\d+) px,.*\bcurrent\b", text, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_sway_outputs(text: str) -> tuple[int, int] | None:
    """Size of the focused (else first active) output from `swaymsg -t get_outputs -r`."""
    try:
        outputs = [o for o in json.loads(text) if isinstance(o, dict) and o.get("active", True)]
    except (ValueError, TypeError):
        return None
    outputs.sort(key=lambda o: not o.get("focused", False))
    for o in outputs:
        rect = o.get("rect") or {}
        if rect.get("width") and rect.get("height"):
            return int(rect["width"]), int(rect["height"])
    return None


def parse_kscreen(text: str) -> tuple[int, int] | None:
    """Size of the first enabled output from `kscreen-doctor -o` (`Geometry: 0,0 2560x1440`)."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    m = re.search(r"Geometry:\s*-?\d+,-?\d+\s+(\d+)x(\d+)", plain)
    return (int(m.group(1)), int(m.group(2))) if m else None


def sysfs_screen(root: Path = Path("/sys/class/drm")) -> tuple[int, int] | None:
    """Preferred mode of the first connected connector. Needs no tool and no session, but
    knows nothing about scaling or which monitor is primary, so it is the last resort."""
    try:
        connectors = sorted(root.glob("card*-*"))
    except OSError:
        return None
    for connector in connectors:
        try:
            if (connector / "status").read_text().strip() != "connected":
                continue
            m = re.match(r"(\d+)x(\d+)", (connector / "modes").read_text())
        except OSError:
            continue
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def desktop_env(env: Mapping[str, str] | None = None, *, uid: int | None = None,
                x11_dir: Path = Path("/tmp/.X11-unix")) -> dict[str, str]:  # noqa: S108 - the X server's fixed socket directory
    """Environment a window needs, or NoDesktopError.

    A daemon started by a service manager often has a desktop session but none of its
    variables, so the session's display sockets are looked for as well.
    """
    env = dict(os.environ if env is None else env)
    uid = os.getuid() if uid is None else uid
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return env
    runtime = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}")
    try:
        wayland = sorted(p.name for p in runtime.glob("wayland-[0-9]*") if p.is_socket())
    except OSError:
        wayland = []
    try:
        x11 = sorted(int(p.name[1:]) for p in x11_dir.glob("X[0-9]*") if p.name[1:].isdigit() and p.stat().st_uid == uid)
    except OSError:
        x11 = []
    if wayland:
        env.update(WAYLAND_DISPLAY=wayland[0], XDG_RUNTIME_DIR=str(runtime))
    if x11:
        env["DISPLAY"] = f":{x11[0]}"
    if wayland or x11:
        return env
    who = "root, which has no desktop session" if uid == 0 else "a user with no desktop session"
    raise NoDesktopError(
        f"No display to open a window on: the daemon runs as {who} (no DISPLAY or WAYLAND_DISPLAY, and no "
        "display socket of this user was found). Run `sf serve` as the user who is logged in to the desktop, "
        "or use the live view in the web panel, which needs no desktop (headless servers).")


def screen_size(env: Mapping[str, str] | None = None) -> tuple[tuple[int, int], str]:
    """((width, height), how it was found). Never fails: falls back to 1920x1080."""
    env = dict(os.environ if env is None else env)
    probes: list[tuple[str, list[str], Any]] = []
    if env.get("DISPLAY"):
        probes.append(("xrandr", ["xrandr", "--current"], parse_xrandr))
    if env.get("WAYLAND_DISPLAY") or env.get("SWAYSOCK"):
        probes += [("wlr-randr", ["wlr-randr"], parse_wlr_randr),
                   ("swaymsg", ["swaymsg", "-t", "get_outputs", "-r"], parse_sway_outputs),
                   ("kscreen-doctor", ["kscreen-doctor", "-o"], parse_kscreen)]
    for name, argv, parse in probes:
        if not which(argv[0]):
            continue
        try:
            size = parse(run(argv, timeout=4, check=False, env=env).stdout)
        except CommandError:
            continue
        if size:
            return size, name
    size = sysfs_screen()
    return (size, "sysfs") if size else (DEFAULT_SCREEN, "default")


def window_args(serial: str, port: int, title: str, *, bitrate: str, max_size: int, max_fps: int,
                version: tuple[int, ...] = (3,), rect: Rect | None = None,
                extra: Sequence[str] = ()) -> list[str]:
    """scrcpy arguments for one window (everything after the program name)."""
    args = ["-s", serial, "-p", str(int(port)), f"--window-title={title}"]
    if rect is not None:
        args += [f"--window-x={rect.x}", f"--window-y={rect.y}", f"--window-width={rect.w}",
                 f"--window-height={rect.h}"]
    args += ["-b", str(bitrate)]
    if max_size:
        args += ["-m", str(int(max_size))]
    args += [f"--max-fps={int(max_fps)}"]
    # No buffering: these windows are for driving a device, where delay matters more than
    # smoothness. The option was called --display-buffer before 2.1.
    args.append("--video-buffer=0" if version >= (2, 1) else "--display-buffer=0")
    if version >= (2, 0):
        args.append("--no-audio")   # sound of many devices at once is noise; "listen" is its own feature
    args.append("--stay-awake")
    return [*args, *(str(a) for a in extra)]


def free_port() -> int:
    """A local port nothing listens on right now, from the kernel's ephemeral range."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --------------------------------------------------------------------------- windows

class _Manager(Protocol):
    def adb(self, id: int) -> Adb: ...
    def resolved(self, id: int) -> Any: ...


@dataclass
class _Window:
    id: int
    proc: subprocess.Popen
    port: int
    title: str
    rect: Rect | None
    log: IO[bytes]


class ScrcpyWindows:
    """Native windows on the desktop the daemon runs on. Thread-safe."""

    def __init__(self, mgr: _Manager, config: Any, *, env: Mapping[str, str] | None = None, binary: str = "scrcpy"):
        self._mgr = mgr
        self._config = config
        self._env = dict(env) if env is not None else None
        self._binary = binary
        self._lock = threading.RLock()
        self._windows: dict[int, _Window] = {}
        self._version: tuple[int, ...] | None = None

    def reload_config(self, config: Any) -> None:
        self._config = config

    # ------------------------------------------------------------------ public
    def available(self) -> dict:
        """Whether windows can be opened here, and if not, why and how to fix it."""
        try:
            self._desktop()
            version = self._scrcpy_version()
        except ScrcpyError as e:
            return {"ok": False, "reason": str(e)}
        return {"ok": True, "reason": "", "version": ".".join(map(str, version))}

    def open(self, id: int, *, rect: Rect | None = None) -> dict:
        """Open a window for one device (or report the one already open)."""
        id = check_id(id)
        env = self._desktop()
        version = self._scrcpy_version()
        with self._lock:
            self._reap()
            if id in self._windows:
                return {**self._describe(self._windows[id]), "already_open": True}
        window = self._launch(id, rect, env, version)
        self._confirm(window, time.monotonic() + _SETTLE)
        return {**self._describe(window), "already_open": False}

    def close(self, id: int) -> None:
        with self._lock:
            window = self._windows.pop(check_id(id), None)
        if window:
            self._stop(window)

    def close_all(self) -> None:
        with self._lock:
            windows, self._windows = list(self._windows.values()), {}
        for window in windows:
            self._stop(window)

    def running(self) -> list[int]:
        with self._lock:
            self._reap()
            return sorted(self._windows)

    def grid(self, ids: list[int], *, screen: tuple[int, int] | None = None) -> dict:
        """Open (or reopen) windows for `ids`, tiled to fit the screen.

        A running scrcpy window cannot be moved from outside, so windows of these ids
        are restarted at their new place. One device failing does not stop the rest.
        """
        targets = list(dict.fromkeys(check_id(i) for i in ids))
        if len(targets) > MAX_WINDOWS:
            raise ScrcpyError(f"{len(targets)} windows requested, the limit is {MAX_WINDOWS}")
        env = self._desktop()
        version = self._scrcpy_version()
        size, source = (tuple(screen), "given") if screen else screen_size(env)
        layout = tile(len(targets), size, aspect=self._aspect(targets))
        results: dict[int, dict] = {}
        launched: list[_Window] = []
        for i, (id, rect) in enumerate(zip(targets, layout.rects, strict=True)):
            self.close(id)
            try:
                launched.append(self._launch(id, rect, env, version))
            except Exception as e:   # per-device outcome: the rest of the wall still opens
                results[id] = {"ok": False, "detail": str(e)[:300]}
            if i < len(targets) - 1:
                time.sleep(_STAGGER)
        # All of them are watched together for an early exit, so a wall of a hundred windows
        # does not wait half a second per window.
        deadline = time.monotonic() + _SETTLE
        for window in launched:
            try:
                self._confirm(window, deadline)
                results[window.id] = {"ok": True, "detail": "", **self._describe(window)}
            except ScrcpyError as e:
                results[window.id] = {"ok": False, "detail": str(e)[:300]}
        results = {id: results[id] for id in targets}
        return {
            "screen": list(size), "screen_source": source, "cols": layout.cols, "rows": layout.rows,
            "overlap": layout.overlap,
            # Wayland compositors place windows themselves; only X11 (incl. XWayland) honours positions.
            "positioned": env.get("SDL_VIDEODRIVER") == "x11",
            "opened": sum(1 for r in results.values() if r["ok"]), "results": results,
        }

    # ---------------------------------------------------------------- internals
    def _launch(self, id: int, rect: Rect | None, env: dict[str, str], version: tuple[int, ...]) -> _Window:
        """Start scrcpy for one device and track it. Does not wait to see whether it stays up."""
        with self._lock:
            if len(self._windows) >= MAX_WINDOWS:
                raise ScrcpyError(f"{MAX_WINDOWS} windows are already open; close some first")
        adb = self._mgr.adb(id)
        if not adb.connect(attempts=3, delay=0.6):
            raise ScrcpyError(f"Android {id} does not answer over ADB ({adb.serial}); start it and wait for it to boot")
        stream = self._config.stream
        title = self._title(id)
        port = free_port()
        argv = [self._binary, *window_args(adb.serial, port, title, bitrate=stream.scrcpy_bitrate,
                                           max_size=stream.scrcpy_max_size, max_fps=stream.scrcpy_max_fps,
                                           version=version, rect=rect, extra=stream.scrcpy_extra_args)]
        log = tempfile.TemporaryFile(prefix="sf-scrcpy-")   # the window's own messages, read only if it fails
        try:
            proc = spawn(argv, env=env, stdout=log, stderr=log)
        except OSError as e:
            log.close()
            raise ScrcpyError(f"could not start scrcpy: {e}") from None
        window = _Window(id, proc, port, title, rect, log)
        with self._lock:
            old = self._windows.pop(id, None)   # lost a race with another open(): keep one
            self._windows[id] = window
        if old:
            self._stop(old)
        return window

    def _confirm(self, window: _Window, deadline: float) -> None:
        """A wrong option or an unreachable display ends the process within a moment; catching
        that turns "nothing happened" into the program's own error text."""
        while time.monotonic() < deadline and window.proc.poll() is None:
            time.sleep(0.02)
        if window.proc.poll() is None:
            return
        tail = self._log_tail(window)
        with self._lock:
            if self._windows.get(window.id) is window:
                del self._windows[window.id]
        window.log.close()
        raise ScrcpyError(f"scrcpy for Android {window.id} exited at once (code {window.proc.returncode}): "
                          f"{tail or 'no output'}")

    def _desktop(self) -> dict[str, str]:
        env = desktop_env(self._env)
        if env.get("DISPLAY") and "SDL_VIDEODRIVER" not in env:
            # Pin X11 when it is there: it is the only backend that honours window positions.
            env["SDL_VIDEODRIVER"] = "x11"
        return env

    def _scrcpy_version(self) -> tuple[int, ...]:
        if self._version is None:
            if not which(self._binary):
                raise ScrcpyError(f"`{self._binary}` is not installed. Install the scrcpy package "
                                  "(apt/dnf/pacman/zypper install scrcpy) to open native windows.")
            try:
                out = run([self._binary, "--version"], timeout=10, check=False).stdout
            except CommandError as e:
                raise ScrcpyError(f"`{self._binary} --version` failed: {e}") from None
            self._version = parse_version(out) or (3,)
        return self._version

    def _title(self, id: int) -> str:
        try:
            name = str(self._mgr.resolved(id).name)
        except Exception:   # noqa: BLE001 - a title is not worth failing for
            name = ""
        name = re.sub(r"[^\w .#()\[\]+-]", "", name).strip()[:60]
        return name or f"Android {id}"

    def _aspect(self, ids: Sequence[int]) -> float:
        """Shape of the windows: the first device's, as nearly every farm is uniform."""
        for id in ids:
            try:
                r = self._mgr.resolved(id)
                return int(r.width) / int(r.height)
            except Exception:   # noqa: BLE001, S112 - try the next device
                continue
        return self._config.android.width / self._config.android.height

    def _reap(self) -> None:
        for id, window in list(self._windows.items()):
            if window.proc.poll() is not None:   # the person closed the window
                del self._windows[id]
                window.log.close()

    @staticmethod
    def _describe(window: _Window) -> dict:
        return {"id": window.id, "pid": window.proc.pid, "port": window.port, "title": window.title,
                "rect": asdict(window.rect) if window.rect else None}

    @staticmethod
    def _log_tail(window: _Window) -> str:
        try:
            window.log.seek(0)
            lines = window.log.read()[-2000:].decode(errors="replace").strip().splitlines()
        except (OSError, ValueError):
            return ""
        return " | ".join(lines[-3:])

    @staticmethod
    def _stop(window: _Window) -> None:
        proc = window.proc
        if proc.poll() is None:
            kill_tree(proc.pid)   # scrcpy removes its ADB tunnel and device-side server on SIGTERM
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid, 9)
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    pass
        window.log.close()

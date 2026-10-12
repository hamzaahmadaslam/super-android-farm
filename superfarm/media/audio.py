"""Listen to one Android on the host's speakers.

A second scrcpy, without window, picture or control, carries only that Android's sound and
plays it through the audio session of the user the daemon runs as. It runs next to any
normal scrcpy window (which stays silent) and nothing runs on the other Androids.

Things that were learned the hard way and are encoded here:

* Uncompressed audio (`--audio-codec=raw`): the Android images have no Opus encoder.
* `--audio-buffer=200`: a busy Android delivers sound in bursts; with a small buffer every
  late burst is a stutter.
* Media volume is raised *after* capture has started. Captured sound passes through
  Android's remote-submix device, which has its own volume (default 5 of 15, about 30 dB
  down) and only exists while something captures. Setting the volume before scrcpy is up
  changes the wrong device.
* Only one Android at a time: two streams mixed on one pair of speakers are useless, and
  every listener disables playback inside its Android.
* The port scrcpy tunnels through is a free one picked at start, so it can never collide
  with scrcpy windows or with another farm on the same machine.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from collections.abc import Callable

from ..config import Config
from ..core.models import check_id
from ..paths import Paths
from ..util.files import read_json, write_json
from ..util.proc import which
from .feeds import Launcher, ProcRef, tail

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.instances import InstanceManager

log = logging.getLogger(__name__)

# Seconds between scrcpy's start and the volume step: long enough for the capture (and with
# it the remote-submix device) to exist on a loaded machine.
VOLUME_DELAY = 3.0
MEDIA_STREAM = 3            # AudioManager.STREAM_MUSIC


def free_port(host: str = "127.0.0.1") -> int:
    """A TCP port nobody is listening on right now, chosen by the kernel."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def listen_command(*, scrcpy: str, serial: str, port: int, buffer_ms: int) -> list[str]:
    return [scrcpy, f"--serial={serial}", f"--port={int(port)}",
            "--no-video", "--no-control", "--no-window",
            "--audio-source=output", "--audio-codec=raw", f"--audio-buffer={int(buffer_ms)}",
            # SDL's own output buffer; the default of 5 ms sounds robotic with raw audio.
            "--audio-output-buffer=20"]


def volume_command(level: int) -> list[str]:
    return ["cmd", "media_session", "volume", "--stream", str(MEDIA_STREAM), "--set", str(int(level))]


def session_env(environ: dict[str, str] | None = None, uid: int | None = None) -> dict[str, str]:
    """Extra environment that lets scrcpy find the daemon user's own sound server.

    A daemon started by systemd (not from a desktop terminal) may lack XDG_RUNTIME_DIR, and
    PipeWire / PulseAudio clients look for their socket there. Only the daemon user's own
    runtime directory is ever used: there is no reaching into another user's session.
    """
    environ = dict(os.environ if environ is None else environ)
    uid = os.getuid() if uid is None else uid
    extra: dict[str, str] = {}
    runtime = f"/run/user/{uid}"
    if not environ.get("XDG_RUNTIME_DIR") and os.path.isdir(runtime):
        extra["XDG_RUNTIME_DIR"] = runtime
    if not environ.get("DISPLAY") and not environ.get("WAYLAND_DISPLAY"):
        # No window is opened, but SDL still initialises its video layer; without a display
        # that fails unless it is told to use the dummy driver.
        extra["SDL_VIDEODRIVER"] = "dummy"
    return extra


class _EnvLauncher(Launcher):
    """Launcher that adds environment variables to what it starts."""

    def __init__(self, extra: dict[str, str]):
        super().__init__()
        self._extra = extra

    def start(self, argv, *, log, pass_fds=()):  # type: ignore[override]
        if not self._extra:
            return super().start(argv, log=log, pass_fds=pass_fds)
        # `env` keeps the argv-only Launcher interface: no shell, no global os.environ change.
        return super().start(["env", *[f"{k}={v}" for k, v in self._extra.items()], *argv],
                             log=log, pass_fds=pass_fds)


class Listener:
    """`listen to one device`. At most one scrcpy audio stream exists at any time."""

    def __init__(self, config: Config, paths: Paths, mgr: InstanceManager, *,
                 launcher: Launcher | None = None, port_picker: Callable[[], int] = free_port,
                 timer: Callable[[float, Callable[[], None]], object] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.config, self.paths, self.mgr = config, paths, mgr
        self.launcher = launcher or _EnvLauncher(session_env())
        self._port_picker, self._sleep = port_picker, sleep
        self._timer = timer or self._start_timer
        self._lock = threading.RLock()
        self._state_file: Path = paths.run / "listen.json"
        self._log_file: Path = paths.logs / "listen.log"

    def reload_config(self, config: Config) -> None:
        self.config = config

    @staticmethod
    def _start_timer(delay: float, fn: Callable[[], None]) -> object:
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        return t

    # ------------------------------------------------------------------- state
    def _load(self) -> tuple[int, ProcRef, dict] | None:
        data = read_json(self._state_file)
        ref = ProcRef.from_dict(data)
        if ref is None or not isinstance(data.get("id"), int):
            return None
        return data["id"], ref, data

    def _clear(self) -> None:
        self._state_file.unlink(missing_ok=True)

    def current(self) -> int | None:
        """Id of the Android being listened to, or None."""
        with self._lock:
            state = self._load()
            if state is None:
                return None
            if not self.launcher.alive(state[1]):
                self._clear()       # scrcpy ended by itself (Android stopped, ADB dropped)
                return None
            return state[0]

    def status(self) -> dict:
        with self._lock:
            instance_id = self.current()
            state = self._load() if instance_id is not None else None
            if state is None:
                return {"listening": None}
            return {"listening": instance_id, "since": state[2].get("started"), "port": state[2].get("port"),
                    "pid": state[1].pid}

    # ------------------------------------------------------------------ control
    def stop(self) -> None:
        """Stop listening. Also stops a listener left behind by a previous daemon."""
        with self._lock:
            state = self._load()
            if state is not None:
                self.launcher.stop(state[1])
            self._clear()

    def start(self, id: int) -> dict:  # noqa: A002 - name fixed by the public interface
        """Play this Android's sound on the host. Whatever was playing before stops first."""
        instance_id = check_id(id)
        scrcpy = which("scrcpy")
        if not scrcpy:
            raise RuntimeError("scrcpy is not installed. Install scrcpy 2.5 or newer (the `scrcpy` package, "
                               "or https://github.com/Genymobile/scrcpy) and try again.")
        adb = self.mgr.adb(instance_id)
        if not adb.connect(attempts=2):
            raise RuntimeError(f"Android {instance_id} is not reachable over ADB. Start it first.")
        audio = self.config.media.audio
        with self._lock:
            self.stop()
            self._set_volume(adb, audio.listen_volume)      # harmless now, right for the speaker path
            port = self._port_picker()
            argv = listen_command(scrcpy=scrcpy, serial=adb.serial, port=port, buffer_ms=audio.listen_buffer_ms)
            ref = self.launcher.start(argv, log=self._log_file)
            write_json(self._state_file, {**ref.to_dict(), "id": instance_id, "port": port, "started": time.time()})
            # A wrong flag, a missing sound server or a dead ADB link end scrcpy within a
            # moment; report that now instead of pretending to listen.
            self._sleep(0.5)
            if not self.launcher.alive(ref):
                self._clear()
                why = tail(self._log_file) or "scrcpy exited without a message"
                raise RuntimeError(f"could not start listening to Android {instance_id}: {why}")
            self._timer(VOLUME_DELAY, lambda: self._raise_volume(instance_id, ref, audio.listen_volume))
        return {"ok": True, "listening": instance_id, "port": port, "pid": ref.pid}

    # ------------------------------------------------------------------- volume
    def _set_volume(self, adb, level: int) -> None:
        try:
            adb.shell(volume_command(level), timeout=10, check=False)
        except Exception as e:  # noqa: BLE001 - quiet sound is better than no sound
            log.debug("listen: volume step failed: %s", e)

    def _raise_volume(self, instance_id: int, ref: ProcRef, level: int) -> None:
        """Runs VOLUME_DELAY after start, only if that same listener is still the current one."""
        with self._lock:
            state = self._load()
            if state is None or state[0] != instance_id or state[1] != ref or not self.launcher.alive(ref):
                return
        try:
            self._set_volume(self.mgr.adb(instance_id), level)
        except Exception as e:  # noqa: BLE001
            log.debug("listen: volume step failed: %s", e)

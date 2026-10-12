"""The composition root: the one place where the farm's objects are created and wired.

The daemon, the HTTP API and anything else that needs "the farm" builds one `Farm` and
reaches every module through it. Nothing here does work of its own; it only decides which
object is handed to which.

    farm = Farm()                 # $SUPERFARM_HOME or the default home
    farm.instances.list_status()
    farm.close()

Components are built on first use. One that cannot be built (an optional tool is missing,
a module failed to import) raises `Unavailable` with the reason every time it is asked for;
the rest of the farm keeps working.
"""
from __future__ import annotations

import logging
import os
import platform
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, load_config, save_config
from .paths import Paths, get_paths

log = logging.getLogger("superfarm.farm")

# Settings an Android reads only when its containers are (re)created or it boots. Changing
# one is saved at once and reaches each Android at its next restart.
ANDROID_RESTART = ("android.", "resources.", "gpu.", "network.", "device.", "media.camera.", "docker.log_",
                   "docker.name_prefix", "performance.restart_policy", "performance.trim_",
                   "performance.disable_animations")
# Settings the listening socket and the log files are opened with: daemon restart.
DAEMON_RESTART = ("server.host", "server.port", "server.tls_cert", "server.tls_key", "server.allowed_origins",
                  "logging.")

DOCTOR_TTL = 60.0      # seconds a doctor run is reused; it runs a few dozen probes


class Unavailable(RuntimeError):
    """A component cannot be used on this machine right now; `reason` says why."""

    def __init__(self, component: str, reason: str):
        super().__init__(f"{component} is not available: {reason}")
        self.component, self.reason = component, reason


def _flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(data, dict) or not data:
        return {prefix.rstrip("."): data}
    out: dict[str, Any] = {}
    for key, value in data.items():
        out.update(_flatten(value, f"{prefix}{key}."))
    return out


def merge_patch(base: Any, patch: Any) -> Any:
    """JSON Merge Patch (RFC 7396): mappings merge key by key, anything else replaces, and
    `null` removes a key. Removing a setting puts it back to its default; removing an entry
    of a mapping (a gateway, an image) deletes that entry."""
    if not isinstance(patch, dict):
        return patch
    out = dict(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def config_effects(old: Config, new: Config) -> dict[str, list[str]]:
    """Which dotted keys differ between two configs, and what each needs to take effect."""
    a, b = _flatten(old.model_dump(mode="json")), _flatten(new.model_dump(mode="json"))
    changed = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    return {
        "changed": changed,
        "restart_android": [k for k in changed if k.startswith(ANDROID_RESTART)],
        "restart_daemon": [k for k in changed if k.startswith(DAEMON_RESTART)],
    }


class Farm:
    def __init__(self, home: str | os.PathLike | None = None):
        self.paths: Paths = get_paths(home).ensure()
        self.config: Config = load_config(self.paths)
        self._lock = threading.RLock()
        self._built: dict[str, Any] = {}
        self._failed: dict[str, str] = {}
        self._doctor: tuple[float, list] | None = None
        self._closed = False

    # ---------------------------------------------------------------- building
    def _get(self, name: str) -> Any:
        try:
            return self._built[name]
        except KeyError:
            pass
        with self._lock:
            if name in self._built:
                return self._built[name]
            if name in self._failed:
                raise Unavailable(name, self._failed[name])
            try:
                component = self._FACTORIES[name](self)
            except Unavailable:
                raise
            except Exception as e:  # noqa: BLE001 - one broken component must not take the daemon down
                log.warning("component %s could not be built: %s: %s", name, type(e).__name__, e)
                self._failed[name] = f"{type(e).__name__}: {e}"
                raise Unavailable(name, self._failed[name]) from e
            self._built[name] = component
            return component

    def _make_docker(self):
        from .core.docker import Docker
        return Docker(self.config.docker.binary)

    def _make_store(self):
        from .core.store import Store
        return Store(self.paths)

    def _make_tasks(self):
        from .core.tasks import TaskManager
        return TaskManager()

    def _make_instances(self):
        from .core.instances import InstanceManager
        mgr = InstanceManager(self.config, self.paths, self.docker, self.store, self.tasks)
        self._built["instances"] = mgr
        # The camera adds a mount to every Android's container plan and a step after boot.
        # It has to be wired before the first start, or the plan of an Android would change
        # (and its containers be rebuilt) the first time somebody opened the camera page.
        try:
            self._get("camera")
        except Unavailable as e:
            log.warning("camera support is off: %s", e.reason)
        return mgr

    def _make_images(self):
        return self.instances.images       # the manager's own, so builds and pulls share its locks

    def _make_backups(self):
        from .core.backup import Backups
        return Backups(self.instances)

    def _make_library(self):
        from .control import AppLibrary
        return AppLibrary(self.paths, self.config)

    def _make_apps(self):
        from .control import Apps
        return Apps(self.instances, self.library, self.tasks, docker=self.docker)

    def _make_files(self):
        from .control import Files
        return Files(self.instances, self.tasks, self.paths)

    def _make_device(self):
        from .control import Device
        return Device(self.instances, self.tasks)

    def _make_shell(self):
        from .control import Shell
        return Shell(self.instances, self.tasks)

    def _make_screen(self):
        from .stream import ScreenHub
        return ScreenHub(self.instances, self.config)

    def _make_input(self):
        from .stream import Input
        return Input(self.instances, self.config, screen=self.screen)

    def _make_windows(self):
        from .stream import ScrcpyWindows
        return ScrcpyWindows(self.instances, self.config)

    def _make_h264(self):
        from .stream import H264Streams
        return H264Streams(self.instances, self.config)

    def _make_camera(self):
        from .media.camera import Camera
        return Camera(self.config, self.paths, self.docker, self.instances)

    def _make_listener(self):
        from .media.audio import Listener
        return Listener(self.config, self.paths, self.instances)

    def _make_automation(self):
        from .automation import Automation
        return Automation(self.config, self.paths, self.instances, self.tasks)

    _FACTORIES: dict[str, Callable[[Farm], Any]] = {
        "docker": _make_docker, "store": _make_store, "tasks": _make_tasks, "instances": _make_instances,
        "images": _make_images, "backups": _make_backups, "library": _make_library, "apps": _make_apps,
        "files": _make_files, "device": _make_device, "shell": _make_shell, "screen": _make_screen,
        "input": _make_input, "windows": _make_windows, "h264": _make_h264, "camera": _make_camera,
        "listener": _make_listener, "automation": _make_automation,
    }

    def use(self, name: str, component: Any) -> None:
        """Put a ready-made component in place of the one the farm would build. For tests
        and for embedding the farm in a program that already has, say, its own Docker handle."""
        if name not in self._FACTORIES:
            raise KeyError(f"no component named {name!r}")
        with self._lock:
            self._built[name] = component
            self._failed.pop(name, None)

    @property
    def docker(self): return self._get("docker")
    @property
    def store(self): return self._get("store")
    @property
    def tasks(self): return self._get("tasks")
    @property
    def instances(self): return self._get("instances")
    @property
    def images(self): return self._get("images")
    @property
    def backups(self): return self._get("backups")
    @property
    def library(self): return self._get("library")
    @property
    def apps(self): return self._get("apps")
    @property
    def files(self): return self._get("files")
    @property
    def device(self): return self._get("device")
    @property
    def shell(self): return self._get("shell")
    @property
    def screen(self): return self._get("screen")
    @property
    def input(self): return self._get("input")
    @property
    def windows(self): return self._get("windows")
    @property
    def h264(self): return self._get("h264")
    @property
    def camera(self): return self._get("camera")
    @property
    def listener(self): return self._get("listener")
    @property
    def automation(self): return self._get("automation")

    def components(self) -> dict[str, dict]:
        """Every component with whether it could be built: {name: {available, reason}}."""
        out: dict[str, dict] = {}
        for name in self._FACTORIES:
            try:
                self._get(name)
                out[name] = {"available": True, "reason": ""}
            except Unavailable as e:
                out[name] = {"available": False, "reason": e.reason}
        return out

    # ------------------------------------------------------------------ config
    def set_config(self, patch: dict) -> Config:
        """Apply a nested patch (see merge_patch): validate, save, and hand the new config to
        every component that keeps one. Raises ValueError, and changes nothing, when the
        result is not a valid config."""
        if not isinstance(patch, dict):
            raise ValueError("a config patch is a mapping of sections to new values")
        with self._lock:
            new = Config.model_validate(merge_patch(self.config.model_dump(mode="json"), patch))
            save_config(new, self.paths)
            self._apply(new)
        return new

    def reload_config(self) -> Config:
        """Read config.yaml again (after it was restored or edited outside the daemon)."""
        with self._lock:
            new = load_config(self.paths)
            self._apply(new)
        return new

    def _apply(self, new: Config) -> None:
        self.config = new
        for name, component in list(self._built.items()):
            if name == "docker":
                component.binary = new.docker.binary
            reload = getattr(component, "reload_config", None)
            if callable(reload):
                try:
                    reload(new)
                except Exception:  # noqa: BLE001 - the config is saved; say which part still runs on the old one
                    log.exception("component %s did not take the new config", name)
        self._doctor = None

    # -------------------------------------------------------------------- info
    def doctor(self, *, fresh: bool = False) -> list:
        """host.doctor.run_checks, reused for a minute: the panel asks for it on every page load."""
        from .host import doctor
        with self._lock:
            cached = self._doctor
        if cached and not fresh and time.monotonic() - cached[0] < DOCTOR_TTL:
            return cached[1]
        checks = doctor.run_checks(self.config, self.paths)
        with self._lock:
            self._doctor = (time.monotonic(), checks)
        return checks

    def gpus(self) -> list:
        from .host import gpu
        return gpu.nvidia_vram(gpu.detect_gpus())

    def capacity(self) -> dict:
        """InstanceManager.capacity(): the advisory estimate next to what is running now."""
        return self.instances.capacity()

    def system_info(self) -> dict:
        """What the panel's System page shows. Every part is gathered on its own: a probe
        that fails is reported in place, it never blanks the page."""
        from .host import distro as distro_mod
        from .host import doctor as doctor_mod
        from .host.capacity import meminfo

        def part(fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                return {"error": f"{type(e).__name__}: {e}"[:300]}

        distro = distro_mod.detect()
        memory = meminfo()
        checks = part(self.doctor)
        return {
            "version": __version__,
            "farm_name": self.config.farm_name,
            "home": str(self.paths.home),
            "host": {
                "distro": distro.name, "distro_id": distro.id, "family": distro.family, "kernel": distro.kernel,
                "arch": platform.machine(),
                "cpu": {"model": _cpu_model(), "threads": os.cpu_count() or 0},
                "ram_mb": memory.get("MemTotal", 0), "swap_mb": memory.get("SwapTotal", 0),
            },
            "gpus": part(lambda: [asdict(g) for g in self.gpus()]),
            "capacity": part(self.capacity),
            "doctor": doctor_mod.summarize(checks) if isinstance(checks, list) else checks,
            "components": self.components(),
        }

    # ------------------------------------------------------------------ daemon
    def tick(self) -> None:
        """Housekeeping, called by the daemon about every 30 s. Each job is independent."""
        def job(what: str, fn: Callable[[], Any]) -> None:
            try:
                fn()
            except Unavailable:
                pass
            except Exception:  # noqa: BLE001 - one failing job must not stop the ticker
                log.exception("tick: %s failed", what)

        job("automation schedules", lambda: self.automation.tick())

        job("backup schedule", lambda: self.backups.tick())

        def camera() -> None:
            if self.config.media.camera.enabled:
                self.camera.heal()
        job("camera", camera)

    def close(self) -> None:
        """Stop the host processes this farm started (scrcpy windows, the listener, camera
        feeds, screen captures). Androids keep running and a GPU relay is never touched:
        restarting the daemon must not interrupt the devices.

        H.264 sessions live on the server's event loop and are closed there (see api.app)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            built = dict(self._built)
        for name, call in (("windows", "close_all"), ("listener", "stop"), ("camera", "unwatch"),
                           ("camera", "down"), ("screen", "close"), ("input", "close"), ("tasks", "shutdown")):
            component = built.get(name)
            if component is None:
                continue
            try:
                getattr(component, call)()
            except Exception:  # noqa: BLE001 - keep closing the rest
                log.exception("close: %s.%s failed", name, call)


def _cpu_model(cpuinfo: Path = Path("/proc/cpuinfo")) -> str:
    try:
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or ""

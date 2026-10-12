"""Virtual front camera and microphone for the farm's Androids.

A video file from the media library, or a live RTMP stream, becomes the camera picture and
the microphone sound of any number of Androids. See `feeds.py` for the pipeline and
`native/camera/README.md` for the Android side (HAL overlay and shims).

Who does what
    root, once      `sf host apply` loads v4l2loopback and creates the loopback devices with
                    group access; host_device_commands() returns exactly those commands.
    the daemon      (not root) runs ffmpeg and the forwarders, writes to the devices through
                    that group access, and performs the few in-container steps with
                    `docker exec`, which is root *inside* the container only.

Lessons from the farm this was ported from, kept here on purpose
    * One decode + one encode per source, fanned out with tee: per-Android cost is two
      near-idle helpers, not a transcode.
    * The copier never closes its device, so changing the video never interrupts an app
      that has the camera open.
    * A fresh device has no frame size until frames are written; it is primed with black
      frames, and `keep_format=1` pins the size so the HAL always finds a usable format.
    * The Android may only see its own device, as /dev/video0. A privileged container
      inherits every host video node at start; those are removed inside the container.
    * `dumpsys media.camera` must always be read to the end. Closing the pipe early sends
      SIGPIPE into the camera HAL, which is busy writing its state, and kills it.
"""
from __future__ import annotations

import io
import json
import logging
import os
import secrets
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from collections.abc import Callable, Iterable

from ..config import Config
from ..core.docker import Docker
from ..core.models import Resolved, check_id
from ..paths import Paths
from ..util.files import inside, read_json
from ..util.proc import CommandError, Result, run, which
from . import feeds as F
from .feeds import Feed, Launcher, Layout, Slots, VideoSettings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.adb import Adb
    from ..core.instances import InstanceManager

log = logging.getLogger(__name__)

SOUND_FROM_SOURCE = "source"
SOUND_NONE = "none"
MIC_MODES = ("mute", "unmute", "solo")
# Files the overlay must contain before it is copied into an Android: the HAL service, its
# init file (without which nothing starts it) and the buffer shim (without which the camera
# opens but delivers no frames).
OVERLAY_REQUIRED = (
    f"bin/hw/{F.HAL_SERVICE}",
    f"etc/init/{F.HAL_SERVICE}.rc",
    "etc/external_camera_config.xml",
    "lib/libfarmcam_shim.so",
    "lib/hw/gralloc.redroid.so",
)
# Runs inside the Android as root. "$1"/"$2" are the device's major and minor numbers,
# passed as arguments so nothing is ever spliced into the script text.
_MKNOD_SCRIPT = (
    'for node in /dev/video*; do rm -f "$node"; done; '
    f'mknod -m 666 {F.CONTAINER_CAMERA} c "$1" "$2"'
)
# `grep -c` reads its whole input, so dumpsys is never cut short (see module docstring).
_COUNT_SCRIPT = "dumpsys media.camera 2>/dev/null | grep -c '^Number of camera devices: 1'"
_RESTART_HAL_SCRIPT = f"kill $(pidof {F.HAL_SERVICE}) 2>/dev/null"


# --------------------------------------------------------------------------- host setup (root)

def host_device_numbers(config: Config) -> list[int]:
    """Numbers N of the /dev/videoN devices this farm uses: device_base + 1 .. device_base + slots."""
    c = config.media.camera
    return list(range(c.device_base + 1, c.device_base + 1 + c.slots))


def _check_group(group: str) -> None:
    if not group or group.startswith("-") or not all(ch.isalnum() or ch in "_-." for ch in group):
        raise ValueError(f"bad group name {group!r}")


def host_device_commands(config: Config, *, group: str = "video", ctl: str | None = None,
                         sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[list[str]]:
    """Commands the root one-shot (`sf host apply`) runs so the daemon can use cameras.

    Returns argv lists, in order, for what is still missing *now*; run each as root and stop
    at the first failure. Running the result twice is harmless: devices that already exist
    are not created again, only their permissions are re-asserted. An empty list means the
    camera is switched off (media.camera.enabled false or slots 0).

    What they do
        modprobe videodev / v4l2loopback      the module, with no devices of its own
        v4l2loopback-ctl add ... /dev/videoN  one device per slot, N = device_base + 1 ..
                                              device_base + slots, named "Super Farm Camera
                                              <N - device_base>", exclusive_caps=1 (Android's
                                              HAL needs a pure capture device), 8 buffers
        chgrp <group> / chmod 0660            the daemon user reaches the devices through
                                              this group and nothing else

    group   a group the daemon user belongs to. `video` is what udev gives video devices
            anyway; pass another group when the daemon user is not in it. udev re-applies
            its own rules on some events: install host_udev_rule(group) to make a
            non-default group stick.
    ctl     path of v4l2loopback-ctl when it is not on PATH. Without it devices can only
            be created while loading the module, which stock builds limit to 8.

    Raises ValueError (with the fix) when a wanted device number is taken by something that
    is not ours - a webcam, a capture card, another farm - or the numbers do not fit.
    The module itself must be installed first (and signed, under Secure Boot): that is
    native/v4l2loopback, not this function.
    """
    c = config.media.camera
    if not c.enabled or c.slots <= 0:
        return []
    _check_group(group)
    numbers = host_device_numbers(config)
    if numbers[-1] > F.MAX_VIDEO_NR:
        raise ValueError(
            f"media.camera.device_base {c.device_base} + slots {c.slots} goes past /dev/video{F.MAX_VIDEO_NR}, "
            "the last number the kernel has. Lower one of them.")
    missing: list[int] = []
    for number in numbers:
        device = F.device_path(number, dev_root)
        if (sys_root / "class" / "video4linux" / f"video{number}").exists():
            if not F.device_is_ours(device, sys_root):
                raise ValueError(
                    f"{device} already exists and is not a Super Farm camera (it is "
                    f"{F.device_name(device, sys_root)!r}). Set media.camera.device_base so that the "
                    f"{c.slots} numbers after it are free (see `ls /dev/video*`).")
        else:
            missing.append(number)
    ctl = ctl or which("v4l2loopback-ctl")
    loaded = (sys_root / "module" / "v4l2loopback").is_dir()
    commands: list[list[str]] = [["modprobe", "videodev"]]
    if not loaded:
        if ctl:
            # devices=0: without it the module creates /dev/video0 of its own, which a desktop
            # then offers as a webcam.
            commands.append(["modprobe", "v4l2loopback", "devices=0", "max_buffers=8"])
        elif len(numbers) <= 8:
            commands.append([
                "modprobe", "v4l2loopback", "max_buffers=8",
                "video_nr=" + ",".join(str(n) for n in numbers),
                "card_label=" + ",".join(f"{F.CARD_LABEL_PARAM}-{n - c.device_base}" for n in numbers),
                "exclusive_caps=" + ",".join("1" for _ in numbers)])
            missing = []
        else:
            raise ValueError(
                "v4l2loopback-ctl was not found, and without it at most 8 camera devices can be made. "
                "Build it (native/v4l2loopback) or lower media.camera.slots to 8.")
    elif missing and not ctl:
        raise ValueError(
            "v4l2loopback is loaded but v4l2loopback-ctl was not found, so no further camera devices "
            "can be added. Build it (native/v4l2loopback), or unload the module (`modprobe -r "
            "v4l2loopback`, nothing may be using it) and run `sf host apply` again.")
    for number in missing:
        commands.append([str(ctl), "add", "-n", f"{F.CARD_LABEL} {number - c.device_base}", "-x", "1", "-b", "8",
                         F.device_path(number, dev_root)])
    # Device nodes appear asynchronously; chgrp before udev is done would be undone by it.
    commands.append(["udevadm", "settle", "--timeout=10"])
    devices = [F.device_path(n, dev_root) for n in numbers]
    commands.append(["chgrp", group, *devices])
    commands.append(["chmod", "0660", *devices])
    return commands


def host_udev_rule(group: str = "video") -> str:
    """Content for /etc/udev/rules.d/70-superfarm-camera.rules: keeps the group access of
    the farm's loopback devices across udev re-triggers and module reloads."""
    _check_group(group)
    return "".join(f'SUBSYSTEM=="video4linux", ATTR{{name}}=="{label}*", GROUP="{group}", MODE="0660"\n'
                   for label in F.OUR_LABELS)


# --------------------------------------------------------------------------- the camera

@dataclass(frozen=True)
class _Media:
    """A resolved source: a library file or the live ingest."""
    name: str
    path: Path | None
    video: bool
    audio: bool
    live: bool = False


def _default_stat_device(device: str) -> tuple[int, int]:
    st = os.stat(device)
    return os.major(st.st_rdev), os.minor(st.st_rdev)


class Camera:
    def __init__(self, config: Config, paths: Paths, docker: Docker, mgr: InstanceManager, *,
                 launcher: Launcher | None = None, runner: Callable[..., Result] = run,
                 sleep: Callable[[float], None] = time.sleep,
                 stat_device: Callable[[str], tuple[int, int]] = _default_stat_device,
                 sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")):
        self.config, self.paths, self.docker, self.mgr = config, paths, docker, mgr
        self.layout = Layout(paths.run / "camera")
        self.launcher = launcher or Launcher()
        self._run, self._sleep, self._stat_device = runner, sleep, stat_device
        self.sys_root, self.dev_root = sys_root, dev_root
        self._lock = threading.RLock()
        self._sink_locks: dict[int, threading.Lock] = {}
        self._sink_guard = threading.Lock()
        self._heal_failed: set[int] = set()
        self._probe_cache: dict[tuple[str, int, int], tuple[bool, bool]] = {}
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        # An Android that was just (re)started has a fresh /dev and a fresh HAL: its camera
        # must be shown to it again. The instance manager calls this after every boot.
        hooks = getattr(mgr, "post_boot_hooks", None)
        if isinstance(hooks, list) and self.post_boot not in hooks:
            hooks.append(self.post_boot)
        # ... and its container must be created with the microphone FIFO mounted.
        providers = getattr(mgr, "extra_run_args_providers", None)
        if isinstance(providers, list) and self.extra_run_args not in providers:
            providers.append(self.extra_run_args)

    def reload_config(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------ small helpers
    @property
    def _settings(self) -> VideoSettings:
        return VideoSettings.from_config(self.config)

    def _slots(self) -> Slots:
        c = self.config.media.camera
        return Slots(self.layout.slots_file, c.device_base, c.slots)

    def _ffmpeg(self) -> str:
        path = which("ffmpeg")
        if not path:
            raise RuntimeError("ffmpeg is not installed. Install the `ffmpeg` package and try again.")
        return path

    def _wants_camera(self, instance_id: int) -> bool:
        try:
            return bool(self.mgr.resolved(instance_id).camera)
        except Exception:  # noqa: BLE001 - a deleted or unreadable instance needs no device
            return False

    def _overlay_dir(self, version: str) -> Path:
        configured = self.config.media.camera.hal_overlay
        return Path(configured).expanduser() if configured else self.paths.overlays / "camera" / str(version)

    def device(self, instance_id: int) -> str:
        """Host device of this Android, e.g. /dev/video105 (allocates a slot on first use)."""
        slots = self._slots()
        slot = slots.assign(instance_id, in_use=self._wants_camera)
        return F.device_path(slots.number(slot), self.dev_root)

    def _check_device(self, device: str) -> None:
        name = F.device_name(device, self.sys_root)
        if not name:
            raise RuntimeError(
                f"{device} does not exist. Camera devices are created by the one-time host setup: "
                "set media.camera.enabled, then run `sudo sf host apply`.")
        if not name.startswith(F.OUR_LABELS):
            raise RuntimeError(
                f"{device} is not a Super Farm camera (it is {name!r}); refusing to write to it. "
                "Set media.camera.device_base to a free range and run `sudo sf host apply`.")
        if not os.access(device, os.R_OK | os.W_OK):
            raise RuntimeError(
                f"no permission to use {device}. Run `sudo sf host apply`; if the daemon's user was "
                "only just added to the device's group, log out and in (or restart the daemon).")

    def _has_format(self, device: str) -> bool:
        current = F.device_format(device, self.sys_root)
        if current is not None:
            return current.startswith(self._settings.sysfs_format)
        # Older v4l2loopback without the sysfs attribute: ask the device itself.
        s = self._settings
        try:
            out = self._run(["v4l2-ctl", "-d", device, "--list-formats-ext"], timeout=10, check=False).stdout
        except CommandError:
            return False
        return f"Discrete {s.width}x{s.height}" in out

    def _container(self, instance_id: int) -> str:
        return self.mgr.resolved(instance_id).names.android

    @staticmethod
    def _ids(ids: Iterable[int]) -> list[int]:
        return sorted({check_id(i) for i in ids})

    # ------------------------------------------------------------------ report
    def available(self) -> dict:
        """Can cameras work on this host right now? Every `reasons` entry says how to fix it."""
        c = self.config.media.camera
        reasons: list[str] = []
        loaded = (self.sys_root / "module" / "v4l2loopback").is_dir()
        present, absent, foreign, no_access = [], [], [], []
        for number in host_device_numbers(self.config):
            device = F.device_path(number, self.dev_root)
            name = F.device_name(device, self.sys_root)
            if not name:
                absent.append(number)
            elif not name.startswith(F.OUR_LABELS):
                foreign.append(number)
            elif not os.access(device, os.R_OK | os.W_OK):
                no_access.append(number)
            else:
                present.append(number)
        tools = {name: which(name) for name in ("ffmpeg", "ffprobe", "v4l2-ctl")}
        if not c.enabled:
            reasons.append("media.camera.enabled is off: set it to true in the farm's config, "
                           "then run `sudo sf host apply`.")
        if not loaded:
            reasons.append("the v4l2loopback kernel module is not loaded: run `sudo sf host apply` "
                           "(build and, under Secure Boot, sign it first: native/v4l2loopback).")
        if foreign:
            reasons.append(f"/dev/video{foreign[0]} (and {len(foreign) - 1} more) belong to something else: "
                           "set media.camera.device_base to a free range, then `sudo sf host apply`.")
        if absent and c.slots:
            reasons.append(f"{len(absent)} of {c.slots} camera devices do not exist: run `sudo sf host apply`.")
        if no_access:
            reasons.append(f"no permission for {len(no_access)} camera devices: run `sudo sf host apply`, "
                           "then log out and in if the daemon's user was just added to the group.")
        for name, path in tools.items():
            if not path:
                package = "v4l-utils" if name == "v4l2-ctl" else "ffmpeg"
                reasons.append(f"{name} is not installed: install the `{package}` package.")
        overlays = {}
        versions = {self.config.android.default_version, "13"}
        for version in sorted(versions):
            folder = self._overlay_dir(version)
            lacking = [f for f in OVERLAY_REQUIRED if not (folder / f).is_file()]
            overlays[version] = {"path": str(folder), "present": not lacking, "missing": lacking}
        if not overlays.get("13", {}).get("present"):
            reasons.append(f"the camera HAL overlay for Android 13 is missing in {overlays['13']['path']}: "
                           "build it with native/camera/extract_hal.sh (see native/camera/README.md).")
        return {
            "enabled": c.enabled,
            "ok": bool(c.enabled and loaded and present and all(tools.values()) and overlays["13"]["present"]),
            "reasons": reasons,
            "module": loaded,
            "devices": {"first": c.device_base + 1, "slots": c.slots, "usable": present, "missing": absent,
                        "foreign": foreign, "no_access": no_access},
            "tools": tools,
            "overlay": overlays,
            "android_versions": ["13"],
            "rtmp": F.rtmp_url(self.config),
        }

    def sources(self) -> list[dict]:
        """What can be played: the media library's files, and the live ingest."""
        out: list[dict] = []
        if self.paths.media.is_dir():
            for path in sorted(self.paths.media.iterdir(), key=lambda p: p.name.lower()):
                ext = path.suffix.lower()
                if not path.is_file() or path.name.startswith(".") or ext not in F.VIDEO_EXT | F.AUDIO_EXT:
                    continue
                st = path.stat()
                out.append({"id": path.name, "name": path.name, "kind": "video" if ext in F.VIDEO_EXT else "audio",
                            "size": st.st_size, "modified": st.st_mtime})
        c = self.config.media.camera
        host = f"[{c.rtmp_bind}]" if ":" in c.rtmp_bind else c.rtmp_bind
        out.append({"id": F.RTMP_SOURCE, "name": "Live stream (RTMP)", "kind": "live",
                    "server": f"rtmp://{host}:{c.rtmp_port}/{F.RTMP_APP}", "key": F.RTMP_KEY,
                    "active": any(f.source == F.RTMP_SOURCE and self.launcher.alive(f.ref)
                                  for f in F.load_feeds(self.layout))})
        return out

    def status(self, *, android: bool = False) -> dict:
        """Feeds, helpers and mutes. With android=True each running Android is also asked
        whether it has the driver and sees its camera (one bounded `docker exec` each)."""
        feeds_out = []
        for feed in self._feeds():
            running = self.launcher.alive(feed.ref)
            entry = {**feed.to_dict(), "running": running}
            if not running:
                entry["error"] = F.tail(self.layout.feed_log(feed.id)) or "the source stopped"
            feeds_out.append(entry)
        table = self._slots().table()
        instances: dict[int, dict] = {}
        for instance_id in sorted(set(self.layout.instance_ids()) | set(table)):
            slot = table.get(instance_id)
            device = F.device_path(self._slots().number(slot), self.dev_root) if slot is not None else ""
            instances[instance_id] = {
                "device": device,
                "format": (F.device_format(device, self.sys_root) or "") if device else "",
                "copier": self.launcher.alive(F.read_ref(self.layout.proc_file(instance_id, "copier"))),
                "forwarder": self.launcher.alive(F.read_ref(self.layout.proc_file(instance_id, "forwarder"))),
                "muted": self.layout.mute_marker(instance_id).exists(),
                "feed": next((f["id"] for f in feeds_out if instance_id in f["ids"]), None),
            }
        if android:
            targets = [i for i in instances if self._running(i)]
            for instance_id, state in zip(targets, self._parallel(self._android_state, targets), strict=True):
                instances[instance_id].update(state)
        return {"enabled": self.config.media.camera.enabled, "feeds": feeds_out, "instances": instances,
                "muted": [i for i, s in instances.items() if s["muted"]], "rtmp": F.rtmp_url(self.config)}

    def _android_state(self, instance_id: int) -> dict:
        try:
            name = self._container(instance_id)
            driver = self._has_driver(name)
            return {"driver": driver, "cameras": self._camera_count(name) if driver else 0,
                    "mic_mounted": self._has_mic(name)}
        except Exception as e:  # noqa: BLE001 - one unreachable Android must not break the report
            return {"driver": False, "cameras": 0, "mic_mounted": False, "error": str(e)[:200]}

    def _running(self, instance_id: int) -> bool:
        try:
            return self.docker.running(self._container(instance_id))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _parallel(fn: Callable[[int], dict], ids: list[int]) -> list[dict]:
        if not ids:
            return []
        # Each step takes seconds on a busy Android; a fleet must not take minutes.
        with ThreadPoolExecutor(max_workers=min(8, len(ids)), thread_name_prefix="sf-camera") as pool:
            return list(pool.map(fn, ids))

    # ------------------------------------------------------------------- sinks
    def _ensure_sink(self, instance_id: int) -> bool:
        """Make sure this Android's copier and forwarder run and its device has a frame size.
        Returns True when a helper was (re)started, i.e. a feed writing to it must reconnect."""
        # play(), attach() and heal() may all reach the same Android at once; two copiers on
        # one device would fight over it. (Not self._lock: play() holds that while its worker
        # threads run this.)
        with self._sink_guard:
            sink_lock = self._sink_locks.setdefault(instance_id, threading.Lock())
        with sink_lock:
            return self._ensure_sink_locked(instance_id)

    def _ensure_sink_locked(self, instance_id: int) -> bool:
        device = self.device(instance_id)
        self._check_device(device)
        layout, restarted = self.layout, False
        layout.instance_dir(instance_id).mkdir(parents=True, exist_ok=True)
        F.ensure_fifo(layout.mic(instance_id), 0o644)
        if not self.launcher.alive(F.read_ref(layout.proc_file(instance_id, "copier"))):
            fifo = F.fresh_fifo(layout.video_fifo(instance_id))
            # The copier inherits this read-write handle and keeps it for life. With a writer
            # always present, a source that stops is not end-of-file for the copier: it just
            # waits for the next source, with the device still open.
            keep_open = os.open(fifo, os.O_RDWR)
            try:
                ref = self.launcher.start(
                    F.copier_command(ffmpeg=self._ffmpeg(), fifo=fifo, device=device, settings=self._settings),
                    log=layout.log(instance_id, "copier"), pass_fds=[keep_open])
            finally:
                os.close(keep_open)
            F.write_ref(layout.proc_file(instance_id, "copier"), ref, started=time.time(), device=device)
            restarted = True
        if not self._has_format(device):
            self._prime(instance_id, device)
        if not self.launcher.alive(F.read_ref(layout.proc_file(instance_id, "forwarder"))):
            fifo = F.fresh_fifo(layout.audio_fifo(instance_id))
            ref = self.launcher.start(
                F.forwarder_command(source=fifo, target=layout.mic(instance_id),
                                    mute_marker=layout.mute_marker(instance_id)),
                log=layout.log(instance_id, "forwarder"))
            F.write_ref(layout.proc_file(instance_id, "forwarder"), ref, started=time.time())
            restarted = True
        return restarted

    def _prime(self, instance_id: int, device: str) -> None:
        fifo = self.layout.video_fifo(instance_id)
        try:
            self._run(F.prime_command(ffmpeg=self._ffmpeg(), fifo=fifo, settings=self._settings),
                      timeout=30, check=False)
        except CommandError as e:
            raise RuntimeError(f"could not send the first frames to {device}: {e}") from None
        for _ in range(100):
            if self._has_format(device):
                return
            self._sleep(0.1)
        current = F.device_format(device, self.sys_root)
        why = F.tail(self.layout.log(instance_id, "copier")) or "no message from the copier"
        if current:
            why = (f"it is locked to {current}, not {self._settings.sysfs_format.rstrip('@')}. After changing "
                   "the camera size take this Android's camera down (Camera.down), then play again")
        raise RuntimeError(f"{device} did not take the camera's frame size: {why}")

    def _sink_started(self, instance_id: int) -> float:
        newest = 0.0
        for role in ("copier", "forwarder"):
            data = read_json(self.layout.proc_file(instance_id, role), {}) or {}
            newest = max(newest, float(data.get("started", 0.0) or 0.0))
        return newest

    def _ensure_sinks(self, ids: list[int]) -> dict[int, dict]:
        def one(instance_id: int) -> dict:
            try:
                self._ensure_sink(instance_id)
                return {"ok": True, "detail": "ready"}
            except Exception as e:  # noqa: BLE001 - one Android's failure is a result, not a crash
                return {"ok": False, "detail": str(e)}
        return dict(zip(ids, self._parallel(one, ids), strict=True))

    # ------------------------------------------------------------------- feeds
    def _feeds(self) -> list[Feed]:
        """Feeds on record. Records from before the last host boot describe processes that
        no longer exist and are dropped."""
        boot = _boot_time()
        feeds = []
        for feed in F.load_feeds(self.layout):
            if feed.started and feed.started < boot:
                F.drop_feed(self.layout, feed.id)
            else:
                feeds.append(feed)
        return feeds

    def _probe(self, path: Path) -> tuple[bool, bool]:
        """(has picture, has sound). Cover art inside a music file is not a picture."""
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if key in self._probe_cache:
            return self._probe_cache[key]
        ffprobe = which("ffprobe")
        if not ffprobe:
            raise RuntimeError("ffprobe is not installed. Install the `ffmpeg` package and try again.")
        try:
            out = self._run([ffprobe, "-v", "error", "-show_entries",
                             "stream=codec_type:stream_disposition=attached_pic", "-of", "json", str(path)],
                            timeout=20).stdout
        except CommandError as e:
            raise ValueError(f"{path.name} cannot be read as video or audio: {e}") from None
        try:
            streams = json.loads(out or "{}").get("streams", [])
        except json.JSONDecodeError:
            streams = []
        video = any(s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
                    for s in streams)
        audio = any(s.get("codec_type") == "audio" for s in streams)
        if len(self._probe_cache) > 256:
            self._probe_cache.clear()
        self._probe_cache[key] = (video, audio)
        return video, audio

    def _library_file(self, name: str) -> Path:
        # The name comes from the API: it may only ever select a file inside the library.
        if not name or "\x00" in name:
            raise ValueError("no source given")
        try:
            path = inside(self.paths.media, self.paths.media / name)
        except ValueError:
            raise ValueError(f"{name!r} is not a file in the media library") from None
        if not path.is_file():
            raise ValueError(f"no such file in the media library: {name}")
        return path

    def _resolve(self, source: str) -> _Media:
        if source == F.RTMP_SOURCE:
            # What a publisher will send is unknown until it connects; sound is assumed.
            return _Media(F.RTMP_SOURCE, None, video=True, audio=True, live=True)
        path = self._library_file(source)
        video, audio = self._probe(path)
        if not video and not audio:
            raise ValueError(f"{source} has neither picture nor sound")
        return _Media(path.name, path, video, audio)

    def _start_feed(self, media: _Media, sound: str, ids: list[int]) -> Feed:
        ffmpeg, layout = self._ffmpeg(), self.layout
        sound_input: list[str] | None = None
        want_audio = False
        if sound == SOUND_FROM_SOURCE:
            want_audio = media.audio
        elif sound != SOUND_NONE:
            extra = self._library_file(sound)
            if not self._probe(extra)[1]:
                raise ValueError(f"{sound} has no sound")
            sound_input, want_audio = F.file_input(extra), True
        if not media.video and not (want_audio and sound == SOUND_FROM_SOURCE):
            raise ValueError(f"{media.name} has no picture: play it with sound=\"source\" to use it as "
                             "the microphone, or choose a video")
        main_input = F.rtmp_input(F.rtmp_url(self.config)) if media.live else F.file_input(media.path)  # type: ignore[arg-type]
        command = F.feed_command(
            ffmpeg=ffmpeg, main_input=main_input, sound_input=sound_input,
            video_fifos=[layout.video_fifo(i) for i in ids] if media.video else [],
            audio_fifos=[layout.audio_fifo(i) for i in ids] if want_audio else [],
            settings=self._settings)
        feed = Feed(id=secrets.token_hex(4), source=media.name, sound=sound, ids=list(ids),
                    video=media.video, audio=want_audio, started=time.time())
        ref = self.launcher.start(F.supervised(command, give_up=0 if media.live else 5),
                                  log=layout.feed_log(feed.id))
        feed.pid, feed.ticks = ref.pid, ref.ticks
        F.save_feed(layout, feed)
        return feed

    def _stop_feed(self, feed: Feed) -> None:
        self.launcher.stop(feed.ref)
        F.drop_feed(self.layout, feed.id)

    def _restart_feed(self, feed: Feed, ids: list[int]) -> Feed | None:
        """Stop a feed and start it again for `ids` (same source and sound)."""
        self._stop_feed(feed)
        if not ids:
            return None
        return self._start_feed(self._resolve(feed.source), feed.sound, ids)

    def _release(self, ids: set[int]) -> list[str]:
        """Take these Androids off whatever feeds serve them. A feed shared with others is
        restarted for the rest: one writer per FIFO is the invariant everything relies on."""
        stopped = []
        for feed in self._feeds():
            if not ids & set(feed.ids):
                continue
            stopped.append(feed.id)
            rest = [i for i in feed.ids if i not in ids]
            try:
                self._restart_feed(feed, rest if self.launcher.alive(feed.ref) else [])
            except Exception as e:  # noqa: BLE001 - the others lose this feed; say so, carry on
                log.warning("camera: feed %s (%s) could not be restarted for %s: %s", feed.id, feed.source, rest, e)
        return stopped

    # ------------------------------------------------------------- public: play
    def play(self, ids: list[int], source: str, *, sound: str = SOUND_FROM_SOURCE, attach: bool = True) -> dict:
        """Play `source` as the camera (and microphone) of these Androids.

        source   a file name from the media library, or "rtmp" for the live ingest
        sound    "source": the source's own soundtrack becomes the microphone
                 "none":   picture only, the microphone stays silent
                 a library file name: that file's sound, looped, next to the picture
        attach   also show the camera to those Androids that are running (see attach())

        Whatever these Androids were playing before is replaced. Androids already watching
        the same source with the same sound are merged into one feed with them.
        """
        ids = self._ids(ids)
        if not ids:
            raise ValueError("no Android selected")
        with self._lock:
            media = self._resolve(source)
            if sound not in (SOUND_FROM_SOURCE, SOUND_NONE):
                self._library_file(sound)
            self._ffmpeg()
            wanted, targets = set(ids), set(ids)
            for feed in self._feeds():
                alive = self.launcher.alive(feed.ref)
                if alive and feed.source == media.name and feed.sound == sound:
                    # Same picture, same sound: join that feed instead of decoding twice (and a
                    # second live ingest could not even bind the port).
                    targets |= set(feed.ids)
                    self._stop_feed(feed)
                elif wanted & set(feed.ids):
                    # One writer per FIFO: whoever fed these Androids before lets go of them,
                    # and carries on for its other Androids.
                    rest = [i for i in feed.ids if i not in wanted] if alive else []
                    try:
                        self._restart_feed(feed, rest)
                    except Exception as e:  # noqa: BLE001 - say so, carry on with this play
                        log.warning("camera: feed %s (%s) could not be restarted for %s: %s",
                                    feed.id, feed.source, rest, e)
            targets = sorted(targets)
            results = self._ensure_sinks(targets)
            good = [i for i in targets if results[i]["ok"]]
            feed_out = None
            if good:
                feed_out = self._start_feed(media, sound, good).to_dict()
        if feed_out and attach:
            running = [i for i in ids if results[i]["ok"] and self._running(i)]
            for instance_id, outcome in self.attach(running)["results"].items():
                results[instance_id] = outcome
        return {"ok": bool(feed_out) and all(results[i]["ok"] for i in ids), "feed": feed_out,
                "results": {i: results[i] for i in ids}}

    def stop(self, ids: list[int] | None = None) -> dict:
        """Stop the video on these Androids (all when ids is None). The camera stays
        connected and shows its last picture; use down() to disconnect it."""
        with self._lock:
            wanted = set(self._ids(ids)) if ids is not None else {i for f in self._feeds() for i in f.ids}
            if ids is None:
                stopped = []
                for feed in self._feeds():
                    self._stop_feed(feed)
                    stopped.append(feed.id)
            else:
                stopped = self._release(wanted)
        return {"ok": True, "stopped": stopped, "ids": sorted(wanted)}

    def down(self, ids: list[int] | None = None) -> dict:
        """Stop the video *and* the per-Android helpers, releasing the devices. The
        microphone FIFO stays: a running Android has it mounted."""
        with self._lock:
            targets = self._ids(ids) if ids is not None else self.layout.instance_ids()
            self.stop(targets if ids is not None else None)
            table = self._slots().table()
            for instance_id in targets:
                for role in ("copier", "forwarder"):
                    file = self.layout.proc_file(instance_id, role)
                    self.launcher.stop(F.read_ref(file), hard=True)
                    file.unlink(missing_ok=True)
                for fifo in (self.layout.video_fifo(instance_id), self.layout.audio_fifo(instance_id)):
                    fifo.unlink(missing_ok=True)
                slot = table.get(instance_id)
                if slot is not None:
                    device = F.device_path(self._slots().number(slot), self.dev_root)
                    if F.device_is_ours(device, self.sys_root):
                        # Unpin the frame size, or a later change of media.camera.width/height
                        # could never take effect on this device.
                        try:
                            self._run(["v4l2-ctl", "-d", device, "-c", "keep_format=0"], timeout=10, check=False)
                        except CommandError:
                            pass
        return {"ok": True, "ids": targets}

    def forget(self, instance_id: int) -> None:
        """An Android was deleted: stop its helpers and free its device for another one."""
        instance_id = check_id(instance_id)
        with self._lock:
            self.down([instance_id])
            self._slots().release(instance_id)
            folder = self.layout.instance_dir(instance_id)
            for file in (folder.iterdir() if folder.is_dir() else []):
                file.unlink(missing_ok=True)
            if folder.is_dir():
                folder.rmdir()

    # ------------------------------------------------------------- public: heal
    def heal(self) -> dict:
        """Restart helpers that died and reconnect the feeds that fed them.

        A copier or forwarder that was restarted has a new FIFO; the feed still holds the old
        one and has already dropped that output (`onfail=ignore`), so the feed is restarted
        too. Feeds whose source gave up (a broken file) are left alone and reported by
        status(). Safe to call at any time; meant to be called periodically (see watch()).
        """
        restarted_sinks: list[int] = []
        restarted_feeds: list[str] = []
        with self._lock:
            feeds = [f for f in self._feeds() if self.launcher.alive(f.ref)]
            # Every Android that was set up and not taken down again, playing or not: an
            # attached camera without a copier is a frozen picture.
            wanted = {i for f in feeds for i in f.ids}
            wanted |= {i for i in self.layout.instance_ids() if self.layout.proc_file(i, "copier").exists()}
            for instance_id in sorted(wanted):
                try:
                    if self._ensure_sink(instance_id):
                        restarted_sinks.append(instance_id)
                    self._heal_failed.discard(instance_id)
                except Exception as e:  # noqa: BLE001
                    # Once per outage: this runs every few seconds and must not flood the log.
                    if instance_id not in self._heal_failed:
                        self._heal_failed.add(instance_id)
                        log.warning("camera: helpers of Android %s could not be restarted: %s", instance_id, e)
            for feed in feeds:
                if any(self._sink_started(i) > feed.started for i in feed.ids):
                    try:
                        self._restart_feed(feed, feed.ids)
                        restarted_feeds.append(feed.id)
                    except Exception as e:  # noqa: BLE001
                        log.warning("camera: feed %s could not be restarted: %s", feed.id, e)
        return {"sinks": restarted_sinks, "feeds": restarted_feeds}

    def watch(self, interval: float = 15.0) -> None:
        """Start a background thread that calls heal() every `interval` seconds."""
        if self._watchdog and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()

        def loop() -> None:
            while not self._watchdog_stop.wait(interval):
                try:
                    self.heal()
                except Exception:  # noqa: BLE001 - the watchdog must outlive any single failure
                    log.exception("camera: heal failed")

        self._watchdog = threading.Thread(target=loop, name="sf-camera-watch", daemon=True)
        self._watchdog.start()

    def unwatch(self) -> None:
        self._watchdog_stop.set()

    # -------------------------------------------------------------- public: mic
    def mic(self, ids: list[int], mode: str) -> dict:
        """mute | unmute | solo. Takes effect within half a second, nothing is restarted.

        mute     these Androids record silence, whatever is playing
        unmute   these Androids hear their feed again (an empty list = all of them)
        solo     only these Androids keep their microphone; every other one is muted
        """
        if mode not in MIC_MODES:
            raise ValueError(f"bad microphone mode {mode!r}: use one of {', '.join(MIC_MODES)}")
        ids = self._ids(ids)
        known = set(self.layout.instance_ids())
        if mode == "solo":
            if not ids:
                raise ValueError("solo needs at least one Android")
            try:
                known |= {int(s["id"]) for s in self.mgr.list_status() if "id" in s}
            except Exception as e:  # noqa: BLE001 - without the manager, mute what the camera knows
                log.debug("camera: instance list unavailable for solo: %s", e)
            mute, unmute = known - set(ids), set(ids)
        elif mode == "mute":
            mute, unmute = set(ids), set()
        else:
            mute, unmute = set(), (set(ids) if ids else known)
        for instance_id in mute:
            marker = self.layout.mute_marker(instance_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        for instance_id in unmute:
            self.layout.mute_marker(instance_id).unlink(missing_ok=True)
        muted = [i for i in self.layout.instance_ids() if self.layout.mute_marker(i).exists()]
        return {"ok": True, "mode": mode, "muted": muted}

    # ----------------------------------------------------------- public: attach
    def _exec(self, name: str, argv: list[str], timeout: float = 30) -> Result:
        try:
            return self.docker.exec(name, argv, timeout=timeout, check=False)
        except CommandError as e:
            return Result(124 if e.timed_out else 1, e.stdout, e.stderr or str(e))

    def _has_driver(self, name: str) -> bool:
        return self._exec(name, ["sh", "-c", f"[ -s /vendor/etc/init/{F.HAL_SERVICE}.rc ]"], 20).ok

    def _has_mic(self, name: str) -> bool:
        return self._exec(name, ["sh", "-c", f"[ -p {F.CONTAINER_MIC} ]"], 20).ok

    def _camera_count(self, name: str) -> int:
        out = self._exec(name, ["sh", "-c", _COUNT_SCRIPT], 45).out
        return int(out) if out.isdigit() else 0

    def _node_matches(self, name: str, major: int, minor: int) -> bool:
        # toybox prints device numbers in hex for %t / %T.
        return self._exec(name, ["stat", "-c", "%t:%T", F.CONTAINER_CAMERA], 20).out.lower() == f"{major:x}:{minor:x}"

    def _attach_one(self, instance_id: int) -> dict:
        try:
            name = self._container(instance_id)
        except KeyError:
            return {"ok": False, "detail": f"no Android {instance_id}"}
        except Exception as e:  # noqa: BLE001 - e.g. a spec that no longer resolves
            return {"ok": False, "detail": str(e)}
        if not self.docker.running(name):
            return {"ok": False, "detail": "the Android is not running"}
        if not self._has_driver(name):
            return {"ok": False, "detail": "no camera driver in this Android: install it (camera install_hal) "
                                           "and restart the Android"}
        try:
            # The HAL reads the format once, when it finds the node: frames must flow first.
            self._ensure_sink(instance_id)
            device = self.device(instance_id)
            major, minor = self._stat_device(device)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)}
        mic = self._has_mic(name)
        note = "" if mic else ("; microphone not connected: this Android was created before its camera was "
                               "enabled, recreate it")
        if self._camera_count(name) == 1 and self._node_matches(name, major, minor):
            return {"ok": True, "detail": "camera attached" + note, "microphone": mic}
        try:
            # Without this the device only advertises a fixed frame size while a writer
            # happens to own it, and the HAL then finds no usable format.
            self._run(["v4l2-ctl", "-d", device, "-c", "keep_format=1"], timeout=10)
        except CommandError as e:
            return {"ok": False, "detail": f"could not pin the frame size of {device} (is v4l-utils "
                                           f"installed?): {e}"}
        made = self._exec(name, ["sh", "-c", _MKNOD_SCRIPT, "sh", str(int(major)), str(int(minor))], 30)
        if not made.ok:
            return {"ok": False, "detail": f"could not create {F.CONTAINER_CAMERA} in the Android: "
                                           f"{(made.stderr or made.stdout).strip()[:200]}"}
        self._sleep(2.0)
        if self._camera_count(name) != 1:
            # The HAL looked before the node was ready; restarting it makes it scan again.
            self._exec(name, ["sh", "-c", _RESTART_HAL_SCRIPT], 20)
            self._sleep(4.0)
            if self._camera_count(name) != 1:
                return {"ok": False, "detail": "Android does not see the camera (the camera service did not "
                                               "accept the device); check `logcat` for ExtCam messages",
                        "microphone": mic}
        return {"ok": True, "detail": "camera attached" + note, "microphone": mic}

    def attach(self, ids: list[int]) -> dict:
        """Show each of these running Androids its own camera device, as /dev/video0.

        Needed once after every start of an Android (done automatically after boot) and
        harmless to repeat. All Androids are handled at once; each can take several seconds.
        """
        ids = self._ids(ids)
        results = dict(zip(ids, self._parallel(self._attach_one, ids), strict=True))
        return {"ok": all(r["ok"] for r in results.values()), "results": results}

    def post_boot(self, r: Resolved, adb: Adb | None = None) -> list[str]:
        """Instance-manager hook: after an Android with a camera has booted, attach it.

        Never raises: a camera problem must not turn a successful start into a failed one.
        What went wrong is returned as a list of warnings, which the instance manager adds
        to the start result, and logged."""
        if not r.camera:
            return []
        try:
            outcome = self._attach_one(r.id)
        except Exception as e:  # noqa: BLE001
            outcome = {"ok": False, "detail": str(e)}
        (log.info if outcome["ok"] else log.warning)("camera: Android %s: %s", r.id, outcome["detail"])
        return [] if outcome["ok"] else [f"camera: {outcome['detail']}"]

    # ---------------------------------------------------------- public: install
    def install_hal(self, ids: list[int]) -> dict:
        """Copy the camera + microphone HAL overlay into these Androids' /vendor.

        Takes effect at the next start of each Android; nothing is restarted here. The copy
        lives in the container, so it is lost when the container is recreated: bake the
        overlay into the image for good (native/camera/README.md). Android 13 only.
        """
        results: dict[int, dict] = {}
        for instance_id in self._ids(ids):
            try:
                r = self.mgr.resolved(instance_id)
                folder = self._overlay_dir(r.android_version)
                lacking = [f for f in OVERLAY_REQUIRED if not (folder / f).is_file()]
                if lacking:
                    raise RuntimeError(
                        f"no complete camera HAL for Android {r.android_version} in {folder} (missing "
                        f"{lacking[0]}). The HAL exists for Android 13 only; build it with "
                        "native/camera/extract_hal.sh. Other Android versions need their own.")
                if not self.docker.exists(r.names.android):
                    raise RuntimeError("its container does not exist yet: start the Android once first")
                self.docker.run(["cp", "-", f"{r.names.android}:/vendor/"], input=overlay_archive(folder),
                                binary=True, timeout=300)
                running = self.docker.running(r.names.android)
                results[instance_id] = {"ok": True, "restart_needed": running,
                                        "detail": "installed" + ("; restart the Android to load it" if running else "")}
            except Exception as e:  # noqa: BLE001
                results[instance_id] = {"ok": False, "detail": str(e)}
        return {"ok": all(r["ok"] for r in results.values()), "results": results}

    # --------------------------------------------------------- container wiring
    def extra_run_args(self, r: Resolved) -> list[str]:
        """`docker run` arguments an Android with a camera needs: its microphone FIFO.

        The FIFO lives on the host and is mounted at /dev/farm_mic, where the audio shim
        looks for it. The daemon can then feed the microphone as an ordinary user, with no
        need to reach into the container's file system. The FIFO is created here because it
        must exist before the container does; `--mount` (unlike `-v`) also refuses to start
        the container if it is missing, instead of quietly creating a folder in its place.
        """
        if not r.camera:
            return []
        fifo = F.ensure_fifo(self.layout.mic(r.id), 0o644)
        text = str(fifo)
        if any(ch in text for ch in ',"\n\r'):
            raise ValueError(f"the farm home path {self.paths.home} contains a character Docker cannot "
                             "mount (comma, quote or line break): move the farm home (SUPERFARM_HOME)")
        return ["--mount", f"type=bind,source={text},target={F.CONTAINER_MIC}"]


def overlay_archive(folder: Path) -> bytes:
    """The overlay as a tar stream for `docker cp - <container>:/vendor/`.

    Copying the folder itself (`docker cp <folder>/. ...`) carries the numeric owner of the
    files on the host into the Android. The daemon is an ordinary user, typically uid 1000,
    which inside Android is the `system` user: every HAL file, and every /vendor directory
    the copy touches, would end up owned and writable by Android's system processes. So
    each file is written as root:root with mode 0755 (executables) or 0644, and no
    directory entries are sent, which leaves the existing /vendor directories exactly as
    the image made them. Symbolic links are not followed and not copied: an overlay is
    plain files, and a link could point anywhere on the host.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for path in sorted(folder.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            st = path.stat()
            info = tarfile.TarInfo(path.relative_to(folder).as_posix())
            info.size, info.mtime = st.st_size, int(st.st_mtime)
            info.mode = 0o755 if st.st_mode & 0o111 else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(path, "rb") as f:
                tar.addfile(info, f)
    return buffer.getvalue()


def _boot_time() -> float:
    try:
        return time.time() - float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0

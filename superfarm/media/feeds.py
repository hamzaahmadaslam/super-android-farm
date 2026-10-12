"""Building blocks of the virtual camera: files on disk, helper processes, ffmpeg commands.

Everything here is either pure (command construction, slot arithmetic) or a thin, replaceable
wrapper around the operating system (FIFOs, pid files, process start/stop). `camera.py` holds
the policy; unit tests drive it with a fake `Launcher`.

The pipeline, per video that is playing:

    source  one ffmpeg per feed: decode + scale + encode MJPEG *once*, tee to
            <run>/camera/<id>/video.fifo for every Android that watches it, and the
            soundtrack (48 kHz stereo s16le) to <run>/camera/<id>/audio.fifo
    copier  one tiny ffmpeg per Android: copies frames from its FIFO into its
            v4l2loopback device and never closes that device
    forwarder  one small Python process per Android: paces the sound into the
            microphone FIFO <run>/camera/<id>/mic, which is bind-mounted into the
            container as /dev/farm_mic

Playing one video to fifty Androids therefore costs one decode and one encode. Changing the
video replaces only the source: an Android that has its camera open keeps the same, never
closed device and simply sees a new picture.
"""
from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Callable, Iterable, Sequence

from ..config import Config
from ..core.models import check_id
from ..paths import REPO_DIR
from ..util.files import locked, read_json, write_json

# Name the HAL shim opens inside Android. Fixed in the compiled shim (native/camera/mic_shim.c);
# changing it here would silently disconnect every microphone.
CONTAINER_MIC = "/dev/farm_mic"
# The one node an Android may see as its camera.
CONTAINER_CAMERA = "/dev/video0"
# Card name given to the loopback devices at host setup ("Super Farm Camera 7"). A device at
# one of our numbers whose name starts with neither label was created by something else (a
# webcam, a capture card, another farm) and is never written to.
CARD_LABEL = "Super Farm Camera"
# Label used when devices are created through module parameters instead of v4l2loopback-ctl:
# a blank would end the parameter's value there.
CARD_LABEL_PARAM = "superfarm-cam"
OUR_LABELS = (CARD_LABEL, CARD_LABEL_PARAM)
HAL_SERVICE = "android.hardware.camera.provider@2.5-external-service"
# What the microphone shim expects; anything else would play at the wrong speed.
MIC_RATE = 48000
MIC_CHANNELS = 2
RTMP_APP = "live"
RTMP_KEY = "camera"
RTMP_SOURCE = "rtmp"
# v4l2 hands out 256 minors in total (VIDEO_NUM_DEVICES).
MAX_VIDEO_NR = 255

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".flv"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}


# --------------------------------------------------------------------------- settings

@dataclass(frozen=True)
class VideoSettings:
    """Sensor geometry and encoder settings, taken from media.camera."""
    width: int
    height: int
    fps: int
    quality: int
    mirror: bool
    # ffmpeg transpose value. The HAL declares a phone front sensor mounted sideways
    # (orientation 270), so an upright portrait picture has to be stored rotated by 90
    # degrees clockwise or Android shows it lying on its side.
    rotate: int = 1

    @classmethod
    def from_config(cls, config: Config) -> VideoSettings:
        c = config.media.camera
        return cls(width=c.width, height=c.height, fps=c.fps, quality=c.quality, mirror=c.mirror)

    @property
    def sysfs_format(self) -> str:
        """Prefix of /sys/class/video4linux/videoN/format once the device has our frame size."""
        return f"MJPG:{self.width}x{self.height}@"


def video_filter(s: VideoSettings) -> str:
    """Scale-to-fill, crop, optional mirror, rotate, full-range 4:2:0.

    The sensor frame is landscape W x H; the picture inside is portrait H x W, rotated. A
    9:16 portrait video therefore fills a phone screen without bars.
    """
    parts = [
        f"fps={s.fps}",
        f"scale={s.height}:{s.width}:force_original_aspect_ratio=increase:flags=fast_bilinear",
        f"crop={s.height}:{s.width}",
    ]
    if s.mirror:
        parts.append("hflip")
    parts += [f"transpose={s.rotate}", "format=yuvj420p"]
    return ",".join(parts)


def tee_escape(path: str | os.PathLike) -> str:
    """Quote a file name for ffmpeg's tee muxer, whose syntax gives `|`, `[`, `]`, quotes,
    backslashes and surrounding blanks a meaning. The farm home is chosen by the operator and
    may contain any of them."""
    return "".join("\\" + c if c in "\\'|[], \t" else c for c in str(path))


def tee_outputs(paths: Iterable[str | os.PathLike], fmt: str) -> str:
    """One tee slave per FIFO. `onfail=ignore`: an Android whose copier died must not take
    the picture away from every other Android sharing this source."""
    return "|".join(f"[f={fmt}:onfail=ignore]{tee_escape(p)}" for p in paths)


def rtmp_url(config: Config) -> str:
    c = config.media.camera
    host = f"[{c.rtmp_bind}]" if ":" in c.rtmp_bind else c.rtmp_bind
    return f"rtmp://{host}:{c.rtmp_port}/{RTMP_APP}/{RTMP_KEY}"


def file_input(path: Path) -> list[str]:
    """Loop a file forever at its natural speed."""
    return ["-re", "-stream_loop", "-1", "-i", str(path)]


def rtmp_input(url: str) -> list[str]:
    """Wait for one publisher (OBS and friends) on the ingest address."""
    return ["-fflags", "nobuffer", "-analyzeduration", "1000000", "-listen", "1", "-i", url]


def feed_command(*, ffmpeg: str, main_input: Sequence[str], sound_input: Sequence[str] | None,
                 video_fifos: Sequence[Path], audio_fifos: Sequence[Path], settings: VideoSettings) -> list[str]:
    """The source: every input decoded once, every output fanned out with tee.

    main_input    ffmpeg input arguments of the source (see file_input / rtmp_input)
    sound_input   None   = the sound, if any is wanted, is the source's own soundtrack
                  a list = a second input that supplies the sound instead
    video_fifos   one per Android that gets the picture; empty for a sound-only feed
    audio_fifos   one per Android that gets the sound; empty for a silent feed
    """
    if not main_input:
        raise ValueError("a feed needs an input")
    if not video_fifos and not audio_fifos:
        raise ValueError("a feed needs at least one picture or sound output")
    argv = [ffmpeg, "-nostdin", "-loglevel", "error", *main_input]
    sound_index = 0
    if sound_input and audio_fifos:
        argv += list(sound_input)
        sound_index = 1
    if video_fifos:
        # Android's external-camera HAL only accepts MJPEG from a V4L2 device.
        argv += ["-map", "0:v:0", "-an", "-vf", video_filter(settings),
                 "-c:v", "mjpeg", "-q:v", str(settings.quality),
                 "-flush_packets", "1", "-f", "tee", tee_outputs(video_fifos, "mjpeg")]
    if audio_fifos:
        argv += ["-map", f"{sound_index}:a:0", "-vn", "-ac", str(MIC_CHANNELS), "-ar", str(MIC_RATE),
                 "-c:a", "pcm_s16le", "-flush_packets", "1", "-f", "tee", tee_outputs(audio_fifos, "s16le")]
    return argv


def copier_command(*, ffmpeg: str, fifo: Path, device: str, settings: VideoSettings) -> list[str]:
    """Copy MJPEG frames from the FIFO into the loopback device, without re-encoding.

    A small probe size makes the first picture appear after about ninety frames instead of
    ffmpeg's default five megabytes of input.
    """
    return [ffmpeg, "-nostdin", "-loglevel", "error", "-f", "mjpeg", "-framerate", str(settings.fps),
            "-probesize", "150000", "-analyzeduration", "0", "-i", str(fifo),
            "-c", "copy", "-flush_packets", "1", "-f", "v4l2", device]


def prime_command(*, ffmpeg: str, fifo: Path, settings: VideoSettings, frames: int = 90) -> list[str]:
    """Black frames that give a fresh device its frame size.

    A loopback device only learns its format from the first frames written to it, and
    Android's HAL refuses a camera without one. Priming means attaching never depends on a
    video already playing. Ninety frames fill the copier's probe buffer. `timeout` bounds it:
    opening a FIFO nobody reads would block forever.
    """
    return ["timeout", "20", ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi",
            "-i", f"color=black:s={settings.width}x{settings.height}:r={settings.fps}",
            "-frames:v", str(int(frames)), "-c:v", "mjpeg", "-q:v", "2", "-pix_fmt", "yuvj420p",
            "-f", "mjpeg", "-y", str(fifo)]


def script_path(name: str) -> Path:
    """Helper scripts ship in scripts/camera of the source tree."""
    path = REPO_DIR / "scripts" / "camera" / name
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} is missing. The camera helpers ship in the project's scripts/camera folder; "
            "run Super Farm from a source checkout or reinstall it.")
    return path


def forwarder_command(*, source: Path, target: Path, mute_marker: Path, python: str | None = None) -> list[str]:
    return [python or sys.executable, str(script_path("mic_forwarder.py")),
            "--source", str(source), "--target", str(target), "--mute-marker", str(mute_marker)]


def supervised(argv: Sequence[str], *, give_up: int, delay: float = 1.0, python: str | None = None) -> list[str]:
    """Wrap a source in the restart loop (scripts/camera/run_loop.py).

    give_up = 0   restart forever: a live ingest must listen again each time the publisher
                  stops and starts its stream
    give_up = N   stop after N runs in a row that died within seconds, so a broken file
                  shows up as a failed feed with a log instead of a busy loop
    """
    return [python or sys.executable, str(script_path("run_loop.py")),
            "--delay", str(delay), "--give-up", str(int(give_up)), "--", *argv]


# --------------------------------------------------------------------------- devices

def device_path(number: int, dev_root: Path = Path("/dev")) -> str:
    return str(dev_root / f"video{int(number)}")


def device_number(device: str) -> int:
    name = os.path.basename(device)
    if not name.startswith("video") or not name[5:].isdigit():
        raise ValueError(f"not a video device: {device!r}")
    return int(name[5:])


def device_name(device: str, sys_root: Path = Path("/sys")) -> str:
    """Card name from sysfs. Reading it does not open the device."""
    try:
        return (sys_root / "class" / "video4linux" / os.path.basename(device) / "name").read_text().strip()
    except OSError:
        return ""


def device_is_ours(device: str, sys_root: Path = Path("/sys")) -> bool:
    return device_name(device, sys_root).startswith(OUR_LABELS)


def device_format(device: str, sys_root: Path = Path("/sys")) -> str | None:
    """Current format as v4l2loopback reports it (`MJPG:640x360@20`), "" when the device has
    none yet, None when this v4l2loopback has no such attribute."""
    try:
        return (sys_root / "class" / "video4linux" / os.path.basename(device) / "format").read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return ""


class Slots:
    """Which loopback device belongs to which Android.

    Host setup creates `count` devices, numbered base + 1 .. base + count. An instance id
    may be anything up to 9999, so ids are mapped onto that pool instead of being used as
    device numbers: an id N from 1 to `count` gets device base + N (stable, and easy to
    recognise in `v4l2-ctl --list-devices`); any other id takes a free device from the top
    of the pool. The table is kept on disk because a running Android holds a device node
    made for one specific device.
    """

    def __init__(self, file: Path, base: int, count: int):
        self.file, self.base, self.count = file, int(base), int(count)

    def _load(self) -> dict[int, int]:
        raw = read_json(self.file, {}) or {}
        table: dict[int, int] = {}
        for key, slot in raw.items():
            # A pool that was shrunk in the config invalidates entries beyond its end.
            if str(key).isdigit() and isinstance(slot, int) and 0 <= slot < self.count:
                table[int(key)] = slot
        return table

    def _save(self, table: dict[int, int]) -> None:
        write_json(self.file, {str(k): v for k, v in sorted(table.items())})

    def table(self) -> dict[int, int]:
        return self._load()

    def lookup(self, instance_id: int) -> int | None:
        return self._load().get(check_id(instance_id))

    def number(self, slot: int) -> int:
        """Device number of a slot (slots count from 0, devices from base + 1)."""
        return self.base + 1 + slot

    def numbers(self) -> list[int]:
        return [self.number(slot) for slot in range(self.count)]

    def assign(self, instance_id: int, *, in_use: Callable[[int], bool] | None = None) -> int:
        """Slot of this instance, allocating one if needed. `in_use(other_id)` tells whether
        another instance still needs its slot; stale owners are evicted only when the pool
        is full."""
        instance_id = check_id(instance_id)
        if self.count <= 0:
            raise RuntimeError("no camera devices are configured: set media.camera.slots and run `sf host apply`")
        with locked(self.file):
            table = self._load()
            if instance_id in table:
                return table[instance_id]
            taken = set(table.values())
            if 1 <= instance_id <= self.count and instance_id - 1 not in taken:
                slot = instance_id - 1
            else:
                free = [s for s in range(self.count - 1, -1, -1) if s not in taken]
                if not free and in_use is not None:
                    for other in [o for o in table if not in_use(o)]:
                        taken.discard(table.pop(other))
                    free = [s for s in range(self.count - 1, -1, -1) if s not in taken]
                if not free:
                    raise RuntimeError(
                        f"all {self.count} camera devices are taken (instances "
                        f"{', '.join(str(i) for i in sorted(table))}). Raise media.camera.slots and run "
                        "`sf host apply`, or switch the camera off on an Android that does not need it.")
                slot = free[0]
            table[instance_id] = slot
            self._save(table)
            return slot

    def release(self, instance_id: int) -> None:
        with locked(self.file):
            table = self._load()
            if table.pop(check_id(instance_id), None) is not None:
                self._save(table)


# --------------------------------------------------------------------------- processes

@dataclass(frozen=True)
class ProcRef:
    """A process identified by pid *and* start time. Pids are reused; a pid file that
    outlived its process must never make us signal a stranger."""
    pid: int
    ticks: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> ProcRef | None:
        if not isinstance(data, dict):
            return None
        pid, ticks = data.get("pid"), data.get("ticks")
        if isinstance(pid, int) and isinstance(ticks, int) and pid > 1:
            return cls(pid, ticks)
        return None


def proc_stat(pid: int) -> tuple[str, int] | None:
    """(state letter, start time in clock ticks) from /proc/<pid>/stat, None when gone."""
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text()
    except (OSError, ValueError):
        return None
    # The command name may contain blanks and brackets; the fields after the last ')' are safe.
    fields = text[text.rfind(")") + 2:].split()
    try:
        return fields[0], int(fields[19])
    except (IndexError, ValueError):
        return None


class Launcher:
    """Starts and stops the helper processes.

    Each helper gets its own session: it survives a daemon restart (an Android that is
    using its camera must not lose the device because the panel was restarted) and it can
    be stopped together with everything it started.
    """

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen] = {}

    def start(self, argv: Sequence[str], *, log: Path, pass_fds: Sequence[int] = ()) -> ProcRef:
        if isinstance(argv, (str, bytes)) or not argv:
            raise TypeError("command must be a non-empty list of arguments, not a string")
        log.parent.mkdir(parents=True, exist_ok=True)
        # Truncated on every start: the log of a helper is only ever read to explain why the
        # current one failed, and nothing rotates these files.
        with open(log, "wb") as out:
            try:
                child = subprocess.Popen([str(a) for a in argv], stdin=subprocess.DEVNULL, stdout=out,
                                         stderr=subprocess.STDOUT, start_new_session=True,
                                         pass_fds=tuple(pass_fds), close_fds=True)
            except FileNotFoundError:
                raise RuntimeError(f"{argv[0]}: command not found. Install it and try again.") from None
        self._children[child.pid] = child
        stat_now = proc_stat(child.pid)
        return ProcRef(child.pid, stat_now[1] if stat_now else 0)

    def _reap(self) -> None:
        # Our own exited children stay as zombies until waited for.
        for pid, child in list(self._children.items()):
            if child.poll() is not None:
                del self._children[pid]

    def alive(self, ref: ProcRef | None) -> bool:
        if ref is None:
            return False
        self._reap()
        stat_now = proc_stat(ref.pid)
        if stat_now is None or stat_now[0] in ("Z", "X"):
            return False
        return ref.ticks in (0, stat_now[1])

    def stop(self, ref: ProcRef | None, *, grace: float = 2.0, hard: bool = False) -> None:
        """Stop the helper and its whole session. hard=True skips the polite signal: a copier
        blocked on its FIFO never acts on SIGTERM and holds no state worth saving."""
        if ref is None or not self.alive(ref):
            return
        if not hard:
            _signal_session(ref.pid, signal.SIGTERM)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if not self.alive(ref):
                    return
                time.sleep(0.05)
        _signal_session(ref.pid, signal.SIGKILL)
        deadline = time.monotonic() + 2.0
        while self.alive(ref) and time.monotonic() < deadline:
            time.sleep(0.02)


def _signal_session(pid: int, sig: int) -> None:
    try:
        # Helpers are session leaders, so their pid is also their process-group id. Asking
        # the kernel for the group (instead of assuming it) protects against a recycled pid
        # that is an ordinary member of somebody else's group.
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# --------------------------------------------------------------------------- files

class Layout:
    """Where the camera keeps its runtime files (all under <run>/camera)."""

    def __init__(self, root: Path):
        self.root = root

    @property
    def feeds_dir(self) -> Path: return self.root / "feeds"
    @property
    def slots_file(self) -> Path: return self.root / "slots.json"

    def instance_dir(self, instance_id: int) -> Path: return self.root / str(check_id(instance_id))
    def mic(self, instance_id: int) -> Path: return self.instance_dir(instance_id) / "mic"
    def video_fifo(self, instance_id: int) -> Path: return self.instance_dir(instance_id) / "video.fifo"
    def audio_fifo(self, instance_id: int) -> Path: return self.instance_dir(instance_id) / "audio.fifo"
    def mute_marker(self, instance_id: int) -> Path: return self.instance_dir(instance_id) / "mute"
    def proc_file(self, instance_id: int, role: str) -> Path: return self.instance_dir(instance_id) / f"{role}.json"
    def log(self, instance_id: int, role: str) -> Path: return self.instance_dir(instance_id) / f"{role}.log"
    def feed_file(self, feed_id: str) -> Path: return self.feeds_dir / f"{feed_id}.json"
    def feed_log(self, feed_id: str) -> Path: return self.feeds_dir / f"{feed_id}.log"

    def instance_ids(self) -> list[int]:
        if not self.root.is_dir():
            return []
        return sorted(int(d.name) for d in self.root.iterdir() if d.is_dir() and d.name.isdigit())


def ensure_fifo(path: Path, mode: int = 0o600) -> Path:
    """Create the FIFO unless it already exists. An existing FIFO is *kept*: a running Android
    has this very inode bind-mounted and its audio HAL holds it open, so replacing it would
    cut the microphone until the container is recreated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        os.mkfifo(path, mode)
        os.chmod(path, mode)        # mkfifo applies the umask; the mode here is deliberate
        return path
    if not stat.S_ISFIFO(st.st_mode):
        raise RuntimeError(
            f"{path} exists but is not a FIFO (Docker creates a folder there when a container is "
            "started before the FIFO exists). Stop that Android, remove the path and start it again.")
    if stat.S_IMODE(st.st_mode) != mode:
        os.chmod(path, mode)
    return path


def fresh_fifo(path: Path, mode: int = 0o600) -> Path:
    """Replace the FIFO with a new, empty one. Used for the FIFOs between our own helpers when
    the reader is restarted: whatever a previous writer left half-written must not reach the
    new reader, and a writer still attached to the old FIFO gets a clean error instead."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    os.mkfifo(path, mode)
    os.chmod(path, mode)
    return path


def read_ref(path: Path) -> ProcRef | None:
    return ProcRef.from_dict(read_json(path))


def write_ref(path: Path, ref: ProcRef, **extra: object) -> None:
    write_json(path, {**ref.to_dict(), **extra})


@dataclass
class Feed:
    """One running source and the Androids it serves."""
    id: str
    source: str            # media library file name, or "rtmp"
    sound: str             # "source" | "none" | media library file name
    ids: list[int]
    video: bool            # this feed writes the picture
    audio: bool            # this feed writes the microphone
    pid: int = 0
    ticks: int = 0
    started: float = 0.0

    @property
    def ref(self) -> ProcRef | None:
        return ProcRef(self.pid, self.ticks) if self.pid > 1 else None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> Feed | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(id=str(data["id"]), source=str(data["source"]), sound=str(data.get("sound", "source")),
                       ids=sorted({check_id(i) for i in data["ids"]}), video=bool(data.get("video", True)),
                       audio=bool(data.get("audio", False)), pid=int(data.get("pid", 0)),
                       ticks=int(data.get("ticks", 0)), started=float(data.get("started", 0.0)))
        except (KeyError, TypeError, ValueError):
            return None


def load_feeds(layout: Layout) -> list[Feed]:
    feeds = []
    if layout.feeds_dir.is_dir():
        for file in sorted(layout.feeds_dir.glob("*.json")):
            feed = Feed.from_dict(read_json(file))
            if feed is not None:
                feeds.append(feed)
    return feeds


def save_feed(layout: Layout, feed: Feed) -> None:
    write_json(layout.feed_file(feed.id), feed.to_dict())


def drop_feed(layout: Layout, feed_id: str) -> None:
    for path in (layout.feed_file(feed_id), layout.feed_log(feed_id)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def tail(path: Path, lines: int = 5, limit: int = 600) -> str:
    """Last lines of a helper's log, for error messages."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            text = f.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])[-limit:]

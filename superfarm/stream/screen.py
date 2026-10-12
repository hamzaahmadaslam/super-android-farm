"""Screens of devices as JPEG pictures, shared between everyone who is looking.

A device nobody watches costs nothing: no thread, no ADB traffic. The first viewer of
a device starts one capture worker for it; every further viewer of the same device
receives the very same frames (one capture, one JPEG encode per requested width). When
the last viewer leaves, the worker stops capturing at once and exits after a short
grace period, so a page reload does not pay the start-up cost again.

Rules the worker follows
- Captures never queue. The next one starts only after the previous one returned, so
  a slow device simply delivers fewer frames instead of piling up ADB processes.
- Every capture has a hard timeout. A busy Android can stall any ADB command.
- Failures back off (0.5 s doubling to 8 s) and are classified into a state the panel
  can show: `offline` (ADB cannot reach it), `booting` (reachable, Android not up yet),
  `error` (anything else). Viewers receive a small placeholder picture for that state.

Capture path
`screencap -p` makes the device compress a PNG that the host immediately decodes
again. Plain `screencap` sends the raw pixels instead: more bytes over ADB, far less
work on both ends. Which one wins depends on resolution and on how ADB reaches the
device, so each device is timed on its first frames and the faster path is kept
(480x854 over loopback ADB: raw about 60 ms per frame whatever is on screen, PNG 60 ms
for a flat page and 210 ms for a photo). A tie goes to raw.
"""
from __future__ import annotations

import asyncio
import io
import struct
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol
from collections.abc import AsyncIterator

from PIL import Image, ImageDraw

from ..core.adb import Adb
from ..core.models import check_id

STATES = ("idle", "live", "offline", "booting", "error")

_RAW_LAYOUTS = {1: ("RGBX", 4), 2: ("RGBX", 4), 3: ("RGB", 3)}   # Android PixelFormat -> (Pillow raw mode, bytes per pixel)
_MAX_SIDE = 8192
_PROBE_SAMPLES = 2          # frames timed per capture path before one is chosen
_PNG_MUST_WIN = 0.75        # PNG is chosen only when it takes less than this fraction of raw's time
_BACKOFF_FIRST, _BACKOFF_MAX = 0.5, 8.0
_BREATHER = 0.15            # fraction of a capture's duration the device is left alone afterwards
_STALE_VIEWER = 15.0        # a viewer that has not asked for this long no longer keeps the capture running
_KEEPALIVE = 5.0            # placeholder repeat interval, so a stalled stream is not mistaken for a dead socket
_FRAME_TTL = 30.0           # an unwatched device's last picture is dropped after this (about 1 MB each)
_MAX_ENCODINGS = 8          # distinct widths cached per frame


class ScreenError(RuntimeError):
    """A picture could not be produced. `state` is one of offline | booting | error."""

    def __init__(self, state: str, detail: str):
        super().__init__(f"{state}: {detail}" if detail else state)
        self.state, self.detail = state, detail


class ViewerLimit(RuntimeError):
    """stream.max_viewers live views are already open."""


class _Manager(Protocol):
    def adb(self, id: int) -> Adb: ...


def decode_raw(data: bytes) -> Image.Image:
    """Pixels from plain `screencap`: a little-endian header (width, height, format, and a colour
    space word since Android 9) followed by the rows. Returns an RGBX or RGB image."""
    if len(data) < 16:
        raise ValueError("raw screenshot is too short")
    width, height, fmt = struct.unpack_from("<III", data)
    layout = _RAW_LAYOUTS.get(fmt)
    if layout is None or not (0 < width <= _MAX_SIDE and 0 < height <= _MAX_SIDE):
        raise ValueError(f"raw screenshot has an unsupported layout (format {fmt}, {width}x{height})")
    mode, bpp = layout
    body = width * height * bpp
    header = len(data) - body
    if header not in (12, 16):   # 12 = before Android 9; the length decides, not the version
        raise ValueError(f"raw screenshot has {len(data)} bytes, expected {body} of pixels plus a header")
    # RGBX is mapped onto the received bytes without a copy, and both the JPEG encoder and
    # resize() take it as it is; converting to RGB first would only cost time per frame.
    return Image.frombuffer(mode, (width, height), memoryview(data)[header:], "raw", mode, 0, 1)


def decode_png(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    if max(image.size) > _MAX_SIDE:
        raise ValueError(f"screenshot is implausibly large ({image.size[0]}x{image.size[1]})")
    return image.convert("RGB")


def _clean_width(width: int | None) -> int | None:
    """Validate a requested width once, where the request enters (not later inside a worker)."""
    if width is None or width == 0:
        return None
    if isinstance(width, bool) or not isinstance(width, (int, float, str)):
        raise ValueError(f"bad width {width!r}")
    try:
        value = int(width)
    except ValueError:
        raise ValueError(f"bad width {width!r}") from None
    if not 0 < value <= _MAX_SIDE:
        raise ValueError(f"width {value} is outside 1-{_MAX_SIDE}")
    return value


def _fit_width(width: int | None, native: int) -> int:
    """0 = native size. Pictures are never scaled up."""
    if not width:
        return 0
    width = max(32, int(width))
    return 0 if width >= native else width


@lru_cache(maxsize=64)
def placeholder(state: str, width: int = 240, height: int = 427, quality: int = 60) -> bytes:
    """A dark picture with one word on it, sent in place of a frame that cannot be had."""
    width, height = max(32, min(width, 1080)), max(32, min(height, 1920))
    image = Image.new("RGB", (width, height), (24, 26, 30))
    draw = ImageDraw.Draw(image)
    label = state.upper()[:24]
    box = draw.textbbox((0, 0), label)
    draw.text(((width - (box[2] - box[0])) // 2, (height - (box[3] - box[1])) // 2), label, fill=(150, 155, 165))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


class _Frame:
    """One captured picture and its JPEG encodings, one per requested width."""

    __slots__ = ("seq", "taken", "image", "_jpegs", "_lock")

    def __init__(self, seq: int, image: Image.Image):
        self.seq, self.taken, self.image = seq, time.monotonic(), image
        self._jpegs: dict[tuple[int, int], bytes] = {}
        self._lock = threading.Lock()

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def cached(self, width: int | None, quality: int) -> bytes | None:
        return self._jpegs.get((_fit_width(width, self.image.width), quality))

    def jpeg(self, width: int | None, quality: int) -> bytes:
        key = (_fit_width(width, self.image.width), quality)
        with self._lock:   # two viewers asking for the same width share one encode
            data = self._jpegs.get(key)
            if data is None:
                image = self.image
                if key[0]:
                    height = max(1, round(image.height * key[0] / image.width))
                    image = image.resize((key[0], height), Image.Resampling.BILINEAR, reducing_gap=2.0)
                out = io.BytesIO()
                image.save(out, "JPEG", quality=quality)
                data = out.getvalue()
                if len(self._jpegs) < _MAX_ENCODINGS:
                    self._jpegs[key] = data
            return data


@dataclass(eq=False)
class _Viewer:
    device: _Device
    fps: float
    width: int | None
    loop: asyncio.AbstractEventLoop
    event: asyncio.Event
    pulled: float = field(default_factory=time.monotonic)

    def wake(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:   # the viewer's event loop is gone; its slot is released by stream()'s cleanup
            pass


class _Device:
    """Everything known about one device's screen. `lock` guards the fields; `capturing`
    is held for the length of one capture so there is never a second one in flight."""

    def __init__(self, id: int):
        self.id = id
        self.lock = threading.Condition()
        self.capturing = threading.Lock()
        self.frame: _Frame | None = None
        self.seq = 0
        self.state = "idle"
        self.detail = ""
        self.failures = 0
        self.failed_at = 0.0
        self.mode = ""                                   # chosen capture path: "raw" | "png"
        self.timings: dict[str, list[float]] = {"raw": [], "png": []}
        self.capture_ms = 0.0
        self.viewers: set[_Viewer] = set()
        self.thread: threading.Thread | None = None
        self.idle_since: float | None = None

    def demand(self, now: float) -> tuple[float, set[int | None]] | None:
        """(highest wanted rate, widths) of the viewers that are still pulling frames."""
        active = [v for v in self.viewers if now - v.pulled < _STALE_VIEWER]
        if not active:
            return None
        return max(v.fps for v in active), {v.width for v in active}

    def retry_in(self, now: float) -> float:
        """Seconds until another attempt makes sense after the last failure."""
        if not self.failures:
            return 0.0
        return self.failed_at + min(_BACKOFF_MAX, _BACKOFF_FIRST * 2 ** (self.failures - 1)) - now


class ScreenHub:
    """Screenshots shared between all viewers of a device. Thread-safe; one per daemon."""

    def __init__(self, mgr: _Manager, config: Any, *, idle_grace: float = 5.0, capture_timeout: float = 8.0):
        """
        idle_grace       seconds a device's worker survives without viewers (it does not capture meanwhile).
        capture_timeout  hard limit for one screenshot.
        """
        self._mgr = mgr
        self._idle_grace = float(idle_grace)
        self._capture_timeout = float(capture_timeout)
        self._lock = threading.Lock()
        self._devices: dict[int, _Device] = {}
        self._viewer_count = 0
        self._closing = threading.Event()
        self._swept = 0.0
        self.reload_config(config)

    def reload_config(self, config: Any) -> None:
        stream = config.stream
        self._quality = int(stream.jpeg_quality)
        self._max_viewers = int(stream.max_viewers)
        self._live_fps = float(stream.live_fps)
        self._placeholder_size = (int(config.android.width), int(config.android.height))

    # ------------------------------------------------------------------ public
    def frame(self, id: int, *, width: int | None = None, max_age: float = 1.0) -> bytes:
        """One JPEG of the device's screen, at most `max_age` seconds old.

        Callers arriving together share one capture. Raises ScreenError (with `.state`)
        when the device cannot deliver a picture; a recent failure is answered from memory
        for a moment instead of sending every caller into the same ADB timeout.

        Blocking (it may run one ADB command): call it from a worker thread, i.e. a plain
        `def` endpoint or `asyncio.to_thread`, never directly on the event loop.
        """
        width = _clean_width(width)
        device = self._device(check_id(id))
        self._sweep()
        frame = self._fresh(device, max(0.0, float(max_age)), wait=True)
        return frame.jpeg(width, self._quality)

    async def stream(self, id: int, fps: float | None = None, width: int | None = None) -> AsyncIterator[bytes]:
        """JPEG frames of one device, at most `fps` per second, for as long as the caller iterates.

        While the device has no picture to give, a placeholder for its state (offline,
        booting, error) is yielded instead, and repeated every few seconds. Raises
        ViewerLimit when stream.max_viewers views are already open.
        """
        rate = min(30.0, max(0.1, float(fps or self._live_fps)))
        width = _clean_width(width)
        viewer = self._attach(check_id(id), rate, width, asyncio.get_running_loop())
        device = viewer.device
        try:
            seq, shown, sent, opened = -1, "", 0.0, time.monotonic()
            while not self._closing.is_set():
                viewer.event.clear()   # cleared before looking, so a frame published meanwhile is not missed
                viewer.pulled = now = time.monotonic()
                with device.lock:
                    frame, state = device.frame, device.state
                if state == "live" and frame is not None and frame.seq != seq:
                    early = sent + 1.0 / rate - now
                    if early > 0.001:
                        await asyncio.sleep(early)   # then look again: send the newest, not this one
                        continue
                    data = frame.cached(width, self._quality)
                    if data is None:   # a width the worker has not prepared yet (the first frame after joining)
                        data = await asyncio.to_thread(frame.jpeg, width, self._quality)
                    seq, shown, sent = frame.seq, "live", time.monotonic()
                    yield data
                    continue
                if state in ("idle", "live"):
                    # Nothing is wrong, the next frame is on its way. Only a view that has shown
                    # nothing at all for a second gets a "connecting" picture, once.
                    label = "connecting"
                    show = seq == -1 and not shown and now - opened >= 1.0
                    if device.thread is None:
                        self._ensure_worker(device)   # it left while this viewer was not pulling
                else:
                    label = state
                    show = label != shown or now - sent >= _KEEPALIVE
                if show:
                    shown, sent = label, now
                    yield self._placeholder(device, label, width)
                    continue
                try:
                    await asyncio.wait_for(viewer.event.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._detach(viewer)

    def frame_size(self, id: int, *, max_age: float = 5.0) -> tuple[int, int] | None:
        """Size of the last captured picture, if recent. Used to map clicks on a live view."""
        device = self._devices.get(check_id(id))
        frame = device.frame if device else None
        if frame is None or time.monotonic() - frame.taken > max_age:
            return None
        return frame.size

    def status(self, id: int) -> dict:
        """State of one device's screen for the panel (no capture is made)."""
        device = self._devices.get(check_id(id))
        if device is None:
            return {"id": int(id), "state": "idle", "detail": "", "viewers": 0, "capturing": False,
                    "mode": "", "capture_ms": 0, "probe_ms": {}, "age": None, "size": None}
        now = time.monotonic()
        with device.lock:
            frame = device.frame
            return {
                "id": device.id, "state": device.state, "detail": device.detail, "viewers": len(device.viewers),
                "capturing": device.thread is not None and device.demand(now) is not None,
                "mode": device.mode, "capture_ms": round(device.capture_ms, 1),
                "probe_ms": {m: round(min(t), 1) for m, t in device.timings.items() if t},
                "age": round(now - frame.taken, 2) if frame else None, "size": list(frame.size) if frame else None,
            }

    def stats(self) -> dict:
        with self._lock:
            ids, viewers = sorted(self._devices), self._viewer_count
        devices = [self.status(i) for i in ids]
        return {"viewers": viewers, "max_viewers": self._max_viewers,
                "capturing": sum(1 for d in devices if d["capturing"]), "devices": devices}

    def close(self) -> None:
        """Stop every worker. Bounded: waits at most one capture timeout per worker."""
        self._closing.set()
        with self._lock:
            devices = list(self._devices.values())
        threads = []
        for device in devices:
            with device.lock:
                device.lock.notify_all()
                if device.thread:
                    threads.append(device.thread)
        for thread in threads:
            thread.join(self._capture_timeout + 2)

    # ----------------------------------------------------------------- viewers
    def _device(self, id: int) -> _Device:
        with self._lock:
            device = self._devices.get(id)
            if device is None:
                device = self._devices[id] = _Device(id)
            return device

    def _attach(self, id: int, fps: float, width: int | None, loop: asyncio.AbstractEventLoop) -> _Viewer:
        if self._closing.is_set():
            raise ScreenError("error", "the screen service is shutting down")
        with self._lock:
            if self._viewer_count >= self._max_viewers:
                raise ViewerLimit(f"{self._max_viewers} live views are already open; close one or raise "
                                  "stream.max_viewers in the configuration")
            self._viewer_count += 1
            device = self._devices.get(id)
            if device is None:
                device = self._devices[id] = _Device(id)
        viewer = _Viewer(device, fps, width, loop, asyncio.Event())
        with device.lock:
            device.viewers.add(viewer)
        self._ensure_worker(device)
        return viewer

    def _ensure_worker(self, device: _Device) -> None:
        with device.lock:
            device.idle_since = None
            if device.thread is None and not self._closing.is_set():
                device.thread = threading.Thread(target=self._work, args=(device,), daemon=True,
                                                 name=f"sf-screen-{device.id}")
                device.thread.start()
            device.lock.notify_all()   # a parked worker starts capturing again at once

    def _detach(self, viewer: _Viewer) -> None:
        device = viewer.device
        with device.lock:
            if viewer not in device.viewers:
                return
            device.viewers.discard(viewer)
        with self._lock:
            self._viewer_count -= 1

    def _placeholder(self, device: _Device, label: str, width: int | None) -> bytes:
        frame = device.frame
        w, h = frame.size if frame else self._placeholder_size
        target = _fit_width(width, w) or w
        return placeholder(label, target, max(1, round(h * target / w)), self._quality)

    # ------------------------------------------------------------------ worker
    def _work(self, device: _Device) -> None:
        while not self._closing.is_set():
            now = time.monotonic()
            with device.lock:
                demand = device.demand(now)
                if demand is None:
                    # Nobody is looking: capture nothing. Stay parked for the grace period so a
                    # viewer that comes straight back (page reload) finds the worker warm.
                    if device.idle_since is None:
                        device.idle_since = now
                    left = device.idle_since + self._idle_grace - now
                    if left <= 0:
                        device.thread = None
                        device.idle_since = None
                        device.frame = None   # about 1 MB per device; a later request captures anew
                        if device.state == "live":
                            device.state = "idle"
                        return
                    device.lock.wait(min(left, 1.0))
                    continue
                device.idle_since = None
                pause = device.retry_in(now)
            if pause > 0:
                self._closing.wait(min(pause, 1.0))
                continue
            fps, widths = demand
            started = time.monotonic()
            try:
                frame = self._fresh(device, 0.0, wait=False)
            except ScreenError:
                continue   # state and back-off are already recorded; viewers were woken
            if frame is None:
                self._closing.wait(0.05)   # a frame() caller is capturing right now; its result is ours too
                continue
            for width in widths:
                try:
                    frame.jpeg(width, self._quality)   # encode here, not on the event loop of the viewers
                except Exception:   # noqa: S112 - an encode problem must not end the worker; the viewer retries it
                    continue
            with device.lock:
                viewers = list(device.viewers)
            for viewer in viewers:
                viewer.wake()
            took = time.monotonic() - started
            self._closing.wait(max(1.0 / fps - took, took * _BREATHER))
        with device.lock:
            device.thread = None

    # ----------------------------------------------------------------- capture
    def _fresh(self, device: _Device, max_age: float, *, wait: bool) -> _Frame | None:
        """Return a frame no older than max_age, capturing one if needed.

        With wait=False (the worker) None is returned when someone else is capturing."""
        now = time.monotonic()
        with device.lock:
            frame = device.frame
            if frame is not None and max_age > 0 and now - frame.taken <= max_age:
                return frame
            if wait and device.retry_in(now) > 0:
                raise ScreenError(device.state, device.detail)
        got = device.capturing.acquire(timeout=self._capture_timeout + 5) if wait else device.capturing.acquire(False)
        if not got:
            if wait:
                raise ScreenError("error", "a screenshot of this device is still running; it is not answering")
            return None
        try:
            now = time.monotonic()
            with device.lock:   # whoever held the lock may just have produced what we need
                frame = device.frame
                if frame is not None and max_age > 0 and now - frame.taken <= max_age:
                    return frame
                if wait and device.retry_in(now) > 0:
                    raise ScreenError(device.state, device.detail)
            return self._capture(device)
        finally:
            device.capturing.release()

    def _capture(self, device: _Device) -> _Frame:
        started = time.monotonic()
        adb: Adb | None = None
        try:
            adb = self._mgr.adb(device.id)
            mode = device.mode or self._probe_mode(device)
            try:
                image = self._grab(adb, mode)
            except ValueError:
                if mode != "raw":
                    raise
                # Raw pixels in a layout this code does not read (not a transport failure, which
                # is an AdbError): PNG works everywhere, so this device stays on it.
                image = self._grab(adb, "png")
                mode = device.mode = "png"
        except Exception as e:   # noqa: BLE001 - every failure becomes a state the panel can show
            state, detail = self._diagnose(adb, e)
            with device.lock:
                device.failures += 1
                device.failed_at = time.monotonic()
                device.state, device.detail = state, detail
                if device.failures >= 3:
                    device.mode = ""                  # the device may have been recreated: time the paths again
                    device.timings = {"raw": [], "png": []}
                viewers = list(device.viewers)
                device.lock.notify_all()
            for viewer in viewers:
                viewer.wake()
            raise ScreenError(state, detail) from None
        took_ms = (time.monotonic() - started) * 1000
        with device.lock:
            if not device.mode:
                device.timings[mode].append(took_ms)
                self._choose_mode(device)
            device.capture_ms = took_ms if not device.capture_ms else device.capture_ms * 0.7 + took_ms * 0.3
            device.seq += 1
            frame = device.frame = _Frame(device.seq, image)
            device.state, device.detail, device.failures = "live", "", 0
            device.lock.notify_all()
        return frame

    def _grab(self, adb: Adb, mode: str) -> Image.Image:
        if mode == "raw":
            return decode_raw(adb.exec_out(["screencap"], timeout=self._capture_timeout))
        return decode_png(adb.screencap_png(timeout=self._capture_timeout))

    @staticmethod
    def _probe_mode(device: _Device) -> str:
        """While undecided, alternate so both paths are timed on real frames (no frame is wasted)."""
        raw, png = device.timings["raw"], device.timings["png"]
        return "raw" if len(raw) <= len(png) else "png"

    @staticmethod
    def _choose_mode(device: _Device) -> None:
        raw, png = device.timings["raw"], device.timings["png"]
        if len(raw) >= _PROBE_SAMPLES and len(png) >= _PROBE_SAMPLES:
            # Minimum, because the first sample also pays for connecting. PNG has to win clearly:
            # its cost depends on the picture (measured 60 ms on a flat settings page, 210 ms on a
            # photo wallpaper, same device), raw costs the same whatever is shown. PNG only pays
            # off where moving the bytes is the slow part: big screens over a slow ADB link.
            device.mode = "png" if min(png) < _PNG_MUST_WIN * min(raw) else "raw"

    @staticmethod
    def _diagnose(adb: Adb | None, error: Exception) -> tuple[str, str]:
        """Turn a failed capture into offline | booting | error with a one-line reason."""
        reason = (str(error) or type(error).__name__)[:200]
        if adb is None:
            return "offline", reason   # the manager does not know this device (or cannot reach its config)
        try:
            state = adb.state()
            if state != "device":
                # The ADB server forgets TCP devices when it restarts or the container is
                # recreated; one reconnect attempt per failure brings them back without a person.
                if not adb.connect(attempts=1):
                    return "offline", f"not reachable over ADB ({state or 'no answer'})"
                if not adb.booted():
                    return "booting", "Android is still starting"
                return "error", reason
            if not adb.booted():
                return "booting", "Android is still starting"
        except Exception as e:   # noqa: BLE001 - diagnosis must never raise
            return "offline", (str(e) or type(e).__name__)[:200]
        return "error", reason

    def _sweep(self) -> None:
        """Forget pictures of devices nobody has asked about for a while (called from frame())."""
        now = time.monotonic()
        if now - self._swept < 10.0:
            return
        self._swept = now
        with self._lock:
            devices = list(self._devices.values())
        for device in devices:
            with device.lock:
                if device.thread is None and device.frame and now - device.frame.taken > _FRAME_TTL:
                    device.frame = None
                    if device.state == "live":
                        device.state = "idle"

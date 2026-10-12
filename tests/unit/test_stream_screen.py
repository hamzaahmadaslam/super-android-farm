import asyncio
import io
import struct
import threading
import time
from contextlib import aclosing

import pytest
from PIL import Image
from stream_fakes import FakeAdb, FakeManager, offline, online

from superfarm.config import Config, update_config
from superfarm.stream import screen
from superfarm.stream.screen import ScreenError, ScreenHub, ViewerLimit


@pytest.fixture
def fast(monkeypatch):
    """Short back-off and keep-alive so failure paths run in milliseconds."""
    monkeypatch.setattr(screen, "_BACKOFF_FIRST", 0.05)
    monkeypatch.setattr(screen, "_BACKOFF_MAX", 0.1)
    monkeypatch.setattr(screen, "_KEEPALIVE", 0.2)


@pytest.fixture
def hubs():
    made = []

    def make(mgr, config=None, **kw):
        hub = ScreenHub(mgr, config or Config(), **{"idle_grace": 0.15, "capture_timeout": 2.0, **kw})
        made.append(hub)
        return hub
    yield make
    for hub in made:
        hub.close()


def picture(data: bytes) -> Image.Image:
    assert data[:3] == b"\xff\xd8\xff", "not a JPEG"
    return Image.open(io.BytesIO(data))


async def take(hub, n, id=1, fps=30.0, width=None):
    out = []
    async with aclosing(hub.stream(id, fps, width)) as stream:
        async for frame in stream:
            out.append(frame)
            if len(out) >= n:
                break
    return out


# ------------------------------------------------------------------ decoding

@pytest.mark.parametrize("header", [12, 16])
def test_decode_raw_reads_both_header_sizes(header):
    pixels = bytes([10, 20, 30, 255]) * 6
    data = struct.pack("<III", 3, 2, 1) + b"\x00" * (header - 12) + pixels
    image = screen.decode_raw(data)
    assert image.size == (3, 2) and image.getpixel((2, 1))[:3] == (10, 20, 30)
    out = io.BytesIO()
    image.resize((2, 1)).save(out, "JPEG")           # what the hub does with it
    assert out.getvalue()[:3] == b"\xff\xd8\xff"


def test_decode_raw_rgb888():
    data = struct.pack("<IIII", 2, 2, 3, 0) + bytes([1, 2, 3]) * 4
    assert screen.decode_raw(data).getpixel((0, 0)) == (1, 2, 3)


@pytest.mark.parametrize("data", [
    b"", b"short", struct.pack("<IIII", 3, 2, 1, 0) + b"\x00" * 23,          # one byte missing
    struct.pack("<IIII", 3, 2, 4, 0) + b"\x00" * 12,                         # RGB565: not read here
    struct.pack("<IIII", 0, 2, 1, 0) + b"\x00" * 16, struct.pack("<IIII", 99999, 2, 1, 0) + b"\x00" * 16,
])
def test_decode_raw_rejects_what_it_cannot_read(data):
    with pytest.raises(ValueError):
        screen.decode_raw(data)


def test_placeholder_is_a_small_jpeg_of_the_asked_size():
    image = picture(screen.placeholder("offline", 120, 213))
    assert image.size == (120, 213)
    assert screen.placeholder("offline", 120, 213) is screen.placeholder("offline", 120, 213)


# ------------------------------------------------------------------ frame()

def test_frame_is_a_jpeg_of_the_screen(hubs):
    hub = hubs(FakeManager({1: FakeAdb(48, 80)}))
    image = picture(hub.frame(1))
    assert image.size == (48, 80)
    r, g, b = image.convert("RGB").getpixel((10, 10))
    assert abs(r - 200) < 12 and abs(g - 100) < 12 and abs(b - 50) < 12
    assert hub.status(1)["state"] == "live" and hub.status(1)["size"] == [48, 80]
    assert hub.frame_size(1) == (48, 80)


def test_frame_resizes_down_but_never_up(hubs):
    hub = hubs(FakeManager({1: FakeAdb(480, 854)}))
    assert picture(hub.frame(1, width=240)).size == (240, 427)
    assert picture(hub.frame(1, width=5000)).size == (480, 854)
    assert picture(hub.frame(1, width=1)).size[0] == 32


def test_frame_is_reused_within_max_age(hubs):
    mgr = FakeManager()
    hub = hubs(mgr)
    first = hub.frame(1, max_age=5)
    assert hub.frame(1, max_age=5) is first          # same capture, same encode
    assert hub.frame(1, max_age=5, width=32) != first
    assert mgr.devices[1].captures == 1
    hub.frame(1, max_age=0)
    assert mgr.devices[1].captures == 2


def test_callers_arriving_together_share_one_capture(hubs):
    adb = FakeAdb(raw_delay=0.15, png_delay=0.15)
    hub = hubs(FakeManager({1: adb}))
    results = []
    threads = [threading.Thread(target=lambda: results.append(hub.frame(1, max_age=2))) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(results) == 10 and len(set(results)) == 1
    assert adb.captures == 1 and adb.max_in_flight == 1


@pytest.mark.parametrize("raw_delay, png_delay, expected", [(0.0, 0.04, "raw"), (0.04, 0.0, "png"),
                                                            (0.05, 0.045, "raw")])     # a near tie goes to raw
def test_faster_capture_path_is_measured_and_kept(hubs, raw_delay, png_delay, expected):
    adb = FakeAdb(raw_delay=raw_delay, png_delay=png_delay)
    hub = hubs(FakeManager({1: adb}))
    for _ in range(4):
        hub.frame(1, max_age=0)
    assert (adb.raw_calls, adb.png_calls) == (2, 2), "both paths are timed on real frames first"
    assert hub.status(1)["mode"] == expected
    for _ in range(3):
        hub.frame(1, max_age=0)
    assert (adb.raw_calls, adb.png_calls) == ((5, 2) if expected == "raw" else (2, 5))
    assert set(hub.status(1)["probe_ms"]) == {"raw", "png"}


def test_unreadable_raw_output_falls_back_to_png_for_good(hubs):
    adb = FakeAdb()
    adb.raw_garbage = True
    hub = hubs(FakeManager({1: adb}))
    for _ in range(3):
        assert picture(hub.frame(1, max_age=0)).size == (48, 80)
    assert hub.status(1)["mode"] == "png" and adb.raw_calls == 1 and adb.png_calls == 3


@pytest.mark.parametrize("adb_state, booted, expected", [("offline", True, "offline"), ("device", False, "booting"),
                                                         ("device", True, "error")])
def test_failures_become_a_state(hubs, fast, adb_state, booted, expected):
    adb = FakeAdb()
    offline(adb)
    adb.adb_state, adb.is_booted = adb_state, booted
    hub = hubs(FakeManager({1: adb}))
    with pytest.raises(ScreenError) as e:
        hub.frame(1)
    assert e.value.state == expected and e.value.detail
    assert hub.status(1)["state"] == expected


def test_unknown_instance_is_offline_with_the_reason(hubs, fast):
    hub = hubs(FakeManager({}))
    with pytest.raises(ScreenError) as e:
        hub.frame(7)
    assert e.value.state == "offline" and "7" in e.value.detail


def test_recent_failure_is_answered_from_memory_then_retried(hubs, monkeypatch):
    monkeypatch.setattr(screen, "_BACKOFF_FIRST", 0.3)
    adb = FakeAdb()
    offline(adb)
    calls = []
    adb.state = lambda: calls.append(1) or "offline"
    hub = hubs(FakeManager({1: adb}))
    for _ in range(5):
        with pytest.raises(ScreenError):
            hub.frame(1)
    assert len(calls) == 1, "four of five callers must not go to ADB at all"
    online(adb)
    time.sleep(0.35)
    assert picture(hub.frame(1)).size == (48, 80)
    assert hub.status(1)["state"] == "live"


def test_bad_ids_and_widths_are_refused(hubs):
    mgr = FakeManager()
    hub = hubs(mgr)
    for bad in ("x", -1, "1;ls", 10**6):
        with pytest.raises(ValueError):
            hub.frame(bad)
    for bad in ("wide", -5, 10**6, [240], True):
        with pytest.raises(ValueError):
            hub.frame(1, width=bad)
    assert mgr.devices[1].captures == 0
    assert picture(hub.frame(1, width="24")).size[0] == 32       # a query-string number is fine


async def test_bad_stream_width_is_refused_before_a_worker_starts(hubs):
    hub = hubs(FakeManager())
    with pytest.raises(ValueError):
        await hub.stream(1, 5, "wide").__anext__()
    assert hub.stats() == {"viewers": 0, "max_viewers": 64, "capturing": 0, "devices": []}


# ------------------------------------------------------------------ stream()

async def test_stream_yields_frames_and_stops_capturing_when_nobody_watches(hubs):
    adb = FakeAdb()
    hub = hubs(FakeManager({1: adb}))
    frames = await take(hub, 5)
    assert all(picture(f).size == (48, 80) for f in frames)
    assert hub.stats()["viewers"] == 0
    await asyncio.sleep(0.05)
    seen = adb.captures
    await asyncio.sleep(0.4)                       # longer than the grace period
    assert adb.captures == seen, "no viewer, no ADB traffic"
    status = hub.status(1)
    assert not status["capturing"] and status["viewers"] == 0 and status["state"] == "idle"
    assert not [t for t in threading.enumerate() if t.name == "sf-screen-1"], "the worker left after the grace period"
    assert len(await take(hub, 2)) == 2            # and a new viewer starts it again


async def test_viewers_of_one_device_share_every_capture(hubs):
    adb = FakeAdb(raw_delay=0.02, png_delay=0.02)
    hub = hubs(FakeManager({1: adb}))
    results = await asyncio.gather(*(take(hub, 6, width=w) for w in (None, None, 24, 24, None)))
    assert [len(r) for r in results] == [6] * 5
    assert picture(results[2][0]).size[0] == 32 and picture(results[0][0]).size == (48, 80)
    assert adb.captures <= 6 + 4, f"{adb.captures} captures for 5 viewers of 6 frames: they are not shared"
    assert adb.max_in_flight == 1, "captures of one device must never overlap"


async def test_stream_is_paced_per_viewer(hubs):
    adb = FakeAdb()
    hub = hubs(FakeManager({1: adb}))
    started = time.monotonic()
    slow, quick = await asyncio.gather(take(hub, 3, fps=5), take(hub, 12, fps=40))
    assert time.monotonic() - started >= 0.38        # 3 frames at 5 fps need two intervals
    assert len(slow) == 3 and len(quick) == 12


async def test_slow_device_delivers_fewer_frames_instead_of_queueing(hubs):
    adb = FakeAdb(raw_delay=0.1, png_delay=0.1)
    hub = hubs(FakeManager({1: adb}))
    started = time.monotonic()
    await take(hub, 4, fps=30)
    assert time.monotonic() - started >= 0.38
    assert adb.max_in_flight == 1


async def test_viewer_limit(hubs):
    hub = hubs(FakeManager({1: FakeAdb(), 2: FakeAdb()}), update_config(Config(), {"stream": {"max_viewers": 2}}))
    a, b = hub.stream(1, 30), hub.stream(2, 30)
    await a.__anext__()
    await b.__anext__()
    with pytest.raises(ViewerLimit, match="max_viewers"):
        await hub.stream(1, 30).__anext__()
    await a.aclose()
    assert len(await take(hub, 1)) == 1              # the slot is free again
    await b.aclose()
    assert hub.stats()["viewers"] == 0


async def test_offline_device_streams_a_placeholder_then_recovers(hubs, fast):
    adb = FakeAdb()
    offline(adb)
    hub = hubs(FakeManager({1: adb}))
    stream = hub.stream(1, 30, 24)
    first = await asyncio.wait_for(stream.__anext__(), 3)
    assert first == screen.placeholder("offline", 32, 57, 60)
    assert hub.status(1)["state"] == "offline"
    again = await asyncio.wait_for(stream.__anext__(), 3)      # repeated, so the connection stays alive
    assert again == first
    online(adb)
    for _ in range(20):
        frame = await asyncio.wait_for(stream.__anext__(), 3)
        if frame != first:
            break
    assert frame != first and picture(frame).size == (32, 53)
    assert hub.status(1)["state"] == "live"
    await stream.aclose()


async def test_booting_device_says_so(hubs, fast):
    adb = FakeAdb()
    offline(adb)
    adb.adb_state, adb.is_booted = "device", False
    hub = hubs(FakeManager({1: adb}))
    stream = hub.stream(1, 30)
    assert await asyncio.wait_for(stream.__anext__(), 3) == screen.placeholder("booting", 480, 854, 60)
    await stream.aclose()


async def test_failures_back_off(hubs, monkeypatch):
    monkeypatch.setattr(screen, "_BACKOFF_FIRST", 0.1)
    monkeypatch.setattr(screen, "_BACKOFF_MAX", 0.2)
    adb = FakeAdb()
    offline(adb)
    attempts = []
    adb.state = lambda: attempts.append(time.monotonic()) or "offline"
    hub = hubs(FakeManager({1: adb}))
    stream = hub.stream(1, 30)
    await asyncio.wait_for(stream.__anext__(), 3)
    await asyncio.sleep(0.75)
    await stream.aclose()
    # 0.1 + 0.2 + 0.2 + 0.2 ... : at 30 fps without back-off there would be about 20 attempts.
    assert 3 <= len(attempts) <= 6, attempts


async def test_frame_and_stream_share_frames(hubs):
    adb = FakeAdb()
    hub = hubs(FakeManager({1: adb}))
    stream = hub.stream(1, 20)
    await stream.__anext__()
    before = adb.captures
    data = await asyncio.to_thread(hub.frame, 1, max_age=1.0)
    assert picture(data).size == (48, 80)
    assert adb.captures - before <= 1                # served from the stream's frames, not a capture of its own
    await stream.aclose()


async def test_close_stops_workers(hubs):
    hub = hubs(FakeManager(), idle_grace=30)
    stream = hub.stream(1, 30)
    await stream.__anext__()
    await asyncio.to_thread(hub.close)
    assert not [t for t in threading.enumerate() if t.name == "sf-screen-1"]
    with pytest.raises(StopAsyncIteration):                     # open views end instead of hanging
        await asyncio.wait_for(stream.__anext__(), 3)
    with pytest.raises(ScreenError):
        await hub.stream(1, 30).__anext__()

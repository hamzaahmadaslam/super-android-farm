import asyncio
import struct

import pytest
from stream_fakes import FakeAdb, FakeManager

from superfarm.config import Config, update_config
from superfarm.stream import h264
from superfarm.stream.h264 import FLAG_CONFIG, FLAG_KEY, HEADER, H264Error, H264Session

# Config packets (SPS + PPS) exactly as an Android 13 device sent them for three video sizes.
CONFIG_408x720 = bytes.fromhex("000000016742c0298d681a05be5e42000003000f08846a0000000168ce01a835c8")
CONFIG_480x848 = bytes.fromhex("000000016742c0298d681e06ba42000003000f08846a0000000168ce01a835c8")
CONFIG_176x320 = bytes.fromhex("000000016742c0298d682c29a42000000300f08846a00000000168ce01a835c8")
SPS_1080P_HIGH = bytes.fromhex("0000000167640028acd940780227e5c044000003000400000300f03c60c658")
IDR = b"\x00\x00\x00\x01\x65\x88\x84\x00\x10"
DELTA = b"\x00\x00\x00\x01\x41\x9a\x00\x10"


# ------------------------------------------------------------------ control messages
# Expected bytes follow scrcpy's own serialisation tests for the same values.

def test_key_message():
    assert h264.key_msg(h264.ACTION_UP, 66, repeat=5, meta=0x41).hex() == "0001000000420000000500000041"
    with pytest.raises(ValueError):
        h264.key_msg(2, 66)


def test_touch_message():
    msg = h264.touch_msg(h264.ACTION_DOWN, 100, 200, 1080, 1920, pointer=0x1234567887654321)
    assert msg.hex() == "0200" "1234567887654321" "00000064" "000000c8" "0438" "0780" "ffff" "00000000" "00000000"
    assert len(msg) == 32
    up = h264.touch_msg(h264.ACTION_UP, 1, 2, 3, 4)
    assert up[1] == 1 and up[22:24] == b"\x00\x00", "a lifted finger has no pressure"
    assert h264.touch_msg(h264.ACTION_MOVE, 0, 0, 1, 1, pointer=-2)[2:10] == b"\xff" * 7 + b"\xfe"
    with pytest.raises(ValueError):
        h264.touch_msg(9, 0, 0, 1, 1)


def test_text_messages_respect_the_size_limit_and_character_boundaries():
    assert h264.text_msgs("hello, world!") == [b"\x01\x00\x00\x00\x0dhello, world!"]
    assert h264.text_msgs("") == []
    parts = h264.text_msgs("é" * 400)
    assert len(parts) == 3 and all(len(p) - 5 <= 300 for p in parts)
    assert b"".join(p[5:] for p in parts).decode() == "é" * 400
    assert [struct.unpack(">I", p[1:5])[0] for p in parts] == [len(p) - 5 for p in parts]


def test_scroll_message():
    msg = h264.scroll_msg(260, 1026, 1080, 1920, 16, -16)
    assert msg.hex() == "03" "00000104" "00000402" "0438" "0780" "7fff" "8000" "00000000"
    assert h264.scroll_msg(0, 0, 1, 1, 100, -100)[13:17] == bytes.fromhex("7fff8000")       # clamped
    assert h264.scroll_msg(0, 0, 1, 1, 8, 0)[13:17] == bytes.fromhex("40000000")


def test_clipboard_and_small_messages():
    msg = h264.clipboard_msg(0x0102030405060708, "hello, world!", paste=True)
    assert msg.hex() == "09" "0102030405060708" "01" "0000000d" + b"hello, world!".hex()
    assert h264.clipboard_msg(1, "é", paste=False)[9] == 0
    with pytest.raises(ValueError):
        h264.clipboard_msg(1, "x" * (1 << 18), paste=True)
    assert h264.back_or_screen_on_msg(h264.ACTION_DOWN) == b"\x04\x00"
    assert h264.reset_video_msg() == b"\x11"


def test_device_messages_are_split_and_partial_ones_kept():
    ack = b"\x01" + struct.pack(">Q", 7)
    clip = b"\x00" + struct.pack(">I", 5) + b"hello"
    uhid = b"\x02" + struct.pack(">HH", 3, 2) + b"\xaa\xbb"
    messages, rest = h264.parse_device_messages(clip + ack + uhid + ack[:4])
    assert messages == [(0, 5), (1, 7), (2, 2)] and rest == ack[:4]
    assert h264.parse_device_messages(b"") == ([], b"")
    assert h264.parse_device_messages(clip[:7]) == ([], clip[:7])
    with pytest.raises(H264Error, match="out of step"):
        h264.parse_device_messages(b"\x09abc")


# ------------------------------------------------------------------ server options / capability

def test_server_args_for_video_and_for_control_only():
    args = h264.server_args((3, 3, 4), 0x1A2B3C, video=True, control=True, max_size=720, max_fps=20, bit_rate=2_000_000)
    assert args[0] == "3.3.4" and args[1] == "scid=001a2b3c"
    for expected in ("video=true", "audio=false", "control=true", "tunnel_forward=true", "send_device_meta=false",
                     "send_frame_meta=true", "send_codec_meta=true", "send_dummy_byte=true", "cleanup=false",
                     "clipboard_autosync=false", "max_size=720", "max_fps=20", "video_bit_rate=2000000"):
        assert expected in args
    assert all(" " not in a and a == a.strip() for a in args)
    control = h264.server_args((3, 3, 4), 1, video=False, control=True, max_size=720, max_fps=20, bit_rate=1)
    assert "video=false" in control and not [a for a in control if a.startswith(("max_", "video_bit"))]


def test_parse_bitrate():
    assert h264.parse_bitrate("2M") == 2_000_000 and h264.parse_bitrate("800k") == 800_000
    assert h264.parse_bitrate("1.5M") == 1_500_000 and h264.parse_bitrate(4_000_000) == 4_000_000
    assert h264.parse_bitrate("1") == 100_000 and h264.parse_bitrate("900M") == 50_000_000      # clamped
    with pytest.raises(ValueError, match="2M"):
        h264.parse_bitrate("fast")


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A host whose scrcpy installation the test controls."""
    server = tmp_path / "scrcpy-server"
    server.write_bytes(b"jar")

    def setup(version: str | None = "3.3.4", has_server: bool = True) -> dict:
        monkeypatch.setattr(h264, "SERVER_PATHS", ())
        monkeypatch.setattr(h264, "which", lambda name: None)
        env = {}
        if has_server:
            env["SCRCPY_SERVER_PATH"] = str(server)
        if version:
            env["SCRCPY_SERVER_VERSION"] = version
        monkeypatch.setattr(h264.os, "environ", env)
        return env
    return setup


def test_capability_verified_version(host):
    cap = h264.capability(host("3.3.4"))
    assert cap["available"] and cap["verified"] and cap["experimental"] and cap["version"] == "3.3.4"
    assert cap["server"].endswith("scrcpy-server") and cap["reason"] == ""


@pytest.mark.parametrize("version, available, verified, word", [
    ("3.1", True, False, "untested"), ("2.4", True, False, "untested"), ("2.0", False, False, "not supported"),
    ("1.25", False, False, "not supported"), ("4.0", False, False, "not supported"), (None, False, False, "unknown"),
])
def test_capability_other_versions(host, version, available, verified, word):
    cap = h264.capability(host(version))
    assert (cap["available"], cap["verified"]) == (available, verified)
    assert word in cap["reason"] and cap["fix"]


def test_capability_without_server_says_how_to_get_one(host):
    cap = h264.capability(host(has_server=False))
    assert not cap["available"] and "not found" in cap["reason"] and "SCRCPY_SERVER_PATH" in cap["fix"]


def test_find_server_order(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    b.write_bytes(b"x")
    monkeypatch.setattr(h264, "which", lambda name: None)
    monkeypatch.setattr(h264, "SERVER_PATHS", (str(a), str(b)))
    assert h264.find_server({}) == b
    a.write_bytes(b"x")
    assert h264.find_server({}) == a
    assert h264.find_server({"SCRCPY_SERVER_PATH": str(b)}) == b
    assert h264.find_server({"SCRCPY_SERVER_PATH": str(tmp_path / "missing")}) == a


def test_installed_version_asks_the_client(monkeypatch):
    monkeypatch.setattr(h264, "which", lambda name: "/usr/bin/scrcpy")
    monkeypatch.setattr(h264, "run", lambda argv, **kw: type("R", (), {"stdout": "scrcpy 3.3.4 <url>\n"})())
    assert h264.installed_version({}) == (3, 3, 4)
    assert h264.installed_version({"SCRCPY_SERVER_VERSION": "2.7"}) == (2, 7)


def test_streams_follow_the_configured_mode(host):
    host("3.3.4")
    def streams(mode):
        return h264.H264Streams(FakeManager(), update_config(Config(), {"stream": {"h264": mode}}))
    assert streams("auto").capability()["enabled"] and streams("on").capability()["enabled"]
    off = streams("off").capability()
    assert not off["enabled"] and "switched off" in off["reason"]
    host("3.1")
    assert not streams("auto").capability()["enabled"] and streams("on").capability()["enabled"]


async def test_streams_refuse_clearly_when_unavailable(host):
    host(has_server=False)
    streams = h264.H264Streams(FakeManager(), Config())
    with pytest.raises(H264Error, match="not available.*scrcpy-server was not found.*To fix"):
        await streams.acquire(1)
    await streams.release(1)
    assert streams.active() == []


def test_server_is_refused_before_touching_the_device_when_scrcpy_is_missing(host):
    host(has_server=False)
    adb = FakeAdb()
    with pytest.raises(H264Error, match="To fix: install the scrcpy package"):
        h264.ScrcpyServer(adb, video=True, control=True)
    assert adb.shell_calls == []


# ------------------------------------------------------------------ H.264 parsing

def test_nal_units_with_both_start_code_lengths():
    data = b"\x00\x00\x00\x01\x67\xaa\x00\x00\x01\x68\xbb\x00\x00\x00\x01\x65\xcc\xdd"
    assert list(h264.nal_units(data)) == [b"\x67\xaa", b"\x68\xbb", b"\x65\xcc\xdd"]
    assert list(h264.nal_units(b"")) == [] and list(h264.nal_units(b"\x01\x02")) == []


@pytest.mark.parametrize("config, expected", [
    (CONFIG_408x720, {"codec": "avc1.42C029", "width": 408, "height": 720}),
    (CONFIG_480x848, {"codec": "avc1.42C029", "width": 480, "height": 848}),
    (CONFIG_176x320, {"codec": "avc1.42C029", "width": 176, "height": 320}),
    (SPS_1080P_HIGH, {"codec": "avc1.640028", "width": 1920, "height": 1080}),      # high profile, cropped
])
def test_describe_config_reads_size_and_codec_from_the_sps(config, expected):
    assert h264.describe_config(config) == expected


def test_describe_config_survives_garbage():
    assert h264.describe_config(b"") is None
    assert h264.describe_config(b"\x00\x00\x01\x68\xce") is None            # PPS only
    assert h264.describe_config(b"\x00\x00\x01\x67\x42") is None            # truncated SPS
    with pytest.raises(ValueError):
        h264.parse_sps(b"\x65\x00")


# ------------------------------------------------------------------ session, fed by a scripted device

def packet(payload: bytes, *, pts: int = 0, config: bool = False, key: bool = False) -> bytes:
    flags = (1 << 63 if config else 0) | (1 << 62 if key else 0)
    return struct.pack(">QI", flags | pts, len(payload)) + payload


class Sink:
    """Stands in for the control socket's writer."""

    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        pass


def scripted(version=(3, 3, 4)) -> tuple[H264Session, asyncio.StreamReader, Sink]:
    session = H264Session(FakeAdb())
    reader, sink = asyncio.StreamReader(), Sink()
    session._video = (reader, sink)
    session._ctl = (asyncio.StreamReader(), sink)
    session.version = version
    session.size = (408, 720)
    session._tasks = [asyncio.create_task(session._pump())]
    return session, reader, sink


async def collect(sub, n):
    out = []
    async for message in sub:
        out.append(message)
        if len(out) >= n:
            break
    return out


async def test_pump_prefixes_key_frames_with_the_configuration():
    session, reader, _ = scripted()
    sub = session.subscribe()
    reader.feed_data(packet(CONFIG_176x320, config=True) + packet(IDR, pts=1000, key=True) + packet(DELTA, pts=2000))
    key, delta = await asyncio.wait_for(collect(sub, 2), 2)
    assert HEADER.unpack_from(key) == (FLAG_KEY | FLAG_CONFIG, 1000, 176, 320)
    assert key[HEADER.size:] == CONFIG_176x320 + IDR
    assert [u[0] & 0x1F for u in h264.nal_units(key[HEADER.size:])] == [7, 8, 5]
    assert HEADER.unpack_from(delta) == (0, 2000, 176, 320) and delta[HEADER.size:] == DELTA
    assert session.info["codec"] == "avc1.42C029" and session.size == (176, 320)
    await session.stop()


async def test_rotation_updates_the_size_touches_are_made_for():
    session, reader, sink = scripted()
    sub = session.subscribe()
    reader.feed_data(packet(CONFIG_176x320, config=True) + packet(IDR, key=True))
    await asyncio.wait_for(collect(sub, 1), 2)
    await session.control.touch("down", 0.5, 0.5, pointer=1)
    assert sink.data[-32:] == h264.touch_msg(h264.ACTION_DOWN, 88, 160, 176, 320, pointer=1)
    reader.feed_data(packet(CONFIG_408x720, config=True) + packet(IDR, key=True))
    await asyncio.wait_for(sub.__anext__(), 2)
    await session.control.touch("up", 1.0, 1.0)
    assert sink.data[-32:] == h264.touch_msg(h264.ACTION_UP, 407, 719, 408, 720)
    with pytest.raises(ValueError):
        await session.control.touch("down", 1.2, 0.5)
    with pytest.raises(ValueError):
        await session.control.touch("wiggle", 0.2, 0.5)
    await session.stop()


async def test_late_joiner_starts_at_a_key_frame():
    session, reader, sink = scripted()
    first = session.subscribe()
    reader.feed_data(packet(CONFIG_176x320, config=True) + packet(IDR, pts=1, key=True) + packet(DELTA, pts=2))
    await asyncio.wait_for(collect(first, 2), 2)
    late = session.subscribe()                     # the recent key frame and what followed are replayed
    replay = await asyncio.wait_for(collect(late, 2), 2)
    assert [HEADER.unpack_from(m)[:2] for m in replay] == [(FLAG_KEY | FLAG_CONFIG, 1), (0, 2)]
    assert b"\x11" not in sink.data, "no encoder restart needed"
    for i in range(h264._GOP_REPLAY + 5):          # now the key frame is too far back to replay
        reader.feed_data(packet(DELTA, pts=10 + i))
    await asyncio.sleep(0.05)
    later = session.subscribe()
    await asyncio.sleep(0.05)
    assert sink.data.endswith(b"\x11"), "a key frame was requested for the newcomer"
    reader.feed_data(packet(DELTA, pts=500) + packet(IDR, pts=501, key=True))
    got = await asyncio.wait_for(later.__anext__(), 2)
    assert HEADER.unpack_from(got)[:2] == (FLAG_KEY | FLAG_CONFIG, 501), "frames before the key frame are withheld"
    await session.stop()


async def test_old_scrcpy_is_never_sent_the_reset_message():
    session, reader, sink = scripted(version=(2, 7))
    sub = session.subscribe()
    await asyncio.sleep(0.05)
    assert sink.data == b"" and await session.control.reset_video() is False
    reader.feed_data(packet(CONFIG_176x320, config=True) + packet(DELTA, pts=1) + packet(IDR, pts=2, key=True))
    assert HEADER.unpack_from(await asyncio.wait_for(sub.__anext__(), 2))[1] == 2
    await session.stop()


async def test_slow_subscriber_is_resynchronised_not_fed_broken_video():
    session, reader, sink = scripted()
    sub = session.subscribe()
    reader.feed_data(packet(CONFIG_176x320, config=True) + packet(IDR, pts=0, key=True))
    for i in range(h264._QUEUE + 20):
        reader.feed_data(packet(DELTA, pts=1 + i))
    await asyncio.sleep(0.1)
    assert sub._queue.qsize() < h264._QUEUE and sub._need_key
    assert sink.data.endswith(b"\x11")
    reader.feed_data(packet(DELTA, pts=900) + packet(IDR, pts=901, key=True))
    await asyncio.sleep(0.05)
    messages = [sub._queue.get_nowait() for _ in range(sub._queue.qsize())]
    assert HEADER.unpack_from(messages[-1])[:2] == (FLAG_KEY | FLAG_CONFIG, 901)
    assert all(HEADER.unpack_from(m)[1] != 900 for m in messages)
    await session.stop()


async def test_end_of_stream_ends_subscribers_and_refuses_new_work():
    session, reader, _ = scripted()
    sub = session.subscribe()
    reader.feed_data(packet(CONFIG_176x320, config=True) + packet(IDR, key=True))
    reader.feed_eof()
    assert len(await asyncio.wait_for(collect(sub, 5), 2)) == 1       # the iterator simply ends
    assert session.closed and "closed the video stream" in session.error
    with pytest.raises(H264Error):
        session.subscribe()
    with pytest.raises(H264Error):
        await session.control.key("HOME")
    await session.stop()                                              # idempotent


async def test_out_of_step_stream_is_an_error_not_a_huge_allocation():
    session, reader, _ = scripted()
    reader.feed_data(struct.pack(">QI", 0, 0xFFFFFFF0))
    await asyncio.sleep(0.05)
    assert session.closed and "out of step" in session.error


async def test_control_keys_text_and_paste():
    session, _, sink = scripted()
    await session.control.key("back")
    assert sink.data == h264.key_msg(h264.ACTION_DOWN, 4) + h264.key_msg(h264.ACTION_UP, 4)
    sink.data = b""
    await session.control.key("SHIFT_LEFT", "down")
    assert sink.data == h264.key_msg(h264.ACTION_DOWN, 59)
    sink.data = b""
    await session.control.text("ab\ncd")
    assert sink.data == (b"\x01\x00\x00\x00\x02ab" + h264.key_msg(0, 66) + h264.key_msg(1, 66) + b"\x01\x00\x00\x00\x02cd")
    sink.data = b""
    drain = asyncio.create_task(session._drain_control())
    typing = asyncio.create_task(session.control.text("ñ"))
    await asyncio.sleep(0.05)
    assert sink.data == h264.clipboard_msg(1, "ñ", paste=True) and not typing.done()   # waits for the device
    session._ctl[0].feed_data(b"\x01" + struct.pack(">Q", 1))
    await asyncio.wait_for(typing, 2)
    with pytest.raises(ValueError):
        await session.control.key("NOPE")
    drain.cancel()
    await session.stop()


async def test_paste_without_confirmation_fails_in_bounded_time():
    session, _, _ = scripted()
    with pytest.raises(H264Error, match="did not confirm"):
        await session._send_acked(h264.clipboard_msg(5, "x", paste=True), 5, timeout=0.1)
    assert session._acks == {}
    await session.stop()

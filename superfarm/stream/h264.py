"""EXPERIMENTAL high-quality live view: scrcpy-server's H.264, relayed for the browser.

The screenshot transport (screen.py) works everywhere but tops out around 10-15
frames per second. This module runs the device half of scrcpy (`scrcpy-server`, the
file the installed scrcpy package ships) and speaks its protocol directly, so a
browser can decode the device's own H.264 with WebCodecs and send touches back over
the same session. Nothing is installed on the device permanently: the server is a
jar pushed to /data/local/tmp and started with `app_process`.

Experimental means: the wire protocol belongs to scrcpy and changes between its
releases. `capability()` reports whether the installed version is one this code was
run against (`verified`); with `stream.h264: auto` only those are offered.

What a subscriber receives (one WebSocket binary message per item)

    byte 0      flags: bit 0 = key frame, bit 1 = starts with SPS/PPS (decoder configuration)
    bytes 1-8   presentation time in microseconds, big-endian
    bytes 9-10  video width, 11-12 video height (big-endian), as of this frame
    bytes 13-   one access unit in Annex-B form (start-code separated NAL units)

Every key frame is sent with the SPS/PPS in front of it, so a decoder can start at
any key frame; `H264Session.info["codec"]` is the matching `avc1.PPCCLL` string.

Control messages (touch, key, text, scroll) use scrcpy's binary control protocol.
Positions carry the video size they were made for and the server drops any that do
not match its current size, so the size is tracked from the stream (it changes when
the device rotates).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import socket
import struct
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import IO, Any, Protocol

from ..core.adb import Adb, AdbError, quote
from ..core.models import check_id
from ..util.proc import CommandError, kill_tree, run, spawn, which
from .input import clean_text, is_plain_ascii, normalize_key
from .scrcpy import parse_version

EXPERIMENTAL = True
VERIFIED = ((3, 3),)                 # scrcpy major.minor this module was exercised against a real Android with
MIN_VERSION, MAX_MAJOR = (2, 1), 3   # option names used below exist from 2.1; 4.x is unknown territory
SERVER_PATHS = ("/usr/share/scrcpy/scrcpy-server", "/usr/local/share/scrcpy/scrcpy-server")
DEVICE_DIR = "/data/local/tmp"

FLAG_KEY, FLAG_CONFIG = 0x01, 0x02
HEADER = struct.Struct(">BQHH")      # flags, pts, width, height (see the module text)

_CODEC_H264 = 0x68323634             # "h264", as announced by the server
_PACKET_CONFIG, _PACKET_KEY = 1 << 63, 1 << 62
_PTS_MASK = _PACKET_KEY - 1
_MAX_PACKET = 16 << 20               # a frame larger than this means the stream is out of step
_GOP_REPLAY = 45                     # frames after a key frame kept for late joiners
_QUEUE = 90                          # frames a slow subscriber may fall behind before it is resynchronised

# Control message types (client -> device) and device message types (device -> client).
_INJECT_KEYCODE, _INJECT_TEXT, _INJECT_TOUCH, _INJECT_SCROLL, _BACK_OR_SCREEN_ON = 0, 1, 2, 3, 4
_SET_CLIPBOARD, _RESET_VIDEO = 9, 17
_DEV_CLIPBOARD, _DEV_ACK_CLIPBOARD, _DEV_UHID_OUTPUT = 0, 1, 2
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
_ACTIONS = {"down": ACTION_DOWN, "up": ACTION_UP, "move": ACTION_MOVE}
_TEXT_MAX = 300                      # bytes per INJECT_TEXT message
_CLIPBOARD_MAX = (1 << 18) - 14      # bytes per SET_CLIPBOARD message
_RESET_FROM = (3, 1)                 # first version known to have the "restart the encoder" message


class H264Error(RuntimeError):
    pass


# --------------------------------------------------------------------------- finding scrcpy

def find_server(env: Mapping[str, str] | None = None) -> Path | None:
    """The scrcpy-server file: SCRCPY_SERVER_PATH, the usual package locations, or next to the binary."""
    env = os.environ if env is None else env
    candidates = [env.get("SCRCPY_SERVER_PATH", ""), *SERVER_PATHS]
    binary = which("scrcpy")
    if binary:
        home = Path(binary).resolve().parent
        candidates += [str(home.parent / "share" / "scrcpy" / "scrcpy-server"), str(home / "scrcpy-server")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.R_OK):
            return Path(candidate)
    return None


def installed_version(env: Mapping[str, str] | None = None) -> tuple[int, ...] | None:
    """Version of the scrcpy-server that find_server() returns.

    The server refuses to start unless it is told its own exact version. The package's
    client and server always match, so the client is asked; SCRCPY_SERVER_VERSION
    overrides for a server file installed on its own.
    """
    env = os.environ if env is None else env
    forced = env.get("SCRCPY_SERVER_VERSION", "")
    if forced:
        return parse_version("scrcpy " + forced)
    if not which("scrcpy"):
        return None
    try:
        return parse_version(run(["scrcpy", "--version"], timeout=10, check=False).stdout)
    except CommandError:
        return None


def capability(env: Mapping[str, str] | None = None) -> dict:
    """Can the H.264 path run on this host? Always answers; never raises."""
    server, version = find_server(env), installed_version(env)
    out: dict[str, Any] = {"experimental": EXPERIMENTAL, "available": False, "verified": False,
                           "version": ".".join(map(str, version)) if version else "",
                           "server": str(server) if server else "", "reason": "", "fix": ""}
    if server is None:
        out["reason"] = "scrcpy-server was not found"
        out["fix"] = "install the scrcpy package, or set SCRCPY_SERVER_PATH to the scrcpy-server file"
    elif version is None:
        out["reason"] = "the version of scrcpy-server is unknown (no working `scrcpy` command)"
        out["fix"] = "install the scrcpy package, or set SCRCPY_SERVER_VERSION (for example 3.3.4)"
    elif version < MIN_VERSION or version[0] > MAX_MAJOR:
        out["reason"] = f"scrcpy {out['version']} is not supported (needs 2.1 up to {MAX_MAJOR}.x)"
        out["fix"] = "install a supported scrcpy release"
    else:
        out["available"] = True
        out["verified"] = version[:2] in VERIFIED
        if not out["verified"]:
            out["reason"] = (f"scrcpy {out['version']} is untested with this release (tested: "
                             + ", ".join(f"{a}.{b}.x" for a, b in VERIFIED) + ")")
            out["fix"] = "set stream.h264 to `on` to use it anyway"
    return out


def parse_bitrate(value: str | int) -> int:
    """`2M`, `800K` or a plain number of bits per second."""
    text = str(value).strip().upper()
    scale = {"K": 1_000, "M": 1_000_000}.get(text[-1:], 1)
    digits = text[:-1] if scale != 1 else text
    try:
        bits = int(float(digits) * scale)
    except ValueError:
        raise ValueError(f"bad bitrate {value!r}: use a number with K or M, for example 2M") from None
    return min(50_000_000, max(100_000, bits))


def server_args(version: tuple[int, ...], scid: int, *, video: bool, control: bool, max_size: int = 0,
                max_fps: int = 0, bit_rate: int = 0) -> list[str]:
    """Arguments of com.genymobile.scrcpy.Server: its own version, then key=value options."""
    def flag(value: bool) -> str:
        return "true" if value else "false"

    args = [".".join(map(str, version)), f"scid={scid:08x}", "log_level=info",
            f"video={flag(video)}", "audio=false", f"control={flag(control)}",
            "tunnel_forward=true",          # the host connects to the device; no listening port on the host
            "send_device_meta=false",       # 64 bytes of device name nobody here reads
            "send_dummy_byte=true",         # one byte on connect: proof the server, not just adb, answered
            "send_codec_meta=true", "send_frame_meta=true",
            "clipboard_autosync=false",     # the device's clipboard is none of the daemon's business
            "cleanup=false",                # nothing is changed that needs restoring; also keeps the pushed jar
            "power_on=true"]
    if video:
        if max_size:
            args.append(f"max_size={int(max_size)}")
        if max_fps:
            args.append(f"max_fps={int(max_fps)}")
        if bit_rate:
            args.append(f"video_bit_rate={int(bit_rate)}")
    return args


# --------------------------------------------------------------------------- control protocol (pure)

def _position(x: int, y: int, width: int, height: int) -> bytes:
    return struct.pack(">iiHH", int(x), int(y), int(width), int(height))


def touch_msg(action: int, x: int, y: int, width: int, height: int, *, pointer: int = 0) -> bytes:
    """One finger event at pixel (x, y) of a width x height video."""
    if action not in (ACTION_DOWN, ACTION_UP, ACTION_MOVE):
        raise ValueError(f"bad touch action {action!r}")
    pressure = 0 if action == ACTION_UP else 0xFFFF
    return (struct.pack(">BBQ", _INJECT_TOUCH, action, pointer & 0xFFFFFFFFFFFFFFFF)
            + _position(x, y, width, height) + struct.pack(">HII", pressure, 0, 0))


def key_msg(action: int, keycode: int, *, repeat: int = 0, meta: int = 0) -> bytes:
    if action not in (ACTION_DOWN, ACTION_UP):
        raise ValueError(f"bad key action {action!r}")
    return struct.pack(">BBIII", _INJECT_KEYCODE, action, int(keycode), int(repeat), int(meta))


def text_msgs(text: str) -> list[bytes]:
    """INJECT_TEXT messages for `text`, cut at character boundaries to the protocol's size limit."""
    out, chunk = [], b""
    for ch in text:
        piece = ch.encode()
        if len(chunk) + len(piece) > _TEXT_MAX:
            out.append(struct.pack(">BI", _INJECT_TEXT, len(chunk)) + chunk)
            chunk = b""
        chunk += piece
    if chunk:
        out.append(struct.pack(">BI", _INJECT_TEXT, len(chunk)) + chunk)
    return out


def scroll_msg(x: int, y: int, width: int, height: int, dx: float, dy: float) -> bytes:
    """Scroll by dx / dy notches (-16..16) at a point."""
    def fixed(v: float) -> int:
        return max(-0x8000, min(0x7FFF, int(max(-1.0, min(1.0, v / 16)) * 0x8000)))
    return (struct.pack(">B", _INJECT_SCROLL) + _position(x, y, width, height)
            + struct.pack(">hhI", fixed(dx), fixed(dy), 0))


def clipboard_msg(sequence: int, text: str, *, paste: bool) -> bytes:
    """Set the device clipboard (and press PASTE). A non-zero sequence is acknowledged."""
    data = text.encode()
    if len(data) > _CLIPBOARD_MAX:
        raise ValueError(f"text is too long for one clipboard message ({len(data)} bytes)")
    return struct.pack(">BQBI", _SET_CLIPBOARD, sequence, 1 if paste else 0, len(data)) + data


def back_or_screen_on_msg(action: int) -> bytes:
    return struct.pack(">BB", _BACK_OR_SCREEN_ON, action)


def reset_video_msg() -> bytes:
    return bytes([_RESET_VIDEO])


def parse_device_messages(buffer: bytes) -> tuple[list[tuple[int, int]], bytes]:
    """Split what the device sent on the control socket into (type, value) and the unread rest.

    value is the acknowledged sequence for clipboard acks, else a length. Clipboard
    *content* is deliberately not kept anywhere.
    """
    messages: list[tuple[int, int]] = []
    pos = 0
    while pos < len(buffer):
        kind, left = buffer[pos], len(buffer) - pos
        if kind == _DEV_ACK_CLIPBOARD:
            if left < 9:
                break
            messages.append((kind, struct.unpack_from(">Q", buffer, pos + 1)[0]))
            pos += 9
        elif kind == _DEV_CLIPBOARD:
            if left < 5:
                break
            size = struct.unpack_from(">I", buffer, pos + 1)[0]
            if left < 5 + size:
                break
            messages.append((kind, size))
            pos += 5 + size
        elif kind == _DEV_UHID_OUTPUT:
            if left < 5:
                break
            size = struct.unpack_from(">H", buffer, pos + 3)[0]
            if left < 5 + size:
                break
            messages.append((kind, size))
            pos += 5 + size
        else:
            raise H264Error(f"unknown message type {kind} from the device; the control channel is out of step")
    return messages, buffer[pos:]


# --------------------------------------------------------------------------- H.264 helpers (pure)

def nal_units(annexb: bytes) -> Iterator[bytes]:
    """NAL units of an Annex-B buffer, without their start codes."""
    starts: list[tuple[int, int]] = []
    pos = annexb.find(b"\x00\x00\x01")
    while pos != -1:
        starts.append((pos, pos + 3))
        pos = annexb.find(b"\x00\x00\x01", pos + 3)
    for i, (_, begin) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(annexb)
        unit = annexb[begin:end]
        if i + 1 < len(starts):
            unit = unit.rstrip(b"\x00")   # the zero byte of a following 4-byte start code
        if unit:
            yield unit


class _Bits:
    def __init__(self, data: bytes):
        self._data, self._pos = data, 0

    def bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte = self._data[self._pos >> 3]   # IndexError = truncated SPS, handled by the caller
            value = (value << 1) | ((byte >> (7 - (self._pos & 7))) & 1)
            self._pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.bits(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad exp-golomb code")
        return (1 << zeros) - 1 + (self.bits(zeros) if zeros else 0)

    def se(self) -> int:
        value = self.ue()
        return (value + 1) // 2 if value & 1 else -(value // 2)


def parse_sps(nal: bytes) -> dict:
    """Profile, level and picture size from a sequence parameter set (a NAL unit of type 7)."""
    if not nal or nal[0] & 0x1F != 7:
        raise ValueError("not an SPS")
    rbsp = nal[1:].replace(b"\x00\x00\x03", b"\x00\x00")   # undo emulation prevention
    r = _Bits(rbsp)
    profile, constraints, level = r.bits(8), r.bits(8), r.bits(8)
    r.ue()                                                # seq_parameter_set_id
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = r.ue()
        if chroma == 3:
            r.bits(1)
        r.ue(), r.ue(), r.bits(1)                         # bit depths, transform bypass
        if r.bits(1):                                     # scaling matrices
            for i in range(8 if chroma != 3 else 12):
                if r.bits(1):
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + r.se() + 256) % 256
                        last = nxt or last
    r.ue()                                                # log2_max_frame_num
    poc = r.ue()
    if poc == 0:
        r.ue()
    elif poc == 1:
        r.bits(1), r.se(), r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue(), r.bits(1)                                     # reference frames, gaps allowed
    width_mbs, height_units = r.ue() + 1, r.ue() + 1
    frame_only = r.bits(1)
    if not frame_only:
        r.bits(1)
    r.bits(1)
    left = right = top = bottom = 0
    if r.bits(1):
        left, right, top, bottom = r.ue(), r.ue(), r.ue(), r.ue()
    unit_x = 1 if chroma in (0, 3) else 2
    unit_y = (1 if chroma in (0, 2, 3) else 2) * (2 - frame_only)
    return {"profile": profile, "constraints": constraints, "level": level,
            "width": width_mbs * 16 - unit_x * (left + right),
            "height": (2 - frame_only) * height_units * 16 - unit_y * (top + bottom)}


def describe_config(config: bytes) -> dict | None:
    """{"codec": "avc1.42C01F", "width": ..., "height": ...} from a config packet, or None."""
    for unit in nal_units(config):
        if unit[0] & 0x1F == 7:
            try:
                sps = parse_sps(unit)
            except (ValueError, IndexError):
                return None
            return {"codec": f"avc1.{sps['profile']:02X}{sps['constraints']:02X}{sps['level']:02X}",
                    "width": sps["width"], "height": sps["height"]}
    return None


# --------------------------------------------------------------------------- the device-side server

class ScrcpyServer:
    """One scrcpy-server process on one device, reached through an `adb forward`. Blocking calls."""

    def __init__(self, adb: Adb, *, video: bool, control: bool, max_size: int = 0, max_fps: int = 0,
                 bit_rate: int = 0, allow_untested: bool = True):
        cap = capability()
        if not cap["available"] or (not cap["verified"] and not allow_untested):
            raise H264Error(f"{cap['reason']}. To fix: {cap['fix']}.")
        self.adb = adb
        self.version = tuple(int(p) for p in cap["version"].split("."))
        self._local = Path(cap["server"])
        self._remote = f"{DEVICE_DIR}/superfarm-scrcpy-server-{cap['version']}.jar"
        self._scid = secrets.randbits(31)
        self._args = server_args(self.version, self._scid, video=video, control=control, max_size=max_size,
                                 max_fps=max_fps, bit_rate=bit_rate)
        self.port = 0
        self._proc: subprocess.Popen | None = None
        self._log: IO[bytes] | None = None

    def start(self) -> None:
        """Push the server if the device does not have it, open the tunnel, start the process."""
        adb = self.adb
        try:
            if not adb.connect(attempts=2, delay=0.5):
                raise H264Error(f"{adb.serial} does not answer over ADB; start the Android and wait for it to boot")
            # Pushed under a name of our own and kept (cleanup=false): scrcpy windows push and
            # delete `scrcpy-server.jar`, and the two must not pull the file from under each other.
            if adb.shell(["stat", "-c", "%s", self._remote], timeout=10, check=False) != str(self._local.stat().st_size):
                adb.push(str(self._local), self._remote, timeout=60)
            # tcp:0 lets ADB pick a free port and report it, so there is no gap between
            # finding a port and binding it.
            out = adb.raw(["forward", "tcp:0", f"localabstract:scrcpy_{self._scid:08x}"], timeout=10).stdout
            self.port = int(str(out).strip().splitlines()[-1])
            line = " ".join(["CLASSPATH=" + quote(self._remote), "app_process", "/",
                             "com.genymobile.scrcpy.Server", *(quote(a) for a in self._args)])
            self._log = tempfile.TemporaryFile(prefix="sf-h264-")
            self._proc = spawn([adb.binary, "-s", adb.serial, "shell", line], stdout=self._log, stderr=self._log)
        except (AdbError, OSError, ValueError) as e:
            self.stop()
            raise H264Error(f"could not start scrcpy-server on {adb.serial}: {e}") from None

    def connect(self, *, first: bool, timeout: float = 10.0) -> socket.socket:
        """Open the next stream socket. The server hands out video first, then control.

        `adb forward` accepts a connection even when nothing listens behind it yet, so the
        first socket is only trusted once the server's one-byte greeting has arrived.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._proc is None or self._proc.poll() is not None:
                raise H264Error(f"scrcpy-server exited on {self.adb.serial}: {self.log_tail() or 'no output'}")
            sock = None
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                if not first:
                    return sock
                sock.settimeout(2)
                if sock.recv(1) == b"\x00":
                    sock.settimeout(None)
                    return sock
            except OSError:
                pass
            if sock is not None:
                sock.close()
            if time.monotonic() > deadline:
                raise H264Error(f"scrcpy-server on {self.adb.serial} did not accept a connection within "
                                f"{timeout:.0f} s: {self.log_tail() or 'no output'}")
            time.sleep(0.1)

    def wait(self, timeout: float) -> bool:
        """Wait for the server to end by itself (it does when its sockets close)."""
        if self._proc is None:
            return True
        try:
            self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def log_tail(self) -> str:
        if self._log is None:
            return ""
        try:
            self._log.seek(0)
            lines = self._log.read()[-2000:].decode(errors="replace").strip().splitlines()
        except (OSError, ValueError):
            return ""
        return " | ".join(lines[-3:])

    def stop(self) -> None:
        """Leave nothing behind: host process, tunnel, device process. Safe to call twice."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            kill_tree(proc.pid)
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid, 9)
        port, self.port = self.port, 0
        try:
            if port:
                self.adb.raw(["forward", "--remove", f"tcp:{port}"], timeout=5, check=False)
            # The brackets keep the pattern from matching the shell that runs this very
            # pkill (the "pkill -f kills its caller" trap).
            self.adb.shell(["pkill", "-f", f"sci[d]={self._scid:08x}"], timeout=5, check=False)
        except AdbError:
            pass   # device gone: its processes went with it
        if self._log is not None:
            self._log.close()
            self._log = None


class ControlSession:
    """A short control-only session: set the clipboard and paste, press keys. Blocking.

    This is how text outside ASCII is typed (see input.py). Use as a context manager.
    """

    def __init__(self, adb: Adb, *, timeout: float = 10.0):
        self._server = ScrcpyServer(adb, video=False, control=True)
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._sequence = 0
        self._buffer = b""

    def __enter__(self) -> ControlSession:
        self._server.start()
        try:
            self._sock = self._server.connect(first=True, timeout=self._timeout)
            self._sock.settimeout(self._timeout)
        except BaseException:
            self._server.stop()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            # The server handles what it was sent in order and leaves when the socket closes;
            # waiting for that is what guarantees the last key press was really injected.
            self._server.wait(3.0)
        self._server.stop()

    def paste(self, text: str) -> None:
        """Put `text` on the device clipboard and press PASTE; returns once the device confirmed."""
        if self._sock is None:
            raise H264Error("the control session is not open")
        for start in range(0, len(text), 4000):   # characters; far below the message limit even at 4 bytes each
            self._sequence += 1
            try:
                self._sock.sendall(clipboard_msg(self._sequence, text[start:start + 4000], paste=True))
                self._await_ack(self._sequence)
            except OSError as e:
                raise H264Error(f"the device did not confirm the paste: {e or 'timed out'} "
                                f"({self._server.log_tail()})") from None
            # The app reads the clipboard when the PASTE key reaches it, a moment after the
            # confirmation. Changing the clipboard again before that would paste the wrong text.
            time.sleep(0.2)

    def key(self, keycode: int) -> None:
        if self._sock is None:
            raise H264Error("the control session is not open")
        try:
            self._sock.sendall(key_msg(ACTION_DOWN, keycode) + key_msg(ACTION_UP, keycode))
        except OSError as e:
            raise H264Error(f"could not send the key: {e}") from None

    def _await_ack(self, sequence: int) -> None:
        assert self._sock is not None
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            messages, self._buffer = parse_device_messages(self._buffer)
            if (_DEV_ACK_CLIPBOARD, sequence) in messages:
                return
            data = self._sock.recv(65536)
            if not data:
                raise OSError("the device closed the control channel")
            self._buffer += data
        raise OSError("timed out")


# --------------------------------------------------------------------------- the relayed stream

class Subscription:
    """One consumer of a session's video. Iterate it; call close() when done."""

    def __init__(self, session: H264Session):
        self._session = session
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(_QUEUE)
        self._need_key = True

    def _offer(self, message: bytes, key: bool) -> None:
        if self._need_key and not key:
            return
        self._need_key = False
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            # Too slow (a stalled browser tab). Dropping single frames would corrupt the picture
            # until the next key frame, so drop everything and restart from a fresh key frame.
            while not self._queue.empty():
                self._queue.get_nowait()
            self._need_key = True
            self._session._request_key()

    def _finish(self) -> None:
        while self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            self._queue.put_nowait(None)   # stay finished for any further call
            raise StopAsyncIteration
        return item

    def close(self) -> None:
        self._session._subscribers.discard(self)


class Control:
    """Touch, keys and text for the device of one session. Coordinates are normalized (0..1)."""

    def __init__(self, session: H264Session):
        self._session = session
        self._sequence = 0

    async def touch(self, action: str | int, x: float, y: float, *, pointer: int = 0) -> None:
        code = _ACTIONS.get(action, -1) if isinstance(action, str) else int(action)
        w, h = self._session.size
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("touch coordinates must be within 0..1")
        if not 0 <= int(pointer) <= 15:
            raise ValueError("pointer must be 0-15")
        await self._session._send(touch_msg(code, min(w - 1, int(x * w)), min(h - 1, int(y * h)), w, h,
                                            pointer=int(pointer)))

    async def tap(self, x: float, y: float, *, hold: float = 0.06) -> None:
        await self.touch(ACTION_DOWN, x, y)
        await asyncio.sleep(hold)
        await self.touch(ACTION_UP, x, y)

    async def key(self, name: str | int, action: str = "press") -> None:
        code = normalize_key(name)[1]
        if action not in ("press", "down", "up"):
            raise ValueError(f"bad key action {action!r}")
        data = b"".join(key_msg(a, code) for a, wanted in ((ACTION_DOWN, "down"), (ACTION_UP, "up"))
                        if action in ("press", wanted))
        await self._session._send(data)

    async def text(self, value: str) -> None:
        """Type text: key events for ASCII, clipboard + PASTE for everything else."""
        for i, line in enumerate(clean_text(value).split("\n")):
            if i:
                await self.key("ENTER")
            for j, part in enumerate(line.split("\t")):
                if j:
                    await self.key("TAB")
                if not part:
                    continue
                if is_plain_ascii(part):
                    for message in text_msgs(part):
                        await self._session._send(message)
                else:
                    self._sequence += 1
                    await self._session._send_acked(clipboard_msg(self._sequence, part, paste=True), self._sequence)
                    await asyncio.sleep(0.2)   # let the app take the clipboard before it can change again

    async def scroll(self, x: float, y: float, dx: float, dy: float) -> None:
        w, h = self._session.size
        await self._session._send(scroll_msg(min(w - 1, int(x * w)), min(h - 1, int(y * h)), w, h, dx, dy))

    async def back(self) -> None:
        await self._session._send(back_or_screen_on_msg(ACTION_DOWN) + back_or_screen_on_msg(ACTION_UP))

    async def reset_video(self) -> bool:
        """Ask the encoder to start over with a key frame. False when this scrcpy cannot."""
        if self._session.version < _RESET_FROM:
            return False
        await self._session._send(reset_video_msg())
        return True


class H264Session:
    """One device's H.264 stream and control channel. Lives on one asyncio event loop."""

    def __init__(self, adb: Adb, *, max_size: int = 720, max_fps: int = 20, bit_rate: int = 2_000_000,
                 allow_untested: bool = True):
        self._adb = adb
        self._options = {"max_size": int(max_size), "max_fps": int(max_fps), "bit_rate": int(bit_rate),
                         "allow_untested": allow_untested}
        self._server: ScrcpyServer | None = None
        self._video: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._ctl: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._tasks: list[asyncio.Task] = []
        self._subscribers: set[Subscription] = set()
        self._send_lock = asyncio.Lock()
        self._acks: dict[int, asyncio.Future] = {}
        self._config = b""
        self._gop: list[bytes] | None = None
        self._seen_key = False
        self._ready = asyncio.Event()
        self._key_requested = 0.0
        self.version: tuple[int, ...] = ()
        self.size: tuple[int, int] = (0, 0)
        self.info: dict[str, Any] = {}
        self.error = ""
        self.closed = False
        self.control = Control(self)

    async def start(self, timeout: float = 15.0) -> dict:
        """Start the server and wait for the stream's first configuration. Returns `info`."""
        try:
            self._server = server = await asyncio.to_thread(ScrcpyServer, self._adb, video=True, control=True,
                                                            **self._options)
            self.version = server.version
            await asyncio.to_thread(server.start)
            video = await asyncio.to_thread(server.connect, first=True, timeout=timeout)
            self._video = await asyncio.open_connection(sock=video)
            control = await asyncio.to_thread(server.connect, first=False, timeout=timeout)
            self._ctl = await asyncio.open_connection(sock=control)
            codec, width, height = struct.unpack(">III", await asyncio.wait_for(self._video[0].readexactly(12), timeout))
            if codec != _CODEC_H264:
                raise H264Error(f"the device announced codec {codec:#x}, expected H.264")
            self.size = (width, height)
            self.info = {"codec": "", "width": width, "height": height, "version": ".".join(map(str, self.version))}
            self._tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._drain_control())]
            await asyncio.wait_for(self._ready.wait(), timeout)
            if self.error:
                raise H264Error(self.error)
        except (H264Error, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, struct.error) as e:
            tail = self._server.log_tail() if self._server else ""
            await self.stop()
            reason = "timed out waiting for the device" if isinstance(e, asyncio.TimeoutError) else str(e)
            raise H264Error(f"H.264 stream did not start: {reason}" + (f" (server said: {tail})" if tail else "")) from None
        return self.info

    def subscribe(self) -> Subscription:
        """A new consumer. It starts at a key frame: replayed if recent, else freshly requested."""
        if self.closed:
            raise H264Error(self.error or "the stream has ended")
        sub = Subscription(self)
        if self._gop:
            for i, message in enumerate(self._gop):
                sub._offer(message, i == 0)
        elif self._seen_key:
            self._request_key()   # before the first key frame there is nothing to ask for: it is on its way
        self._subscribers.add(sub)
        return sub

    async def stop(self) -> None:
        """End the stream and remove everything it created on the host and the device."""
        if self.closed and self._server is None:
            return
        self.closed = True
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        for pair in (self._video, self._ctl):
            if pair:
                pair[1].close()
        self._video = self._ctl = None
        for sub in list(self._subscribers):
            sub._finish()
        for future in self._acks.values():
            if not future.done():
                future.cancel()
        server, self._server = self._server, None
        if server:
            await asyncio.to_thread(server.stop)

    # ---------------------------------------------------------------- internals
    async def _pump(self) -> None:
        assert self._video is not None
        reader = self._video[0]
        try:
            while True:
                pts_flags, size = struct.unpack(">QI", await reader.readexactly(12))
                if not 0 < size <= _MAX_PACKET:
                    raise H264Error(f"implausible packet size {size}; the video stream is out of step")
                payload = await reader.readexactly(size)
                if pts_flags & _PACKET_CONFIG:
                    self._config = payload
                    described = describe_config(payload)
                    if described:
                        self.size = (described["width"], described["height"])
                        self.info.update(described)
                    self._ready.set()
                    continue
                key = bool(pts_flags & _PACKET_KEY)
                flags = FLAG_KEY | (FLAG_CONFIG if self._config else 0) if key else 0
                message = (HEADER.pack(flags, pts_flags & _PTS_MASK, *self.size)
                           + (self._config + payload if key else payload))
                if key:
                    self._seen_key = True
                    self._gop = [message]
                elif self._gop is not None:
                    self._gop.append(message)
                    if len(self._gop) > _GOP_REPLAY:
                        self._gop = None   # too long to replay; late joiners ask for a new key frame
                for sub in list(self._subscribers):
                    sub._offer(message, key)
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            tail = self._server.log_tail() if self._server else ""
            self.error = "the device closed the video stream" + (f" ({tail})" if tail else "")
        except H264Error as e:
            self.error = str(e)
        self._ready.set()   # wake start() if it is still waiting; it reads self.error
        await self.stop()

    async def _drain_control(self) -> None:
        """Read what the device says on the control socket so it never blocks; resolve acks."""
        assert self._ctl is not None
        reader, buffer = self._ctl[0], b""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                messages, buffer = parse_device_messages(buffer + data)
                for kind, value in messages:
                    future = self._acks.pop(value, None) if kind == _DEV_ACK_CLIPBOARD else None
                    if future and not future.done():
                        future.set_result(True)
        except (H264Error, ConnectionError, OSError):
            return   # control is lost; the video pump notices the end of the session

    async def _send(self, data: bytes) -> None:
        if self.closed or self._ctl is None:
            raise H264Error(self.error or "the stream has ended")
        async with self._send_lock:
            writer = self._ctl[1]
            try:
                writer.write(data)
                await asyncio.wait_for(writer.drain(), 5)
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                raise H264Error(f"could not send to the device: {e or 'timed out'}") from None

    async def _send_acked(self, data: bytes, sequence: int, timeout: float = 5.0) -> None:
        future = self._acks[sequence] = asyncio.get_running_loop().create_future()
        try:
            await self._send(data)
            await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise H264Error("the device did not confirm the paste") from None
        finally:
            self._acks.pop(sequence, None)

    def _request_key(self) -> None:
        """Ask for a key frame on behalf of a subscriber that cannot use the frames in flight."""
        now = time.monotonic()
        if self.closed or self._ctl is None or now - self._key_requested < 1.0 or self.version < _RESET_FROM:
            return
        self._key_requested = now
        task = asyncio.ensure_future(self._send(reset_video_msg()))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # a lost request is not an error


# --------------------------------------------------------------------------- per-farm manager

class _Manager(Protocol):
    def adb(self, id: int) -> Adb: ...


class H264Streams:
    """Shared H.264 sessions by instance id: the first viewer starts one, the last one ends it."""

    def __init__(self, mgr: _Manager, config: Any):
        self._mgr = mgr
        self._config = config
        self._sessions: dict[int, tuple[H264Session, int]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def reload_config(self, config: Any) -> None:
        self._config = config

    def capability(self) -> dict:
        """capability() plus `enabled`: whether this farm's configuration offers the H.264 path."""
        cap = capability()
        mode = getattr(self._config.stream, "h264", "auto")
        cap["mode"] = mode
        cap["enabled"] = cap["available"] and (mode == "on" or (mode == "auto" and cap["verified"]))
        if cap["available"] and mode == "off":
            cap["reason"], cap["fix"] = "the H.264 view is switched off", "set stream.h264 to `auto`"
        return cap

    async def acquire(self, id: int) -> H264Session:
        """The running session of a device, started if needed. Pair every call with release()."""
        id = check_id(id)
        cap = self.capability()
        if not cap["enabled"]:
            raise H264Error(f"H.264 view is not available: {cap['reason']}. To fix: {cap['fix']}.")
        async with self._locks.setdefault(id, asyncio.Lock()):
            session, users = self._sessions.get(id, (None, 0))
            if session is not None and session.closed:
                session, users = None, 0
            if session is None:
                if len(self._sessions) >= int(self._config.stream.max_viewers):
                    raise H264Error(f"{len(self._sessions)} H.264 views are already open (stream.max_viewers)")
                stream = self._config.stream
                session = H264Session(self._mgr.adb(id), max_size=stream.scrcpy_max_size,
                                      max_fps=stream.scrcpy_max_fps, bit_rate=parse_bitrate(stream.scrcpy_bitrate))
                await session.start()
            self._sessions[id] = (session, users + 1)
            return session

    async def release(self, id: int) -> None:
        id = check_id(id)
        async with self._locks.setdefault(id, asyncio.Lock()):
            session, users = self._sessions.get(id, (None, 0))
            if session is None:
                return
            if users > 1 and not session.closed:
                self._sessions[id] = (session, users - 1)
                return
            del self._sessions[id]
            await session.stop()

    def active(self) -> list[int]:
        return sorted(id for id, (session, _) in self._sessions.items() if not session.closed)

    async def close_all(self) -> None:
        sessions, self._sessions = [s for s, _ in self._sessions.values()], {}
        await asyncio.gather(*(s.stop() for s in sessions), return_exceptions=True)

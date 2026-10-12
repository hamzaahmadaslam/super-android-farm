"""Touching devices: taps, swipes, keys and text, for one device or many at once.

Coordinates are normalized (0..1) by default, so one gesture made on one live view
lands on the same spot of every selected device whatever its resolution. Every
operation fans out over a thread pool and returns one result per device; a device
that is slow or offline never delays or fails the others.

Text typing
-----------
`input text` on the device is the transport for plain ASCII. It has two traps that
are handled here (see `ascii_chunks`): a space must be sent as `%s`, and a literal
`%` directly followed by `s` would be read as that escape, so the text is split
between the two characters. Newlines and tabs become ENTER / TAB key events.

`input text` cannot type anything outside ASCII (it builds key events from the
virtual keyboard's character map, which has no entries for other characters, and
fails without typing). Text containing such characters is therefore sent through the
device clipboard and pasted: a short control-only scrcpy-server session sets the
clipboard and presses PASTE (`superfarm.stream.h264.ControlSession`). That needs
scrcpy installed on the host and *replaces the device's clipboard content*. The
result of `text()` says which method was used for each device.
"""
from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Protocol
from collections.abc import Callable, Iterable, Sequence

from ..core.adb import Adb
from ..core.models import check_id

# Android key codes by position (index = code). Names are KeyEvent.KEYCODE_* without the prefix.
_KEY_NAMES = (
    "UNKNOWN SOFT_LEFT SOFT_RIGHT HOME BACK CALL ENDCALL 0 1 2 3 4 5 6 7 8 9 STAR POUND DPAD_UP DPAD_DOWN "
    "DPAD_LEFT DPAD_RIGHT DPAD_CENTER VOLUME_UP VOLUME_DOWN POWER CAMERA CLEAR A B C D E F G H I J K L M N O P "
    "Q R S T U V W X Y Z COMMA PERIOD ALT_LEFT ALT_RIGHT SHIFT_LEFT SHIFT_RIGHT TAB SPACE SYM EXPLORER ENVELOPE "
    "ENTER DEL GRAVE MINUS EQUALS LEFT_BRACKET RIGHT_BRACKET BACKSLASH SEMICOLON APOSTROPHE SLASH AT NUM "
    "HEADSETHOOK FOCUS PLUS MENU NOTIFICATION SEARCH MEDIA_PLAY_PAUSE MEDIA_STOP MEDIA_NEXT MEDIA_PREVIOUS "
    "MEDIA_REWIND MEDIA_FAST_FORWARD MUTE PAGE_UP PAGE_DOWN PICTSYMBOLS SWITCH_CHARSET BUTTON_A BUTTON_B "
    "BUTTON_C BUTTON_X BUTTON_Y BUTTON_Z BUTTON_L1 BUTTON_R1 BUTTON_L2 BUTTON_R2 BUTTON_THUMBL BUTTON_THUMBR "
    "BUTTON_START BUTTON_SELECT BUTTON_MODE ESCAPE FORWARD_DEL CTRL_LEFT CTRL_RIGHT CAPS_LOCK SCROLL_LOCK "
    "META_LEFT META_RIGHT FUNCTION SYSRQ BREAK MOVE_HOME MOVE_END INSERT FORWARD MEDIA_PLAY MEDIA_PAUSE "
    "MEDIA_CLOSE MEDIA_EJECT MEDIA_RECORD F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 F11 F12 NUM_LOCK NUMPAD_0 NUMPAD_1 "
    "NUMPAD_2 NUMPAD_3 NUMPAD_4 NUMPAD_5 NUMPAD_6 NUMPAD_7 NUMPAD_8 NUMPAD_9 NUMPAD_DIVIDE NUMPAD_MULTIPLY "
    "NUMPAD_SUBTRACT NUMPAD_ADD NUMPAD_DOT NUMPAD_COMMA NUMPAD_ENTER NUMPAD_EQUALS NUMPAD_LEFT_PAREN "
    "NUMPAD_RIGHT_PAREN VOLUME_MUTE INFO CHANNEL_UP CHANNEL_DOWN ZOOM_IN ZOOM_OUT TV WINDOW GUIDE DVR BOOKMARK "
    "CAPTIONS SETTINGS TV_POWER TV_INPUT STB_POWER STB_INPUT AVR_POWER AVR_INPUT PROG_RED PROG_GREEN "
    "PROG_YELLOW PROG_BLUE APP_SWITCH BUTTON_1 BUTTON_2 BUTTON_3 BUTTON_4 BUTTON_5 BUTTON_6 BUTTON_7 BUTTON_8 "
    "BUTTON_9 BUTTON_10 BUTTON_11 BUTTON_12 BUTTON_13 BUTTON_14 BUTTON_15 BUTTON_16 LANGUAGE_SWITCH "
    "MANNER_MODE 3D_MODE CONTACTS CALENDAR MUSIC CALCULATOR ZENKAKU_HANKAKU EISU MUHENKAN HENKAN "
    "KATAKANA_HIRAGANA YEN RO KANA ASSIST BRIGHTNESS_DOWN BRIGHTNESS_UP MEDIA_AUDIO_TRACK SLEEP WAKEUP PAIRING "
    "MEDIA_TOP_MENU 11 12 LAST_CHANNEL TV_DATA_SERVICE VOICE_ASSIST TV_RADIO_SERVICE TV_TELETEXT "
    "TV_NUMBER_ENTRY TV_TERRESTRIAL_ANALOG TV_TERRESTRIAL_DIGITAL TV_SATELLITE TV_SATELLITE_BS TV_SATELLITE_CS "
    "TV_SATELLITE_SERVICE TV_NETWORK TV_ANTENNA_CABLE TV_INPUT_HDMI_1 TV_INPUT_HDMI_2 TV_INPUT_HDMI_3 "
    "TV_INPUT_HDMI_4 TV_INPUT_COMPOSITE_1 TV_INPUT_COMPOSITE_2 TV_INPUT_COMPONENT_1 TV_INPUT_COMPONENT_2 "
    "TV_INPUT_VGA_1 TV_AUDIO_DESCRIPTION TV_AUDIO_DESCRIPTION_MIX_UP TV_AUDIO_DESCRIPTION_MIX_DOWN TV_ZOOM_MODE "
    "TV_CONTENTS_MENU TV_MEDIA_CONTEXT_MENU TV_TIMER_PROGRAMMING HELP NAVIGATE_PREVIOUS NAVIGATE_NEXT "
    "NAVIGATE_IN NAVIGATE_OUT STEM_PRIMARY STEM_1 STEM_2 STEM_3 DPAD_UP_LEFT DPAD_DOWN_LEFT DPAD_UP_RIGHT "
    "DPAD_DOWN_RIGHT MEDIA_SKIP_FORWARD MEDIA_SKIP_BACKWARD MEDIA_STEP_FORWARD MEDIA_STEP_BACKWARD SOFT_SLEEP "
    "CUT COPY PASTE SYSTEM_NAVIGATION_UP SYSTEM_NAVIGATION_DOWN SYSTEM_NAVIGATION_LEFT SYSTEM_NAVIGATION_RIGHT "
    "ALL_APPS REFRESH THUMBS_UP THUMBS_DOWN PROFILE_SWITCH"
).split()
KEYCODES: dict[str, int] = {name: code for code, name in enumerate(_KEY_NAMES)}
MAX_KEYCODE = len(_KEY_NAMES) - 1

# What people type instead of the Android name.
KEY_ALIASES = {
    "RETURN": "ENTER", "BACKSPACE": "DEL", "DELETE": "FORWARD_DEL", "ESC": "ESCAPE", "RECENTS": "APP_SWITCH",
    "RECENT": "APP_SWITCH", "OVERVIEW": "APP_SWITCH", "UP": "DPAD_UP", "DOWN": "DPAD_DOWN", "LEFT": "DPAD_LEFT",
    "RIGHT": "DPAD_RIGHT", "OK": "DPAD_CENTER", "VOL_UP": "VOLUME_UP", "VOL_DOWN": "VOLUME_DOWN",
    "PLAY_PAUSE": "MEDIA_PLAY_PAUSE", "PGUP": "PAGE_UP", "PGDN": "PAGE_DOWN",
}

MAX_TEXT = 10_000          # characters per call; typing is ~0.5 s per chunk, so longer is never interactive
MAX_SWIPE_MS = 30_000
_CHUNK = 200               # characters per `input text` call, far below the device's command-line limit
_SIZE_TTL = 30.0           # seconds a cached screen size is trusted (rotation changes it)


def normalize_key(name: str | int) -> tuple[str, int]:
    """Validate a key and return (argument for `input keyevent`, numeric key code).

    Accepts `HOME`, `keycode_home`, an alias such as `backspace`, or a number. Anything
    else is refused here, before a command is built, with a message that says what works.
    """
    raw = str(name).strip().upper().replace("-", "_").replace(" ", "_")
    if not raw:
        raise ValueError("empty key name")
    if raw.isdigit():
        # A bare number is a key code, as in `input keyevent 4`; the digit *keys* are KEYCODE_0..9.
        code = int(raw)
        if not 0 < code <= 1023:
            raise ValueError(f"key code {code} is out of range (1-1023)")
        return str(code), code
    short = raw[8:] if raw.startswith("KEYCODE_") else raw
    short = KEY_ALIASES.get(short, short)
    if short not in KEYCODES:
        raise ValueError(f"unknown key {name!r}: use an Android key name such as HOME, BACK, ENTER, DEL, "
                         "APP_SWITCH, VOLUME_UP, POWER, DPAD_UP, PASTE, or a numeric key code")
    return "KEYCODE_" + short, KEYCODES[short]


def is_plain_ascii(value: str) -> bool:
    """True when `input text` can type every character (printable ASCII, newline, tab)."""
    return all(c in "\n\t" or " " <= c <= "~" for c in value)


def clean_text(value: str) -> str:
    """Normalise line ends and refuse characters no keyboard can produce."""
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    if len(value) > MAX_TEXT:
        raise ValueError(f"text is too long ({len(value)} characters, limit {MAX_TEXT})")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    bad = sorted({c for c in value if (c < " " and c not in "\n\t") or c == "\x7f"})
    if bad:
        raise ValueError("text contains control characters that cannot be typed: "
                         + ", ".join(f"U+{ord(c):04X}" for c in bad))
    return value


def text_steps(value: str) -> list[tuple[str, str]]:
    """Split text into what a keyboard does: ("text", run) and ("key", "ENTER" | "TAB")."""
    steps: list[tuple[str, str]] = []
    for part in re.split(r"([\n\t])", clean_text(value)):
        if part == "\n":
            steps.append(("key", "ENTER"))
        elif part == "\t":
            steps.append(("key", "TAB"))
        elif part:
            steps.append(("text", part))
    return steps


def ascii_chunks(run: str, size: int = _CHUNK) -> list[str]:
    """Arguments for `input text`, in order, that together type `run` exactly.

    The device command turns `%s` into a space and has no way to escape a percent
    sign (`%%` types two). So spaces are sent as `%s`, and wherever the text itself
    has `%` followed by `s`, the text is cut between them: a trailing `%` in one
    call and a leading `s` in the next are both typed literally.
    """
    pieces: list[str] = []
    start = 0
    for i in range(1, len(run)):
        if (run[i] == "s" and run[i - 1] == "%") or i - start >= size:
            pieces.append(run[start:i])
            start = i
    pieces.append(run[start:])
    return [p.replace(" ", "%s") for p in pieces if p]


def parse_current_size(dumpsys_window_displays: str) -> tuple[int, int] | None:
    """Screen size *as currently rotated* from `dumpsys window displays` (`cur=480x854`)."""
    m = re.search(r"\bcur=(\d{2,5})x(\d{2,5})\b", dumpsys_window_displays)
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_wm_size(wm_size: str) -> tuple[int, int] | None:
    """Size from `wm size`; an override (user-set resolution) wins over the physical size."""
    found = dict(re.findall(r"(Override|Physical) size:\s*(\d+x\d+)", wm_size))
    text = found.get("Override") or found.get("Physical")
    if not text:
        return None
    w, h = text.split("x")
    return int(w), int(h)


def to_pixels(x: float, y: float, size: tuple[int, int], normalized: bool) -> tuple[int, int]:
    """Map one point onto a device. Normalized 1.0 is the last pixel, not one past it."""
    w, h = size
    if normalized:
        return min(w - 1, int(x * w)), min(h - 1, int(y * h))
    px, py = int(x), int(y)
    if not (0 <= px < w and 0 <= py < h):
        raise ValueError(f"point {px},{py} is outside this device's {w}x{h} screen")
    return px, py


def _check_point(x: float, y: float, normalized: bool) -> None:
    for v in (x, y):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"bad coordinate {v!r}")
        if normalized and not 0.0 <= v <= 1.0:
            raise ValueError(f"normalized coordinate {v} is outside 0..1 (pass normalized=False for pixels)")
        if v < 0:
            raise ValueError(f"negative coordinate {v}")


class _Manager(Protocol):
    def adb(self, id: int) -> Adb: ...


class Input:
    """Send input to one or many devices. Thread-safe; share one instance."""

    def __init__(self, mgr: _Manager, config: Any = None, *, screen: Any = None, max_workers: int = 32,
                 timeout: float = 30.0, unicode_typer: Callable[[Adb, list[tuple[str, str]]], None] | None = None):
        """
        screen         a ScreenHub: when it has a recent frame of a device, that frame's size is used
                       for mapping, because it is exactly the picture the person clicked on.
        max_workers    devices handled at the same moment; more than this queue behind them.
        timeout        seconds after which a device that has not answered is reported as failed.
        unicode_typer  replaces the scrcpy clipboard method for non-ASCII text (tests, other IMEs).
        """
        self._mgr = mgr
        self._config = config
        self._screen = screen
        self._timeout = float(timeout)
        self._max_workers = max(1, int(max_workers))
        self._unicode_typer = unicode_typer
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._sizes: dict[int, tuple[tuple[int, int], float]] = {}

    # ------------------------------------------------------------------ public
    def tap(self, ids: Iterable[int], x: float, y: float, *, normalized: bool = True) -> dict[int, dict]:
        _check_point(x, y, normalized)

        def work(adb: Adb, id: int) -> dict:
            px, py = to_pixels(x, y, self.size(id, adb), normalized)
            adb.tap(px, py)
            return {"x": px, "y": py}
        return self._fan_out(ids, work)

    def swipe(self, ids: Iterable[int], x1: float, y1: float, x2: float, y2: float, ms: int = 300, *,
              normalized: bool = True) -> dict[int, dict]:
        _check_point(x1, y1, normalized)
        _check_point(x2, y2, normalized)
        ms = int(ms)
        if not 1 <= ms <= MAX_SWIPE_MS:
            raise ValueError(f"swipe duration {ms} ms is outside 1-{MAX_SWIPE_MS}")

        def work(adb: Adb, id: int) -> dict:
            size = self.size(id, adb)
            a, b = to_pixels(x1, y1, size, normalized), to_pixels(x2, y2, size, normalized)
            adb.swipe(*a, *b, ms)
            return {"from": list(a), "to": list(b)}
        return self._fan_out(ids, work, extra_time=ms / 1000)

    def long_press(self, ids: Iterable[int], x: float, y: float, ms: int = 700, *,
                   normalized: bool = True) -> dict[int, dict]:
        """A swipe that does not move is how `input` holds a finger down."""
        return self.swipe(ids, x, y, x, y, ms, normalized=normalized)

    def key(self, ids: Iterable[int], name: str | int) -> dict[int, dict]:
        arg, _code = normalize_key(name)

        def work(adb: Adb, id: int) -> dict:
            adb.shell(["input", "keyevent", arg], timeout=15)
            return {"key": arg}
        return self._fan_out(ids, work)

    def text(self, ids: Iterable[int], value: str) -> dict[int, dict]:
        steps = text_steps(value)
        plain = is_plain_ascii(value)
        calls = sum(len(ascii_chunks(v)) if kind == "text" else 1 for kind, v in steps)

        def work(adb: Adb, id: int) -> dict:
            if plain:
                self._type_ascii(adb, steps)
                return {"method": "input-text"}
            self._type_unicode(adb, steps)
            return {"method": "clipboard-paste"}
        # Each device-side `input` call starts a small Java process (~0.5 s), so long text needs longer.
        return self._fan_out(ids, work, extra_time=calls * 1.5)

    def size(self, id: int, adb: Adb | None = None) -> tuple[int, int]:
        """Current screen size of a device in pixels, as rotated right now."""
        id = check_id(id)
        if self._screen is not None:
            seen = self._screen.frame_size(id, max_age=5.0)
            if seen:
                return seen
        now = time.monotonic()
        with self._lock:
            hit = self._sizes.get(id)
        if hit and now - hit[1] < _SIZE_TTL:
            return hit[0]
        adb = adb or self._mgr.adb(id)
        # `dumpsys window displays` is read whole, never through `grep -m1`: a reader that closes
        # early has killed Android services with SIGPIPE on this stack before.
        size = parse_current_size(adb.shell("dumpsys window displays", timeout=10, check=False))
        if size is None:
            size = parse_wm_size(adb.shell("wm size", timeout=10))
        if size is None:
            raise ValueError("could not read the screen size")
        with self._lock:
            self._sizes[id] = (size, now)
        return size

    def forget(self, id: int | None = None) -> None:
        """Drop cached sizes (after a resolution change or a recreate)."""
        with self._lock:
            if id is None:
                self._sizes.clear()
            else:
                self._sizes.pop(check_id(id), None)

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------------------------------- internals
    @staticmethod
    def _type_ascii(adb: Adb, steps: Sequence[tuple[str, str]]) -> None:
        for kind, value in steps:
            if kind == "key":
                adb.shell(["input", "keyevent", normalize_key(value)[0]], timeout=15)
                continue
            for chunk in ascii_chunks(value):
                # A list is quoted per argument for the device shell, so quotes, `$`, `;` and
                # friends in the text reach `input` untouched and are never run.
                adb.shell(["input", "text", chunk], timeout=30)

    def _type_unicode(self, adb: Adb, steps: list[tuple[str, str]]) -> None:
        if self._unicode_typer is not None:
            self._unicode_typer(adb, steps)
            return
        from . import h264   # imported late: plain ASCII typing must work without scrcpy installed

        with h264.ControlSession(adb) as control:
            for kind, value in steps:
                if kind == "key":
                    control.key(normalize_key(value)[1])
                else:
                    control.paste(value)

    def _executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="sf-input")
            return self._pool

    def _fan_out(self, ids: Iterable[int], work: Callable[[Adb, int], dict], *,
                 extra_time: float = 0.0) -> dict[int, dict]:
        targets = list(dict.fromkeys(check_id(i) for i in ids))   # validated, de-duplicated, order kept
        if not targets:
            return {}

        def one(id: int) -> dict:
            started = time.monotonic()
            try:
                out = {"ok": True, "detail": "", **work(self._mgr.adb(id), id)}
            except Exception as e:   # noqa: BLE001 - one device's failure is a result, never an exception for the rest
                self.forget(id)      # a stale size is a common reason for a failed gesture
                out = {"ok": False, "detail": (str(e) or type(e).__name__)[:300]}
            out["ms"] = int((time.monotonic() - started) * 1000)
            return out

        if len(targets) == 1:
            return {targets[0]: one(targets[0])}
        pool = self._executor()
        futures: dict[Future, int] = {pool.submit(one, id): id for id in targets}
        limit = self._timeout + extra_time
        done, pending = wait(futures, timeout=limit)
        results: dict[int, dict] = {}
        for fut in done:
            results[futures[fut]] = fut.result()
        for fut in pending:
            # Still queued behind stuck devices, or stuck itself. The ADB call inside carries its
            # own timeout, so the thread is released on its own; the caller is not kept waiting.
            fut.cancel()
            results[futures[fut]] = {"ok": False, "detail": f"no answer within {limit:.0f} s", "ms": int(limit * 1000)}
        return {id: results[id] for id in targets}

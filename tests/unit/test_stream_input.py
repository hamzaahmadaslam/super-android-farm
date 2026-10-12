import random
import time

import pytest
from stream_fakes import FakeAdb, FakeManager

from superfarm.core.adb import AdbError
from superfarm.stream import h264
from superfarm.stream import input as inp
from superfarm.stream.input import Input


def device_decodes(argument: str) -> str:
    """What Android's `input text <argument>` types (the escape loop of its Java source)."""
    buff = list(argument)
    escape = False
    i = 0
    while i < len(buff):
        if escape:
            escape = False
            if buff[i] == "s":
                buff[i] = " "
                i -= 1
                del buff[i]
        if buff[i] == "%":
            escape = True
        i += 1
    return "".join(buff)


def typed(text: str) -> str:
    return "".join(device_decodes(chunk) for chunk in inp.ascii_chunks(text))


# ------------------------------------------------------------------ keys

@pytest.mark.parametrize("name, expected", [
    ("HOME", ("KEYCODE_HOME", 3)), ("home", ("KEYCODE_HOME", 3)), ("KEYCODE_BACK", ("KEYCODE_BACK", 4)),
    ("keycode_enter", ("KEYCODE_ENTER", 66)), ("backspace", ("KEYCODE_DEL", 67)), ("app-switch", ("KEYCODE_APP_SWITCH", 187)),
    ("recents", ("KEYCODE_APP_SWITCH", 187)), ("volume up", ("KEYCODE_VOLUME_UP", 24)), ("paste", ("KEYCODE_PASTE", 279)),
    ("KEYCODE_5", ("KEYCODE_5", 12)), ("a", ("KEYCODE_A", 29)), ("F12", ("KEYCODE_F12", 142)),
    (4, ("4", 4)), ("66", ("66", 66)), ("all_apps", ("KEYCODE_ALL_APPS", 284)),
])
def test_normalize_key(name, expected):
    assert inp.normalize_key(name) == expected


@pytest.mark.parametrize("bad", ["", "  ", "NOPE", "HOME; reboot", "$(id)", "KEYCODE_", "0", "99999", "-1", "ho\nme"])
def test_normalize_key_rejects(bad):
    with pytest.raises(ValueError):
        inp.normalize_key(bad)


def test_keycode_table_matches_android():
    codes = inp.KEYCODES
    assert (codes["HOME"], codes["BACK"], codes["0"], codes["A"], codes["Z"]) == (3, 4, 7, 29, 54)
    assert (codes["ENTER"], codes["DEL"], codes["ESCAPE"], codes["F1"], codes["NUMPAD_0"]) == (66, 67, 111, 131, 144)
    assert (codes["APP_SWITCH"], codes["WAKEUP"], codes["CUT"], codes["PASTE"], codes["ALL_APPS"]) == (187, 224, 277, 279, 284)
    assert len(set(codes.values())) == len(codes)


# ------------------------------------------------------------------ text

@pytest.mark.parametrize("text", [
    "hello", "two words", "  lead and trail  ", "100% sure", "50%s off", "%s", "%%s", "%%%sss", "a%", "%", "s%s%s",
    "it's \"quoted\" $(reboot) `id` ;ls|x&", "-5 --help", "".join(chr(c) for c in range(32, 127)),
])
def test_ascii_chunks_type_exactly_the_text(text):
    assert typed(text) == text
    assert all(" " not in chunk for chunk in inp.ascii_chunks(text))


def test_ascii_chunks_random_and_bounded():
    rng = random.Random(7)
    for _ in range(300):
        text = "".join(rng.choice("%s ab%") for _ in range(rng.randint(1, 40)))
        assert typed(text) == text
    long = "x%s " * 400
    chunks = inp.ascii_chunks(long, size=50)
    assert typed(long) == long and max(len(c.replace("%s", " ")) for c in chunks) <= 50


def test_text_steps_split_lines_and_tabs():
    assert inp.text_steps("ab\ncd\tef\r\n") == [("text", "ab"), ("key", "ENTER"), ("text", "cd"), ("key", "TAB"),
                                                ("text", "ef"), ("key", "ENTER")]
    assert inp.text_steps("") == []


def test_clean_text_refuses_control_characters_and_overlong():
    with pytest.raises(ValueError, match="U\\+001B"):
        inp.clean_text("a\x1b[0m")
    with pytest.raises(ValueError, match="too long"):
        inp.clean_text("x" * (inp.MAX_TEXT + 1))
    assert inp.is_plain_ascii("abc ~\n\t") and not inp.is_plain_ascii("é") and not inp.is_plain_ascii("🙂")


# ------------------------------------------------------------------ geometry

def test_parse_sizes():
    dump = "  DisplayContent\n    init=480x854 160dpi cur=854x480 app=806x480 rng=480x456-806x782\n"
    assert inp.parse_current_size(dump) == (854, 480)
    assert inp.parse_current_size("nothing here") is None
    assert inp.parse_wm_size("Physical size: 1080x1920\nOverride size: 720x1280") == (720, 1280)
    assert inp.parse_wm_size("Physical size: 480x854") == (480, 854)
    assert inp.parse_wm_size("error") is None


def test_to_pixels():
    assert inp.to_pixels(0.5, 0.5, (480, 854), True) == (240, 427)
    assert inp.to_pixels(1.0, 1.0, (480, 854), True) == (479, 853)     # the last pixel, not one past it
    assert inp.to_pixels(0.0, 0.0, (480, 854), True) == (0, 0)
    assert inp.to_pixels(100, 200, (480, 854), False) == (100, 200)
    with pytest.raises(ValueError, match="outside"):
        inp.to_pixels(480, 10, (480, 854), False)


# ------------------------------------------------------------------ Input

def test_tap_maps_normalized_point_onto_each_resolution():
    mgr = FakeManager({1: FakeAdb(480, 854), 2: FakeAdb(1080, 1920), 3: FakeAdb(720, 1280)})
    out = Input(mgr).tap([1, 2, 3], 0.25, 0.5)
    assert {i: (r["ok"], r["x"], r["y"]) for i, r in out.items()} == {
        1: (True, 120, 427), 2: (True, 270, 960), 3: (True, 180, 640)}
    assert mgr.devices[2].inputs() == [["input", "tap", "270", "960"]]
    assert list(out) == [1, 2, 3]


def test_sizes_are_cached_and_forgotten():
    mgr = FakeManager()
    control = Input(mgr)
    control.tap([1], 0.1, 0.1)
    control.tap([1], 0.2, 0.2)
    assert mgr.devices[1].shell_calls.count("dumpsys window displays") == 1
    control.forget(1)
    control.tap([1], 0.2, 0.2)
    assert mgr.devices[1].shell_calls.count("dumpsys window displays") == 2


def test_size_of_the_live_view_wins():
    class Hub:
        def frame_size(self, id, max_age=5.0):
            return (854, 480)          # the device is shown rotated right now
    mgr = FakeManager({1: FakeAdb(480, 854)})
    out = Input(mgr, screen=Hub()).tap([1], 0.5, 0.5)
    assert (out[1]["x"], out[1]["y"]) == (427, 240)
    assert "dumpsys window displays" not in mgr.devices[1].shell_calls


def test_swipe_and_long_press():
    mgr = FakeManager({1: FakeAdb(100, 200)})
    control = Input(mgr)
    assert control.swipe([1], 0.5, 0.75, 0.5, 0.25, 250)[1]["ok"]
    assert control.long_press([1], 0.1, 0.1, 800)[1]["ok"]
    assert mgr.devices[1].inputs() == [["input", "swipe", "50", "150", "50", "50", "250"],
                                       ["input", "swipe", "10", "20", "10", "20", "800"]]
    with pytest.raises(ValueError):
        control.swipe([1], 0, 0, 1, 1, 0)
    with pytest.raises(ValueError):
        control.swipe([1], 0, 0, 1, 1, 10**6)


@pytest.mark.parametrize("x, y, normalized", [(1.5, 0.5, True), (-0.1, 0.5, True), (float("nan"), 0, True),
                                              (float("inf"), 0, False), (-5, 3, False), ("1", 0, True), (True, 0, True)])
def test_bad_coordinates_are_refused_before_any_device_is_touched(x, y, normalized):
    mgr = FakeManager()
    with pytest.raises(ValueError):
        Input(mgr).tap([1], x, y, normalized=normalized)
    assert mgr.devices[1].shell_calls == []


def test_absolute_point_outside_one_device_fails_only_that_device():
    mgr = FakeManager({1: FakeAdb(480, 854), 2: FakeAdb(1080, 1920)})
    out = Input(mgr).tap([1, 2], 900, 1000, normalized=False)
    assert not out[1]["ok"] and "outside" in out[1]["detail"]
    assert out[2]["ok"]


def test_key_is_validated_then_sent():
    mgr = FakeManager()
    control = Input(mgr)
    result = control.key([1], "back")[1]
    assert (result["ok"], result["detail"], result["key"]) == (True, "", "KEYCODE_BACK") and result["ms"] >= 0
    assert mgr.devices[1].inputs() == [["input", "keyevent", "KEYCODE_BACK"]]
    with pytest.raises(ValueError):
        control.key([1], "BACK && reboot")
    assert len(mgr.devices[1].inputs()) == 1


def test_ascii_text_goes_through_input_text():
    mgr = FakeManager()
    out = Input(mgr).text([1], "50%s off\nok")
    assert out[1]["ok"] and out[1]["method"] == "input-text"
    assert mgr.devices[1].inputs() == [["input", "text", "50%"], ["input", "text", "s%soff"],
                                       ["input", "keyevent", "KEYCODE_ENTER"], ["input", "text", "ok"]]


def test_non_ascii_text_uses_the_unicode_typer():
    seen = []
    mgr = FakeManager()
    out = Input(mgr, unicode_typer=lambda adb, steps: seen.append((adb, steps))).text([1], "héllo\n你好")
    assert out[1]["ok"] and out[1]["method"] == "clipboard-paste"
    assert seen == [(mgr.devices[1], [("text", "héllo"), ("key", "ENTER"), ("text", "你好")])]
    assert mgr.devices[1].inputs() == []


def test_non_ascii_text_without_scrcpy_says_how_to_fix(monkeypatch):
    monkeypatch.setattr(h264, "capability", lambda env=None: {
        "available": False, "verified": False, "version": "", "server": "",
        "reason": "scrcpy-server was not found", "fix": "install the scrcpy package"})
    out = Input(FakeManager()).text([1], "ñ")
    assert not out[1]["ok"]
    assert "scrcpy-server was not found" in out[1]["detail"] and "install the scrcpy package" in out[1]["detail"]


def test_fan_out_is_parallel_and_keeps_failures_apart():
    devices = {i: FakeAdb(delay=0.15) for i in range(1, 13)}
    devices[5].fail = AdbError("fake: device offline")
    mgr = FakeManager(devices)
    started = time.monotonic()
    out = Input(mgr).tap(list(devices) + [99], 0.5, 0.5)
    took = time.monotonic() - started
    assert took < 0.15 * 2 * 3, "12 devices at 0.3 s each must not run one after another"
    assert [i for i, r in out.items() if not r["ok"]] == [5, 99]
    assert "offline" in out[5]["detail"] and "99" in out[99]["detail"]
    assert all(out[i]["ok"] and out[i]["ms"] >= 250 for i in devices if i != 5)


def test_slow_device_is_reported_without_delaying_the_answer():
    mgr = FakeManager({1: FakeAdb(), 2: FakeAdb(delay=1.0)})
    control = Input(mgr, timeout=0.2)
    started = time.monotonic()
    out = control.key([1, 2], "HOME")
    assert time.monotonic() - started < 0.8
    assert out[1]["ok"] and not out[2]["ok"] and "no answer" in out[2]["detail"]
    control.close()


def test_ids_are_validated_and_deduplicated():
    mgr = FakeManager()
    control = Input(mgr)
    assert list(control.key([1, 1, "1"], "HOME")) == [1]
    assert len(mgr.devices[1].inputs()) == 1
    assert control.key([], "HOME") == {}
    with pytest.raises(ValueError):
        control.key([1, "1; rm -rf /"], "HOME")
    with pytest.raises(ValueError):
        control.key([-3], "HOME")

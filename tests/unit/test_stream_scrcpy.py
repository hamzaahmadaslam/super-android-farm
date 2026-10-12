import json
import os
import socket
import stat
import time
from pathlib import Path

import pytest
from stream_fakes import FakeAdb, FakeManager

from superfarm.config import Config, update_config
from superfarm.stream import scrcpy
from superfarm.stream.scrcpy import NoDesktopError, Rect, ScrcpyError, ScrcpyWindows, tile

SCREENS = [(1920, 1080), (2560, 1440), (1366, 768), (3840, 2160), (1280, 720), (1080, 1920)]
ASPECT = 480 / 854


def overlaps(a: Rect, b: Rect, title: int = 0) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y - title < b.y + b.h and b.y - title < a.y + a.h


# ------------------------------------------------------------------ tiling

@pytest.mark.parametrize("screen", SCREENS)
def test_tile_every_count_up_to_200_stays_on_screen(screen):
    width, height = screen
    for count in range(1, 201):
        layout = tile(count, screen, aspect=ASPECT)
        rects = layout.rects
        assert len(rects) == count and layout.cols * layout.rows >= count
        assert len({(r.w, r.h) for r in rects}) == 1, "all windows have one size"
        w, h = rects[0].w, rects[0].h
        assert w >= 1 and abs(w / h - ASPECT) < 0.05 + 1 / h, (count, w, h)
        for r in rects:
            assert r.x >= 0 and r.x + r.w <= width, (count, r)
            assert r.y >= scrcpy._TITLE_BAR and r.y + r.h <= height, (count, r)   # title bar visible above
        assert len({(r.x, r.y) for r in rects}) == count, "no two windows at the same place"
        if not layout.overlap:
            assert w >= scrcpy._MIN_WIDTH
            for i, a in enumerate(rects):
                # Only neighbours can touch in a uniform grid; checking them keeps this fast.
                for b in rects[i + 1:i + 2] + rects[i + layout.cols:i + layout.cols + 1]:
                    assert not overlaps(a, b, scrcpy._TITLE_BAR), (count, a, b)


def test_tile_small_counts_have_no_overlap_at_all():
    for count in range(1, 31):
        layout = tile(count, (2560, 1440), aspect=ASPECT)
        assert not layout.overlap
        for i, a in enumerate(layout.rects):
            for b in layout.rects[i + 1:]:
                assert not overlaps(a, b, scrcpy._TITLE_BAR), (count, a, b)


def test_tile_uses_rows_to_keep_windows_large():
    one = tile(1, (1920, 1080), aspect=ASPECT)
    assert (one.cols, one.rows) == (1, 1) and one.rects[0].h > 900          # one window fills the height
    seven = tile(7, (1920, 1080), aspect=ASPECT)
    assert (seven.cols, seven.rows) == (4, 2) and seven.rects[0].w > 270   # two rows beat one row of 265 px
    twenty = tile(20, (1920, 1080), aspect=ASPECT)
    assert twenty.rows == 2 and twenty.cols == 10 and not twenty.overlap
    many = tile(98, (2560, 1440), aspect=ASPECT)
    assert many.rows >= 4 and not many.overlap and many.rects[0].w >= scrcpy._MIN_WIDTH
    assert many.rects[many.cols].y > many.rects[0].y and many.rects[many.cols].x == many.rects[0].x


def test_tile_overlaps_instead_of_shrinking_below_a_usable_size():
    layout = tile(200, (1366, 768), aspect=ASPECT)
    assert layout.overlap and layout.rects[0].w == scrcpy._MIN_WIDTH
    assert layout.rects[-1].y + layout.rects[-1].h <= 768


def test_tile_landscape_devices_and_edge_cases():
    wide = tile(6, (1920, 1080), aspect=854 / 480)
    assert wide.rows >= 2 and wide.rects[0].w > wide.rects[0].h
    assert tile(0, (1920, 1080)).rects == []
    with pytest.raises(ValueError):
        tile(3, (10, 10))


# ------------------------------------------------------------------ screen size

XRANDR = """Screen 0: minimum 16 x 16, current 4480 x 1440, maximum 32767 x 32767
DP-1 connected 1920x1080+2560+0 (normal left inverted right x axis y axis) 530mm x 300mm
   1920x1080     60.00*+
HDMI-3 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 1210mm x 680mm
   2560x1440    119.95*+
DP-2 disconnected (normal left inverted right x axis y axis)
"""


def test_parse_xrandr_prefers_primary():
    assert scrcpy.parse_xrandr(XRANDR) == (2560, 1440)
    assert scrcpy.parse_xrandr(XRANDR.replace(" primary", "")) == (1920, 1080)
    assert scrcpy.parse_xrandr("Screen 0: minimum\nDP-2 disconnected (normal)\n") is None
    assert scrcpy.parse_xrandr("") is None


def test_parse_wayland_tools():
    wlr = "eDP-1 \"Panel\"\n  Enabled: yes\n  Modes:\n    1920x1200 px, 60.001 Hz (preferred, current)\n    1280x800 px, 60 Hz\n"
    assert scrcpy.parse_wlr_randr(wlr) == (1920, 1200)
    assert scrcpy.parse_wlr_randr("nothing") is None
    sway = json.dumps([{"name": "A", "active": True, "focused": False, "rect": {"width": 1280, "height": 720}},
                       {"name": "B", "active": True, "focused": True, "rect": {"width": 3440, "height": 1440}},
                       {"name": "C", "active": False, "rect": {"width": 800, "height": 600}}])
    assert scrcpy.parse_sway_outputs(sway) == (3440, 1440)
    assert scrcpy.parse_sway_outputs("not json") is None and scrcpy.parse_sway_outputs("[]") is None
    kde = "Output: 1 DP-1 enabled connected priority 1\n\x1b[01;32m\tGeometry: 0,0 2560x1440\x1b[0m\n\tScale: 1\n"
    assert scrcpy.parse_kscreen(kde) == (2560, 1440)
    assert scrcpy.parse_kscreen("") is None


def test_sysfs_screen(tmp_path):
    for name, status, modes in [("card1-DP-1", "disconnected", ""), ("card1-HDMI-A-1", "connected", "3840x2160\n1920x1080\n"),
                                ("card1-eDP-1", "connected", "1920x1080\n")]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "status").write_text(status + "\n")
        (tmp_path / name / "modes").write_text(modes)
    assert scrcpy.sysfs_screen(tmp_path) == (3840, 2160)
    assert scrcpy.sysfs_screen(tmp_path / "missing") is None


def test_screen_size_never_fails(monkeypatch):
    monkeypatch.setattr(scrcpy, "which", lambda name: None)
    monkeypatch.setattr(scrcpy, "sysfs_screen", lambda: None)
    assert scrcpy.screen_size({"DISPLAY": ":0"}) == (scrcpy.DEFAULT_SCREEN, "default")
    monkeypatch.setattr(scrcpy, "sysfs_screen", lambda: (1600, 900))
    assert scrcpy.screen_size({}) == ((1600, 900), "sysfs")


# ------------------------------------------------------------------ desktop / arguments

def test_desktop_env_uses_the_session_it_is_given():
    assert scrcpy.desktop_env({"DISPLAY": ":1"})["DISPLAY"] == ":1"
    assert scrcpy.desktop_env({"WAYLAND_DISPLAY": "wayland-1"})["WAYLAND_DISPLAY"] == "wayland-1"


def test_desktop_env_finds_a_session_the_service_manager_did_not_pass_on(tmp_path):
    runtime, x11 = tmp_path / "run", tmp_path / "x11"
    runtime.mkdir()
    x11.mkdir()
    for path in (runtime / "wayland-0", x11 / "X1"):
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        sock.close()
    env = scrcpy.desktop_env({"XDG_RUNTIME_DIR": str(runtime)}, x11_dir=x11)
    assert env["WAYLAND_DISPLAY"] == "wayland-0" and env["DISPLAY"] == ":1"


def test_headless_is_refused_with_advice(tmp_path):
    with pytest.raises(NoDesktopError, match="logged in to the desktop") as e:
        scrcpy.desktop_env({"XDG_RUNTIME_DIR": str(tmp_path)}, uid=1000, x11_dir=tmp_path)
    assert "web panel" in str(e.value)
    with pytest.raises(NoDesktopError, match="root"):
        scrcpy.desktop_env({}, uid=0, x11_dir=tmp_path)


def test_parse_version():
    assert scrcpy.parse_version("scrcpy 3.3.4 <https://github.com/Genymobile/scrcpy>\n\nDependencies") == (3, 3, 4)
    assert scrcpy.parse_version("scrcpy 2.1") == (2, 1) and scrcpy.parse_version("scrcpy v1.25") == (1, 25)
    assert scrcpy.parse_version("command not found") is None


def test_window_args():
    args = scrcpy.window_args("127.0.0.1:5556", 40123, "Phone 1", bitrate="2M", max_size=720, max_fps=20,
                              version=(3, 3, 4), rect=Rect(8, 38, 270, 480), extra=["--no-mipmaps"])
    assert args[:4] == ["-s", "127.0.0.1:5556", "-p", "40123"]
    for expected in ("--window-title=Phone 1", "--window-x=8", "--window-y=38", "--window-width=270",
                     "--window-height=480", "--max-fps=20", "--video-buffer=0", "--no-audio", "--stay-awake"):
        assert expected in args
    assert args[args.index("-b") + 1] == "2M" and args[args.index("-m") + 1] == "720"
    assert args[-1] == "--no-mipmaps"
    old = scrcpy.window_args("s", 1, "t", bitrate="1M", max_size=0, max_fps=15, version=(1, 25))
    assert "--display-buffer=0" in old and "--no-audio" not in old and "-m" not in old and "--window-x=0" not in old


def test_free_ports_are_usable_and_outside_scrcpys_own_range():
    ports = {scrcpy.free_port() for _ in range(20)}
    assert all(p >= 1024 and not 27183 <= p <= 27199 for p in ports)
    with socket.socket() as s:
        s.bind(("127.0.0.1", ports.pop()))


# ------------------------------------------------------------------ windows (with a stand-in program)

FAKE = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "scrcpy 3.3.4 <test>"; exit 0; fi
printf '%s\\n' "$@" > "$SF_FAKE_OUT.$$"
echo "DISPLAY=$DISPLAY SDL=$SDL_VIDEODRIVER" >> "$SF_FAKE_OUT.$$"
MODE
"""


@pytest.fixture
def program(tmp_path):
    def make(mode: str = "exec sleep 30") -> str:
        path = tmp_path / "scrcpy-fake"
        path.write_text(FAKE.replace("MODE", mode))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)
    return make


@pytest.fixture
def windows(tmp_path, program):
    made = []

    def make(mgr=None, mode="exec sleep 30", env=None, config=None):
        env = {"PATH": os.environ["PATH"], "DISPLAY": ":77", "SF_FAKE_OUT": str(tmp_path / "args"), **(env or {})}
        w = ScrcpyWindows(mgr or FakeManager({1: FakeAdb(), 2: FakeAdb(), 3: FakeAdb()}), config or Config(),
                          env=env, binary=program(mode))
        made.append(w)
        return w
    yield make
    for w in made:
        w.close_all()


def recorded(tmp_path: Path) -> list[list[str]]:
    return [p.read_text().splitlines() for p in sorted(tmp_path.glob("args.*"))]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_open_starts_one_tracked_process_per_device(windows, tmp_path):
    w = windows()
    assert w.available() == {"ok": True, "reason": "", "version": "3.3.4"}
    first = w.open(1)
    assert first["title"] == "Phone 1" and not first["already_open"] and alive(first["pid"])
    again = w.open(1)
    assert again["already_open"] and again["pid"] == first["pid"]
    second = w.open(2)
    assert second["port"] != first["port"] and w.running() == [1, 2]
    time.sleep(0.1)
    args = recorded(tmp_path)
    assert len(args) == 2
    line = args[0]
    assert line[:2] == ["-s", "127.0.0.1:1"] and "--window-title=Phone 1" in line and "--video-buffer=0" in line
    assert line[-1] == "DISPLAY=:77 SDL=x11"
    w.close(1)
    assert w.running() == [2] and not alive(first["pid"])
    w.close(1)                                           # closing twice is fine
    w.close_all()
    assert w.running() == [] and not alive(second["pid"])


def test_window_closed_by_the_person_is_noticed(windows):
    w = windows(mode="sleep 1.0")
    w.open(1)
    assert w.running() == [1]
    time.sleep(0.8)
    assert w.running() == []


def test_program_that_dies_at_once_reports_its_own_words(windows):
    w = windows(mode="echo 'ERROR: Could not open display' >&2; exit 2")
    with pytest.raises(ScrcpyError, match="exited at once.*Could not open display"):
        w.open(1)
    assert w.running() == []


def test_unreachable_device_and_missing_program(windows):
    adb = FakeAdb()
    adb.adb_state = "offline"
    w = windows(FakeManager({4: adb}))
    with pytest.raises(ScrcpyError, match="Android 4 does not answer over ADB"):
        w.open(4)
    missing = ScrcpyWindows(FakeManager(), Config(), env={"DISPLAY": ":1"}, binary="/nonexistent/scrcpy")
    assert not missing.available()["ok"] and "install" in missing.available()["reason"].lower()
    with pytest.raises(ScrcpyError, match="not installed"):
        missing.open(1)


def test_headless_server_refuses_before_starting_anything(windows, tmp_path, monkeypatch):
    monkeypatch.setattr(scrcpy, "desktop_env", lambda env=None: (_ for _ in ()).throw(NoDesktopError("no display here")))
    w = windows()
    assert w.available() == {"ok": False, "reason": "no display here"}
    with pytest.raises(NoDesktopError):
        w.open(1)
    with pytest.raises(NoDesktopError):
        w.grid([1, 2])
    assert recorded(tmp_path) == []


def test_grid_tiles_reopens_and_reports_per_device(windows, tmp_path, monkeypatch):
    monkeypatch.setattr(scrcpy, "_STAGGER", 0.0)
    offline_adb = FakeAdb()
    offline_adb.adb_state = "offline"
    mgr = FakeManager({1: FakeAdb(480, 854), 2: FakeAdb(480, 854), 3: offline_adb})
    config = update_config(Config(), {"stream": {"scrcpy_bitrate": "1M", "scrcpy_max_size": 640, "scrcpy_max_fps": 15,
                                                 "scrcpy_extra_args": ["--no-mipmaps"]}})
    w = windows(mgr, config=config)
    before = w.open(1)
    out = w.grid([1, 2, 3, 2], screen=(1920, 1080))
    assert (out["screen"], out["screen_source"], out["cols"], out["rows"]) == ([1920, 1080], "given", 3, 1)
    assert out["opened"] == 2 and out["positioned"] and not out["overlap"]
    assert out["results"][1]["ok"] and out["results"][2]["ok"]
    assert not out["results"][3]["ok"] and "does not answer" in out["results"][3]["detail"]
    assert out["results"][1]["pid"] != before["pid"] and not alive(before["pid"])   # moved = restarted
    r1, r2 = out["results"][1]["rect"], out["results"][2]["rect"]
    assert r1["y"] == r2["y"] and r2["x"] >= r1["x"] + r1["w"]
    time.sleep(0.1)
    last = recorded(tmp_path)[-1]
    assert f"--window-x={r2['x']}" in last and f"--window-width={r2['w']}" in last
    assert last[last.index("-b") + 1] == "1M" and last[last.index("-m") + 1] == "640" and "--max-fps=15" in last
    assert "--no-mipmaps" in last
    assert w.running() == [1, 2]


def test_title_is_made_safe_and_ids_checked(windows, tmp_path):
    mgr = FakeManager({1: FakeAdb()})
    mgr.resolved = lambda id: type("R", (), {"name": "--evil\n\x1b]0;x\x07 name" + "y" * 100, "width": 480, "height": 854})()
    w = windows(mgr)
    title = w.open(1)["title"]
    assert "\n" not in title and "\x1b" not in title and len(title) <= 60
    time.sleep(0.1)
    assert f"--window-title={title}" in recorded(tmp_path)[0]
    with pytest.raises(ValueError):
        w.open("1; reboot")

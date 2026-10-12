"""Stream area against a real Android (SF_LIVE=1).

By default one throwaway container `sftest-stream-android` is started from a local
image and removed afterwards. Environment:

    SF_LIVE_SERIAL     use this already running device instead (e.g. 127.0.0.1:17301); nothing is started
    SF_LIVE_IMAGE      image for the throwaway Android (default redroid/redroid:13.0.0-latest; never pulled)
    SF_LIVE_ADB_PORT   host port for its ADB (default 17301)

The typing and touch checks drive the stock Settings app, only because every AOSP
image has it; nothing in the code under test knows about any app.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from contextlib import aclosing
from types import SimpleNamespace

import pytest
from PIL import Image

from superfarm.config import Config
from superfarm.core.adb import Adb
from superfarm.stream import h264
from superfarm.stream.input import Input
from superfarm.stream.scrcpy import ScrcpyWindows
from superfarm.stream.screen import ScreenHub

pytestmark = pytest.mark.live

CONTAINER = "sftest-stream-android"


def docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def adb():
    serial = os.environ.get("SF_LIVE_SERIAL", "")
    started = False
    if not serial:
        image = os.environ.get("SF_LIVE_IMAGE", "redroid/redroid:13.0.0-latest")
        port = int(os.environ.get("SF_LIVE_ADB_PORT", "17301"))
        if docker("image", "inspect", image).returncode != 0:
            pytest.skip(f"image {image} is not present locally (set SF_LIVE_IMAGE or SF_LIVE_SERIAL)")
        run = docker("run", "-d", "--privileged", "--pull", "never", "--name", CONTAINER, "--memory", "1536m",
                     "--cpus", "2", "-p", f"127.0.0.1:{port}:5555", image, "androidboot.redroid_width=480",
                     "androidboot.redroid_height=854", "androidboot.redroid_dpi=160", "androidboot.redroid_fps=20")
        if run.returncode != 0:
            pytest.skip(f"could not start the throwaway Android: {run.stderr.strip()[:200]}")
        started, serial = True, f"127.0.0.1:{port}"
    device = Adb(serial)
    try:
        if not device.wait_boot(timeout=180):
            pytest.skip(f"{serial} did not boot")
        yield device
    finally:
        device.disconnect()
        if started:
            docker("rm", "-f", "-v", CONTAINER)


@pytest.fixture(scope="module")
def mgr(adb):
    width, height = adb.size()
    return SimpleNamespace(adb=lambda id: adb,
                           resolved=lambda id: SimpleNamespace(id=id, name=f"Live test {id}", width=width, height=height))


def nodes(adb: Adb) -> list[ET.Element]:
    path = f"/data/local/tmp/sftest-{time.time_ns()}.xml"     # unique, and removed: a stale dump must never be read
    adb.shell(["uiautomator", "dump", path], timeout=40, check=False)
    xml = adb.shell(["cat", path], timeout=10, check=False)
    adb.shell(["rm", "-f", path], timeout=10, check=False)
    return list(ET.fromstring(xml).iter("node")) if xml.startswith("<?xml") else []


def centre(adb: Adb, text: str, size: tuple[int, int]) -> tuple[float, float] | None:
    for node in nodes(adb):
        if node.get("text") == text:
            x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds", "")))
            return (x1 + x2) / 2 / size[0], (y1 + y2) / 2 / size[1]
    return None


def texts(adb: Adb) -> list[str]:
    return [n.get("text", "") for n in nodes(adb) if n.get("text")]


def search_field(adb: Adb) -> str | None:
    for node in nodes(adb):
        if "search_src_text" in (node.get("resource-id") or ""):
            return node.get("text", "")
    return None


def no_leftovers(adb: Adb) -> None:
    assert adb.raw(["forward", "--list"], timeout=10).stdout.strip() == ""
    assert adb.shell("ps -A -o ARGS", timeout=10, check=False).count("com.genymobile.scrcpy.Server") == 0


# ------------------------------------------------------------------ screenshots

def test_frame_and_capture_path(mgr, adb):
    hub = ScreenHub(mgr, Config())
    try:
        size = adb.size()
        full = Image.open(io.BytesIO(hub.frame(1)))
        assert full.format == "JPEG" and full.size == size
        assert Image.open(io.BytesIO(hub.frame(1, width=240))).size[0] == 240
        for _ in range(4):
            hub.frame(1, max_age=0)
        status = hub.status(1)
        assert status["state"] == "live" and status["mode"] in ("raw", "png")
        assert set(status["probe_ms"]) == {"raw", "png"}
        print("capture path:", status["mode"], status["probe_ms"])
    finally:
        hub.close()


async def test_stream_delivers_and_goes_quiet(mgr):
    hub = ScreenHub(mgr, Config(), idle_grace=1.0)
    try:
        started = time.monotonic()
        frames = []
        async with aclosing(hub.stream(1, 5)) as stream:
            async for frame in stream:
                frames.append(frame)
                if len(frames) >= 8:
                    break
        took = time.monotonic() - started
        assert all(f[:3] == b"\xff\xd8\xff" for f in frames)
        assert 1.2 <= took <= 6.0, f"8 frames at 5 fps took {took:.1f} s"
        await asyncio.sleep(1.5)
        assert not hub.status(1)["capturing"] and hub.stats()["viewers"] == 0
    finally:
        await asyncio.to_thread(hub.close)


def test_unreachable_device_is_offline(mgr):
    hub = ScreenHub(SimpleNamespace(adb=lambda id: Adb("127.0.0.1:17399")), Config(), capture_timeout=3)
    try:
        with pytest.raises(Exception) as e:
            hub.frame(1)
        assert getattr(e.value, "state", "") == "offline"
    finally:
        hub.close()


# ------------------------------------------------------------------ input

def test_tap_key_and_ascii_text(mgr, adb):
    control = Input(mgr)
    size = control.size(1)
    assert control.key([1], "HOME")[1]["ok"]
    adb.shell("am start -a android.settings.SETTINGS", timeout=20)
    time.sleep(2)
    target = centre(adb, "Search settings", size)
    if target is None:
        pytest.skip("this image's Settings app has no search bar to type into")
    assert control.tap([1], *target)[1]["ok"]
    time.sleep(2)
    if search_field(adb) is None:
        pytest.skip("the search field did not open")
    for value in ["".join(chr(c) for c in range(32, 127)), "50%s off %%s 100% sure", "it's $(reboot) `id` ;x|y&"]:
        adb.shell("input keyevent KEYCODE_MOVE_END", timeout=15)
        adb.shell(["input", "keyevent", *["67"] * 110], timeout=60)
        result = control.text([1], value)[1]
        assert result["ok"] and result["method"] == "input-text", result
        assert search_field(adb) == value


def test_non_ascii_text_through_the_clipboard(mgr, adb):
    if not h264.capability()["available"]:
        pytest.skip("scrcpy-server is not installed on this host")
    if search_field(adb) is None:
        pytest.skip("no text field is open (the previous test opens it)")
    adb.shell("input keyevent KEYCODE_MOVE_END", timeout=15)
    adb.shell(["input", "keyevent", *["67"] * 110], timeout=60)
    value = "héllo wörld ñ 你好 🙂 100%s"
    result = Input(mgr).text([1], value)[1]
    assert result["ok"] and result["method"] == "clipboard-paste", result
    time.sleep(0.5)
    assert search_field(adb) == value
    no_leftovers(adb)


# ------------------------------------------------------------------ H.264

async def test_h264_stream_and_control(mgr, adb):
    cap = h264.capability()
    if not cap["available"]:
        pytest.skip(f"H.264 path unavailable: {cap['reason']}")
    await asyncio.to_thread(adb.shell, "am start -a android.settings.SETTINGS", timeout=20)
    await asyncio.sleep(2)
    size = await asyncio.to_thread(adb.size)
    apps = await asyncio.to_thread(centre, adb, "Apps", size)
    session = h264.H264Session(adb, max_size=720, max_fps=20, bit_rate=2_000_000)
    try:
        info = await session.start()
        assert info["codec"].startswith("avc1.") and info["width"] > 0 and info["height"] > 0
        sub = session.subscribe()
        first = await asyncio.wait_for(sub.__anext__(), 10)
        flags, _pts, width, height = h264.HEADER.unpack_from(first)
        assert flags == h264.FLAG_KEY | h264.FLAG_CONFIG and (width, height) == (info["width"], info["height"])
        kinds = [unit[0] & 0x1F for unit in h264.nal_units(first[h264.HEADER.size:])]
        assert kinds[:2] == [7, 8] and 5 in kinds, f"expected SPS, PPS, IDR; got NAL types {kinds}"
        if apps is not None:
            await session.control.tap(*apps)
            await asyncio.sleep(2)
            after = await asyncio.to_thread(texts, adb)
            assert "Search settings" not in after, "the tap sent over the control channel did not arrive"
            await session.control.back()
            await asyncio.sleep(1.5)
            assert "Search settings" in await asyncio.to_thread(texts, adb)
        assert await session.control.reset_video()
        seen_key = False
        for _ in range(60):
            message = await asyncio.wait_for(sub.__anext__(), 10)
            if h264.HEADER.unpack_from(message)[0] & h264.FLAG_KEY:
                seen_key = True
                break
        assert seen_key, "no key frame after asking the encoder to restart"
    finally:
        await session.stop()
    await asyncio.to_thread(no_leftovers, adb)


# ------------------------------------------------------------------ native windows

def test_scrcpy_window_in_a_private_display(mgr, adb):
    if not (shutil.which("Xvfb") and shutil.which("scrcpy") and shutil.which("xwininfo")):
        pytest.skip("needs Xvfb, xwininfo and scrcpy")
    display = ":97"
    xvfb = subprocess.Popen(["Xvfb", display, "-screen", "0", "1600x900x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "SDL_VIDEODRIVER")}
    env["DISPLAY"] = display
    windows = ScrcpyWindows(mgr, Config(), env=env)
    try:
        time.sleep(1.5)
        out = windows.grid([1])
        assert out["screen"] == [1600, 900] and out["screen_source"] == "xrandr" and out["opened"] == 1, out
        rect = out["results"][1]["rect"]
        time.sleep(4)
        assert windows.running() == [1]
        tree = subprocess.run(["xwininfo", "-root", "-tree", "-display", display], capture_output=True, text=True).stdout
        line = next((ln for ln in tree.splitlines() if '"Live test 1"' in ln), "")
        assert f"{rect['w']}x{rect['h']}+{rect['x']}+{rect['y']}" in line, tree
    finally:
        windows.close_all()
        xvfb.terminate()
        xvfb.wait(5)
    assert windows.running() == []
    time.sleep(1)
    no_leftovers(adb)

"""Microphone path against real containers (SF_LIVE=1).

    test_fifo_reaches_a_container   needs Docker, ffmpeg and a local `alpine` image (never pulled).
                                    Proves the host FIFO, mounted with Camera.extra_run_args(), is
                                    a readable FIFO for an unrelated uid inside a privileged
                                    container, carries the feed's sound at real-time pace, and
                                    that mute / unmute work.
    test_android_records_the_feed   additionally needs scrcpy, adb and an Android 13 image that has
                                    the camera overlay baked in:
                                        SF_LIVE_MEDIA_IMAGE     that image (never pulled; test is
                                                                skipped without it)
                                        SF_LIVE_MEDIA_ADB_PORT  host port for its ADB (default 17201)
                                    Plays beeps into the microphone and records the Android
                                    microphone back.

No video device is touched: the camera object gets a fake /sys and /dev, and only sound is
played, so these tests are safe on a machine whose real loopback devices are in use.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from superfarm.config import Config
from superfarm.core.adb import Adb
from superfarm.core.docker import Docker
from superfarm.core.models import InstanceSpec, resolve
from superfarm.media import audio as A
from superfarm.media import camera as C
from superfarm.media import feeds as F
from superfarm.paths import Paths

pytestmark = pytest.mark.live

PREFIX = "sftestmedia"
FIFO_CONTAINER = "sftest-media-fifo"
ANDROID_CONTAINER = "sftest-media-android"
ADB_PORT = int(os.environ.get("SF_LIVE_MEDIA_ADB_PORT", "17201"))
BEEPS = "0.5*sin(2*PI*(400+400*mod(floor(3*t),4))*t)*lt(mod(3*t,1),0.6)"


class _Mgr:
    def __init__(self, config: Config):
        self.config, self.post_boot_hooks = config, []

    def resolved(self, instance_id: int):
        return resolve(InstanceSpec(id=instance_id, camera=True), self.config)

    def list_status(self):
        return [{"id": 1}]

    def adb(self, instance_id: int) -> Adb:
        return Adb.for_port(ADB_PORT)


@pytest.fixture
def rig(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    docker = Docker()
    if not docker.available()[0]:
        pytest.skip("Docker is not available")
    config = Config.model_validate({"docker": {"name_prefix": PREFIX},
                                    "media": {"camera": {"enabled": True, "slots": 2, "rtmp_port": 17250}}})
    paths = Paths(tmp_path / "home").ensure()
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    node = sysfs / "class" / "video4linux" / "video101"
    node.mkdir(parents=True)
    dev.mkdir()
    (node / "name").write_text(f"{F.CARD_LABEL} 1\n")
    (node / "format").write_text("MJPG:640x360@20\n")
    (dev / "video101").write_text("")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc=exprs='{BEEPS}':s=48000:c=stereo", "-t", "10", str(paths.media / "beeps.wav")],
                   check=True, timeout=60)
    camera = C.Camera(config, paths, docker, _Mgr(config), sys_root=sysfs, dev_root=dev)
    try:
        yield camera, docker
    finally:
        camera.down()
        docker.run(["rm", "-f", "-v", FIFO_CONTAINER, ANDROID_CONTAINER], check=False)


def _local_image(docker: Docker, ref: str) -> None:
    if not ref or not docker.image_exists(ref):
        pytest.skip(f"image {ref or '(not set)'} is not available locally (it is never pulled)")


def test_fifo_reaches_a_container(rig):
    camera, docker = rig
    _local_image(docker, "alpine:latest")
    r = camera.mgr.resolved(1)
    # uid 1041 is Android's audioserver: some unrelated, unprivileged user.
    docker.create_and_start(["--name", FIFO_CONTAINER, "--privileged", "--memory", "64m", "--user", "1041:1005",
                             *camera.extra_run_args(r), "alpine:latest", "sleep", "300"])
    assert camera._has_mic(FIFO_CONTAINER)
    out = camera.play([1], "beeps.wav", attach=False)
    assert out["ok"] and out["feed"]["audio"] and not out["feed"]["video"]
    time.sleep(1.0)
    began = time.monotonic()
    raw = docker.exec(FIFO_CONTAINER, ["sh", "-c", f"dd if={F.CONTAINER_MIC} bs=3840 count=150 2>/dev/null"],
                      binary=True, timeout=30).stdout
    took = time.monotonic() - began
    assert len(raw) == 150 * 3840 and any(raw)
    assert 2.0 < took < 4.5                              # 3 s of sound, minus what the pipe had buffered

    camera.mic([1], "mute")
    time.sleep(0.8)
    drain = f"timeout 1 dd if={F.CONTAINER_MIC} of=/dev/null bs=65536 2>/dev/null; true"
    docker.exec(FIFO_CONTAINER, ["sh", "-c", drain], timeout=20)
    count = f"timeout 2 dd if={F.CONTAINER_MIC} bs=3840 2>/dev/null | wc -c"
    assert docker.exec(FIFO_CONTAINER, ["sh", "-c", count], timeout=20).out == "0"
    camera.mic([1], "unmute")
    time.sleep(0.8)
    assert int(docker.exec(FIFO_CONTAINER, ["sh", "-c", count], timeout=20).out) > 100_000

    inode = os.stat(camera.layout.mic(1)).st_ino
    camera.stop([1])
    camera.play([1], "beeps.wav", attach=False)
    assert os.stat(camera.layout.mic(1)).st_ino == inode  # the mounted FIFO is never replaced


def _band(wav: Path, freq: int) -> float:
    err = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-ss", "2", "-i", str(wav), "-af",
                          f"bandpass=f={freq}:width_type=h:w=60,volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True, timeout=60).stderr
    for line in err.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split()[0])
    return -200.0


def test_android_records_the_feed(rig, tmp_path):
    camera, docker = rig
    image = os.environ.get("SF_LIVE_MEDIA_IMAGE", "")
    _local_image(docker, image)
    if not shutil.which("scrcpy") or not shutil.which("adb"):
        pytest.skip("scrcpy and adb are needed")
    r = camera.mgr.resolved(1)
    docker.create_and_start(["--name", ANDROID_CONTAINER, "--privileged", "--memory", "1536m", "--cpus", "2",
                             "-p", f"127.0.0.1:{ADB_PORT}:5555", *camera.extra_run_args(r), image,
                             "androidboot.redroid_width=480", "androidboot.redroid_height=854",
                             "androidboot.redroid_dpi=160"])
    adb = Adb.for_port(ADB_PORT)
    try:
        assert adb.wait_boot(timeout=180)
        # A privileged container starts with copies of the host's video nodes: drop them.
        docker.exec(ANDROID_CONTAINER, ["sh", "-c", 'for n in /dev/video*; do rm -f "$n"; done'], check=False)
        assert camera._has_driver(ANDROID_CONTAINER), "the image needs the camera overlay (native/camera)"
        assert camera._has_mic(ANDROID_CONTAINER)
        assert camera.play([1], "beeps.wav", attach=False)["ok"]
        wav = tmp_path / "mic.wav"
        subprocess.run(["scrcpy", f"--serial={adb.serial}", f"--port={A.free_port()}", "--no-video", "--no-control",
                        "--no-window", "--no-playback", "--audio-source=mic", "--audio-codec=raw",
                        f"--record={wav}", "--time-limit=8"], capture_output=True, timeout=60,
                       env={**os.environ, "SDL_AUDIODRIVER": "dummy", "SDL_VIDEODRIVER": "dummy"})
        assert wav.exists(), "scrcpy made no recording"
        beeps = [_band(wav, f) for f in (400, 800, 1200, 1600)]
        assert min(beeps) > -30 and _band(wav, 2500) < min(beeps) - 15, beeps
    finally:
        adb.disconnect()

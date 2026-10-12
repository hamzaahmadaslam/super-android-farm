"""The standalone helpers: microphone forwarder, restart loop, HAL patches."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from superfarm.paths import REPO_DIR


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


fwd = load(REPO_DIR / "scripts" / "camera" / "mic_forwarder.py")
loop = load(REPO_DIR / "scripts" / "camera" / "run_loop.py")
clip = load(REPO_DIR / "scripts" / "camera" / "make_test_clip.py")
hal = load(REPO_DIR / "native" / "camera" / "hal_patch.py")


# ------------------------------------------------------------------- pacer

def test_block_is_20_ms_and_fits_one_atomic_pipe_write():
    assert fwd.BLOCK == 48000 * 4 // 50 and fwd.BLOCK <= 4096 and fwd.BLOCK % fwd.FRAME == 0
    assert fwd.TICK == 0.02


def test_pacer_waits_for_a_cushion_then_sends_one_block_per_tick():
    p = fwd.Pacer()
    assert p.wait(0.0) is None                          # nothing to send: sleep until sound arrives
    p.feed(b"\x01" * (2 * fwd.BLOCK))
    assert p.due(0.0) is None                           # cushion not reached yet
    p.feed(b"\x01" * (2 * fwd.BLOCK))
    assert len(p.due(1.0)) == fwd.BLOCK                 # primed: first block at once
    assert p.due(1.001) is None                         # the next one only a tick later
    assert p.wait(1.001) == pytest.approx(0.019)
    assert len(p.due(1.02)) == fwd.BLOCK
    assert len(p.due(1.045)) == fwd.BLOCK               # woke late ...
    assert p.wait(1.045) == pytest.approx(0.015)        # ... so the next wait is shorter: no drift


def test_pacer_keeps_the_exact_rate_over_time():
    p = fwd.Pacer()
    sent, now = 0, 0.0
    for _ in range(1000):                               # 10 s of sound arriving in 10 ms pieces
        p.feed(b"\x00" * (fwd.BLOCK // 2))
        now += 0.01
        while p.due(now) is not None:
            sent += 1
    assert 495 <= sent <= 500                           # 50 blocks per second, minus the start-up cushion


def test_pacer_runs_dry_then_gathers_a_cushion_again():
    p = fwd.Pacer()
    p.feed(b"\x00" * (3 * fwd.BLOCK))
    assert [p.due(t) is not None for t in (0.0, 0.02, 0.04)] == [True, True, True]
    assert p.due(0.06) is None and p.primed is False
    p.feed(b"\x00" * fwd.BLOCK)
    assert p.due(0.08) is None                          # one block is not a cushion
    p.feed(b"\x00" * (2 * fwd.BLOCK))
    assert p.due(5.0) is not None


def test_pacer_skips_forward_on_a_frame_boundary_when_the_source_runs_ahead():
    p = fwd.Pacer()
    data = bytes(i % 251 for i in range(fwd.MAX_PENDING + 4 * 1000 + 2))
    p.feed(data)
    assert len(p.pending) <= fwd.RESUME + fwd.FRAME
    dropped = len(data) - len(p.pending)
    assert dropped % fwd.FRAME == 0                     # left and right channels never swap
    assert bytes(p.pending) == data[dropped:]


def test_same_file_detects_a_replaced_fifo(tmp_path):
    path = tmp_path / "mic"
    os.mkfifo(path)
    fd = fwd.open_target(str(path))
    assert fd is not None and fwd.same_file(fd, str(path))
    os.unlink(path)
    assert not fwd.same_file(fd, str(path))
    os.mkfifo(path)
    assert not fwd.same_file(fd, str(path))
    os.close(fd)
    assert fwd.open_target(str(tmp_path / "missing")) is None


def _drain(fd: int, seconds: float) -> bytes:
    got, deadline = bytearray(), time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            got += os.read(fd, 65536)
        except BlockingIOError:
            time.sleep(0.005)
    return bytes(got)


def test_forwarder_end_to_end_paces_drops_and_mutes(tmp_path):
    source, target, marker = tmp_path / "audio.fifo", tmp_path / "mic", tmp_path / "mute"
    os.mkfifo(source)
    os.mkfifo(target)
    proc = subprocess.Popen([sys.executable, str(REPO_DIR / "scripts" / "camera" / "mic_forwarder.py"),
                             "--source", str(source), "--target", str(target), "--mute-marker", str(marker)])
    writer = os.open(source, os.O_WRONLY)               # blocks until the forwarder has opened its end
    reader = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    try:
        pattern = bytes(range(256)) * (fwd.BLOCK // 256)            # 3840 bytes, recognisable
        # Half a second of sound written at once, while "no app is recording" (nobody reads):
        # the write must not block and the forwarder must keep running.
        os.write(writer, pattern * 25)
        time.sleep(0.3)
        assert proc.poll() is None
        got = _drain(reader, 0.5)
        assert len(got) >= 4 * fwd.BLOCK and len(got) % fwd.BLOCK == 0
        assert got[:fwd.BLOCK] == pattern                          # whole blocks, in order
        # Muted: sound keeps arriving, nothing comes out.
        marker.touch()
        time.sleep(fwd.MUTE_CHECK + 0.2)
        _drain(reader, 0.1)
        os.write(writer, pattern * 20)
        assert _drain(reader, 0.4) == b""
        # Unmuted: it flows again without restarting anything.
        marker.unlink()
        time.sleep(fwd.MUTE_CHECK + 0.2)
        os.write(writer, pattern * 20)
        assert len(_drain(reader, 0.6)) >= fwd.BLOCK
    finally:
        proc.terminate()
        assert proc.wait(timeout=5) == 0                           # SIGTERM is a clean exit
        os.close(writer)
        os.close(reader)


def test_forwarder_fails_cleanly_without_a_source(tmp_path):
    r = subprocess.run([sys.executable, str(REPO_DIR / "scripts" / "camera" / "mic_forwarder.py"),
                        "--source", str(tmp_path / "none"), "--target", str(tmp_path / "mic")],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "mic_forwarder:" in r.stderr


# ---------------------------------------------------------------- run_loop

def test_loop_gives_up_on_a_command_that_keeps_dying(capsys):
    assert loop.Loop(["false"], delay=0.0, give_up=3).run() == 1
    assert "3 times in a row; giving up" in capsys.readouterr().err


def test_loop_restarts_until_stopped(tmp_path):
    counter = tmp_path / "count"
    runner = loop.Loop(["sh", "-c", f'echo x >> "{counter}"'], delay=0.0, give_up=0)
    original = runner._sleep

    def sleep_then_maybe_stop(seconds):
        if counter.read_text().count("x") >= 4:
            runner.stop()
        original(seconds)

    runner._sleep = sleep_then_maybe_stop
    assert runner.run() == 0
    assert counter.read_text().count("x") == 4


def test_loop_reports_a_missing_program(capsys):
    assert loop.Loop(["/nonexistent/program"], delay=0.0, give_up=0).run() == 127
    assert "cannot start" in capsys.readouterr().err


def test_loop_as_a_process_stops_its_command_on_sigterm(tmp_path):
    proc = subprocess.Popen([sys.executable, str(REPO_DIR / "scripts" / "camera" / "run_loop.py"),
                             "--give-up", "0", "--", "sh", "-c", f'echo $$ > "{tmp_path}/pid"; exec sleep 60'])
    deadline = time.monotonic() + 10
    while not (tmp_path / "pid").exists() or not (tmp_path / "pid").read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    child = int((tmp_path / "pid").read_text())
    proc.terminate()
    assert proc.wait(timeout=10) == 0
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_loop_cli_needs_a_command():
    r = subprocess.run([sys.executable, str(REPO_DIR / "scripts" / "camera" / "run_loop.py"), "--give-up", "1"],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "no command given" in r.stderr


# ---------------------------------------------------------- make_test_clip

def test_test_clip_is_portrait_asymmetric_and_beeps_instead_of_humming():
    argv = clip.command("/tmp/x.mp4", 20, ffmpeg="ffmpeg")
    assert argv[0] == "ffmpeg" and argv[-1] == "/tmp/x.mp4" and argv[argv.index("-t") + 1] == "20"
    video = argv[argv.index("-i") + 1]
    assert "size=720x1280" in video                                 # 9:16 portrait
    assert "drawbox=x=0:y=0" in video and "color=red" in video      # a mark on one edge shows mirroring
    audio = [a for a in argv if a.startswith("aevalsrc")][0]
    assert "s=48000" in audio and "c=stereo" in audio
    # Noise suppression deletes a steady tone: the pitch has to keep changing.
    expr = clip.beep_expression()
    assert "mod(floor(3*t),4)" in expr and "400+400*" in expr and "lt(mod(3*t,1),0.6)" in expr
    assert f"'{expr}'" in audio                                     # quoted: its commas are not filter separators


def test_test_clip_cli_validates_length():
    r = subprocess.run([sys.executable, str(REPO_DIR / "scripts" / "camera" / "make_test_clip.py"), "x.mp4", "--seconds", "0"],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "--seconds" in r.stderr


# --------------------------------------------------------------- hal_patch

def _facing_code(value: int, slot: int = 0x2D) -> bytes:
    """The compiled shape of `facing = <value>; UPDATE(ANDROID_LENS_FACING, &facing, 1)`."""
    return bytes([0xC6, 0x44, 0x24, slot, value, 0x8D, 0x44, 0x24, slot, 0x6A, 0x01, 0x50]) + hal.LENS_FACING_TAG_PUSH


def test_lens_facing_external_becomes_front():
    blob = b"\x90" * 40 + _facing_code(2) + b"\xe8\xec\x30\x03\x00" + b"\x90" * 40
    out = hal.patch_lens_facing(blob)
    assert len(out) == len(blob)
    diff = [i for i in range(len(blob)) if blob[i] != out[i]]
    assert diff == [44] and out[44] == 0                # exactly one byte: the immediate
    assert hal.patch_lens_facing(out) == out            # running it again changes nothing


def test_lens_facing_refuses_anything_it_does_not_recognise():
    with pytest.raises(hal.PatchError, match="found 0"):
        hal.patch_lens_facing(b"\x00" * 500)
    with pytest.raises(hal.PatchError, match="found 0"):      # the tag is pushed, but not after that store
        hal.patch_lens_facing(b"\x90" * 20 + hal.LENS_FACING_TAG_PUSH + b"\x90" * 20)
    with pytest.raises(hal.PatchError, match="found 2"):
        hal.patch_lens_facing(_facing_code(2) + b"\x90" * 8 + _facing_code(2, slot=0x31))
    with pytest.raises(hal.PatchError, match="expected 2"):   # BACK (1): somebody else's patch, leave it
        hal.patch_lens_facing(_facing_code(1))
    mismatched = bytearray(_facing_code(2))
    mismatched[8] = 0x40                                      # lea of a different stack slot: not our pattern
    with pytest.raises(hal.PatchError):
        hal.patch_lens_facing(bytes(mismatched))


STOCK_CAMERA_CONFIG = """<ExternalCamera>
    <Provider>
        <ignore>
            <!-- <id>0</id> -->
        </ignore>
    </Provider>
    <Device>
        <!-- Max JPEG buffer size in bytes-->
        <MaxJpegBufferSize bytes="3145728"/>
        <NumVideoBuffers count="4"/>
    </Device>
</ExternalCamera>
"""


def test_camera_config_gets_a_sideways_front_sensor():
    out = hal.patch_camera_config(STOCK_CAMERA_CONFIG)
    lines = out.splitlines()
    at = lines.index("    <Device>")
    assert lines[at + 2] == '        <Orientation degree="270"/>'
    assert lines[at + 3] == "        <!-- Max JPEG buffer size in bytes-->"
    assert hal.patch_camera_config(out) == out
    assert '<Orientation degree="90"/>' in hal.patch_camera_config(out, 90)
    import xml.dom.minidom
    xml.dom.minidom.parseString(out)                    # still well-formed
    with pytest.raises(hal.PatchError):
        hal.patch_camera_config("<ExternalCamera/>")


STOCK_AUDIO_POLICY = """<module name="primary" halVersion="2.0">
    <mixPorts>
        <mixPort name="primary output" role="source" flags="AUDIO_OUTPUT_FLAG_PRIMARY">
            <profile name="" format="AUDIO_FORMAT_PCM_16_BIT"
                     samplingRates="44100" channelMasks="AUDIO_CHANNEL_OUT_STEREO"/>
        </mixPort>
        <mixPort name="primary input" role="sink">
            <profile name="" format="AUDIO_FORMAT_PCM_16_BIT"
                     samplingRates="8000,16000" channelMasks="AUDIO_CHANNEL_IN_MONO"/>
        </mixPort>
   </mixPorts>
</module>
"""


def test_audio_policy_declares_the_48k_stereo_microphone_only():
    out = hal.patch_audio_policy(STOCK_AUDIO_POLICY)
    assert 'samplingRates="48000" channelMasks="AUDIO_CHANNEL_IN_STEREO"' in out
    assert 'samplingRates="44100" channelMasks="AUDIO_CHANNEL_OUT_STEREO"' in out   # the speaker is untouched
    assert "8000,16000" not in out and "IN_MONO" not in out
    assert hal.patch_audio_policy(out) == out
    with pytest.raises(hal.PatchError):
        hal.patch_audio_policy("<module/>")


def test_fps_property_is_renamed_in_place():
    blob = b"\x00abc\x00ro.boot.redroid_fps\x00ro.boot.redroid_width\x00"
    out = hal.patch_fps_property(blob)
    assert out == b"\x00abc\x00ro.farm.redroid_fps\x00ro.boot.redroid_width\x00" and len(out) == len(blob)
    assert hal.patch_fps_property(out) == out
    with pytest.raises(hal.PatchError, match="found it 0 times"):
        hal.patch_fps_property(b"nothing here")
    with pytest.raises(hal.PatchError, match="found it 2 times"):
        hal.patch_fps_property(blob + blob)


def test_build_prop_gets_exactly_one_fps_line():
    out = hal.patch_build_prop("ro.hardware=redroid\n# end of file\n", 25)
    assert out.endswith("ro.farm.redroid_fps=25\n") and out.startswith("ro.hardware=redroid\n")
    again = hal.patch_build_prop(out, 15)
    assert again.count("ro.farm.redroid_fps=") == 1 and again.endswith("ro.farm.redroid_fps=15\n")
    assert again.count("# Display frame rate") == 1
    with pytest.raises(hal.PatchError):
        hal.patch_build_prop("", 0)


def test_hal_patch_cli(tmp_path):
    script = str(REPO_DIR / "native" / "camera" / "hal_patch.py")
    (tmp_path / "in.xml").write_text(STOCK_AUDIO_POLICY)
    ok = subprocess.run([sys.executable, script, "audio-policy", str(tmp_path / "in.xml"), str(tmp_path / "out.xml")],
                        capture_output=True, text=True, timeout=20)
    assert ok.returncode == 0 and "48000" in (tmp_path / "out.xml").read_text()
    (tmp_path / "bad.so").write_bytes(b"\x00" * 64)
    bad = subprocess.run([sys.executable, script, "lens-facing", str(tmp_path / "bad.so"), str(tmp_path / "o.so")],
                         capture_output=True, text=True, timeout=20)
    assert bad.returncode == 1 and "hal_patch: lens-facing:" in bad.stderr and not (tmp_path / "o.so").exists()


# ------------------------------------------------------------ native files

def test_native_sources_are_present_and_free_of_private_data():
    folder = REPO_DIR / "native" / "camera"
    for name in ("gralloc_shim.c", "mic_shim.c", "build_shim.sh", "extract_hal.sh", "hal_patch.py", "README.md"):
        text = (folder / name).read_text()
        assert text.strip(), name
        lowered = text.lower()
        for word in ("/home/", "/run/media", "/mnt/", "c:\\"):
            assert word not in lowered, f"{name} mentions {word}"
    assert '#define MIC_PATH "/dev/farm_mic"' in (folder / "mic_shim.c").read_text()


def test_shell_scripts_parse():
    for name in ("build_shim.sh", "extract_hal.sh"):
        r = subprocess.run(["bash", "-n", str(REPO_DIR / "native" / "camera" / name)], capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, r.stderr

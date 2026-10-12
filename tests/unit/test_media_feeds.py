"""Command construction, slot arithmetic and process/file bookkeeping of the virtual camera."""
from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from superfarm.config import Config
from superfarm.media import feeds as F


def settings(**over) -> F.VideoSettings:
    base = dict(width=640, height=360, fps=20, quality=5, mirror=False)
    return F.VideoSettings(**{**base, **over})


# ----------------------------------------------------------------- commands

def test_settings_come_from_config():
    config = Config.model_validate({"media": {"camera": {"width": 1280, "height": 720, "fps": 30,
                                                          "quality": 3, "mirror": True}}})
    s = F.VideoSettings.from_config(config)
    assert (s.width, s.height, s.fps, s.quality, s.mirror, s.rotate) == (1280, 720, 30, 3, True, 1)
    assert s.sysfs_format == "MJPG:1280x720@"


def test_picture_is_mirrored_by_default():
    # A live broadcast test showed viewers the source reversed unless the feed flips it.
    assert Config().media.camera.mirror is True
    assert ",hflip,transpose=1," in F.video_filter(F.VideoSettings.from_config(Config()))


def test_video_filter_stores_portrait_picture_rotated():
    # Sensor frame 640x360 landscape; the picture inside is 360x640 portrait, then rotated.
    assert F.video_filter(settings()) == (
        "fps=20,scale=360:640:force_original_aspect_ratio=increase:flags=fast_bilinear,"
        "crop=360:640,transpose=1,format=yuvj420p")


def test_video_filter_mirror_flips_before_rotating():
    parts = F.video_filter(settings(mirror=True)).split(",")
    assert parts.index("hflip") == parts.index("transpose=1") - 1


@pytest.mark.parametrize("raw,escaped", [
    ("/run/camera/1/video.fifo", "/run/camera/1/video.fifo"),
    ("/my farm/a|b", "/my\\ farm/a\\|b"),
    ("/x[1]'q,z\\", "/x\\[1\\]\\'q\\,z\\\\"),
])
def test_tee_escape(raw, escaped):
    assert F.tee_escape(raw) == escaped


def test_tee_outputs_ignore_a_failing_android():
    out = F.tee_outputs(["/r/1/video.fifo", "/r/2/video.fifo"], "mjpeg")
    assert out == "[f=mjpeg:onfail=ignore]/r/1/video.fifo|[f=mjpeg:onfail=ignore]/r/2/video.fifo"


def test_feed_command_one_decode_fans_out_picture_and_sound():
    argv = F.feed_command(ffmpeg="ffmpeg", main_input=F.file_input(Path("/lib/clip.mp4")), sound_input=None,
                          video_fifos=[Path("/r/1/video.fifo"), Path("/r/2/video.fifo")],
                          audio_fifos=[Path("/r/1/audio.fifo"), Path("/r/2/audio.fifo")], settings=settings())
    assert argv[:4] == ["ffmpeg", "-nostdin", "-loglevel", "error"]
    assert argv.count("-i") == 1                                   # one input = one decode
    assert argv[4:9] == ["-re", "-stream_loop", "-1", "-i", "/lib/clip.mp4"]
    video = argv[argv.index("-map"):]
    assert video[:2] == ["-map", "0:v:0"]
    assert ["-c:v", "mjpeg", "-q:v", "5"] == argv[argv.index("-c:v"):argv.index("-c:v") + 4]
    tees = [argv[i + 1] for i, a in enumerate(argv) if a == "tee"]
    assert tees == [
        "[f=mjpeg:onfail=ignore]/r/1/video.fifo|[f=mjpeg:onfail=ignore]/r/2/video.fifo",
        "[f=s16le:onfail=ignore]/r/1/audio.fifo|[f=s16le:onfail=ignore]/r/2/audio.fifo"]
    # The microphone shim wants 48 kHz stereo 16-bit.
    sound = argv[argv.index("0:a:0") - 1:]
    assert sound[:10] == ["-map", "0:a:0", "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "-flush_packets"]


def test_feed_command_without_sound_has_no_audio_output():
    argv = F.feed_command(ffmpeg="ffmpeg", main_input=F.file_input(Path("/lib/clip.mp4")), sound_input=None,
                          video_fifos=[Path("/r/1/video.fifo")], audio_fifos=[], settings=settings())
    assert "pcm_s16le" not in argv and argv.count("tee") == 1 and "-an" in argv


def test_feed_command_sound_from_a_second_file():
    argv = F.feed_command(ffmpeg="ffmpeg", main_input=F.file_input(Path("/lib/clip.mp4")),
                          sound_input=F.file_input(Path("/lib/voice.mp3")),
                          video_fifos=[Path("/r/1/video.fifo")], audio_fifos=[Path("/r/1/audio.fifo")],
                          settings=settings())
    assert argv.count("-i") == 2
    assert "1:a:0" in argv and "0:a:0" not in argv


def test_feed_command_second_input_is_dropped_when_no_sound_is_wanted():
    argv = F.feed_command(ffmpeg="ffmpeg", main_input=F.file_input(Path("/lib/clip.mp4")),
                          sound_input=F.file_input(Path("/lib/voice.mp3")),
                          video_fifos=[Path("/r/1/video.fifo")], audio_fifos=[], settings=settings())
    assert argv.count("-i") == 1


def test_feed_command_sound_only():
    argv = F.feed_command(ffmpeg="ffmpeg", main_input=F.file_input(Path("/lib/voice.mp3")), sound_input=None,
                          video_fifos=[], audio_fifos=[Path("/r/1/audio.fifo")], settings=settings())
    assert "mjpeg" not in argv and "0:a:0" in argv


def test_feed_command_needs_an_output():
    with pytest.raises(ValueError):
        F.feed_command(ffmpeg="ffmpeg", main_input=["-i", "x"], sound_input=None, video_fifos=[],
                       audio_fifos=[], settings=settings())


def test_rtmp_input_listens_on_the_configured_address():
    config = Config.model_validate({"media": {"camera": {"rtmp_bind": "127.0.0.1", "rtmp_port": 17201}}})
    url = F.rtmp_url(config)
    assert url == "rtmp://127.0.0.1:17201/live/camera"
    assert F.rtmp_input(url)[-4:] == ["-listen", "1", "-i", url]
    v6 = Config.model_validate({"media": {"camera": {"rtmp_bind": "::1", "rtmp_port": 17201}}})
    assert F.rtmp_url(v6) == "rtmp://[::1]:17201/live/camera"


def test_copier_copies_without_reencoding_into_the_device():
    argv = F.copier_command(ffmpeg="ffmpeg", fifo=Path("/r/1/video.fifo"), device="/dev/video105", settings=settings())
    assert argv[argv.index("-i") + 1] == "/r/1/video.fifo"
    assert argv[-7:] == ["-c", "copy", "-flush_packets", "1", "-f", "v4l2", "/dev/video105"]
    assert argv[argv.index("-framerate") + 1] == "20"


def test_prime_sends_black_frames_of_the_sensor_size_and_is_bounded():
    argv = F.prime_command(ffmpeg="ffmpeg", fifo=Path("/r/1/video.fifo"), settings=settings())
    assert argv[:2] == ["timeout", "20"]
    assert "color=black:s=640x360:r=20" in argv
    assert argv[argv.index("-frames:v") + 1] == "90" and argv[-1] == "/r/1/video.fifo"


def test_helper_scripts_exist_and_commands_point_at_them():
    cmd = F.forwarder_command(source=Path("/r/1/audio.fifo"), target=Path("/r/1/mic"), mute_marker=Path("/r/1/mute"))
    assert cmd[0] == sys.executable and Path(cmd[1]).name == "mic_forwarder.py" and Path(cmd[1]).is_file()
    assert cmd[2:] == ["--source", "/r/1/audio.fifo", "--target", "/r/1/mic", "--mute-marker", "/r/1/mute"]
    loop = F.supervised(["ffmpeg", "-i", "x"], give_up=5)
    assert Path(loop[1]).name == "run_loop.py" and loop[2:] == ["--delay", "1.0", "--give-up", "5", "--", "ffmpeg", "-i", "x"]


def test_script_path_explains_a_missing_helper():
    with pytest.raises(FileNotFoundError, match="scripts/camera"):
        F.script_path("nope.py")


# ------------------------------------------------------------------ devices

def test_device_helpers_read_sysfs_not_the_device(tmp_path):
    sysfs = tmp_path / "sys"
    node = sysfs / "class" / "video4linux" / "video105"
    node.mkdir(parents=True)
    (node / "name").write_text(f"{F.CARD_LABEL} 5\n")
    (node / "format").write_text("MJPG:640x360@20\n")
    assert F.device_path(105) == "/dev/video105" and F.device_number("/dev/video105") == 105
    assert F.device_name("/dev/video105", sysfs) == "Super Farm Camera 5"
    assert F.device_is_ours("/dev/video105", sysfs)
    (node / "name").write_text("superfarm-cam-5\n")       # created through module parameters
    assert F.device_is_ours("/dev/video105", sysfs)
    (node / "name").write_text("Farm Camera 5\n")         # another farm on the same machine
    assert not F.device_is_ours("/dev/video105", sysfs)
    assert F.device_format("/dev/video105", sysfs) == "MJPG:640x360@20"
    (node / "name").write_text("HD Webcam\n")
    assert not F.device_is_ours("/dev/video105", sysfs)
    assert F.device_name("/dev/video9", sysfs) == "" and F.device_format("/dev/video9", sysfs) is None
    with pytest.raises(ValueError):
        F.device_number("/dev/sda")


def test_slots_ids_inside_the_pool_map_straight_onto_their_number(tmp_path):
    slots = F.Slots(tmp_path / "slots.json", base=100, count=32)
    assert slots.numbers()[0] == 101 and slots.numbers()[-1] == 132
    assert slots.number(slots.assign(5)) == 105          # Android 5 -> /dev/video105
    assert slots.number(slots.assign(1)) == 101 and slots.number(slots.assign(32)) == 132
    assert slots.assign(5) == 4                          # stable
    assert slots.lookup(5) == 4 and slots.lookup(6) is None


def test_slots_other_ids_take_free_devices_from_the_top(tmp_path):
    slots = F.Slots(tmp_path / "slots.json", base=100, count=4)
    assert slots.number(slots.assign(4000)) == 104       # far beyond any device number
    assert slots.number(slots.assign(0)) == 103
    assert slots.number(slots.assign(4)) == 102          # its own number is taken: next free one
    assert slots.number(slots.assign(1)) == 101
    assert sorted(slots.table().values()) == [0, 1, 2, 3]


def test_slots_survive_a_restart(tmp_path):
    F.Slots(tmp_path / "slots.json", 100, 8).assign(999)
    assert F.Slots(tmp_path / "slots.json", 100, 8).lookup(999) == 7


def test_slots_full_pool_evicts_only_instances_that_no_longer_need_one(tmp_path):
    slots = F.Slots(tmp_path / "slots.json", 100, 2)
    slots.assign(1), slots.assign(2)
    with pytest.raises(RuntimeError, match="media.camera.slots"):
        slots.assign(7, in_use=lambda other: True)
    assert slots.assign(7, in_use=lambda other: other != 2) == 1
    assert slots.lookup(2) is None and slots.lookup(1) == 0


def test_slots_release_and_shrunk_pool(tmp_path):
    slots = F.Slots(tmp_path / "slots.json", 100, 8)
    slots.assign(8), slots.assign(3)
    slots.release(8)
    assert slots.table() == {3: 2}
    assert F.Slots(tmp_path / "slots.json", 100, 2).table() == {}      # slot 2 no longer exists
    with pytest.raises(RuntimeError):
        F.Slots(tmp_path / "slots.json", 100, 0).assign(1)
    with pytest.raises(ValueError):
        slots.assign(-1)


# ---------------------------------------------------------------- processes

def test_launcher_runs_helpers_in_their_own_session_and_stops_them(tmp_path):
    launcher = F.Launcher()
    ref = launcher.start(["sleep", "30"], log=tmp_path / "logs" / "x.log")
    try:
        assert launcher.alive(ref)
        assert os.getsid(ref.pid) == ref.pid != os.getsid(0)
        assert ref.ticks > 0
    finally:
        launcher.stop(ref)
    assert not launcher.alive(ref)


def test_launcher_hard_stop_and_exited_child_is_not_alive(tmp_path):
    launcher = F.Launcher()
    quick = launcher.start(["true"], log=tmp_path / "a.log")
    deadline = time.monotonic() + 5
    while launcher.alive(quick) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not launcher.alive(quick)                 # a zombie does not count as running
    ref = launcher.start(["sleep", "30"], log=tmp_path / "b.log")
    launcher.stop(ref, hard=True)
    assert not launcher.alive(ref)
    launcher.stop(ref)                               # stopping twice is fine
    launcher.stop(None)


def test_launcher_never_trusts_a_reused_pid(tmp_path):
    launcher = F.Launcher()
    ref = launcher.start(["sleep", "30"], log=tmp_path / "x.log")
    try:
        stranger = F.ProcRef(ref.pid, ref.ticks + 12345)   # same pid, different start time
        assert not launcher.alive(stranger)
        launcher.stop(stranger, hard=True)                 # must not touch the real process
        assert launcher.alive(ref)
    finally:
        launcher.stop(ref, hard=True)


def test_launcher_passes_file_descriptors_and_writes_the_log(tmp_path):
    launcher = F.Launcher()
    fifo = F.fresh_fifo(tmp_path / "v.fifo")
    keep = os.open(fifo, os.O_RDWR)
    try:
        ref = launcher.start(["sh", "-c", f"ls /proc/self/fd/{keep} >/dev/null && echo has-fd; echo oops >&2"],
                             log=tmp_path / "x.log", pass_fds=[keep])
    finally:
        os.close(keep)
    deadline = time.monotonic() + 5
    while launcher.alive(ref) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert F.tail(tmp_path / "x.log") == "has-fd\noops"


def test_launcher_rejects_shell_strings_and_reports_a_missing_program(tmp_path):
    launcher = F.Launcher()
    with pytest.raises(TypeError):
        launcher.start("sleep 1", log=tmp_path / "x.log")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="command not found"):
        launcher.start(["/nonexistent/program"], log=tmp_path / "x.log")


def test_proc_ref_round_trip_and_rejects_garbage():
    ref = F.ProcRef(4321, 99)
    assert F.ProcRef.from_dict(ref.to_dict()) == ref
    for bad in (None, {}, {"pid": "1"}, {"pid": 1, "ticks": 5}, {"pid": 0, "ticks": 1}, []):
        assert F.ProcRef.from_dict(bad) is None
    assert F.proc_stat(os.getpid())[0] in "RS"
    assert F.proc_stat(2 ** 22 + 7) is None


# -------------------------------------------------------------------- files

def test_layout_paths(tmp_path):
    lay = F.Layout(tmp_path / "camera")
    assert lay.mic(7) == tmp_path / "camera" / "7" / "mic"
    assert lay.video_fifo(7).name == "video.fifo" and lay.audio_fifo(7).name == "audio.fifo"
    assert lay.feed_file("ab12") == tmp_path / "camera" / "feeds" / "ab12.json"
    assert lay.instance_ids() == []
    lay.instance_dir(3).mkdir(parents=True)
    lay.instance_dir(12).mkdir()
    lay.feeds_dir.mkdir()
    assert lay.instance_ids() == [3, 12]
    with pytest.raises(ValueError):
        lay.mic("../../etc")  # type: ignore[arg-type]


def test_ensure_fifo_keeps_an_existing_fifo(tmp_path):
    path = F.ensure_fifo(tmp_path / "1" / "mic", 0o644)
    first = os.stat(path)
    assert stat.S_ISFIFO(first.st_mode) and stat.S_IMODE(first.st_mode) == 0o644
    os.chmod(path, 0o600)
    again = os.stat(F.ensure_fifo(path, 0o644))
    assert again.st_ino == first.st_ino                 # same inode: a mounted FIFO stays valid
    assert stat.S_IMODE(again.st_mode) == 0o644


def test_ensure_fifo_refuses_a_folder_docker_left_behind(tmp_path):
    (tmp_path / "mic").mkdir()
    with pytest.raises(RuntimeError, match="not a FIFO"):
        F.ensure_fifo(tmp_path / "mic")


def test_fresh_fifo_replaces(tmp_path):
    first = os.stat(F.fresh_fifo(tmp_path / "v.fifo")).st_ino
    keep = os.open(tmp_path / "v.fifo", os.O_RDWR)      # hold the old inode so its number is not reused
    try:
        second = os.stat(F.fresh_fifo(tmp_path / "v.fifo"))
    finally:
        os.close(keep)
    assert second.st_ino != first and stat.S_ISFIFO(second.st_mode)


def test_feed_records_round_trip(tmp_path):
    lay = F.Layout(tmp_path / "camera")
    feed = F.Feed(id="ab12", source="clip.mp4", sound="source", ids=[3, 1], video=True, audio=True,
                  pid=4321, ticks=7, started=123.5)
    F.save_feed(lay, feed)
    (lay.feeds_dir / "junk.json").write_text("{not json")
    (lay.feeds_dir / "bad.json").write_text('{"id": "x", "source": "y", "ids": ["zz"]}')
    loaded = F.load_feeds(lay)
    assert len(loaded) == 1 and loaded[0].ids == [1, 3] and loaded[0].ref == F.ProcRef(4321, 7)
    F.drop_feed(lay, "ab12")
    F.drop_feed(lay, "ab12")
    assert [f.id for f in F.load_feeds(lay)] == []


def test_tail_of_a_missing_log_is_empty(tmp_path):
    assert F.tail(tmp_path / "none.log") == ""
    (tmp_path / "a.log").write_text("\n".join(f"line {i}" for i in range(20)))
    assert F.tail(tmp_path / "a.log", lines=2) == "line 18\nline 19"

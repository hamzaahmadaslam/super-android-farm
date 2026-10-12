"""Camera policy, driven with fakes: no Docker, no ffmpeg, no video devices."""
from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from superfarm.config import Config
from superfarm.core.models import InstanceSpec, resolve
from superfarm.media import camera as C
from superfarm.media import feeds as F
from superfarm.paths import Paths
from superfarm.util.proc import CommandError, Result

FORMAT = "MJPG:640x360@20"


class FakeLauncher:
    def __init__(self):
        self.started: list[tuple[list[str], F.ProcRef, tuple[int, ...]]] = []
        self.stopped: list[tuple[int, bool]] = []
        self.live: set[int] = set()
        self._next = 1000

    def start(self, argv, *, log, pass_fds=()):
        self._next += 1
        ref = F.ProcRef(self._next, 1)
        self.live.add(ref.pid)
        for fd in pass_fds:
            os.fstat(fd)                      # must be a valid, open descriptor at start
        self.started.append((list(argv), ref, tuple(pass_fds)))
        return ref

    def alive(self, ref):
        return ref is not None and ref.pid in self.live

    def stop(self, ref, *, grace=2.0, hard=False):
        if ref is not None and ref.pid in self.live:
            self.live.discard(ref.pid)
            self.stopped.append((ref.pid, hard))

    # helpers for assertions
    def kinds(self) -> list[str]:
        out = []
        for argv, _, _ in self.started:
            joined = " ".join(argv)
            out.append("feed" if "run_loop.py" in joined else "forwarder" if "mic_forwarder.py" in joined else "copier")
        return out

    def feeds(self) -> list[list[str]]:
        return [argv for argv, _, _ in self.started if any("run_loop.py" in a for a in argv)]

    def live_of(self, kind: str) -> list[F.ProcRef]:
        return [ref for (_, ref, _), k in zip(self.started, self.kinds(), strict=True) if k == kind and ref.pid in self.live]


class FakeDocker:
    def __init__(self):
        self.running_names: set[str] = set()
        self.existing: set[str] = set()
        self.driver = True
        self.mic = True
        self.counts: list[int] = [0, 1]       # successive answers of the camera-count check
        self.node = ""                        # what `stat /dev/video0` prints inside the Android
        self.execs: list[tuple[str, list[str]]] = []
        self.copies: list[tuple[str, str]] = []

    def running(self, name):
        return name in self.running_names

    def exists(self, name):
        return name in self.existing or name in self.running_names

    def run(self, argv, *, timeout=120, check=True, input=None, binary=False):
        assert argv[:2] == ["cp", "-"] and binary and isinstance(input, bytes)
        self.copies.append((argv[2], input))
        return Result(0, b"", "")

    def exec(self, name, argv, *, timeout=60, check=True, input=None, user=None, binary=False):
        self.execs.append((name, list(argv)))
        text = " ".join(argv)
        if ".rc ]" in text:
            return Result(0 if self.driver else 1, "", "")
        if "-p /dev/farm_mic" in text:
            return Result(0 if self.mic else 1, "", "")
        if "dumpsys media.camera" in text:
            count = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
            return Result(0, f"{count}\n", "")
        if argv[0] == "stat":
            return Result(0 if self.node else 1, self.node + "\n", "")
        if "mknod" in text:
            self.node = f"{int(argv[-2]):x}:{int(argv[-1]):x}"
            return Result(0, "", "")
        return Result(0, "", "")


class FakeMgr:
    def __init__(self, config: Config, camera_off: set[int] = frozenset()):
        self.config, self.camera_off = config, set(camera_off)
        self.post_boot_hooks: list = []
        self.extra_run_args_providers: list = []
        self.known = set(range(0, 10000))

    def resolved(self, instance_id: int):
        if instance_id not in self.known:
            raise KeyError(f"no instance {instance_id}")
        return resolve(InstanceSpec(id=instance_id, camera=instance_id not in self.camera_off), self.config)

    def list_status(self):
        return [{"id": i} for i in (1, 2, 3, 4)]


class Host:
    """A throwaway farm home plus fake /sys and /dev trees."""

    def __init__(self, tmp_path: Path, config: Config | None = None, devices: int = 8):
        self.tmp = tmp_path
        self.config = config or Config.model_validate({"media": {"camera": {"enabled": True, "slots": devices}}})
        self.paths = Paths(tmp_path / "home").ensure()
        self.sys, self.dev = tmp_path / "sys", tmp_path / "dev"
        self.dev.mkdir()
        (self.sys / "module" / "v4l2loopback").mkdir(parents=True)
        base = self.config.media.camera.device_base
        for k in range(1, devices + 1):
            self.add_device(base + k, f"{F.CARD_LABEL} {k}")
        self.launcher, self.docker, self.mgr = FakeLauncher(), FakeDocker(), FakeMgr(self.config)
        self.ran: list[list[str]] = []
        self.sleeps: list[float] = []
        self.probe = {"clip.mp4": ("video", "audio"), "silent.mp4": ("video",), "voice.mp3": ("audio",),
                      "other.mp4": ("video", "audio"), "cover.mp3": ("cover", "audio")}
        self.prime_works = True
        self.camera = C.Camera(self.config, self.paths, self.docker, self.mgr, launcher=self.launcher,
                               runner=self.run, sleep=self.sleeps.append,
                               stat_device=lambda device: (81, F.device_number(device)),
                               sys_root=self.sys, dev_root=self.dev)

    def add_device(self, number: int, name: str, fmt: str = "") -> None:
        node = self.sys / "class" / "video4linux" / f"video{number}"
        node.mkdir(parents=True, exist_ok=True)
        (node / "name").write_text(name + "\n")
        (node / "format").write_text(fmt + "\n" if fmt else "")
        (self.dev / f"video{number}").write_text("")

    def format_of(self, number: int) -> str:
        return (self.sys / "class" / "video4linux" / f"video{number}" / "format").read_text().strip()

    def media(self, *names: str) -> None:
        for name in names:
            (self.paths.media / name).write_bytes(b"x")

    def run(self, argv, *, timeout=60, check=True, **_):
        argv = [str(a) for a in argv]
        self.ran.append(argv)
        tool = os.path.basename(argv[0])
        if tool == "ffprobe":
            kinds = self.probe.get(os.path.basename(argv[-1]))
            if kinds is None:
                raise CommandError(argv, 1, "", "Invalid data found when processing input")
            streams = [{"codec_type": "video", "disposition": {"attached_pic": 1}} if k == "cover"
                       else {"codec_type": k, "disposition": {"attached_pic": 0}} for k in kinds]
            return Result(0, json.dumps({"streams": streams}), "")
        if tool == "timeout":                       # the black priming frames
            instance_id = int(Path(argv[-1]).parent.name)
            if self.prime_works:
                number = F.device_number(self.camera.device(instance_id))
                (self.sys / "class" / "video4linux" / f"video{number}" / "format").write_text(FORMAT + "\n")
            return Result(0, "", "")
        return Result(0, "", "")


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "which", lambda name: f"/usr/bin/{name}")
    return Host(tmp_path)


def run_names(host: Host, *ids: int) -> None:
    for i in ids:
        host.docker.running_names.add(f"sf-android-{i}")


# ------------------------------------------------------------ host commands

def camera_config(**over) -> Config:
    return Config.model_validate({"media": {"camera": {"enabled": True, "slots": 3, "device_base": 120, **over}}})


def test_host_commands_create_missing_devices_with_group_access(tmp_path):
    cmds = C.host_device_commands(camera_config(), ctl="/usr/local/bin/v4l2loopback-ctl",
                                  sys_root=tmp_path / "sys", dev_root=Path("/dev"))
    assert cmds[0] == ["modprobe", "videodev"]
    assert cmds[1] == ["modprobe", "v4l2loopback", "devices=0", "max_buffers=8"]
    assert cmds[2] == ["/usr/local/bin/v4l2loopback-ctl", "add", "-n", "Super Farm Camera 1", "-x", "1", "-b", "8", "/dev/video121"]
    assert [c[-1] for c in cmds[2:5]] == ["/dev/video121", "/dev/video122", "/dev/video123"]   # Android N -> base + N
    assert cmds[5] == ["udevadm", "settle", "--timeout=10"]
    assert cmds[6] == ["chgrp", "video", "/dev/video121", "/dev/video122", "/dev/video123"]
    assert cmds[7] == ["chmod", "0660", "/dev/video121", "/dev/video122", "/dev/video123"]
    assert all(isinstance(c, list) and all(isinstance(a, str) for a in c) for c in cmds)
    assert C.host_device_numbers(camera_config()) == [121, 122, 123]


def test_host_commands_are_idempotent_and_respect_the_group(tmp_path):
    host = Host(tmp_path, camera_config(), devices=2)        # 121 and 122 exist and are ours, module loaded
    cmds = C.host_device_commands(host.config, group="docker", ctl="/x/v4l2loopback-ctl",
                                  sys_root=host.sys, dev_root=host.dev)
    adds = [c for c in cmds if "add" in c]
    assert len(adds) == 1 and adds[0][-1] == str(host.dev / "video123") and "Super Farm Camera 3" in adds[0]
    assert not any(c[:2] == ["modprobe", "v4l2loopback"] for c in cmds)
    assert cmds[-2][:2] == ["chgrp", "docker"] and len(cmds[-2]) == 5


def test_host_commands_nothing_when_camera_is_off(tmp_path):
    assert C.host_device_commands(Config(), sys_root=tmp_path) == []
    assert C.host_device_commands(camera_config(slots=0), sys_root=tmp_path) == []


def test_host_commands_refuse_a_number_that_belongs_to_something_else(tmp_path):
    host = Host(tmp_path, camera_config(), devices=0)
    host.add_device(122, "Farm Camera 22")                   # e.g. another farm's loopback device
    with pytest.raises(ValueError, match="device_base"):
        C.host_device_commands(host.config, ctl="/x/ctl", sys_root=host.sys, dev_root=host.dev)


def test_host_commands_without_ctl_fall_back_to_module_parameters(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "which", lambda name: None)
    cmds = C.host_device_commands(camera_config(), sys_root=tmp_path / "sys")
    assert cmds[1] == ["modprobe", "v4l2loopback", "max_buffers=8", "video_nr=121,122,123",
                       "card_label=superfarm-cam-1,superfarm-cam-2,superfarm-cam-3", "exclusive_caps=1,1,1"]
    assert not any("add" in c for c in cmds)
    assert not any(" " in arg for arg in cmds[1])            # a blank would end a module parameter
    with pytest.raises(ValueError, match="v4l2loopback-ctl"):
        C.host_device_commands(camera_config(slots=9), sys_root=tmp_path / "sys")
    (tmp_path / "sys" / "module" / "v4l2loopback").mkdir(parents=True)     # loaded, devices missing, no ctl
    with pytest.raises(ValueError, match="v4l2loopback-ctl"):
        C.host_device_commands(camera_config(), sys_root=tmp_path / "sys")


def test_host_commands_validate_range_and_group(tmp_path):
    with pytest.raises(ValueError, match="past /dev/video255"):
        C.host_device_commands(camera_config(device_base=200, slots=100), ctl="/x", sys_root=tmp_path)
    for bad in ("", "-R", "a b", "x;y"):
        with pytest.raises(ValueError):
            C.host_device_commands(camera_config(), group=bad, ctl="/x", sys_root=tmp_path)


def test_udev_rule_matches_only_our_devices():
    rule = C.host_udev_rule("video")
    assert rule == ('SUBSYSTEM=="video4linux", ATTR{name}=="Super Farm Camera*", GROUP="video", MODE="0660"\n'
                    'SUBSYSTEM=="video4linux", ATTR{name}=="superfarm-cam*", GROUP="video", MODE="0660"\n')
    with pytest.raises(ValueError):
        C.host_udev_rule('x", RUN+="/bin/sh')


# ------------------------------------------------------------------ wiring

def test_camera_wires_itself_into_the_instance_manager_once(host):
    assert host.mgr.post_boot_hooks == [host.camera.post_boot]
    assert host.mgr.extra_run_args_providers == [host.camera.extra_run_args]
    host.camera.__init__(host.config, host.paths, host.docker, host.mgr, launcher=host.launcher)
    assert len(host.mgr.post_boot_hooks) == 1 and len(host.mgr.extra_run_args_providers) == 1


def test_camera_works_with_a_manager_that_has_no_hook_lists(host):
    class Bare:
        def resolved(self, instance_id):
            return host.mgr.resolved(instance_id)
    cam = C.Camera(host.config, host.paths, host.docker, Bare(), launcher=host.launcher)
    assert cam.extra_run_args(host.mgr.resolved(2))[0] == "--mount"


def test_extra_run_args_mount_a_host_fifo_as_the_microphone(host):
    r = host.mgr.resolved(4321)                      # ids are not limited by device numbers
    args = host.camera.extra_run_args(r)
    fifo = host.paths.run / "camera" / "4321" / "mic"
    assert args == ["--mount", f"type=bind,source={fifo},target=/dev/farm_mic"]
    st = os.stat(fifo)
    assert stat.S_ISFIFO(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o644   # Android's audio user is "other"
    assert host.camera.extra_run_args(r) == args and os.stat(fifo).st_ino == st.st_ino
    host.mgr.camera_off.add(5)
    assert host.camera.extra_run_args(host.mgr.resolved(5)) == []
    assert not (host.paths.run / "camera" / "5").exists()


def test_extra_run_args_refuse_a_home_docker_cannot_mount(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "which", lambda name: f"/usr/bin/{name}")
    odd = Host(tmp_path / "a,b")
    with pytest.raises(ValueError, match="SUPERFARM_HOME"):
        odd.camera.extra_run_args(odd.mgr.resolved(1))


# --------------------------------------------------------------- available

def test_available_reports_every_gap_with_its_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "which", lambda name: None)
    host = Host(tmp_path, Config(), devices=0)
    (host.sys / "module" / "v4l2loopback").rmdir()
    state = host.camera.available()
    assert state["ok"] is False and state["enabled"] is False and state["module"] is False
    text = " | ".join(state["reasons"])
    for hint in ("media.camera.enabled", "sf host apply", "ffmpeg", "v4l-utils", "extract_hal.sh"):
        assert hint in text
    assert state["devices"]["missing"] == list(range(101, 133)) and state["devices"]["first"] == 101
    assert state["android_versions"] == ["13"]


def test_available_is_ok_when_everything_is_in_place(host):
    overlay = host.paths.overlays / "camera" / "13"
    for rel in C.OVERLAY_REQUIRED:
        (overlay / rel).parent.mkdir(parents=True, exist_ok=True)
        (overlay / rel).write_bytes(b"x")
    host.add_device(107, "Integrated Webcam")       # one number is somebody else's
    state = host.camera.available()
    assert state["ok"] is True
    assert state["devices"]["usable"] == [101, 102, 103, 104, 105, 106, 108] and state["devices"]["foreign"] == [107]
    assert any("device_base" in r for r in state["reasons"])
    assert state["overlay"]["13"]["present"] is True


def test_sources_list_the_library_and_the_live_ingest(host):
    host.media("b.mp4", "A.mkv", "voice.mp3", "notes.txt", ".hidden.mp4")
    (host.paths.media / "folder.mp4").mkdir()
    out = host.camera.sources()
    assert [s["id"] for s in out] == ["A.mkv", "b.mp4", "voice.mp3", "rtmp"]
    assert [s["kind"] for s in out] == ["video", "video", "audio", "live"]
    assert out[-1]["server"] == "rtmp://127.0.0.1:1935/live" and out[-1]["key"] == "camera" and out[-1]["active"] is False


# -------------------------------------------------------------------- play

def test_play_starts_one_source_for_many_androids(host):
    host.media("clip.mp4")
    out = host.camera.play([3, 1, 2], "clip.mp4", attach=False)
    assert out["ok"] and out["feed"]["ids"] == [1, 2, 3] and out["feed"]["video"] and out["feed"]["audio"]
    kinds = host.launcher.kinds()
    assert kinds.count("copier") == 3 and kinds.count("forwarder") == 3 and kinds.count("feed") == 1
    assert kinds[-1] == "feed"                       # every reader is up before the source starts
    feed = host.launcher.feeds()[0]
    assert feed.count("-i") == 1 and "--give-up" in feed and feed[feed.index("--give-up") + 1] == "5"
    tee = feed[feed.index("tee") + 1]
    for i in (1, 2, 3):
        assert str(host.paths.run / "camera" / str(i) / "video.fifo") in tee
    assert str(host.paths.media / "clip.mp4") in feed
    # devices: Android N -> /dev/video(100+N), each primed to the sensor size before the feed
    assert [host.format_of(n) for n in (101, 102, 103)] == [FORMAT] * 3
    copier = host.launcher.started[kinds.index("copier")]
    assert copier[0][-1].startswith(str(host.dev / "video10")) and len(copier[2]) == 1   # keeps its FIFO open
    assert stat.S_ISFIFO(os.stat(host.paths.run / "camera" / "1" / "mic").st_mode)


def test_play_does_not_prime_a_device_that_already_has_the_format(host):
    host.media("clip.mp4")
    host.add_device(101, f"{F.CARD_LABEL} 1", FORMAT)
    host.camera.play([1], "clip.mp4", attach=False)
    assert not any(a[0] == "timeout" for a in host.ran)


def test_play_same_source_joins_the_running_feed(host):
    host.media("clip.mp4")
    first = host.camera.play([1, 2], "clip.mp4", attach=False)["feed"]
    second = host.camera.play([3], "clip.mp4", attach=False)["feed"]
    assert second["ids"] == [1, 2, 3] and second["id"] != first["id"]
    assert len(host.launcher.live_of("feed")) == 1 and len(host.camera.status()["feeds"]) == 1
    assert len(host.launcher.live_of("copier")) == 3          # copiers of 1 and 2 were never restarted
    assert host.launcher.kinds().count("copier") == 3


def test_play_another_source_takes_androids_off_the_old_feed(host):
    host.media("clip.mp4", "other.mp4")
    host.camera.play([1, 2, 3], "clip.mp4", attach=False)
    host.camera.play([2], "other.mp4", attach=False)
    feeds = {f["source"]: f["ids"] for f in host.camera.status()["feeds"]}
    assert feeds == {"clip.mp4": [1, 3], "other.mp4": [2]}    # one writer per FIFO
    assert len(host.launcher.live_of("feed")) == 2


def test_play_sound_options(host):
    host.media("clip.mp4", "silent.mp4", "voice.mp3", "cover.mp3")
    assert host.camera.play([1], "silent.mp4", attach=False)["feed"]["audio"] is False
    assert "pcm_s16le" not in host.launcher.feeds()[-1]
    assert host.camera.play([1], "clip.mp4", sound="none", attach=False)["feed"]["audio"] is False
    out = host.camera.play([1], "clip.mp4", sound="voice.mp3", attach=False)["feed"]
    assert out["audio"] and host.launcher.feeds()[-1].count("-i") == 2 and "1:a:0" in host.launcher.feeds()[-1]
    only_sound = host.camera.play([1], "voice.mp3", attach=False)["feed"]
    assert only_sound["video"] is False and only_sound["audio"] and "mjpeg" not in host.launcher.feeds()[-1]
    # cover art in a music file is not a camera picture
    assert host.camera.play([1], "cover.mp3", attach=False)["feed"]["video"] is False
    with pytest.raises(ValueError, match="no picture"):
        host.camera.play([1], "voice.mp3", sound="none", attach=False)
    with pytest.raises(ValueError, match="no sound"):
        host.camera.play([1], "clip.mp4", sound="silent.mp4", attach=False)


def test_play_rtmp_listens_forever_on_the_configured_address(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "which", lambda name: f"/usr/bin/{name}")
    config = Config.model_validate({"media": {"camera": {"enabled": True, "slots": 4, "rtmp_port": 17250}}})
    host = Host(tmp_path, config, devices=4)
    out = host.camera.play([1], "rtmp", attach=False)
    feed = host.launcher.feeds()[0]
    assert out["feed"]["source"] == "rtmp" and feed[feed.index("--give-up") + 1] == "0"
    assert feed[feed.index("-listen"):feed.index("-listen") + 4] == ["-listen", "1", "-i", "rtmp://127.0.0.1:17250/live/camera"]
    host.camera.play([2], "rtmp", attach=False)               # a second ingest could not bind the port: merged
    assert len(host.launcher.live_of("feed")) == 1
    assert host.camera.sources()[-1]["active"] is True


@pytest.mark.parametrize("bad", ["../secrets/x.mp4", "/etc/passwd", "missing.mp4", "", "sub/../../x"])
def test_play_only_accepts_files_inside_the_media_library(host, bad):
    host.media("clip.mp4")
    (host.paths.home / "secrets" / "x.mp4").write_bytes(b"x")
    with pytest.raises(ValueError):
        host.camera.play([1], bad, attach=False)
    assert host.launcher.started == []


def test_play_validates_ids_and_unreadable_files(host):
    host.media("clip.mp4", "broken.mp4")
    with pytest.raises(ValueError):
        host.camera.play([], "clip.mp4")
    with pytest.raises(ValueError):
        host.camera.play([-1], "clip.mp4")
    with pytest.raises(ValueError, match="cannot be read"):
        host.camera.play([1], "broken.mp4", attach=False)


def test_play_reports_per_android_failures_and_serves_the_rest(host):
    host.media("clip.mp4")
    host.add_device(102, "Integrated Webcam")                 # instance 2's number is not ours
    os.chmod(host.dev / "video103", 0o000)
    out = host.camera.play([1, 2, 3, 9], "clip.mp4", attach=False)
    assert out["ok"] is False and out["feed"]["ids"] == [1, 9]
    assert "refusing to write" in out["results"][2]["detail"]
    if os.geteuid() != 0:
        assert "no permission" in out["results"][3]["detail"]
    assert out["results"][1]["ok"] and out["results"][9]["ok"]
    # id 9 is outside the 8-device pool: it got a free device from the top
    assert host.camera.status()["instances"][9]["device"] == str(host.dev / "video108")


def test_play_fails_clearly_when_a_device_never_takes_the_format(host):
    host.media("clip.mp4")
    host.prime_works = False
    out = host.camera.play([1], "clip.mp4", attach=False)
    assert out["ok"] is False and out["feed"] is None
    assert "did not take the camera's frame size" in out["results"][1]["detail"]
    assert sum(host.sleeps) == pytest.approx(10.0)            # bounded wait, not forever
    host.add_device(101, f"{F.CARD_LABEL} 1", "MJPG:1280x720@30")
    assert "locked to MJPG:1280x720@30" in host.camera.play([1], "clip.mp4", attach=False)["results"][1]["detail"]


def test_play_needs_ffmpeg(host, monkeypatch):
    host.media("clip.mp4")
    monkeypatch.setattr(C, "which", lambda name: None if name == "ffmpeg" else f"/usr/bin/{name}")
    with pytest.raises(RuntimeError, match="ffmpeg is not installed"):
        host.camera.play([1], "clip.mp4", attach=False)


# ------------------------------------------------------------- stop / down

def test_stop_freezes_the_picture_but_keeps_the_camera(host):
    host.media("clip.mp4")
    host.camera.play([1, 2, 3], "clip.mp4", attach=False)
    out = host.camera.stop([2])
    assert out["ids"] == [2] and len(out["stopped"]) == 1
    assert [f["ids"] for f in host.camera.status()["feeds"]] == [[1, 3]]   # shared feed carries on for the rest
    assert len(host.launcher.live_of("copier")) == 3                      # device stays open
    host.camera.stop()
    assert host.camera.status()["feeds"] == [] and host.launcher.live_of("feed") == []
    assert len(host.launcher.live_of("copier")) == 3


def test_down_stops_helpers_unpins_the_device_and_keeps_the_mic_fifo(host):
    host.media("clip.mp4")
    host.camera.play([1, 2], "clip.mp4", attach=False)
    mic = os.stat(host.paths.run / "camera" / "1" / "mic").st_ino
    host.camera.down([1])
    assert len(host.launcher.live_of("copier")) == 1 and len(host.launcher.live_of("forwarder")) == 1
    assert any(hard for _, hard in host.launcher.stopped)                 # copiers are killed, not asked
    assert ["v4l2-ctl", "-d", str(host.dev / "video101"), "-c", "keep_format=0"] in host.ran
    assert not (host.paths.run / "camera" / "1" / "video.fifo").exists()
    assert os.stat(host.paths.run / "camera" / "1" / "mic").st_ino == mic  # a running Android has it mounted
    assert [f["ids"] for f in host.camera.status()["feeds"]] == [[2]]
    host.camera.down()
    assert host.launcher.live == set()


def test_forget_frees_the_device(host):
    host.media("clip.mp4")
    host.camera.play([9], "clip.mp4", attach=False)
    host.camera.forget(9)
    assert 9 not in host.camera.status()["instances"]
    assert not (host.paths.run / "camera" / "9").exists() and host.launcher.live == set()


# -------------------------------------------------------------------- heal

def test_heal_restarts_a_dead_copier_and_reconnects_its_feed(host):
    host.media("clip.mp4")
    host.camera.play([1, 2], "clip.mp4", attach=False)
    assert host.camera.heal() == {"sinks": [], "feeds": []}
    dead = host.launcher.live_of("copier")[0]
    host.launcher.live.discard(dead.pid)
    feed_before = host.camera.status()["feeds"][0]["id"]
    healed = host.camera.heal()
    assert len(healed["sinks"]) == 1 and healed["feeds"] == [feed_before]
    assert len(host.launcher.live_of("copier")) == 2 and len(host.launcher.live_of("feed")) == 1
    assert host.camera.status()["feeds"][0]["ids"] == [1, 2]
    assert host.camera.heal() == {"sinks": [], "feeds": []}


def test_heal_also_looks_after_an_attached_android_that_is_not_playing(host, caplog):
    run_names(host, 5)
    host.camera.attach([5])                                        # black frames, no feed
    host.launcher.live.discard(host.launcher.live_of("copier")[0].pid)
    assert host.camera.heal() == {"sinks": [5], "feeds": []}
    assert len(host.launcher.live_of("copier")) == 1
    host.camera.down([5])
    assert host.camera.heal() == {"sinks": [], "feeds": []}        # taken down on purpose: left alone
    assert host.launcher.live == set()


def test_heal_reports_a_lasting_problem_once(host, caplog):
    host.media("clip.mp4")
    host.camera.play([1], "clip.mp4", attach=False)
    host.launcher.live.discard(host.launcher.live_of("copier")[0].pid)
    host.add_device(101, "Integrated Webcam")                      # the device is no longer ours
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert host.camera.heal()["sinks"] == []
    assert len([r for r in caplog.records if "could not be restarted" in r.message]) == 1


def test_a_source_that_gave_up_is_reported_not_restarted(host):
    host.media("clip.mp4")
    feed = host.camera.play([1], "clip.mp4", attach=False)["feed"]
    host.launcher.live.discard(feed["pid"])
    (host.paths.run / "camera" / "feeds" / f"{feed['id']}.log").write_text("clip.mp4: Invalid data found\n")
    assert host.camera.heal() == {"sinks": [], "feeds": []}
    shown = host.camera.status()["feeds"][0]
    assert shown["running"] is False and "Invalid data" in shown["error"]
    host.camera.stop([1])
    assert host.camera.status()["feeds"] == []


def test_feed_records_from_before_the_last_boot_are_dropped(host, monkeypatch):
    host.media("clip.mp4")
    host.camera.play([1], "clip.mp4", attach=False)
    monkeypatch.setattr(C, "_boot_time", lambda: 4e12)
    assert host.camera.status()["feeds"] == []


# --------------------------------------------------------------------- mic

def test_mic_mute_unmute_solo(host):
    host.media("clip.mp4")
    host.camera.play([1, 2], "clip.mp4", attach=False)
    assert host.camera.mic([1], "mute")["muted"] == [1]
    assert (host.paths.run / "camera" / "1" / "mute").exists()
    assert host.camera.status()["muted"] == [1] and host.camera.status()["instances"][1]["muted"]
    assert host.camera.mic([1], "unmute")["muted"] == []
    assert host.camera.mic([2], "solo")["muted"] == [1, 3, 4]      # every other known Android
    assert host.camera.mic([], "unmute")["muted"] == []            # no ids = everybody
    with pytest.raises(ValueError):
        host.camera.mic([1], "loud")
    with pytest.raises(ValueError):
        host.camera.mic([], "solo")
    assert len(host.launcher.started) == 5                         # nothing was restarted for any of this


# ------------------------------------------------------------------ attach

def test_attach_shows_the_android_only_its_own_device(host):
    run_names(host, 5)
    out = host.camera.attach([5])
    assert out == {"ok": True, "results": {5: {"ok": True, "detail": "camera attached", "microphone": True}}}
    assert ["v4l2-ctl", "-d", str(host.dev / "video105"), "-c", "keep_format=1"] in host.ran
    mknod = next(argv for _, argv in host.docker.execs if "mknod" in " ".join(argv))
    assert mknod[:2] == ["sh", "-c"] and mknod[-2:] == ["81", "105"]
    assert 'rm -f "$node"' in mknod[2] and "mknod -m 666 /dev/video0 c" in mknod[2]
    assert "105" not in mknod[2]                                   # numbers travel as arguments, not as script text
    assert all(name == "sf-android-5" for name, _ in host.docker.execs)
    # frames were flowing before the node appeared
    assert host.format_of(105) == FORMAT and host.launcher.kinds() == ["copier", "forwarder"]
    assert host.sleeps[-1] == 2.0


def test_attach_never_cuts_dumpsys_short(host):
    run_names(host, 1)
    host.camera.attach([1])
    counts = [argv[2] for _, argv in host.docker.execs if "dumpsys media.camera" in " ".join(argv)]
    assert counts and all(c.endswith("grep -c '^Number of camera devices: 1'") for c in counts)
    assert not any(bad in c for c in counts for bad in ("head", "grep -m", "grep -q"))


def test_attach_is_a_no_op_when_already_attached(host):
    run_names(host, 5)
    host.docker.counts, host.docker.node = [1], "51:69"            # 81:105 in hex
    out = host.camera.attach([5])
    assert out["results"][5]["ok"]
    assert not any("mknod" in " ".join(argv) for _, argv in host.docker.execs)
    assert not any("keep_format=1" in a for a in host.ran for a in a)


def test_attach_recreates_a_node_that_points_at_another_device(host):
    run_names(host, 5)
    host.docker.counts, host.docker.node = [1], "51:0"             # camera present, but it is not ours
    host.camera.attach([5])
    assert host.docker.node == "51:69"


def test_attach_restarts_the_hal_when_it_missed_the_node(host):
    run_names(host, 1)
    host.docker.counts = [0, 0, 1]
    out = host.camera.attach([1])
    assert out["ok"]
    assert any("kill $(pidof android.hardware.camera.provider@2.5-external-service)" in argv[2]
               for _, argv in host.docker.execs if len(argv) > 2)
    assert host.sleeps[-2:] == [2.0, 4.0]


def test_attach_failures_say_what_to_do(host):
    run_names(host, 2, 3, 4)
    host.docker.counts = [0]
    assert "not running" in host.camera.attach([1])["results"][1]["detail"]
    out = host.camera.attach([2])
    assert out["ok"] is False and "does not see the camera" in out["results"][2]["detail"]
    host.docker.driver = False
    assert "install" in host.camera.attach([3])["results"][3]["detail"]
    host.docker.driver = True
    host.mgr.known.discard(77)
    assert "no Android 77" in host.camera.attach([77])["results"][77]["detail"]


def test_attach_reports_a_microphone_that_is_not_mounted(host):
    run_names(host, 1)
    host.docker.mic = False
    result = host.camera.attach([1])["results"][1]
    assert result["ok"] and result["microphone"] is False and "recreate" in result["detail"]


def test_play_attaches_running_androids(host):
    host.media("clip.mp4")
    run_names(host, 1)
    out = host.camera.play([1, 2], "clip.mp4")
    assert out["ok"] and out["results"][1]["detail"] == "camera attached" and out["results"][2]["detail"] == "ready"
    assert host.docker.node == "51:65"


def test_post_boot_hook_attaches_and_reports_problems_as_warnings(host):
    run_names(host, 3)
    assert host.camera.post_boot(host.mgr.resolved(3), None) == []
    assert host.docker.node == "51:67"
    host.mgr.camera_off.add(4)
    before = len(host.docker.execs)
    assert host.camera.post_boot(host.mgr.resolved(4), None) == []     # camera off: untouched
    assert len(host.docker.execs) == before
    host.docker.driver = False
    warnings = host.camera.post_boot(host.mgr.resolved(3), None)       # a problem is a warning on the start result
    assert len(warnings) == 1 and warnings[0].startswith("camera: no camera driver")
    host.docker.exec = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("docker is gone"))
    assert host.camera.post_boot(host.mgr.resolved(3), None) == ["camera: docker is gone"]   # never raises


def test_status_can_ask_the_androids(host):
    host.media("clip.mp4")
    run_names(host, 1)
    host.camera.play([1, 2], "clip.mp4", attach=False)
    host.docker.counts = [1]
    deep = host.camera.status(android=True)["instances"]
    assert deep[1]["driver"] is True and deep[1]["cameras"] == 1 and deep[1]["mic_mounted"] is True
    assert "driver" not in deep[2]                                 # not running: not asked
    assert deep[1]["copier"] and deep[1]["forwarder"] and deep[1]["format"] == FORMAT
    assert deep[1]["feed"] == host.camera.status()["feeds"][0]["id"]


# ------------------------------------------------------------- install_hal

def test_install_hal_streams_the_overlay_into_vendor_as_root(host):
    overlay = host.paths.overlays / "camera" / "13"
    for rel in C.OVERLAY_REQUIRED:
        (overlay / rel).parent.mkdir(parents=True, exist_ok=True)
        (overlay / rel).write_bytes(b"x" * 10)
    os.chmod(overlay / C.OVERLAY_REQUIRED[0], 0o775)                # the service binary is executable
    os.chmod(overlay / "etc" / "external_camera_config.xml", 0o666)  # sloppy modes are normalised
    (overlay / "lib" / "link.so").symlink_to("/etc/hostname")        # never followed, never copied
    run_names(host, 1)
    host.docker.existing.add("sf-android-2")
    out = host.camera.install_hal([1, 2, 3])
    assert [dst for dst, _ in host.docker.copies] == ["sf-android-1:/vendor/", "sf-android-2:/vendor/"]
    with tarfile.open(fileobj=io.BytesIO(host.docker.copies[0][1])) as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert sorted(members) == sorted(C.OVERLAY_REQUIRED)             # files only: /vendor's own folders stay as they are
    assert all(m.isfile() and m.uid == 0 and m.gid == 0 for m in members.values())   # not the daemon's uid
    assert members[C.OVERLAY_REQUIRED[0]].mode == 0o755
    assert {m.mode for name, m in members.items() if name != C.OVERLAY_REQUIRED[0]} == {0o644}
    assert out["results"][1] == {"ok": True, "restart_needed": True, "detail": "installed; restart the Android to load it"}
    assert out["results"][2]["restart_needed"] is False
    assert out["ok"] is False and "start the Android once" in out["results"][3]["detail"]


def test_install_hal_refuses_an_incomplete_or_missing_overlay(host):
    run_names(host, 1)
    out = host.camera.install_hal([1])
    assert out["ok"] is False and "Android 13 only" in out["results"][1]["detail"]
    assert "extract_hal.sh" in out["results"][1]["detail"] and host.docker.copies == []


def test_overlay_folder_can_be_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "which", lambda name: f"/usr/bin/{name}")
    custom = tmp_path / "my-overlay"
    config = Config.model_validate({"media": {"camera": {"enabled": True, "slots": 2, "hal_overlay": str(custom)}}})
    host = Host(tmp_path, config, devices=2)
    assert host.camera.available()["overlay"]["13"]["path"] == str(custom)

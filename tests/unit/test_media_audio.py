"""`Listen to one device`: command construction and the one-at-a-time state machine."""
from __future__ import annotations

import socket

import pytest

from superfarm.config import Config
from superfarm.media import audio as A
from superfarm.media.feeds import ProcRef
from superfarm.paths import Paths


class FakeLauncher:
    def __init__(self):
        self.started: list[list[str]] = []
        self.stopped: list[int] = []
        self.live: set[int] = set()
        self.die_at_once = False
        self._next = 500

    def start(self, argv, *, log, pass_fds=()):
        self._next += 1
        self.started.append(list(argv))
        self.log = log
        if self.die_at_once:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("ERROR: Could not open audio device\n")
        else:
            self.live.add(self._next)
        return ProcRef(self._next, 7)

    def alive(self, ref):
        return ref is not None and ref.pid in self.live

    def stop(self, ref, *, grace=2.0, hard=False):
        if ref is not None and ref.pid in self.live:
            self.live.discard(ref.pid)
            self.stopped.append(ref.pid)


class FakeAdb:
    def __init__(self, instance_id: int, events: list, reachable: bool = True):
        self.serial = f"127.0.0.1:{17200 + instance_id}"
        self.id, self.events, self.reachable = instance_id, events, reachable

    def connect(self, attempts: int = 5, delay: float = 1.0) -> bool:
        return self.reachable

    def shell(self, command, *, timeout=30, check=True):
        self.events.append(("volume", self.id, list(command)))
        return ""


class FakeMgr:
    def __init__(self, events: list):
        self.events, self.unreachable = events, set()

    def adb(self, instance_id: int) -> FakeAdb:
        return FakeAdb(instance_id, self.events, instance_id not in self.unreachable)


class Rig:
    def __init__(self, tmp_path, config: Config | None = None):
        self.events: list = []
        self.timers: list = []
        self.launcher = FakeLauncher()
        self.mgr = FakeMgr(self.events)
        self.paths = Paths(tmp_path / "home").ensure()
        self.ports = iter(range(40001, 40100))
        self.listener = A.Listener(config or Config(), self.paths, self.mgr, launcher=self.launcher,
                                   port_picker=lambda: next(self.ports),
                                   timer=lambda delay, fn: self.timers.append((delay, fn)),
                                   sleep=lambda s: self.events.append(("sleep", s)))

    def fire_timers(self):
        timers, self.timers = self.timers, []
        for _, fn in timers:
            fn()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "which", lambda name: f"/usr/bin/{name}")
    return Rig(tmp_path)


def test_listen_command_uses_the_proven_flags():
    argv = A.listen_command(scrcpy="scrcpy", serial="127.0.0.1:17203", port=40123, buffer_ms=200)
    assert argv == ["scrcpy", "--serial=127.0.0.1:17203", "--port=40123", "--no-video", "--no-control",
                    "--no-window", "--audio-source=output", "--audio-codec=raw", "--audio-buffer=200",
                    "--audio-output-buffer=20"]


def test_volume_command_sets_the_media_stream():
    assert A.volume_command(15) == ["cmd", "media_session", "volume", "--stream", "3", "--set", "15"]


def test_free_port_is_really_free_and_not_in_the_old_fixed_range():
    port = A.free_port()
    assert not 27183 <= port <= 27400 and port > 1024
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))               # nobody else holds it


def test_session_env_points_at_the_daemon_users_own_sound_server(tmp_path, monkeypatch):
    monkeypatch.setattr(A.os.path, "isdir", lambda p: p == "/run/user/1234")
    assert A.session_env({}, uid=1234) == {"XDG_RUNTIME_DIR": "/run/user/1234", "SDL_VIDEODRIVER": "dummy"}
    desktop = {"XDG_RUNTIME_DIR": "/run/user/1234", "WAYLAND_DISPLAY": "wayland-0"}
    assert A.session_env(desktop, uid=1234) == {}        # a desktop session is left exactly as it is
    assert "XDG_RUNTIME_DIR" not in A.session_env({"DISPLAY": ":0"}, uid=99)   # no such runtime dir: not invented


def test_env_launcher_prefixes_env_without_a_shell(tmp_path):
    launcher = A._EnvLauncher({"SF_TEST_VALUE": "a b;c"})
    ref = launcher.start(["sh", "-c", 'printf "%s" "$SF_TEST_VALUE"'], log=tmp_path / "x.log")
    import time
    deadline = time.monotonic() + 5
    while launcher.alive(ref) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "x.log").read_text() == "a b;c"


def test_start_listens_and_raises_the_volume_only_after_capture_is_up(rig):
    out = rig.listener.start(3)
    assert out == {"ok": True, "listening": 3, "port": 40001, "pid": 501}
    argv = rig.launcher.started[0]
    assert argv[0] == "/usr/bin/scrcpy" and "--serial=127.0.0.1:17203" in argv and "--port=40001" in argv
    assert "--audio-buffer=200" in argv
    assert rig.listener.current() == 3
    assert [d for d, _ in rig.timers] == [A.VOLUME_DELAY] and A.VOLUME_DELAY >= 3
    before = [e for e in rig.events if e[0] == "volume"]
    rig.fire_timers()
    after = [e for e in rig.events if e[0] == "volume"]
    assert len(after) == len(before) + 1
    assert after[-1] == ("volume", 3, ["cmd", "media_session", "volume", "--stream", "3", "--set", "15"])


def test_buffer_and_volume_come_from_config(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "which", lambda name: f"/usr/bin/{name}")
    rig = Rig(tmp_path, Config.model_validate({"media": {"audio": {"listen_buffer_ms": 350, "listen_volume": 9}}}))
    rig.listener.start(1)
    rig.fire_timers()
    assert "--audio-buffer=350" in rig.launcher.started[0]
    assert rig.events[-1][2][-1] == "9"


def test_only_one_android_is_heard_at_a_time(rig):
    rig.listener.start(1)
    rig.listener.start(2)
    assert rig.launcher.stopped == [501] and rig.launcher.live == {502}
    assert rig.listener.current() == 2
    assert rig.listener.status()["listening"] == 2 and rig.listener.status()["port"] == 40002
    # the first listener's delayed volume step must not act on behalf of the second
    volumes = len([e for e in rig.events if e[0] == "volume"])
    rig.timers[0][1]()
    assert len([e for e in rig.events if e[0] == "volume"]) == volumes
    rig.timers[1][1]()
    assert rig.events[-1][:2] == ("volume", 2)


def test_stop_and_current(rig):
    assert rig.listener.current() is None and rig.listener.status() == {"listening": None}
    rig.listener.stop()                                   # nothing to stop is fine
    rig.listener.start(4)
    rig.listener.stop()
    assert rig.listener.current() is None and rig.launcher.live == set()
    rig.fire_timers()                                     # volume step of a stopped listener does nothing
    assert [e for e in rig.events if e[0] == "volume"][-1][1] == 4
    assert len([e for e in rig.events if e[0] == "volume"]) == 1


def test_current_notices_that_scrcpy_ended_by_itself(rig):
    rig.listener.start(4)
    rig.launcher.live.clear()
    assert rig.listener.current() is None
    assert not (rig.paths.run / "listen.json").exists()


def test_a_listener_left_by_a_previous_daemon_is_found_and_stopped(rig, tmp_path):
    rig.listener.start(6)
    again = A.Listener(Config(), rig.paths, rig.mgr, launcher=rig.launcher, port_picker=lambda: 40050,
                       timer=lambda d, fn: None, sleep=lambda s: None)
    assert again.current() == 6
    again.start(7)
    assert rig.launcher.stopped == [501] and again.current() == 7


def test_start_reports_why_scrcpy_died(rig):
    rig.launcher.die_at_once = True
    with pytest.raises(RuntimeError, match="Could not open audio device"):
        rig.listener.start(2)
    assert rig.listener.current() is None and rig.timers == []


def test_start_needs_a_reachable_android_and_scrcpy(rig, monkeypatch):
    rig.mgr.unreachable.add(5)
    with pytest.raises(RuntimeError, match="not reachable over ADB"):
        rig.listener.start(5)
    with pytest.raises(ValueError):
        rig.listener.start(-3)
    assert rig.launcher.started == []
    monkeypatch.setattr(A, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="scrcpy is not installed"):
        rig.listener.start(1)


def test_a_failed_start_still_silences_the_previous_listener_only_when_reachable(rig):
    rig.listener.start(1)
    rig.mgr.unreachable.add(2)
    with pytest.raises(RuntimeError):
        rig.listener.start(2)
    assert rig.listener.current() == 1                    # an unreachable target does not cost the current stream

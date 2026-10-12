"""The shared base every other module builds on: settings, instance model, tasks, files."""
import os
import stat
import threading
import time

import pytest
from pydantic import ValidationError

from superfarm.config import (
    API_IMMUTABLE_KEYS,
    Config,
    check_api_patch,
    get_value,
    load_config,
    save_config,
    set_value,
    update_config,
)
from superfarm.core.models import InstanceSpec, check_id, parse_selector, resolve
from superfarm.core.store import Store
from superfarm.core.tasks import TaskManager, run_on_instances
from superfarm.paths import get_paths
from superfarm.util.files import atomic_write, inside, safe_filename, write_secret
from superfarm.util.proc import CommandError, run


# ------------------------------------------------------------------ config

def test_defaults_are_the_tuned_values():
    c = Config()
    assert (c.android.width, c.android.height, c.android.dpi, c.android.fps) == (480, 854, 160, 20)
    assert (c.resources.cpus, c.resources.memory_mb, c.resources.memory_swap_mb) == (2.0, 1536, 2560)
    assert c.server.host == "127.0.0.1" and c.server.auth_required
    assert c.network.adb_bind == "127.0.0.1" and c.network.killswitch


def test_auth_cannot_be_disabled_on_a_public_address():
    with pytest.raises(ValidationError):
        Config.model_validate({"server": {"host": "0.0.0.0", "auth_required": False}})
    Config.model_validate({"server": {"host": "0.0.0.0", "auth_required": True}})


def test_the_rtmp_ingest_stays_on_loopback_unless_remote_is_asked_for_by_name():
    # ffmpeg's listener checks neither the app nor the stream key: reachable = usable by anyone.
    camera = Config.model_fields["media"].annotation.model_fields["camera"].annotation
    assert "no authentication" in camera.model_fields["rtmp_bind"].description
    assert Config().media.camera.rtmp_allow_remote is False
    for address in ("127.0.0.1", "127.0.0.9", "::1", "localhost"):
        assert Config.model_validate({"media": {"camera": {"rtmp_bind": address}}}).media.camera.rtmp_bind == address
    for address in ("0.0.0.0", "192.168.1.20", "::", "farm.example"):
        with pytest.raises(ValidationError, match="media.camera.rtmp_allow_remote"):
            Config.model_validate({"media": {"camera": {"rtmp_bind": address}}})
        allowed = Config.model_validate({"media": {"camera": {"rtmp_bind": address, "rtmp_allow_remote": True}}})
        assert allowed.media.camera.rtmp_bind == address
    with pytest.raises(ValidationError, match="rtmp_allow_remote"):           # also when changed one key at a time
        set_value(Config(), "media.camera.rtmp_bind", "0.0.0.0")
    remote = Config.model_validate({"media": {"camera": {"rtmp_bind": "0.0.0.0", "rtmp_allow_remote": True}}})
    with pytest.raises(ValidationError, match="rtmp_allow_remote"):
        set_value(remote, "media.camera.rtmp_allow_remote", False)


def test_settings_that_name_a_program_cannot_be_changed_by_an_api_patch():
    config = Config()
    assert "docker.binary" in API_IMMUTABLE_KEYS
    for dotted in API_IMMUTABLE_KEYS:                                          # every listed key is a real setting
        get_value(config, dotted)
    for patch in ({"docker": {"binary": "/tmp/not-docker"}}, {"gpu": {"nvidia": {"relay_dir": "/tmp/relay"}}},
                  {"gpu": {"nvidia": {"icd": "/tmp/icd.json"}}, "android": {"fps": 25}}):
        with pytest.raises(ValueError, match="config.yaml or with `sf config set`"):
            check_api_patch(config, patch)
    # Everything else, and a patch that repeats the current value (a form sending all it shows), passes.
    for patch in ({"android": {"fps": 25}}, {"docker": {"stop_timeout": 5}}, {"docker": {"binary": "docker"}},
                  {"docker": {"binary": None}}, {"docker": None}, {"gpu": {"nvidia": {"debug": True}}}, {}):
        check_api_patch(config, patch)
    # `null` puts a setting back to its default, which is a change when it was set to something else.
    custom = set_value(config, "docker.binary", "/usr/local/bin/docker")       # the config file and the CLI still can
    check_api_patch(custom, {"docker": {"binary": "/usr/local/bin/docker", "stop_timeout": 5}})
    for patch in ({"docker": {"binary": None}}, {"docker": None}, {"docker": {"binary": "docker"}}):
        with pytest.raises(ValueError, match="docker.binary"):
            check_api_patch(custom, patch)
    with pytest.raises(ValueError, match="mapping"):
        check_api_patch(config, ["docker"])


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"android": {"fpz": 30}})


def test_swap_below_memory_is_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"resources": {"memory_mb": 2048, "memory_swap_mb": 1024}})


def test_gateway_mode_needs_a_defined_gateway():
    with pytest.raises(ValidationError):
        Config.model_validate({"network": {"mode": "gateway", "gateway": "eu"}})
    Config.model_validate({"network": {"mode": "gateway", "gateway": "eu",
                                       "gateways": {"eu": {"profile": "eu.ovpn"}}}})


def test_save_writes_only_changes_and_round_trips(tmp_path):
    paths = get_paths(tmp_path).ensure()
    c = set_value(Config(), "android.fps", 25)
    c = update_config(c, {"network": {"dns": ["9.9.9.9"]}})
    save_config(c, paths)
    text = paths.config.read_text()
    assert "fps: 25" in text and "9.9.9.9" in text and "memory_mb" not in text
    again = load_config(paths)
    assert again.android.fps == 25 and again.network.dns == ["9.9.9.9"]
    assert get_value(again, "android.fps") == 25


def test_set_value_validates():
    with pytest.raises(ValidationError):
        set_value(Config(), "android.fps", 1000)
    with pytest.raises(KeyError):
        set_value(Config(), "nope.fps", 1)


def test_secrets_dir_is_owner_only(tmp_path):
    paths = get_paths(tmp_path).ensure()
    assert stat.S_IMODE(os.stat(paths.secrets).st_mode) == 0o700
    f = write_secret(paths.secrets / "vpn-default", "user\npass\n")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


# ------------------------------------------------------------------- models

def test_resolve_merges_overrides_over_defaults():
    c = Config()
    spec = InstanceSpec(id=7, fps=30, resources={"memory_mb": 2048}, network={"mode": "none"})
    r = resolve(spec, c)
    assert r.fps == 30 and r.width == 480
    assert r.memory_mb == 2048 and r.memory_swap_mb == 2560
    assert r.adb_port == 5562 and r.adb_bind == "127.0.0.1"
    assert r.names.android == "sf-android-7" and r.names.net == "sf-net-7" and r.data_volume == "sf-data-7"
    assert r.net_mode == "none" and r.image == "superfarm/android:13"


def test_resolve_keeps_swap_at_least_memory():
    r = resolve(InstanceSpec(id=1, resources={"memory_mb": 4096}), Config())
    assert r.memory_swap_mb >= r.memory_mb


def test_resolve_custom_image_and_adopted_volume():
    r = resolve(InstanceSpec(id=2, image="local/android:custom", data_volume="old_data_2"), Config())
    assert r.image == "local/android:custom" and r.data_volume == "old_data_2"


def test_resolve_unknown_android_version_fails():
    with pytest.raises(ValueError):
        resolve(InstanceSpec(id=1, android_version="99"), Config())


@pytest.mark.parametrize("bad", ["-1", "1;rm", "abc", "10000", "1.5", "", None])
def test_check_id_rejects_anything_but_a_small_int(bad):
    with pytest.raises(ValueError):
        check_id(bad)


def test_spec_validation():
    with pytest.raises(ValidationError):
        InstanceSpec(id=1, tags=["bad tag!"])
    with pytest.raises(ValidationError):
        InstanceSpec(id=1, extra_boot_args=["androidboot.x=1 --privileged"])
    with pytest.raises(ValidationError):
        InstanceSpec(id=1, gpu_node="/etc/passwd")
    assert InstanceSpec(id=1, tags=["EU", "eu"]).tags == ["eu"]


def test_selectors():
    known = {i: InstanceSpec(id=i, tags=["eu"] if i % 2 else ["us"]) for i in range(1, 7)}
    running = {1, 2, 3}
    assert parse_selector("all", known) == [1, 2, 3, 4, 5, 6]
    assert parse_selector("running", known, running) == [1, 2, 3]
    assert parse_selector("stopped", known, running) == [4, 5, 6]
    assert parse_selector("tag:eu", known) == [1, 3, 5]
    assert parse_selector("2-4,6", known) == [2, 3, 4, 6]
    assert parse_selector("all,!tag:us,!1", known) == [3, 5]
    assert parse_selector("99", known) == []
    with pytest.raises(ValueError):
        parse_selector("1; rm -rf /", known)


# -------------------------------------------------------------------- store

def test_store_round_trip_and_state(tmp_path):
    store = Store(get_paths(tmp_path).ensure())
    assert store.next_id() == 1
    store.save(InstanceSpec(id=1, name="one"))
    store.save(InstanceSpec(id=3, name="three"))
    assert store.ids() == [1, 3] and store.next_id() == 2
    assert store.get(3).name == "three"
    store.state_set(3, "vpn_profile", "a.ovpn")
    assert store.state_get(3, "vpn_profile") == "a.ovpn"
    store.state_set(3, "vpn_profile", None)
    assert store.state_get(3, "vpn_profile", "none") == "none"
    store.delete(1)
    assert store.ids() == [3]
    with pytest.raises(KeyError):
        store.get(1)


def test_store_survives_one_broken_spec(tmp_path):
    paths = get_paths(tmp_path).ensure()
    store = Store(paths)
    store.save(InstanceSpec(id=1))
    (paths.instance(2)).mkdir(parents=True)
    (paths.instance(2) / "instance.yaml").write_text("fps: 99999\n")
    assert list(store.list()) == [1]


# -------------------------------------------------------------------- tasks

def test_one_failing_device_does_not_stop_the_others():
    tm = TaskManager()

    def work(i, task):
        if i == 2:
            raise RuntimeError("boom")
        return f"did {i}"

    t = tm.wait(run_on_instances(tm, "test", "t", [1, 2, 3], work, concurrency=2), timeout=10)
    assert t.state == "done"
    assert t.results[1] == {"ok": True, "detail": "did 1"}
    assert t.results[2]["ok"] is False and "boom" in t.results[2]["detail"]
    assert t.results[3]["ok"] is True
    assert t.to_dict()["failed"] == 1 and t.current == 3


def test_concurrency_limit_is_respected():
    tm = TaskManager()
    live, peak, lock = 0, 0, threading.Lock()

    def work(i, task):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1

    tm.wait(run_on_instances(tm, "test", "t", list(range(8)), work, concurrency=3), timeout=10)
    assert peak <= 3


def test_cancel_skips_devices_not_yet_started():
    tm = TaskManager()
    gate = threading.Event()

    def work(i, task):
        gate.wait(5)
        return "ok"

    t = run_on_instances(tm, "test", "t", [1, 2, 3, 4], work, concurrency=1)
    time.sleep(0.1)
    assert tm.cancel(t.id)
    gate.set()
    tm.wait(t, timeout=10)
    assert t.state == "cancelled"
    assert sum(1 for r in t.results.values() if not r["ok"]) >= 2


def test_task_failure_is_recorded_not_raised():
    tm = TaskManager()
    t = tm.wait(tm.submit("x", "x", lambda task: 1 / 0), timeout=5)
    assert t.state == "failed" and "ZeroDivisionError" in t.error


# --------------------------------------------------------------- util

def test_run_refuses_shell_strings():
    with pytest.raises(TypeError):
        run("echo hi")  # type: ignore[arg-type]


def test_run_reports_failure_and_timeout():
    assert run(["true"]).ok
    with pytest.raises(CommandError):
        run(["false"])
    with pytest.raises(CommandError) as e:
        run(["sleep", "5"], timeout=0.2)
    assert e.value.timed_out


def test_missing_binary_is_a_command_error():
    with pytest.raises(CommandError):
        run(["definitely-not-a-command-xyz"])


def test_safe_filename_and_inside(tmp_path):
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("my app (1).apk") == "my_app_1_.apk"
    assert safe_filename("...") == "file"
    (tmp_path / "a").mkdir()
    assert inside(tmp_path, tmp_path / "a" / "b.txt")
    with pytest.raises(ValueError):
        inside(tmp_path / "a", tmp_path / "a" / ".." / "x")


def test_atomic_write_replaces_whole_file(tmp_path):
    f = tmp_path / "x.txt"
    atomic_write(f, "one")
    atomic_write(f, "two")
    assert f.read_text() == "two"
    assert [p.name for p in tmp_path.iterdir()] == ["x.txt"]


# ---------------------------------------------------------------------- adb

class _RecordingAdb:
    """Adb with the device replaced by a list of the shell lines it would have run."""

    def __new__(cls):
        from superfarm.core.adb import Adb
        adb = Adb("127.0.0.1:1")
        adb.lines = []
        adb.shell = lambda command, **kw: adb.lines.append(command) or ""  # type: ignore[method-assign]
        return adb


def test_adb_text_escapes_spaces_and_never_sends_percent_s_together():
    adb = _RecordingAdb()
    adb.text("100%sure a b")
    typed = [c[2] for c in adb.lines if c[:2] == ["input", "text"]]
    assert typed == ["100%", "sure%sa%sb"]
    assert "100%sure" not in "".join(typed[:1])


def test_adb_text_keeps_a_plain_percent_single():
    adb = _RecordingAdb()
    adb.text("50% off")
    # The device turns `%s` into a space and leaves the first `%` alone: "50% off".
    assert [c[2] for c in adb.lines] == ["50%%soff"]


def test_adb_text_newline_is_enter_and_non_ascii_is_refused():
    from superfarm.core.adb import AdbError
    adb = _RecordingAdb()
    adb.text("a\nb")
    assert adb.lines == [["input", "text", "a"], ["input", "keyevent", "KEYCODE_ENTER"], ["input", "text", "b"]]
    with pytest.raises(AdbError):
        adb.text("café")


def test_adb_rejects_bad_serial_and_key():
    from superfarm.core.adb import Adb
    with pytest.raises(ValueError):
        Adb("127.0.0.1:5555; reboot")
    with pytest.raises(ValueError):
        Adb("127.0.0.1:5555").key("HOME; rm")


def test_current_app_parses_the_resumed_activity():
    adb = _RecordingAdb()
    adb.shell = lambda command, **kw: "  mResumedActivity: ActivityRecord{1a2b u0 com.android.settings/.Settings t12}\n"  # type: ignore[method-assign]
    assert adb.current_app() == ("com.android.settings", "com.android.settings.Settings")


def test_a_saved_partial_entry_loads_again(tmp_path):
    """Regression: saving after changing one field of an image entry wrote only that field,
    and the file then failed to load ("android.images.13.base: Field required")."""
    paths = get_paths(tmp_path).ensure()
    c = update_config(Config(), {"android": {"images": {"13": {"tag": "local/android:13"}}}})
    save_config(c, paths)
    text = paths.config.read_text()
    assert "base:" in text and "memory_mb" not in text     # the map is written whole, other sections not at all
    again = load_config(paths)
    assert again.android.images["13"].tag == "local/android:13"
    assert again.android.images["13"].base == Config().android.images["13"].base
    assert set(again.android.images) == set(Config().android.images)   # sibling versions kept
    save_config(set_value(again, "server.session_hours", 13), paths)   # a second save must not lose it
    assert load_config(paths).android.images["13"].tag == "local/android:13"


def test_a_config_file_that_is_not_a_mapping_is_refused_clearly(tmp_path):
    paths = get_paths(tmp_path).ensure()
    paths.config.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="expected a mapping"):
        load_config(paths)

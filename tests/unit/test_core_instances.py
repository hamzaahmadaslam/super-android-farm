"""The instance lifecycle against in-memory Docker and ADB."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core_fakes import PREFIX, Farm, labels_of, option, options  # noqa: E402

from superfarm.core import instances as inst  # noqa: E402
from superfarm.core.instances import (  # noqa: E402
    InstanceBusy,
    InstanceError,
    StartError,
    trim_hash,
    trim_list,
)
from superfarm.core.planner import Core, CpuTopology  # noqa: E402
from superfarm.util.proc import Result  # noqa: E402

ANDROID, NET, VOLUME = f"{PREFIX}-android-1", f"{PREFIX}-net-1", f"{PREFIX}-data-1"


@pytest.fixture
def farm(tmp_path):
    f = Farm(tmp_path)
    yield f
    f.close()


def one(farm, **template):
    return farm.mgr.create(template=template or None)[0]


# ------------------------------------------------------------------------- create

def test_create_numbers_names_and_identifies_each_android(farm):
    specs = farm.mgr.create(count=3, name="Shop", template={"tags": ["EU"], "fps": 30})
    assert [s.id for s in specs] == [1, 2, 3]
    assert [s.name for s in specs] == ["Shop 1", "Shop 2", "Shop 3"]
    assert all(s.tags == ["eu"] and s.fps == 30 for s in specs)
    assert len({s.device.android_id for s in specs}) == 3
    assert all(s.device.profile != "random" and s.device.props["ro.product.model"] for s in specs)
    assert farm.store.ids() == [1, 2, 3]
    assert farm.docker.containers == {}                    # nothing is started by create


def test_create_validates_before_saving(farm):
    with pytest.raises(ValueError):
        farm.mgr.create(count=0)
    with pytest.raises(ValueError):
        farm.mgr.create(template={"android_version": "5"})          # no such image configured
    with pytest.raises(ValueError):
        farm.mgr.create(template={"no_such_field": 1})
    with pytest.raises(ValueError, match="without spaces"):
        farm.mgr.create(template={"device": {"props": {"ro.product.model": "Pixel 7"}}})
    assert farm.store.ids() == []


def test_create_honours_an_explicit_device_block(farm):
    spec = one(farm, device={"profile": "samsung-s23", "android_id": "00000000000000aa", "locale": "de-DE",
                             "props": {"ro.build.tags": "release-keys"}})
    assert spec.device.profile == "samsung-s23" and spec.device.android_id == "00000000000000aa"
    assert spec.device.locale == "de-DE"
    assert spec.device.props["ro.product.model"] == "SM-S911B" and spec.device.props["ro.build.tags"] == "release-keys"


def test_create_can_take_the_screen_shape_of_the_device(farm):
    spec = one(farm, device={"profile": "samsung-s23"}, use_profile_screen=True)
    assert (spec.width, spec.height, spec.dpi) == (480, 1040, 213)
    plain = one(farm, device={"profile": "samsung-s23"})
    assert plain.width is None


def test_create_skips_an_id_whose_old_data_volume_still_exists(farm):
    farm.docker.volume_labels[f"{PREFIX}-data-1"] = {}
    farm.docker.volume_labels[f"{PREFIX}-data-2"] = {}
    assert [s.id for s in farm.mgr.create(count=2)] == [3, 4]


def test_adopting_a_volume_keeps_its_identity(farm):
    farm.docker.volume_labels["old_android_data_7"] = {}
    spec = one(farm, data_volume="old_android_data_7")
    assert spec.data_volume == "old_android_data_7"
    assert spec.device.profile == "none" and spec.device.android_id == "" and spec.device.props == {}
    with pytest.raises(ValueError, match="already belongs to Android 1"):
        one(farm, data_volume="old_android_data_7")
    with pytest.raises(ValueError, match="does not exist"):
        one(farm, data_volume="missing_volume")
    with pytest.raises(ValueError, match="not a path"):
        one(farm, data_volume="/var/lib/android")
    with pytest.raises(ValueError, match="count=1"):
        farm.mgr.create(count=2, template={"data_volume": "old_android_data_7"})


# -------------------------------------------------------------------------- start

def test_first_start_builds_the_pair_network_first(farm):
    one(farm)
    hooked = []
    farm.mgr.post_boot_hooks.append(lambda r, adb: hooked.append((r.id, adb.serial)))
    result = farm.mgr.start(1)

    assert farm.docker.names_called("create") == [NET, ANDROID]
    assert "net-wait" in farm.events
    net, android = farm.docker.containers[NET], farm.docker.containers[ANDROID]
    assert net["labels"]["io.superfarm.plan"] == android["labels"]["io.superfarm.plan"] != ""
    assert option(android["args"], "--network") == f"container:{NET}"
    assert f"{VOLUME}:/data" in options(android["args"], "-v")
    assert farm.docker.volume_labels[VOLUME] == {"io.superfarm.farm": PREFIX, "io.superfarm.instance": "1",
                                                  "io.superfarm.role": "data"}
    assert "local/android:13" in android["args"]
    assert result["state"] == "booted" and result["recreated"] and result["gpu"] == "software"
    assert result["serial"] == "127.0.0.1:17001" and result["warnings"] == []
    assert hooked == [(1, "127.0.0.1:17001")]
    assert farm.store.state_get(1, "last_error") is None
    assert farm.store.state_get(1, "gpu") == {"mode": "software", "node": "", "kind": "", "label": ""}
    assert farm.mgr.status(1)["state"] == "booted"


def test_the_card_the_host_module_chose_is_shown_on_the_android(farm):
    one(farm)
    farm.gpu.mode, farm.gpu.node = "intel", "/dev/dri/renderD128"
    farm.gpu.kind, farm.gpu.label = "igpu", "Intel UHD Graphics 770 (integrated, Mesa)"      # as host.gpu.GpuChoice carries them
    result = farm.mgr.start(1)
    assert result["gpu_label"] == "Intel UHD Graphics 770 (integrated, Mesa)" and result["gpu_reason"] == "test"
    shown = farm.mgr.status(1)["gpu"]
    assert shown == {"requested": "auto", "mode": "intel", "node": "/dev/dri/renderD128", "kind": "igpu",
                     "label": "Intel UHD Graphics 770 (integrated, Mesa)"}
    assert farm.mgr.list_status()[0]["gpu"] == shown


def test_post_boot_applies_identity_animations_and_props(farm):
    spec = one(farm, extra_props={"debug.test.flag": "1", "ro.locked": "x"})
    result = farm.mgr.start(1)
    dev = farm.device(1)
    assert dev["settings"]["secure/android_id"] == spec.device.android_id
    assert dev["props"]["debug.test.flag"] == "1" and "ro.locked" not in dev["props"]
    assert any("ro.locked" in w and "extra_boot_args" in w for w in result["warnings"])
    assert any(isinstance(c, str) and "window_animation_scale 0" in c for c in dev["shell"])


def test_starting_a_running_android_changes_nothing(farm):
    one(farm)
    farm.mgr.start(1)
    farm.mgr.update(1, {"fps": 45})                      # even with a changed plan
    before = list(farm.docker.calls)
    farm.events.clear()
    result = farm.mgr.start(1)
    assert result["already_running"] and not result["recreated"]
    assert not [c for c in farm.docker.calls[len(before):] if c[0] in ("create", "rm", "stop", "start")]
    assert farm.events == []                              # no GPU choice, no sidecar arguments re-drawn


def test_stop_takes_android_down_before_its_network(farm):
    one(farm)
    farm.mgr.start(1)
    farm.mgr.stop(1)
    assert farm.docker.names_called("stop") == [ANDROID, NET]
    assert not farm.docker.running(ANDROID) and not farm.docker.running(NET)
    assert farm.mgr.status(1)["state"] == "stopped"
    assert "<disconnect>" in farm.device(1)["shell"]


def test_start_after_stop_reuses_the_containers(farm):
    one(farm)
    farm.mgr.start(1)
    farm.mgr.stop(1)
    result = farm.mgr.start(1)
    assert not result["recreated"]
    assert farm.docker.names_called("create") == [NET, ANDROID]            # still only the first pair
    assert farm.docker.names_called("start") == [NET, ANDROID]             # network first again
    assert farm.docker.running(ANDROID) and farm.docker.running(NET)


def test_a_changed_plan_rebuilds_both_containers_together(farm):
    one(farm)
    farm.mgr.start(1)
    old = farm.docker.containers[ANDROID]["labels"]["io.superfarm.plan"]
    farm.mgr.stop(1)
    farm.mgr.update(1, {"fps": 45})
    result = farm.mgr.start(1)
    assert result["recreated"]
    assert farm.docker.names_called("rm") == [ANDROID, NET]
    assert farm.docker.names_called("create") == [NET, ANDROID, NET, ANDROID]
    assert farm.docker.containers[ANDROID]["labels"]["io.superfarm.plan"] != old
    assert "androidboot.redroid_fps=45" in farm.docker.containers[ANDROID]["args"]
    assert VOLUME in farm.docker.volume_labels                               # data untouched


def test_restart_applies_a_changed_plan_and_recreate_always_rebuilds(farm):
    one(farm)
    farm.mgr.start(1)
    farm.mgr.update(1, {"dpi": 240})
    assert farm.mgr.restart(1)["recreated"]
    assert "androidboot.redroid_dpi=240" in farm.docker.containers[ANDROID]["args"]
    assert not farm.mgr.restart(1)["recreated"]
    assert farm.mgr.recreate(1)["recreated"]


def test_a_lone_network_container_is_not_started_into(farm):
    one(farm)
    farm.mgr.start(1)
    farm.docker.containers[ANDROID]["running"] = False          # Android died, network kept running
    farm.docker.calls.clear()
    farm.mgr.start(1)
    kinds = [(c[0], c[1]) for c in farm.docker.calls if c[0] in ("stop", "start")]
    assert kinds == [("stop", ANDROID), ("stop", NET), ("start", NET), ("start", ANDROID)]


def test_network_not_ready_means_android_is_never_started(farm):
    one(farm)
    farm.net_ready = {"ready": False, "state": "auth_failed", "detail": "AUTH_FAILED"}
    with pytest.raises(StartError, match="auth_failed"):
        farm.mgr.start(1)
    assert farm.docker.names_called("create") == [NET]
    assert ANDROID not in farm.docker.containers
    assert "auth_failed" in farm.store.state_get(1, "last_error")
    assert farm.mgr.list_status()[0]["state"] == "error"


def test_android_that_dies_while_booting_reports_its_last_output(farm):
    one(farm)
    farm.docker.fail_run_of.add(ANDROID)
    with pytest.raises(StartError, match="cannot find binder"):
        farm.mgr.start(1)
    assert farm.mgr.status(1)["last_error"].startswith("StartError")


def test_boot_timeout_is_an_error_that_names_the_setting(farm):
    one(farm)
    farm.device(1)["boots"] = False
    farm.mgr.config.performance.boot_timeout = 30
    clock = iter(range(0, 10_000, 20))
    farm.mgr.backends.sleep = lambda s: None
    real = inst.time.monotonic
    inst.time.monotonic = lambda: next(clock)
    try:
        with pytest.raises(StartError, match="performance.boot_timeout"):
            farm.mgr.start(1)
    finally:
        inst.time.monotonic = real


def test_nvidia_start_uses_the_mounts_the_gpu_module_returns(farm):
    """The host module starts the relay and returns the socket and runtime mounts; the
    manager passes them on unchanged and adds none of its own."""
    one(farm)
    farm.gpu.mode, farm.gpu.node = "nvidia", "/dev/dri/renderD129"
    farm.gpu.boot_args = ["androidboot.redroid_gpu_mode=venus"]
    farm.gpu.run_args = ["-v", "/run/venus/renderD129:/venus", "-v", "/rt/vulkan.virtio.so:/vendor/lib64/hw/vulkan.virtio.so:ro"]
    result = farm.mgr.start(1)
    args = farm.docker.containers[ANDROID]["args"]
    assert options(args, "-v") == [f"{VOLUME}:/data", "/run/venus/renderD129:/venus",
                                   "/rt/vulkan.virtio.so:/vendor/lib64/hw/vulkan.virtio.so:ro"]
    assert labels_of(args)["io.superfarm.gpu"] == "nvidia" and result["gpu"] == "nvidia"
    assert labels_of(args)["io.superfarm.gpu-node"] == "/dev/dri/renderD129"


def test_gpu_choice_sees_running_androids_per_card(farm):
    farm.mgr.create(count=2)
    farm.gpu.mode, farm.gpu.node = "intel", "/dev/dri/renderD128"
    farm.mgr.start(1)
    farm.events.clear()
    farm.mgr.start(2)
    assert "gpu:{'/dev/dri/renderD128': 1}" in farm.events


def test_unavailable_gpu_mode_fails_with_the_reason(farm):
    one(farm)

    def refuse(r, config, paths, *, counts=None):
        raise ValueError("gpu mode nvidia is not available: the relay is not built")

    farm.mgr.backends.gpu_choose = refuse
    with pytest.raises(StartError, match="Android 1: gpu mode nvidia is not available: the relay is not built"):
        farm.mgr.start(1)
    assert "relay is not built" in farm.mgr.status(1)["last_error"]
    assert farm.docker.containers == {}


def test_a_network_that_cannot_be_planned_fails_the_start_with_the_reason(farm):
    one(farm)

    def refuse(r, config, paths, store, docker):
        raise ValueError("VPN profile 'eu.ovpn' asks for a username and password, and none is stored")

    farm.mgr.backends.sidecar_run_args = refuse
    with pytest.raises(StartError, match=r"Android 1: network \(direct\): VPN profile 'eu.ovpn' asks"):
        farm.mgr.start(1)
    assert farm.docker.containers == {}


def test_extra_run_args_providers_reach_the_android_and_the_plan(farm):
    one(farm)
    farm.mgr.start(1)
    farm.mgr.stop(1)
    farm.mgr.extra_run_args_providers.append(lambda r: ["-v", f"/run/mic/{r.id}:/dev/mic"])
    assert farm.mgr.start(1)["recreated"]                    # new mounts are a new plan
    assert "/run/mic/1:/dev/mic" in options(farm.docker.containers[ANDROID]["args"], "-v")

    def broken(r):
        raise RuntimeError("camera module not loaded")

    farm.mgr.stop(1)
    farm.mgr.extra_run_args_providers.append(broken)
    with pytest.raises(StartError, match="camera module not loaded"):
        farm.mgr.start(1)


def test_a_failing_hook_is_a_warning_not_a_failed_start(farm):
    one(farm)

    def attach_camera(r, adb):
        raise RuntimeError("no video device")

    farm.mgr.post_boot_hooks.append(attach_camera)
    result = farm.mgr.start(1)
    assert result["state"] == "booted"
    assert any("attach_camera" in w and "no video device" in w for w in result["warnings"])


def test_foreign_container_with_our_name_is_never_touched(farm):
    one(farm)
    farm.docker.containers[ANDROID] = {"running": True, "labels": {}, "args": [], "image": "x"}
    for call in (lambda: farm.mgr.start(1), lambda: farm.mgr.stop(1), lambda: farm.mgr.delete(1)):
        with pytest.raises(InstanceError, match="did not create"):
            call()
    assert farm.docker.containers[ANDROID]["running"] is True
    assert farm.docker.names_called("rm") == [] and farm.docker.names_called("stop") == []


def test_neighbouring_modules_are_looked_up_late_and_missing_ones_named(monkeypatch):
    import types

    package = types.ModuleType("superfarm.net")
    package.__path__ = []                                      # a package with no submodules
    package.ensure_image = lambda docker: "superfarm/net"
    monkeypatch.setitem(sys.modules, "superfarm.net", package)
    for sub in ("sidecar", "status", "vpn"):
        monkeypatch.delitem(sys.modules, f"superfarm.net.{sub}", raising=False)
    assert inst._net_function("ensure_image")(None) == "superfarm/net"
    with pytest.raises(InstanceError, match=r"does not provide wait_ready\(\)"):
        inst._net_function("wait_ready")
    # a function that lives in a submodule (and a module of the same name) is still found
    status = types.ModuleType("superfarm.net.status")
    status.status = lambda docker, r: {"state": "connected"}
    package.status = status
    monkeypatch.setitem(sys.modules, "superfarm.net.status", status)
    assert inst._net_function("status")(None, None) == {"state": "connected"}
    assert inst.Backends().net_status(None, None) == {"state": "connected"}


def test_docker_not_answering_is_never_mistaken_for_no_container(farm):
    one(farm)
    farm.mgr.start(1)
    real = farm.docker.run

    def down(argv, **kw):
        if list(argv)[:2] == ["container", "inspect"]:
            return Result(1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
        return real(argv, **kw)

    farm.docker.run = down
    for call in (lambda: farm.mgr.delete(1), lambda: farm.mgr.stop(1), lambda: farm.mgr.start(1)):
        with pytest.raises(InstanceError, match="Docker did not answer"):
            call()
    assert farm.store.ids() == [1] and VOLUME in farm.docker.volume_labels


def test_a_hand_edited_volume_path_is_refused_everywhere(farm):
    one(farm)
    spec = farm.store.get(1)
    spec.data_volume = "/etc"
    farm.store.save(spec)
    with pytest.raises((InstanceError, ValueError), match="volume"):
        farm.mgr.start(1)
    with pytest.raises(InstanceError, match="not a Docker volume name"):
        farm.mgr.delete(1)
    with pytest.raises(InstanceError, match="not a Docker volume name"):
        farm.mgr.clone(1, with_data=True)
    assert farm.docker.helper_runs == [] and farm.docker.containers == {}


def test_missing_adb_is_explained(farm):
    one(farm)

    class NoAdb:
        serial = "127.0.0.1:17001"

        def __init__(self, serial, binary=None):
            pass

        def connect(self, attempts=5, delay=1.0):
            raise inst.CommandError(["adb", "connect"], 127, "", "adb: command not found")

        def state(self):
            raise inst.CommandError(["adb"], 127, "", "adb: command not found")

        def disconnect(self):
            raise inst.CommandError(["adb"], 127, "", "adb: command not found")

    farm.mgr.backends.adb = NoAdb
    with pytest.raises(StartError, match="android-tools"):
        farm.mgr.start(1)
    assert farm.mgr.status(1)["state"] == "running"          # the container is up; status still answers


# ------------------------------------------------------------------ shader caches

def test_shader_caches_are_cleared_when_the_renderer_changes(farm):
    one(farm)
    farm.mgr.start(1)
    assert farm.docker.helper_runs == []                      # brand-new volume: nothing to clear
    assert farm.store.state_get(1, "renderer") == "software"
    farm.mgr.stop(1)
    farm.gpu.mode, farm.gpu.node = "intel", "/dev/dri/renderD128"
    farm.mgr.start(1)
    assert len(farm.docker.helper_runs) == 1
    run = farm.docker.helper_runs[0]
    assert option(run, "--network") == "none" and f"{VOLUME}:/data" in options(run, "-v")
    assert run[-2:] == ["sh", "intel"]                        # the mode is an argument, never part of the script
    assert "shaders_cache" in run[-3] and "intel" not in run[-3]
    assert run.index("alpine:latest") < run.index("sh")
    assert farm.store.state_get(1, "renderer") == "intel"
    assert farm.docker.names_called("create")[-2:] == [NET, ANDROID]
    farm.mgr.restart(1)
    assert len(farm.docker.helper_runs) == 1                  # same renderer: no helper container


def test_adopted_volume_is_checked_against_its_own_marker(farm):
    farm.docker.volume_labels["old_data"] = {}
    one(farm, data_volume="old_data")
    farm.mgr.start(1)
    assert len(farm.docker.helper_runs) == 1 and "old_data:/data" in options(farm.docker.helper_runs[0], "-v")


def test_shader_clearing_can_be_switched_off(tmp_path):
    farm = Farm(tmp_path, {"gpu": {"clear_shader_cache_on_change": False}})
    try:
        farm.docker.volume_labels["old_data"] = {}
        one(farm, data_volume="old_data")
        farm.mgr.start(1)
        assert farm.docker.helper_runs == []
    finally:
        farm.close()


# --------------------------------------------------------------------------- trim

def test_trim_list_files_and_overrides(tmp_path):
    (tmp_path / "13.txt").write_text("# comment\ncom.android.egg\ncom.android.bluetooth  # radio\n\nnot a package\ncom.android.systemui\n")
    (tmp_path / "default.txt").write_text("com.android.egg\n")
    assert trim_list("13", directory=tmp_path) == ["com.android.egg", "com.android.bluetooth"]     # systemui is protected
    assert trim_list("11", directory=tmp_path) == ["com.android.egg"]
    assert trim_list("../13", directory=tmp_path) == ["com.android.egg"]
    assert trim_list("13", extra=["com.example.bloat", "com.android.egg"], keep=["com.android.bluetooth"],
                     directory=tmp_path) == ["com.android.egg", "com.example.bloat"]
    assert trim_hash([]) == "none" and len(trim_hash(["a.b"])) == 12 and trim_hash(["a.b"]) != trim_hash(["a.c"])


def test_shipped_trim_lists_are_valid_and_safe():
    full, fallback = trim_list("13"), trim_list("no-such-version")
    assert len(full) == 38 and "com.android.bluetooth" in full
    assert 5 <= len(fallback) < len(full) and set(fallback) <= set(full)
    assert not set(full) & inst.PROTECTED_PACKAGES


def test_trim_runs_once_per_list(farm):
    one(farm)
    farm.mgr.start(1)
    dev = farm.device(1)
    assert "com.android.egg" not in dev["enabled"] and "com.android.bluetooth" not in dev["enabled"]
    assert {"com.android.systemui", "com.example.app"} <= dev["enabled"]
    marker = dev["files"][inst.TRIM_MARKER].splitlines()
    assert marker[0] == trim_hash(trim_list("13")) and "com.android.egg" in marker[1:]
    assert farm.store.state_get(1, "trim")["hash"] == marker[0]

    dev["shell"].clear()
    farm.mgr.restart(1)
    assert not [c for c in dev["shell"] if isinstance(c, list) and c[:2] == ["sh", "-c"]]
    assert "pm list packages -e" not in dev["shell"]


def test_trim_trusts_the_marker_inside_an_adopted_android(farm):
    one(farm)
    dev = farm.device(1)
    dev["files"][inst.TRIM_MARKER] = trim_hash(trim_list("13")) + "\ncom.android.egg"
    farm.mgr.start(1)
    assert "com.android.egg" in dev["enabled"]              # not touched again: the marker says it was done
    assert farm.store.state_get(1, "trim")["hash"] == trim_hash(trim_list("13"))


def test_trim_list_change_reenables_what_was_dropped_and_off_undoes_all(farm):
    one(farm)
    farm.mgr.start(1)
    dev = farm.device(1)
    farm.mgr.config.performance.trim_keep = ["com.android.egg"]
    farm.mgr.restart(1)
    assert "com.android.egg" in dev["enabled"] and "com.android.bluetooth" not in dev["enabled"]
    farm.mgr.config.performance.trim_packages = False
    farm.mgr.restart(1)
    assert {"com.android.bluetooth", "com.android.deskclock"} <= dev["enabled"]
    assert inst.TRIM_MARKER not in dev["files"]
    assert farm.store.state_get(1, "trim") == {"hash": "none", "packages": []}


# -------------------------------------------------------------------------- proxy

def test_proxy_mode_sets_the_proxy_and_leaving_it_clears_only_ours(farm):
    one(farm, network={"mode": "proxy", "proxy": "10.0.0.5:3128"})
    farm.mgr.start(1)
    dev = farm.device(1)
    assert dev["settings"]["global/http_proxy"] == "10.0.0.5:3128"
    farm.mgr.update(1, {"network": {"mode": "direct", "proxy": None}})
    farm.mgr.restart(1)
    assert dev["settings"]["global/http_proxy"] == ":0"
    dev["settings"]["global/http_proxy"] = "set.by.hand:8080"
    farm.mgr.restart(1)
    assert dev["settings"]["global/http_proxy"] == "set.by.hand:8080"


def test_malformed_proxy_is_a_warning(farm):
    one(farm, network={"mode": "proxy", "proxy": "http://user:pw@host:1"})
    result = farm.mgr.start(1)
    assert any("proxy" in w for w in result["warnings"])
    assert "global/http_proxy" not in farm.device(1)["settings"]


# -------------------------------------------------------------------- cpu pinning

def test_cpu_pinning_gives_each_android_its_own_cores_and_remembers(tmp_path):
    farm = Farm(tmp_path, {"resources": {"cpu_pinning": "spread"}})
    try:
        farm.mgr.create(count=2)
        farm.mgr.start(1)
        farm.mgr.start(2)
        a = option(farm.docker.containers[f"{PREFIX}-android-1"]["args"], "--cpuset-cpus")
        b = option(farm.docker.containers[f"{PREFIX}-android-2"]["args"], "--cpuset-cpus")
        assert (a, b) == ("0-1", "2-3")
        assert farm.docker.containers[f"{PREFIX}-android-2"]["labels"]["io.superfarm.cpuset"] == "2-3"
        farm.mgr.stop(1)
        assert not farm.mgr.start(1)["recreated"]            # the pinning is kept, so the plan is unchanged
        assert farm.mgr.resolved(1).cpuset == "0-1"
        # cores that vanished force a fresh draw
        farm.mgr.backends.cpu_topology = lambda: CpuTopology(cores=(Core(id=0, cpus=(4, 5)),))
        farm.mgr.stop(1)
        assert farm.mgr.start(1)["recreated"]
        assert option(farm.docker.containers[f"{PREFIX}-android-1"]["args"], "--cpuset-cpus") == "4-5"
    finally:
        farm.close()


def test_explicit_cpuset_wins_over_the_policy(tmp_path):
    farm = Farm(tmp_path, {"resources": {"cpu_pinning": "spread"}})
    try:
        farm.mgr.create(template={"resources": {"cpuset": "6,7"}})
        farm.mgr.start(1)
        assert option(farm.docker.containers[ANDROID]["args"], "--cpuset-cpus") == "6,7"
        assert farm.docker.containers[ANDROID]["labels"]["io.superfarm.cpuset"] == "6+7"
    finally:
        farm.close()


# ------------------------------------------------------------------------- delete

def test_delete_removes_containers_data_and_settings(farm):
    one(farm)
    farm.mgr.start(1)
    farm.mgr.delete(1)
    assert farm.docker.containers == {} and VOLUME not in farm.docker.volume_labels
    assert farm.store.ids() == []
    with pytest.raises(KeyError):
        farm.mgr.status(1)


def test_delete_keep_data_leaves_the_volume(farm):
    one(farm)
    farm.mgr.start(1)
    farm.mgr.delete(1, keep_data=True)
    assert VOLUME in farm.docker.volume_labels and farm.store.ids() == []


def test_delete_never_removes_an_adopted_volume_unless_told_to(farm):
    farm.docker.volume_labels["old_data"] = {}
    one(farm, data_volume="old_data")
    farm.mgr.start(1)
    farm.mgr.delete(1)
    assert "old_data" in farm.docker.volume_labels and farm.docker.containers == {} and farm.store.ids() == []
    one(farm, data_volume="old_data")
    farm.mgr.delete(1, remove_adopted=True)
    assert "old_data" not in farm.docker.volume_labels


def test_delete_leaves_a_volume_without_the_farm_label(farm):
    one(farm)
    farm.docker.volume_labels[VOLUME] = {"io.superfarm.farm": "someone-else", "io.superfarm.instance": "1"}
    farm.mgr.delete(1)
    assert VOLUME in farm.docker.volume_labels and farm.store.ids() == []


def test_delete_keeps_the_instance_when_its_volume_cannot_be_removed(farm):
    one(farm)
    farm.mgr.start(1)
    farm.docker.volume_rm = lambda name: False
    with pytest.raises(InstanceError, match="could not be removed"):
        farm.mgr.delete(1)
    assert farm.store.ids() == [1]


# ----------------------------------------------------------------- update / clone

def test_update_validates_and_takes_effect_later(farm):
    one(farm)
    spec = farm.mgr.update(1, {"name": "Main", "tags": ["a", "b"], "resources": {"memory_mb": 2048}})
    assert spec.name == "Main" and spec.resources.memory_mb == 2048
    assert farm.store.get(1).tags == ["a", "b"]
    for bad in ({"fps": 1000}, {"nope": 1}, {"id": 2}, {"network": {"mode": "gateway", "gateway": "none"}},
                {"data_volume": "missing"}, {"device": {"geo": "north"}}, {"android_version": "5"}):
        with pytest.raises(ValueError):
            farm.mgr.update(1, bad)
    assert farm.store.get(1).fps is None
    with pytest.raises(KeyError):
        farm.mgr.update(9, {"name": "x"})


def test_update_merges_maps_replaces_lists_and_null_removes(farm):
    one(farm, tags=["eu", "batch-1"], fps=30, extra_boot_args=["androidboot.a=1", "androidboot.b=2"],
        extra_props={"debug.one": "1", "debug.two": "2"}, resources={"cpus": 3, "memory_mb": 2048},
        network={"mode": "proxy", "proxy": "10.0.0.1:3128"})
    created = farm.store.get(1).created

    # maps merge key by key; a key set to null is removed, the others stay
    spec = farm.mgr.update(1, {"extra_props": {"debug.one": None, "debug.three": "3"}})
    assert spec.extra_props == {"debug.two": "2", "debug.three": "3"}
    assert farm.mgr.update(1, {"extra_props": {}}).extra_props == spec.extra_props      # nothing named, nothing changed
    assert farm.mgr.update(1, {"extra_props": {"debug.never-set": None}}).extra_props == spec.extra_props
    assert farm.mgr.update(1, {"resources": {"cpus": None}}).resources.model_dump(exclude_none=True) == {"memory_mb": 2048}
    assert farm.mgr.resolved(1).cpus == farm.config.resources.cpus                    # back to the farm default

    # lists are replaced whole, never appended to
    assert farm.mgr.update(1, {"tags": ["us"]}).tags == ["us"]
    assert farm.mgr.update(1, {"extra_boot_args": ["androidboot.b=2"]}).extra_boot_args == ["androidboot.b=2"]
    assert farm.mgr.update(1, {"tags": []}).tags == []

    # null on a field removes the override: farm default again, or empty
    spec = farm.mgr.update(1, {"fps": None, "extra_boot_args": None, "extra_props": None, "network": None,
                               "name": None, "tags": None})
    assert spec.fps is None and spec.extra_boot_args == [] and spec.extra_props == {} and spec.tags == []
    assert spec.network.mode is None and spec.network.proxy is None and spec.name == ""
    assert farm.mgr.resolved(1).props == {} and farm.mgr.resolved(1).net_mode == "direct"
    assert farm.store.get(1) == spec and spec.created == created

    # a map that was empty takes new keys; a null among them has nothing to remove
    assert farm.mgr.update(1, {"extra_props": {"debug.x": "1", "debug.y": None}}).extra_props == {"debug.x": "1"}


def test_update_can_remove_one_device_property_but_never_the_identity(farm):
    before = one(farm, device={"profile": "samsung-s23", "props": {"ro.extra.flag": "1"}})
    spec = farm.mgr.update(1, {"device": {"props": {"ro.extra.flag": None}, "timezone": "Europe/Rome"}})
    assert "ro.extra.flag" not in spec.device.props and spec.device.props["ro.product.model"] == "SM-S911B"
    assert spec.device.android_id == before.device.android_id and spec.device.serial == before.device.serial
    assert farm.mgr.update(1, {"device": {"timezone": None}}).device.timezone is None
    with pytest.raises(ValueError, match="device identity cannot be removed"):
        farm.mgr.update(1, {"device": None})
    for fact in ({"id": None}, {"id": 2}):
        with pytest.raises(ValueError, match="id cannot be changed"):
            farm.mgr.update(1, fact)
    assert farm.mgr.update(1, {"id": 1, "created": 0, "notes": "kept"}).created == before.created
    assert farm.store.get(1).device.android_id == before.device.android_id


def test_update_to_another_device_profile_swaps_its_properties(farm):
    spec = one(farm, device={"profile": "samsung-s23"})
    new = farm.mgr.update(1, {"device": {"profile": "xiaomi-13"}})
    assert new.device.props["ro.product.model"] == "2211133G"
    assert new.device.android_id == spec.device.android_id          # the id itself stays


def test_clone_gets_the_settings_but_a_new_identity(farm):
    src = one(farm, name="Original", tags=["x"], fps=30, device={"profile": "samsung-s23"})
    copy = farm.mgr.clone(1, with_data=False)
    assert copy.id == 2 and copy.name == "Original (copy)" and copy.tags == ["x"] and copy.fps == 30
    assert copy.device.profile == "samsung-s23"
    assert copy.device.android_id != src.device.android_id and copy.device.serial != src.device.serial
    assert copy.data_volume is None and farm.docker.helper_runs == []


def test_clone_with_data_copies_the_volume_with_attributes(farm):
    one(farm)
    farm.mgr.start(1)
    with pytest.raises(InstanceError, match="stop it before cloning"):
        farm.mgr.clone(1, with_data=True)
    assert farm.store.ids() == [1]
    farm.mgr.stop(1)
    copy = farm.mgr.clone(1, with_data=True)
    run = farm.docker.helper_runs[-1]
    assert f"{VOLUME}:/from:ro" in options(run, "-v") and f"{PREFIX}-data-{copy.id}:/to" in options(run, "-v")
    assert "--xattrs" in run[-1] and "--numeric-owner" in run[-1] and "--acls" in run[-1] and "--sparse" in run[-1]
    assert option(run, "--network") == "none"
    assert farm.docker.volume_labels[f"{PREFIX}-data-{copy.id}"]["io.superfarm.instance"] == str(copy.id)


def test_failed_data_clone_leaves_nothing_behind(farm):
    one(farm)
    farm.mgr.start(1)
    farm.mgr.stop(1)

    def boom(*a, **k):
        raise inst.CommandError(["docker"], 1, "", "tar: write error")

    farm.mgr._helper_run = boom
    with pytest.raises(inst.CommandError):
        farm.mgr.clone(1, with_data=True)
    assert farm.store.ids() == [1] and f"{PREFIX}-data-2" not in farm.docker.volume_labels


# ------------------------------------------------------------------------ locking

def test_two_operations_on_one_android_do_not_interleave(farm):
    one(farm)
    farm.mgr.create()
    entered, release = threading.Event(), threading.Event()

    def slow_hook(r, adb):
        entered.set()
        release.wait(5)

    farm.mgr.post_boot_hooks.append(slow_hook)
    t = threading.Thread(target=farm.mgr.start, args=(1,))
    t.start()
    assert entered.wait(5)
    try:
        assert farm.mgr.list_status()[0]["busy"] == "starting"
        with pytest.raises(InstanceBusy, match="starting"):
            farm.mgr.stop(1)
        with pytest.raises(InstanceBusy):
            farm.mgr.delete(1)
        farm.mgr.post_boot_hooks.clear()
        assert farm.mgr.start(2)["state"] == "booted"          # another Android is not held up
    finally:
        release.set()
        t.join(5)
    farm.mgr.stop(1)                                           # free again
    assert farm.mgr.list_status()[0]["busy"] == ""


def test_lock_is_released_after_a_failure(farm):
    one(farm)
    farm.net_ready = {"ready": False, "state": "connecting", "detail": ""}
    with pytest.raises(StartError):
        farm.mgr.start(1)
    farm.net_ready = {"ready": True}
    assert farm.mgr.start(1)["state"] == "booted"


# ------------------------------------------------------------------ status / bulk

def test_list_status_costs_one_docker_ps_and_no_adb(farm):
    farm.mgr.create(count=3)
    farm.mgr.start(1)
    farm.mgr.start(2)
    farm.mgr.stop(2)
    farm.docker.calls.clear()
    shells = len(farm.device(1)["shell"])
    rows = farm.mgr.list_status()
    assert [c[0] for c in farm.docker.calls] == ["ps"]
    assert len(farm.device(1)["shell"]) == shells
    assert [(r["id"], r["state"]) for r in rows] == [(1, "booted"), (2, "stopped"), (3, "missing")]
    assert rows[0]["serial"] == "127.0.0.1:17001" and rows[0]["adb_port"] == 17001
    assert rows[0]["gpu"]["mode"] == "software" and rows[0]["net"]["mode"] == "direct"
    assert rows[0]["containers"]["net"]["state"] == "running"


def test_status_gives_live_details(farm):
    spec = one(farm, name="Probe")
    farm.mgr.start(1)
    s = farm.mgr.status(1)
    assert s["name"] == "Probe" and s["state"] == "booted" and s["uptime"] > 0
    assert s["net"]["public_ip"] == "198.51.100.7" and s["net"]["state"] == "connected"
    assert s["device_profile"] == spec.device.profile and s["data_volume"] == VOLUME and not s["adopted"]
    farm.device(1)["boots"] = False
    assert farm.mgr.status(1)["state"] == "running"


def test_status_survives_a_broken_setting(farm):
    one(farm)
    farm.mgr.config.android.images.pop("13")
    row = farm.mgr.list_status()[0]
    assert "config_error" in row and row["state"] == "missing"


def test_select_understands_running_and_stopped(farm):
    farm.mgr.create(count=3, template={"tags": ["eu"]})
    farm.mgr.start(2)
    assert farm.mgr.select("running") == [2]
    assert farm.mgr.select("stopped") == [1, 3]
    assert farm.mgr.select("tag:eu,!2") == [1, 3]


def test_bulk_runs_every_android_and_one_failure_does_not_stop_the_rest(farm):
    farm.mgr.create(count=3)
    farm.docker.fail_run_of.add(f"{PREFIX}-android-2")
    task = farm.tasks.wait(farm.mgr.bulk("start", "all"), timeout=20)
    assert task.state == "done" and task.targets == [1, 2, 3]
    assert task.results[1]["ok"] and task.results[3]["ok"] and not task.results[2]["ok"]
    assert "booted" in task.results[1]["detail"] and "StartError" in task.results[2]["detail"]
    task = farm.tasks.wait(farm.mgr.bulk("stop", "running"), timeout=20)
    assert sorted(task.results) == [1, 3] and farm.mgr.select("running") == []
    with pytest.raises(ValueError, match="unknown action"):
        farm.mgr.bulk("explode", "all")


def test_autostart_picks_configured_and_flagged_androids(farm):
    farm.mgr.create(count=3, template={"tags": ["always"]})
    farm.mgr.update(1, {"tags": []})
    farm.mgr.update(1, {"autostart": True})
    farm.mgr.update(3, {"tags": []})
    assert farm.tasks.wait(farm.mgr.autostart(), timeout=20).targets == [1]     # finished: a second bulk start
    farm.mgr.config.performance.autostart = "tag:always"                        # on the same Android is refused until then
    task = farm.tasks.wait(farm.mgr.autostart(), timeout=20)
    assert task.targets == [1, 2]
    farm.mgr.config.performance.autostart = ""
    farm.mgr.update(1, {"autostart": False})
    assert farm.mgr.autostart() is None


def test_reload_config_reaches_the_image_builder(farm):
    new = farm.config.model_copy(deep=True)
    new.android.fps = 33
    farm.mgr.reload_config(new)
    one(farm)
    assert farm.mgr.resolved(1).fps == 33 and farm.mgr.images.config is new


def test_adb_port_out_of_range_is_refused(tmp_path):
    farm = Farm(tmp_path, {"network": {"adb_port_base": 60000}})
    try:
        farm.store.save(inst.InstanceSpec(id=9000))
        with pytest.raises(ValueError, match="adb_port_base"):
            farm.mgr.resolved(9000)
    finally:
        farm.close()

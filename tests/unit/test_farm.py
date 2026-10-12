"""The composition root: lazy building, unavailable components, config changes, tick, close."""
from __future__ import annotations

import pytest
from api_fakes import make_farm

from superfarm import farm as farm_mod
from superfarm.config import Config, load_config
from superfarm.farm import Farm, Unavailable, config_effects, merge_patch


def test_new_farm_creates_its_home_and_builds_nothing(tmp_path):
    farm = Farm(tmp_path / "home")
    assert (tmp_path / "home" / "secrets").stat().st_mode & 0o777 == 0o700
    assert farm.config == Config()
    assert farm._built == {}


def test_components_are_built_once_and_share_their_neighbours(tmp_path):
    farm, core = make_farm(tmp_path)
    assert farm.instances is core.mgr
    assert farm.images is core.mgr.images            # one set of build locks for manager and API
    assert farm.apps is farm.apps
    assert farm.apps.mgr is core.mgr and farm.apps.library is farm.library
    assert farm.input._screen is farm.screen         # clicks are mapped on the picture the viewer saw
    assert farm.backups.mgr is core.mgr


def test_building_the_manager_wires_the_camera_before_any_start(tmp_path):
    farm = Farm(tmp_path / "home")
    mgr = farm.instances
    camera = farm._built["camera"]
    assert camera.post_boot in mgr.post_boot_hooks
    assert camera.extra_run_args in mgr.extra_run_args_providers


def test_a_component_that_cannot_be_built_is_unavailable_not_fatal(tmp_path, monkeypatch):
    def broken(self):
        raise FileNotFoundError("scrcpy is not installed")
    monkeypatch.setitem(Farm._FACTORIES, "windows", broken)
    farm, _ = make_farm(tmp_path)
    with pytest.raises(Unavailable) as first:
        _ = farm.windows
    assert first.value.component == "windows" and "scrcpy is not installed" in first.value.reason
    with pytest.raises(Unavailable):          # remembered: not rebuilt on every request
        _ = farm.windows
    status = farm.components()
    assert status["windows"] == {"available": False, "reason": "FileNotFoundError: scrcpy is not installed"}
    assert status["instances"]["available"] and status["automation"]["available"]


def test_use_replaces_a_component_and_rejects_unknown_names(tmp_path):
    farm = Farm(tmp_path / "home")
    marker = object()
    farm.use("screen", marker)
    assert farm.screen is marker
    with pytest.raises(KeyError):
        farm.use("nonsense", marker)


# ------------------------------------------------------------------------------ config

def test_merge_patch_follows_rfc_7396():
    base = {"a": {"b": 1, "c": 2}, "list": [1, 2], "x": 1}
    assert merge_patch(base, {"a": {"b": 5}}) == {"a": {"b": 5, "c": 2}, "list": [1, 2], "x": 1}
    assert merge_patch(base, {"a": {"c": None}, "x": None}) == {"a": {"b": 1}, "list": [1, 2]}
    assert merge_patch(base, {"list": [3]})["list"] == [3]
    assert base == {"a": {"b": 1, "c": 2}, "list": [1, 2], "x": 1}      # the input is not modified


def test_set_config_saves_and_reaches_every_built_component(tmp_path):
    farm, core = make_farm(tmp_path)
    screen, automation = farm.screen, farm.automation
    new = farm.set_config({"android": {"fps": 30}, "stream": {"jpeg_quality": 80}, "automation": {"max_parallel": 3}})
    assert new.android.fps == 30 and farm.config is new
    assert load_config(farm.paths).android.fps == 30                  # on disk
    assert core.mgr.config is new and core.mgr.images.config is new
    assert screen._quality == 80
    assert automation.config.automation.max_parallel == 3


def test_set_config_rejects_bad_values_and_changes_nothing(tmp_path):
    farm, core = make_farm(tmp_path)
    before = farm.config
    for patch in ({"android": {"fps": 1000}}, {"no_such_section": {}}, {"server": {"host": "0.0.0.0", "auth_required": False}},
                  "not a mapping"):
        with pytest.raises(ValueError):
            farm.set_config(patch)
    assert farm.config is before and core.mgr.config is before
    assert load_config(farm.paths) == before


def test_null_in_a_patch_resets_a_setting_and_removes_a_mapping_entry(tmp_path):
    farm, _ = make_farm(tmp_path, {"android": {"fps": 30}})
    assert farm.set_config({"android": {"fps": None}}).android.fps == Config().android.fps
    farm.set_config({"network": {"gateways": {"eu": {"profile": "eu.ovpn"}}}})
    assert "eu" in farm.config.network.gateways
    assert farm.set_config({"network": {"gateways": {"eu": None}}}).network.gateways == {}


def test_reload_config_reads_the_file_again(tmp_path):
    farm, core = make_farm(tmp_path)
    farm.paths.config.write_text("android:\n  fps: 42\n")
    assert farm.reload_config().android.fps == 42
    assert core.mgr.config.android.fps == 42


def test_config_effects_say_what_needs_a_restart():
    old = Config()
    new = Config.model_validate({"android": {"fps": 30}, "stream": {"live_fps": 5}, "server": {"port": 7800},
                                 "performance": {"trim_packages": False, "start_concurrency": 2}})
    effects = config_effects(old, new)
    assert effects["changed"] == ["android.fps", "performance.start_concurrency", "performance.trim_packages",
                                  "server.port", "stream.live_fps"]
    assert effects["restart_android"] == ["android.fps", "performance.trim_packages"]
    assert effects["restart_daemon"] == ["server.port"]
    assert config_effects(old, old) == {"changed": [], "restart_android": [], "restart_daemon": []}


# -------------------------------------------------------------------------------- info

def test_system_info_reports_host_capacity_and_components(tmp_path, monkeypatch):
    farm, core = make_farm(tmp_path)
    monkeypatch.setattr(farm, "doctor", lambda fresh=False: [])
    monkeypatch.setattr(farm, "gpus", lambda: [])
    info = farm.system_info()
    assert info["version"] and info["farm_name"] == "Super Farm"
    assert info["host"]["kernel"] and info["host"]["cpu"]["threads"] >= 1 and info["host"]["ram_mb"] > 0
    assert info["gpus"] == []
    assert info["capacity"]["estimate"]["max_instances"] == core.capacity_max
    assert info["doctor"] == {"counts": {"ok": 0, "warn": 0, "fail": 0, "skip": 0}, "ready": True, "worst": "ok"}
    assert info["components"]["instances"]["available"]


def test_system_info_survives_a_failing_probe(tmp_path, monkeypatch):
    farm, _ = make_farm(tmp_path)
    monkeypatch.setattr(farm, "doctor", lambda fresh=False: [])

    def no_gpus():
        raise OSError("sysfs is not mounted")
    monkeypatch.setattr(farm, "gpus", no_gpus)
    info = farm.system_info()
    assert info["gpus"] == {"error": "OSError: sysfs is not mounted"}
    assert info["host"]["kernel"]


def test_doctor_results_are_reused_for_a_while(tmp_path, monkeypatch):
    farm, _ = make_farm(tmp_path)
    calls = []
    from superfarm.host import doctor
    monkeypatch.setattr(doctor, "run_checks", lambda config, paths: calls.append(1) or [])
    farm.doctor()
    farm.doctor()
    assert len(calls) == 1
    farm.doctor(fresh=True)
    assert len(calls) == 2
    farm.set_config({"android": {"fps": 21}})          # a new config may change the verdict
    farm.doctor()
    assert len(calls) == 3


# ------------------------------------------------------------------------ tick / close

class _Recorder:
    def __init__(self, fail: str = ""):
        self.calls, self.fail = [], fail

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append(name)
            if name == self.fail:
                raise RuntimeError(f"{name} broke")
        return method


def test_tick_runs_every_job_even_when_one_fails(tmp_path, caplog):
    farm, _ = make_farm(tmp_path, {"media": {"camera": {"enabled": True}}})
    automation, backups, camera = _Recorder(fail="tick"), _Recorder(), _Recorder()
    farm.use("automation", automation)
    farm.use("backups", backups)
    farm.use("camera", camera)
    farm.tick()
    assert automation.calls == ["tick"] and backups.calls == ["tick"] and camera.calls == ["heal"]
    assert "automation schedules failed" in caplog.text


def test_tick_leaves_the_camera_alone_when_it_is_off_or_unavailable(tmp_path):
    farm, _ = make_farm(tmp_path)
    camera = _Recorder()
    farm.use("camera", camera)
    farm.tick()
    assert camera.calls == []


def test_close_stops_host_processes_and_never_touches_androids(tmp_path):
    farm, core = make_farm(tmp_path)
    core.mgr.create(count=1)
    core.mgr.start(1)
    windows, listener, camera, screen = _Recorder(), _Recorder(fail="stop"), _Recorder(), _Recorder()
    for name, fake in (("windows", windows), ("listener", listener), ("camera", camera), ("screen", screen)):
        farm.use(name, fake)
    before = len(core.docker.calls)
    farm.close()
    assert windows.calls == ["close_all"] and listener.calls == ["stop"] and screen.calls == ["close"]
    assert camera.calls == ["unwatch", "down"]         # a failing listener did not stop the rest
    assert core.docker.calls[before:] == []            # not one Docker call: Androids keep running
    assert core.docker.running(core.names(1).android)
    farm.close()                                        # idempotent
    assert windows.calls == ["close_all"]


def test_close_only_touches_what_was_built(tmp_path, monkeypatch):
    farm = Farm(tmp_path / "home")
    monkeypatch.setitem(Farm._FACTORIES, "windows", lambda self: pytest.fail("built during close"))
    farm.close()
    assert farm._built == {}


def test_cpu_model_reads_proc_cpuinfo(tmp_path):
    f = tmp_path / "cpuinfo"
    f.write_text("processor\t: 0\nmodel name\t: Example CPU 9000\n")
    assert farm_mod._cpu_model(f) == "Example CPU 9000"
    assert isinstance(farm_mod._cpu_model(tmp_path / "missing"), str)


def test_trusted_proxies_are_addresses_or_networks_and_need_a_daemon_restart():
    assert Config().server.trusted_proxies == []
    ok = Config.model_validate({"server": {"trusted_proxies": ["127.0.0.1", "::1", "10.0.0.0/8"]}})
    assert config_effects(Config(), ok)["restart_daemon"] == ["server.trusted_proxies"]
    for bad in ("*", "proxy.example", "10.0.0.0/33", ""):
        with pytest.raises(ValueError, match="trusted_proxies"):
            Config.model_validate({"server": {"trusted_proxies": [bad]}})


def test_the_settings_say_what_the_api_token_is_worth():
    text = Config.model_json_schema()["$defs"]["ServerConfig"]["properties"]["host"]["description"]
    assert "root" in text and "API token" in text

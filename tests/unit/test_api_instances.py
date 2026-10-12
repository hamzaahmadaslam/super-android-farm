"""System, settings, images, tasks and the instance lifecycle routes."""
from __future__ import annotations

import threading
import time

import pytest
from api_fakes import api

from superfarm.config import Config, load_config
from superfarm.host import doctor as doctor_mod
from superfarm.host import gpu as gpu_mod
from superfarm.host import venus


@pytest.fixture
def a(tmp_path):
    with api(tmp_path) as made:
        yield made


def boot(a, count: int = 1) -> None:
    assert a.client.post("/api/instances", json={"count": count}).status_code == 201
    for i in range(1, count + 1):
        assert a.wait(a.client.post(f"/api/instances/{i}/start"))["results"][str(i)]["ok"]


# ------------------------------------------------------------------------------- system

def test_system_info(a, monkeypatch):
    monkeypatch.setattr(a.farm, "doctor", lambda fresh=False: [doctor_mod.Check("x", "X", "warn", "d", "fix")])
    monkeypatch.setattr(a.farm, "gpus", lambda: [])
    body = a.client.get("/api/system").json()
    assert body["version"] and body["host"]["kernel"] and body["gpus"] == []
    assert body["capacity"]["estimate"]["max_instances"] == 20
    assert body["doctor"]["worst"] == "warn" and body["doctor"]["ready"] is True


def test_capacity_route_is_the_managers_report(a):
    boot(a, 2)
    body = a.client.get("/api/system/capacity").json()
    assert body["running"] == 2 and body["headroom"] == 18 and body["over"] is False and body["enforced"] is False
    assert body["advice"] == ["add RAM"]


def test_doctor_lists_checks_with_their_fixes(a, monkeypatch):
    checks = [doctor_mod.Check("binder", "Binder", "fail", "module missing", "sudo modprobe binder_linux"),
              doctor_mod.Check("docker", "Docker", "ok")]
    monkeypatch.setattr(doctor_mod, "run_checks", lambda config, paths: checks)
    body = a.client.get("/api/system/doctor").json()
    assert body["checks"][0] == {"id": "binder", "title": "Binder", "status": "fail", "detail": "module missing",
                                 "fix": "sudo modprobe binder_linux"}
    assert body["summary"]["ready"] is False and body["summary"]["counts"]["fail"] == 1


def test_metrics_snapshot_is_shared_for_a_moment(a):
    boot(a)
    before = len(a.core.docker.calls)
    first = a.client.get("/api/system/metrics").json()
    assert first["totals"] == {"defined": 1, "running": 1, "cpu_percent": 10.0, "mem_mb": 900.0}
    assert first["instances"]["1"]["pids"] == 80
    spent = len(a.core.docker.calls) - before
    assert a.client.get("/api/system/metrics").json()["time"] == first["time"]
    assert len(a.core.docker.calls) - before == spent           # the second caller cost nothing


def test_gpus_lists_cards_options_and_relay(a, monkeypatch):
    card = gpu_mod.GpuInfo(node="/dev/dri/renderD128", driver="i915", vendor="intel", name="Example Graphics",
                           pci="0000:00:02.0", usable_modes=["intel"])
    monkeypatch.setattr(a.farm, "gpus", lambda: [card])
    monkeypatch.setattr(venus, "relay_status", lambda config, nodes=None: [{"node": n} for n in nodes])
    body = a.client.get("/api/system/gpus").json()
    assert body["gpus"][0]["node"] == "/dev/dri/renderD128" and body["gpus"][0]["vendor"] == "intel"
    options = {(o["value"], o["node"]): o for o in body["options"]}
    assert all(set(o) == {"value", "label", "available", "reason", "node"} for o in body["options"])
    assert options[("software", "")]["available"] is True and options[("auto", "")]["available"] is True
    assert options[("intel", "/dev/dri/renderD128")]["available"] is True          # the exact card can be picked
    assert options[("nvidia", "")]["available"] is False and options[("nvidia", "")]["reason"]
    assert body["relay"]["relays"] == [] and "ready" in body["relay"]["runtime"]


def test_gpus_on_a_machine_without_a_gpu_still_offers_cpu_rendering(a, monkeypatch):
    monkeypatch.setattr(a.farm, "gpus", lambda: [])
    monkeypatch.setattr(venus, "relay_status", lambda config, nodes=None: [])
    body = a.client.get("/api/system/gpus").json()
    assert body["gpus"] == []
    assert [o["value"] for o in body["options"] if o["available"]] == ["auto", "software"]


def test_device_profiles(a):
    profiles = a.client.get("/api/device-profiles").json()["profiles"]
    assert profiles and all({"label", "brand", "model", "screen"} <= set(p) for p in profiles.values())


# ------------------------------------------------------------------------------- config

def test_config_schema_describes_every_setting(a):
    schema = a.client.get("/api/config/schema").json()
    assert schema == Config.model_json_schema()
    server = schema["$defs"]["ServerConfig"]["properties"]
    assert "Address the panel and API listen on" in server["host"]["description"]
    assert server["port"]["default"] == 7700 and server["port"]["maximum"] == 65535
    assert set(schema["properties"]) >= {"server", "android", "gpu", "network", "backup", "automation"}


def test_config_get_returns_every_value(a):
    config = a.client.get("/api/config").json()["config"]
    assert config["docker"]["name_prefix"] == "sfx" and config["android"]["fps"] == 20


def test_config_patch_validates_saves_and_reports_restarts(a):
    r = a.client.patch("/api/config", json={"patch": {"android": {"fps": 30}, "stream": {"live_fps": 5},
                                                      "server": {"port": 7800}}})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] == ["android.fps", "server.port", "stream.live_fps"]
    assert body["restart_android"] == ["android.fps"] and body["restart_daemon"] == ["server.port"]
    assert body["config"]["android"]["fps"] == 30
    assert load_config(a.farm.paths).android.fps == 30 and a.core.mgr.config.android.fps == 30


@pytest.mark.parametrize("patch", [{"android": {"fps": 1000}}, {"nope": 1}, {"android": {"nope": 1}},
                                   {"server": {"host": "0.0.0.0", "auth_required": False}},
                                   {"android": {"default_version": "99"}}, {"backup": {"schedule": "every day"}}])
def test_config_patch_refuses_invalid_settings(a, patch):
    before = a.farm.config
    r = a.client.patch("/api/config", json={"patch": patch})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    assert a.farm.config is before and load_config(a.farm.paths) == before


def test_config_patch_body_is_strict(a):
    assert a.client.patch("/api/config", json={"android": {"fps": 30}}).status_code == 422
    assert a.client.patch("/api/config", json={"patch": {}, "extra": 1}).status_code == 422
    assert a.client.patch("/api/config", json={"patch": [1]}).status_code == 422


def test_config_patch_null_resets_to_the_default(a):
    a.client.patch("/api/config", json={"patch": {"android": {"fps": 30}}})
    body = a.client.patch("/api/config", json={"patch": {"android": {"fps": None}}}).json()
    assert body["config"]["android"]["fps"] == 20 and body["changed"] == ["android.fps"]


def test_turning_the_audit_log_off_takes_effect_at_once(a):
    a.client.patch("/api/config", json={"patch": {"server": {"audit_log": False}}})
    count = len(a.audit_lines())
    a.client.post("/api/instances", json={"count": 1})
    assert len(a.audit_lines()) == count


# ------------------------------------------------------------------------------- images

def test_images_status_and_ensure(a):
    rows = a.client.get("/api/images").json()["images"]
    assert rows[0]["version"] == "13" and rows[0]["tag"] == "local/android:13" and rows[0]["built"] is True
    task = a.wait(a.client.post("/api/images/13/ensure"))
    assert task["state"] == "done" and task["output"] == "local/android:13"


def test_image_build_of_a_ready_made_image_fails_in_the_task(a):
    task = a.wait(a.client.post("/api/images/13/build"))
    assert task["state"] == "failed" and "ImageError" in task["error"]


@pytest.mark.parametrize("version", ["99", "..", "13;rm", "a" * 200])
def test_image_routes_refuse_unknown_versions(a, version):
    r = a.client.post(f"/api/images/{version}/ensure")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert a.farm.tasks.list() == []


# ----------------------------------------------------------------------------- instances

def test_create_list_and_read(a):
    r = a.client.post("/api/instances", json={"count": 2, "name": "Phone", "template": {"tags": ["eu"], "fps": 30}})
    assert r.status_code == 201
    made = r.json()["instances"]
    assert [m["id"] for m in made] == [1, 2] and made[1]["name"] == "Phone 2" and made[0]["tags"] == ["eu"]
    rows = a.client.get("/api/instances").json()["instances"]
    assert [row["state"] for row in rows] == ["missing", "missing"] and rows[0]["screen"]["fps"] == 30
    one = a.client.get("/api/instances/1").json()
    assert one["id"] == 1 and one["adb_port"] == 17001 and one["spec"]["fps"] == 30


@pytest.mark.parametrize("body,status", [
    ({"count": 0}, 422), ({"count": 501}, 422), ({"count": 1, "nope": 1}, 422), ({"name": "x" * 61}, 422),
    ({"template": {"fps": 1}}, 400), ({"template": {"no_such_field": 1}}, 400),
    ({"template": {"android_version": "99"}}, 400), ({"template": {"tags": ["Bad Tag!"]}}, 400),
    ({"template": {"data_volume": "../../etc"}}, 400), ({"template": {"gpu_node": "/dev/sda"}}, 400),
])
def test_create_refuses_bad_input(a, body, status):
    r = a.client.post("/api/instances", json=body)
    assert r.status_code == status, r.text
    assert a.farm.store.ids() == []


@pytest.mark.parametrize("bad", ["abc", "99999", "-1", "1e3", "1.5", "%2e%2e", "0x10", " 1", "+1", "1_0", "\u0661"])
def test_bad_instance_ids_are_400(a, bad):
    for method, path in (("GET", f"/api/instances/{bad}"), ("POST", f"/api/instances/{bad}/start"),
                         ("DELETE", f"/api/instances/{bad}"), ("GET", f"/api/instances/{bad}/logs")):
        r = a.client.request(method, path)
        assert r.status_code == 400, (method, path, r.text)
        assert r.json()["error"]["code"] == "bad_request"


def test_unknown_instance_is_404_everywhere(a):
    for method, path, body in (("GET", "/api/instances/7", None), ("PATCH", "/api/instances/7", {"patch": {}}),
                               ("DELETE", "/api/instances/7", None), ("POST", "/api/instances/7/start", None),
                               ("POST", "/api/instances/7/clone", {"with_data": False}),
                               ("GET", "/api/instances/7/logs", None)):
        r = a.client.request(method, path, json=body)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", (method, path, r.text)
    assert a.farm.tasks.list() == []


def test_lifecycle_start_stop_restart_recreate(a):
    a.client.post("/api/instances", json={"count": 1})
    task = a.wait(a.client.post("/api/instances/1/start", json={}))
    assert task["kind"] == "instance.start" and task["targets"] == [1] and task["results"]["1"]["ok"]
    assert a.client.get("/api/instances/1").json()["state"] == "booted"
    assert a.core.docker.running("sfx-android-1") and a.core.docker.running("sfx-net-1")
    assert a.wait(a.client.post("/api/instances/1/restart"))["results"]["1"]["ok"]
    assert a.wait(a.client.post("/api/instances/1/recreate", json={"force": True}))["results"]["1"]["ok"]
    assert a.wait(a.client.post("/api/instances/1/stop"))["results"]["1"]["ok"]
    assert not a.core.docker.running("sfx-android-1")
    assert a.client.get("/api/instances/1").json()["state"] == "stopped"


def test_lifecycle_refuses_unknown_actions_and_strict_bodies(a):
    a.client.post("/api/instances", json={"count": 1})
    assert a.client.post("/api/instances/1/explode").status_code == 422
    assert a.client.post("/api/instances/1/start", json={"force": True, "extra": 1}).status_code == 422
    assert a.farm.tasks.list() == []


def test_a_failed_start_is_reported_by_the_task_not_the_request(a):
    a.client.post("/api/instances", json={"count": 1})
    a.core.net_ready = {"ready": False, "state": "auth_failed", "detail": "bad credentials"}
    task = a.wait(a.client.post("/api/instances/1/start"))
    assert task["state"] == "done" and task["failed"] == 1 and "auth_failed" in task["results"]["1"]["detail"]
    assert a.client.get("/api/instances/1").json()["last_error"]


def test_update_applies_a_patch_and_validates_it(a):
    a.client.post("/api/instances", json={"count": 1})
    r = a.client.patch("/api/instances/1", json={"patch": {"fps": 25, "tags": ["a"], "network": {"mode": "none"}}})
    assert r.status_code == 200 and r.json()["instance"]["fps"] == 25
    assert a.farm.store.get(1).network.mode == "none"
    assert a.client.patch("/api/instances/1", json={"patch": {"fps": 1}}).status_code == 400
    assert a.client.patch("/api/instances/1", json={"patch": {"id": 9}}).status_code == 400
    assert a.client.patch("/api/instances/1", json={"fps": 30}).status_code == 422
    assert a.farm.store.get(1).fps == 25


def test_changing_the_version_of_an_android_with_data_needs_force(tmp_path):
    images = {"12": {"base": "example/android:12", "tag": "local/android:12"},
              "13": {"base": "example/android:13", "tag": "local/android:13"}}
    with api(tmp_path, {"android": {"images": images}}) as a:
        a.core.docker.images["local/android:12"] = {"id": "sha256:android12", "labels": {}}
        boot(a)
        a.wait(a.client.post("/api/instances/1/stop"))
        r = a.client.patch("/api/instances/1", json={"patch": {"android_version": "12"}})
        assert r.status_code == 409 and r.json()["error"]["code"] == "needs_force"
        assert a.farm.store.get(1).android_version is None
        r = a.client.patch("/api/instances/1", json={"patch": {"android_version": "12"}, "force": True})
        assert r.status_code == 200 and a.farm.store.get(1).android_version == "12"


def test_delete_removes_containers_and_data_unless_kept(a):
    boot(a, 2)
    r = a.client.delete("/api/instances/1")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == 1 and body["data"] == "removed"
    assert "sfx-android-1" not in a.core.docker.containers and "sfx-data-1" not in a.core.docker.volume_labels
    body = a.client.delete("/api/instances/2", params={"keep_data": "true"}).json()
    assert body["data"] == "kept" and "sfx-data-2" in a.core.docker.volume_labels
    assert a.farm.store.ids() == []


def test_clone_gets_a_new_identity(a):
    a.client.post("/api/instances", json={"count": 1, "template": {"tags": ["eu"]}})
    r = a.client.post("/api/instances/1/clone", json={"with_data": False})
    assert r.status_code == 201
    copy = r.json()["instance"]
    assert copy["id"] == 2 and copy["tags"] == ["eu"]
    assert copy["device"]["android_id"] != a.farm.store.get(1).device.android_id
    assert a.client.post("/api/instances/1/clone", json={}).status_code == 201         # with_data defaults to false
    assert a.client.post("/api/instances/1/clone", json={"with_data": "maybe"}).status_code == 422


def test_cloning_the_data_of_a_running_android_is_a_conflict(a):
    boot(a)
    r = a.client.post("/api/instances/1/clone", json={"with_data": True})
    assert r.status_code == 409 and "stop it" in r.json()["error"]["message"]
    assert a.farm.store.ids() == [1]


def test_logs_come_from_this_androids_two_containers(a):
    boot(a)
    body = a.client.get("/api/instances/1/logs", params={"tail": 5}).json()
    assert set(body) == {"id", "android", "net"} and "binder" in body["android"]
    assert a.client.get("/api/instances/1/logs", params={"tail": 0}).status_code == 400
    assert a.client.get("/api/instances/1/logs", params={"tail": 999999}).status_code == 400
    assert a.client.get("/api/instances/1/logs", params={"tail": "all"}).status_code == 422


def test_bulk_runs_one_task_over_a_selector(a):
    a.client.post("/api/instances", json={"count": 3, "template": {"tags": ["eu"]}})
    task = a.wait(a.client.post("/api/instances/bulk", json={"action": "start", "selector": "1-2"}))
    assert task["targets"] == [1, 2] and task["output"] == {"ok": 2, "failed": 0}
    task = a.wait(a.client.post("/api/instances/bulk", json={"action": "stop", "selector": "running"}))
    assert task["targets"] == [1, 2]
    task = a.wait(a.client.post("/api/instances/bulk", json={"action": "delete", "selector": "tag:eu,!3"}))
    assert task["targets"] == [1, 2] and a.farm.store.ids() == [3]


@pytest.mark.parametrize("body,status", [
    ({"action": "explode", "selector": "all"}, 422), ({"action": "start"}, 422), ({"action": "start", "selector": ""}, 422),
    ({"action": "start", "selector": "all", "x": 1}, 422), ({"action": "start", "selector": "bogus:1"}, 400),
    ({"action": "start", "selector": "1;rm -rf /"}, 400),
])
def test_bulk_refuses_bad_requests(a, body, status):
    a.client.post("/api/instances", json={"count": 1})
    assert a.client.post("/api/instances/bulk", json=body).status_code == status
    assert a.farm.tasks.list() == []


def test_busy_android_is_409(a):
    a.client.post("/api/instances", json={"count": 1})
    held, release = threading.Event(), threading.Event()

    def hold():
        with a.core.mgr.locked(1, "starting"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert held.wait(5)
        r = a.client.patch("/api/instances/1", json={"patch": {"fps": 25}})
        assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
    finally:
        release.set()
        t.join()


# ----------------------------------------------------------------------------- capacity

def test_over_capacity_is_only_a_warning_by_default(a):
    a.core.capacity_max = 1
    boot(a, 2)
    assert a.client.get("/api/system/capacity").json()["over"] is True


def test_enforced_capacity_answers_409_with_the_report_and_force_runs_anyway(tmp_path):
    with api(tmp_path, {"performance": {"enforce_capacity": True}}) as a:
        a.core.capacity_max = 1
        a.client.post("/api/instances", json={"count": 3})
        assert a.wait(a.client.post("/api/instances/1/start"))["results"]["1"]["ok"]
        r = a.client.post("/api/instances/2/start")
        assert r.status_code == 409
        error = r.json()["error"]
        assert error["code"] == "over_capacity" and error["detail"]
        assert error["capacity"]["running"] == 1 and error["capacity"]["over"] is True
        assert error["capacity"]["estimate"]["max_instances"] == 1
        assert a.farm.tasks.list(active_only=True) == [] and not a.core.docker.exists("sfx-android-2")
        # Restarting what already runs adds nothing, so it is not refused.
        assert a.client.post("/api/instances/1/start").status_code == 202
        assert a.client.post("/api/instances/bulk", json={"action": "start", "selector": "all"}).status_code == 409
        task = a.wait(a.client.post("/api/instances/2/start", json={"force": True}))
        assert task["results"]["2"]["ok"]
        task = a.wait(a.client.post("/api/instances/bulk", json={"action": "start", "selector": "all", "force": True}))
        assert task["output"] == {"ok": 3, "failed": 0}


def test_enforced_capacity_refuses_a_bulk_start_that_does_not_fit(tmp_path):
    with api(tmp_path, {"performance": {"enforce_capacity": True}}) as a:
        a.core.capacity_max = 2
        a.client.post("/api/instances", json={"count": 3})
        r = a.client.post("/api/instances/bulk", json={"action": "start", "selector": "all"})
        assert r.status_code == 409 and "about 2 more fit" in r.json()["error"]["message"]
        a.wait(a.client.post("/api/instances/bulk", json={"action": "start", "selector": "1-2"}))
        # stop and delete are never a capacity question
        assert a.client.post("/api/instances/bulk", json={"action": "stop", "selector": "all"}).status_code == 202


def test_low_memory_counts_as_over_capacity_when_enforced(tmp_path):
    with api(tmp_path, {"performance": {"enforce_capacity": True, "memory_guard_mb": 1500}}) as a:
        a.core.mem_mb = 900
        a.client.post("/api/instances", json={"count": 1})
        r = a.client.post("/api/instances/1/start")
        assert r.status_code == 409 and "memory_guard_mb" in r.json()["error"]["message"]
        assert r.json()["error"]["capacity"]["memory_available_mb"] == 900


# -------------------------------------------------------------------------------- tasks

def test_tasks_list_get_and_cancel(a):
    started, release = threading.Event(), threading.Event()

    def work(task):
        started.set()
        while not release.is_set():
            task.sleep(0.02)

    running = a.farm.tasks.submit("test", "a long job", work, targets=[1])
    assert started.wait(5)
    done = a.farm.tasks.submit("test", "a short job", lambda t: {"answer": 42})
    a.farm.tasks.wait(done, 5)
    listed = a.client.get("/api/tasks").json()["tasks"]
    assert [t["id"] for t in listed] == [done.id, running.id]
    assert [t["id"] for t in a.client.get("/api/tasks", params={"active": "true"}).json()["tasks"]] == [running.id]
    assert a.client.get(f"/api/tasks/{done.id}").json()["task"]["output"] == {"answer": 42}
    assert a.client.get("/api/tasks/t999").status_code == 404
    assert a.client.post("/api/tasks/t999/cancel").status_code == 404
    r = a.client.post(f"/api/tasks/{done.id}/cancel")
    assert r.status_code == 409 and "already finished" in r.json()["error"]["message"]
    assert a.client.post(f"/api/tasks/{running.id}/cancel").status_code == 200
    a.farm.tasks.wait(running, 5)
    assert a.client.get(f"/api/tasks/{running.id}").json()["task"]["state"] == "cancelled"
    assert a.client.get("/api/tasks", params={"limit": 0}).status_code == 400
    release.set()


def test_long_work_answers_at_once_with_202(a):
    a.client.post("/api/instances", json={"count": 1})
    gate = threading.Event()
    a.core.mgr.backends.net_wait_ready = lambda docker, r, timeout=90: (gate.wait(5), {"ready": True})[1]
    began = time.monotonic()
    r = a.client.post("/api/instances/1/start")
    assert r.status_code == 202 and time.monotonic() - began < 2
    assert r.json()["task"]["state"] in ("queued", "running")
    gate.set()
    assert a.wait(r)["results"]["1"]["ok"]

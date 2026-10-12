"""The automation routes (the Automation facade over HTTP) and the backup routes."""
from __future__ import annotations

import json
import stat
import time

import pytest
from api_fakes import api
from automation_fakes import FakeDevice, button, field, screen

from superfarm.core import backup as backup_mod

PKG = "com.example.app"
SECRET = "hunter2-very-secret"      # noqa: S105 - test value that must never come back
HOME = screen(button("search", "Search", (20, 20, 220, 80)))
FORM = screen(field("query", "", (20, 20, 460, 80), focused=True))
FLOW = """\
description: open the search form
package: com.example.app
variables: {query: earbuds}
steps:
  - tap: {id: search}
  - type: {text: "{query}", into: {id: query}}
"""


@pytest.fixture
def a(tmp_path):
    fast = {"settle_seconds": 0, "human_jitter": False, "step_timeout": 1, "run_timeout": 30, "read_timeout": 5}
    with api(tmp_path, {"automation": fast}) as made:
        made.core.mgr.create(count=2)
        made.devices = {}

        def device(instance_id: int) -> FakeDevice:
            dev = FakeDevice({"home": HOME, "form": FORM}, "home", taps={("home", "search"): "form"})
            dev.serial = f"fake:{instance_id}"
            made.devices[instance_id] = dev
            return dev

        made.farm.automation.runner._device_factory = device
        made.auto = "/api/automation"
        yield made


# -------------------------------------------------------------------------------- flows

def test_flows_list_read_save_validate_delete(a):
    names = [f["name"] for f in a.client.get(f"{a.auto}/flows").json()["flows"]]
    assert "examples/open-app" in names
    example = a.client.get(f"{a.auto}/flows/examples/open-app").json()
    assert example["builtin"] is True and "steps:" in example["text"] and example["flow"]["steps"] == 2
    r = a.client.put(f"{a.auto}/flows/search", json={"text": FLOW})
    assert r.status_code == 200 and r.json()["flow"]["steps"] == 2
    assert (a.farm.paths.automation / "flows" / "search.yaml").read_text() == FLOW
    assert a.client.get(f"{a.auto}/flows/search").json()["text"] == FLOW
    assert a.client.post(f"{a.auto}/flows/validate", json={"text": FLOW}).json()["ok"] is True
    assert a.client.post(f"{a.auto}/flows/validate", json={"name": "search"}).json()["flow"]["name"] == "search"
    assert a.client.delete(f"{a.auto}/flows/search").status_code == 200
    assert a.client.delete(f"{a.auto}/flows/search").status_code == 404
    assert a.client.get(f"{a.auto}/flows/search").status_code == 404


@pytest.mark.parametrize("text", ["steps: [", "steps: []", "steps:\n  - explode: {}\n", "- just\n- a list\n",
                                  "variables: {user_password: x}\nsteps:\n  - back: {}\n"])
def test_an_invalid_flow_is_400_and_never_stored(a, text):
    r = a.client.post(f"{a.auto}/flows/validate", json={"text": text})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    assert a.client.put(f"{a.auto}/flows/broken", json={"text": text}).status_code == 400
    assert not (a.farm.paths.automation / "flows" / "broken.yaml").exists()


@pytest.mark.parametrize("name", ["..%2F..%2Fconfig", "examples/..%2F..%2Fx", "UPPER", "a b", "examples/open-app/extra",
                                  "%2e%2e", "x" * 100, "..%2F..%2Fsecrets%2Fapi-token"])
def test_flow_names_cannot_leave_the_flows_folder(a, name):
    def flow_like_files() -> list[str]:
        return sorted(str(p) for p in a.farm.paths.home.rglob("*") if p.is_file() and p.suffix in (".yaml", "") )

    before = flow_like_files()
    for method, body in (("GET", None), ("PUT", {"text": FLOW}), ("DELETE", None)):
        r = a.client.request(method, f"{a.auto}/flows/{name}", json=body)
        assert r.status_code in (400, 404), (method, name, r.status_code, r.text)
        assert a.token not in r.text
    assert flow_like_files() == before                       # nothing written, nothing removed
    assert (a.farm.paths.secrets / "api-token").read_text().strip() == a.token


def test_bundled_examples_are_read_only(a):
    assert a.client.put(f"{a.auto}/flows/examples/open-app", json={"text": FLOW}).status_code == 400
    assert a.client.delete(f"{a.auto}/flows/examples/open-app").status_code == 400
    assert a.client.post(f"{a.auto}/flows/validate", json={}).status_code == 400
    assert a.client.post(f"{a.auto}/flows/validate", json={"name": "does-not-exist"}).status_code == 400


# ---------------------------------------------------------- datasets, recording, training

def test_record_train_and_run_a_program(a):
    made = a.client.post(f"{a.auto}/datasets", json={"name": "Search flow", "package": PKG})
    assert made.status_code == 201 and made.json()["dataset"]["slug"] == "search-flow"
    assert [d["slug"] for d in a.client.get(f"{a.auto}/datasets").json()["datasets"]] == ["search-flow"]
    started = a.client.post(f"{a.auto}/record/start", json={"dataset": "search-flow", "id": 1})
    assert started.status_code == 200, started.text
    assert [r["instance"] for r in a.client.get(f"{a.auto}/record").json()["recordings"]] == [1]
    tap = a.client.post(f"{a.auto}/record/1/tap", json={"x": 0.25, "y": 0.06})
    assert tap.status_code == 200, tap.text
    assert a.devices[1].did("tap") and a.devices[1].screen == "form"
    assert a.client.post(f"{a.auto}/record/1/text", json={"value": "earbuds"}).status_code == 200
    assert a.client.post(f"{a.auto}/record/1/key", json={"name": "ENTER"}).status_code == 200
    assert a.client.post(f"{a.auto}/record/1/wait", json={"seconds": 0.01}).status_code == 200
    assert a.client.post(f"{a.auto}/record/1/undo").json() == {"undone": True}
    assert a.client.get(f"{a.auto}/record/1").json()["steps"] == 3
    stopped = a.client.post(f"{a.auto}/record/1/stop")
    assert stopped.status_code == 200, stopped.text
    dataset = a.client.get(f"{a.auto}/datasets/search-flow").json()["dataset"]
    assert dataset["recordings"] == 1 and len(dataset["recordings_list"]) == 1
    rid = dataset["recordings_list"][0]["id"]
    recording = a.client.get(f"{a.auto}/datasets/search-flow/recordings/{rid}").json()["recording"]
    assert len(recording["steps"]) == 3 and recording["describe"]

    trained = a.wait(a.client.post(f"{a.auto}/datasets/search-flow/train"))
    assert trained["kind"] == "automation-train" and trained["state"] == "done", trained
    assert [p["slug"] for p in a.client.get(f"{a.auto}/programs").json()["programs"]] == ["search-flow"]
    assert a.client.get(f"{a.auto}/programs/search-flow").json()["program"]["outline"]

    run = a.client.post(f"{a.auto}/runs", json={"kind": "program", "name": "search-flow", "selector": "2"})
    task = a.wait(run, 30)
    assert task["targets"] == [2] and task["results"]["2"]["ok"], task
    assert a.client.get(f"{a.auto}/runs/{task['id']}").json()["run"]
    assert [r for r in a.client.get(f"{a.auto}/runs").json()["runs"]]
    assert a.client.get(f"{a.auto}/runs", params={"active": "true"}).json() == {"runs": []}

    assert a.client.delete(f"{a.auto}/programs/search-flow").status_code == 200
    assert a.client.delete(f"{a.auto}/programs/search-flow").status_code == 404
    assert a.client.delete(f"{a.auto}/datasets/search-flow/recordings/{rid}").status_code == 200
    assert a.client.patch(f"{a.auto}/datasets/search-flow", json={"description": "v2"}).json()["dataset"]["description"] == "v2"
    assert a.client.delete(f"{a.auto}/datasets/search-flow").status_code == 200
    assert a.client.delete(f"{a.auto}/datasets/search-flow").status_code == 404


def test_recording_errors(a):
    a.client.post(f"{a.auto}/datasets", json={"name": "d"})
    assert a.client.post(f"{a.auto}/record/1/tap", json={"x": 0.5, "y": 0.5}).status_code == 409      # not recording
    assert a.client.post(f"{a.auto}/record/start", json={"dataset": "nope", "id": 1}).status_code == 404
    assert a.client.post(f"{a.auto}/record/start", json={"dataset": "d", "id": 9}).status_code == 404
    assert a.client.post(f"{a.auto}/record/start", json={"dataset": "d"}).status_code == 422
    assert a.client.post(f"{a.auto}/record/start", json={"dataset": "d", "id": 1}).status_code == 200
    assert a.client.post(f"{a.auto}/record/start", json={"dataset": "d", "id": 1}).status_code == 409  # one per device
    assert a.client.post(f"{a.auto}/record/1/tap", json={"x": 7, "y": 0.5}).status_code == 409        # off the screen
    assert a.client.post(f"{a.auto}/record/1/tap", json={"x": 0.5}).status_code == 422
    assert a.client.post(f"{a.auto}/record/1/launch", json={"package": "not a package"}).status_code == 400
    assert a.client.post(f"{a.auto}/record/1/cancel").json() == {"cancelled": True}
    assert a.client.post(f"{a.auto}/record/1/stop").status_code == 409
    assert a.client.post(f"{a.auto}/record/x/stop").status_code == 400


def test_training_needs_recordings_and_datasets_validate(a):
    a.client.post(f"{a.auto}/datasets", json={"name": "empty"})
    assert a.client.post(f"{a.auto}/datasets/empty/train").status_code == 400
    assert a.client.post(f"{a.auto}/datasets/nope/train").status_code == 404
    assert a.client.post(f"{a.auto}/datasets", json={"name": ""}).status_code == 422
    assert a.client.post(f"{a.auto}/datasets", json={"name": "!!!"}).status_code == 400
    assert a.client.post(f"{a.auto}/datasets", json={"name": "x", "package": "bad package"}).status_code == 400
    assert a.client.get(f"{a.auto}/datasets/nope").status_code == 404
    assert a.farm.tasks.list() == []


@pytest.mark.parametrize("path", [
    "datasets/..%2F..%2Fsecrets", "datasets/UPPER", "datasets/d/recordings/..%2F..", "datasets/d/recordings/r001-000000/shots/..%2F..%2F..%2F..%2F..%2Fsecrets%2Fapi-token",
    "datasets/d/recordings/r001-000000/shots/%2Fetc%2Fpasswd", "programs/..%2F..%2Fsecrets", "programs/d/shots/..%2F..%2F..%2Fsecrets%2Fapi-token",
    "programs/d/shots/%2Fetc%2Fhostname",
])
def test_automation_paths_cannot_leave_their_folders(a, path):
    a.client.post(f"{a.auto}/datasets", json={"name": "d"})
    r = a.client.get(f"{a.auto}/{path}")
    assert r.status_code in (400, 404), (path, r.status_code)
    assert a.token not in r.text and "root:" not in r.text


def test_runs_validate_and_report_unknown_ids(a):
    a.client.put(f"{a.auto}/flows/search", json={"text": FLOW})
    task = a.wait(a.client.post(f"{a.auto}/runs", json={"kind": "flow", "name": "search", "selector": "all",
                                                         "variables": {"query": "tents"}}), 30)
    assert task["targets"] == [1, 2] and task["failed"] == 0, task
    assert ("text", "tents") in a.devices[1].sent
    for body, status in (({"kind": "macro", "name": "search", "selector": "all"}, 422),
                         ({"kind": "flow", "name": "nope", "selector": "all"}, 400),
                         ({"kind": "program", "name": "nope", "selector": "all"}, 404),
                         ({"kind": "flow", "name": "search", "selector": "9"}, 400),
                         ({"kind": "flow", "name": "search"}, 422),
                         ({"kind": "flow", "name": "search", "selector": "all", "variables": {"q": 1}}, 422)):
        r = a.client.post(f"{a.auto}/runs", json=body)
        assert r.status_code == status, (body, r.text)
    assert a.client.get(f"{a.auto}/runs/t999").status_code == 404
    assert a.client.post(f"{a.auto}/runs/t999/cancel").status_code == 404
    assert a.client.post(f"{a.auto}/runs/t999/resolve", json={"id": 1}).status_code == 404
    assert a.client.post(f"{a.auto}/runs/t999/resolve", json={"id": 1, "action": "explode"}).status_code == 422


def test_run_variables_never_reach_the_audit_log(a):
    a.client.put(f"{a.auto}/flows/search", json={"text": FLOW})
    a.wait(a.client.post(f"{a.auto}/runs", json={"kind": "flow", "name": "search", "selector": "1",
                                                  "variables": {"query": SECRET}}), 30)
    assert SECRET not in (a.farm.paths.logs / "audit.log").read_text()


# ----------------------------------------------------------------------------- profiles

def test_profiles_roles_overlays_never_tap_and_teach(a):
    base = f"{a.auto}/profiles/{PKG}"
    assert a.client.get(base).json() == {"profile": {"package": PKG, "roles": {}, "overlays": [], "never_tap": [], "notes": ""}}
    assert a.client.put(f"{base}/roles/search", json={"selector": {"id": "search"}}).json()["profile"]["roles"] == {"search": {"id": "search"}}
    assert a.client.put(f"{base}/roles/title", json={"selector": "text:Battery"}).status_code == 200
    overlay = {"name": "rate-us", "guard": {"id": "rate_dialog"}, "dismiss": {"text": "Not now"}}
    assert a.client.post(f"{base}/overlays", json={"rule": overlay}).json()["profile"]["overlays"][0]["name"] == "rate-us"
    assert a.client.post(f"{base}/never-tap", json={"selector": {"text": "Delete account"}}).status_code == 200
    taught = a.client.post(f"{base}/teach", json={"id": 1, "role": "go", "x": 100, "y": 50})
    assert taught.status_code == 200 and taught.json()["selector"]["id"].endswith("search")
    assert a.client.get(f"{a.auto}/profiles").json() == {"profiles": [PKG]}
    profile = a.client.get(base).json()["profile"]
    assert set(profile["roles"]) == {"search", "title", "go"} and len(profile["never_tap"]) == 1
    assert a.client.delete(f"{base}/roles/title").status_code == 200
    assert a.client.delete(f"{base}/overlays/rate-us").json()["profile"]["overlays"] == []
    assert a.client.delete(f"{base}/never-tap/0").json()["profile"]["never_tap"] == []
    assert a.client.delete(f"{base}/never-tap/5").status_code == 400
    replaced = a.client.put(base, json={"roles": {"only": {"id": "x"}}, "notes": "hand-written"})
    assert replaced.json()["profile"]["roles"] == {"only": {"id": "x"}}
    assert a.client.delete(base).status_code == 200 and a.client.delete(base).status_code == 404


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "profiles/not%20a%20package", {"roles": {}}), ("GET", "profiles/..%2F..%2Fconfig", None),
    ("PUT", f"profiles/{PKG}/roles/bad role!", {"selector": {"id": "x"}}),
    ("PUT", f"profiles/{PKG}/roles/x", {"selector": {"nonsense": 1}}),
    ("POST", f"profiles/{PKG}/overlays", {"rule": {"name": "x", "guard": {"text": "Hi"}, "key": "back"}}),
    ("POST", f"profiles/{PKG}/teach", {"id": 1, "role": "r", "x": 470, "y": 800}),       # nothing there
])
def test_profiles_refuse_bad_input(a, method, path, body):
    r = a.client.request(method, f"{a.auto}/{path}", json=body)
    assert r.status_code in (400, 404), r.text
    assert a.client.get(f"{a.auto}/profiles").json() == {"profiles": []}


# ------------------------------------------------------------------------------- values

def test_shared_instance_values_and_pools(a):
    v = f"{a.auto}/values"
    assert a.client.put(f"{v}/shared/greeting", json={"value": "hello"}).json() == {"name": "greeting", "stored": True}
    assert a.client.put(f"{v}/instances/1/nickname", json={"value": "one"}).status_code == 200
    assert a.client.post(f"{v}/pools/codes", json={"lines": ["A1", "B2", " ", "C3"]}).json() == {
        "name": "codes", "added": 3, "remaining": 3}
    listing = a.client.get(v).json()
    assert listing["shared"] == {"values": {"greeting": "hello"}, "hidden": []}
    assert listing["pools"] == [{"name": "codes", "remaining": 3, "used": 0}]
    assert a.client.get(f"{v}/instances/1").json() == {"values": {"nickname": "one"}, "hidden": []}
    assert a.client.get(f"{v}/pools").json()["pools"][0]["name"] == "codes"
    assert a.client.get(f"{v}/pools/codes").json() == {"name": "codes", "remaining": 3, "used": []}
    assert a.client.delete(f"{v}/shared/greeting").status_code == 200
    assert a.client.delete(f"{v}/shared/greeting").status_code == 404
    assert a.client.delete(f"{v}/instances/1/nickname").status_code == 200
    assert a.client.delete(f"{v}/pools/codes").status_code == 200
    assert a.client.delete(f"{v}/pools/codes").status_code == 404
    assert a.client.get(f"{v}/instances/9").status_code == 404


@pytest.mark.parametrize("name", ["user_password", "pin", "otp_code", "cardNumber"])
def test_secret_named_values_are_refused_and_not_echoed(a, name):
    v = f"{a.auto}/values"
    for r in (a.client.put(f"{v}/shared/{name}", json={"value": SECRET}),
              a.client.put(f"{v}/instances/1/{name}", json={"value": SECRET}),
              a.client.post(f"{v}/pools/{name}", json={"lines": [SECRET]})):
        assert r.status_code == 400 and "secret" in r.json()["error"]["message"]
        assert SECRET not in r.text
    assert SECRET not in "".join(p.read_text(errors="replace") for p in a.farm.paths.home.rglob("*") if p.is_file())


def test_a_secret_that_got_into_the_file_by_hand_is_not_sent_out(a):
    folder = a.farm.paths.automation / "values"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shared.json").write_text(json.dumps({"greeting": "hello", "user_password": SECRET}))
    r = a.client.get(f"{a.auto}/values")
    assert r.json()["shared"] == {"values": {"greeting": "hello"}, "hidden": ["user_password"]}
    assert SECRET not in r.text


@pytest.mark.parametrize("path", ["shared/..%2F..%2Fx", "shared/bad name", "shared/1starts_with_digit", "pools/..%2Fshared",
                                  "instances/1/..%2Fx", "instances/..%2F1/x"])
def test_value_names_are_plain_names(a, path):
    body = {"lines": ["x"]} if path.startswith("pools") else {"value": "x"}
    method = "POST" if path.startswith("pools") else "PUT"
    r = a.client.request(method, f"{a.auto}/values/{path}", json=body)
    assert r.status_code in (400, 404, 405), (path, r.status_code, r.text)
    values = a.farm.paths.automation / "values"
    assert not values.exists() or [p.name for p in values.rglob("*.json")] == []


# ---------------------------------------------------------------------------- schedules

def test_schedules_crud(a):
    a.client.put(f"{a.auto}/flows/search", json={"text": FLOW})
    made = a.client.post(f"{a.auto}/schedules", json={"name": "nightly", "kind": "flow", "target": "search",
                                                       "selector": "all", "cron": "0 4 * * *"})
    assert made.status_code == 201
    schedule = made.json()["schedule"]
    assert schedule["cron"] == "0 4 * * *" and schedule["next_run"] > time.time() and schedule["enabled"] is True
    assert [s["id"] for s in a.client.get(f"{a.auto}/schedules").json()["schedules"]] == [schedule["id"]]
    changed = a.client.patch(f"{a.auto}/schedules/{schedule['id']}", json={"every": 3600, "enabled": False})
    assert changed.json()["schedule"]["every"] == 3600 and changed.json()["schedule"]["cron"] == ""
    assert changed.json()["schedule"]["enabled"] is False
    assert a.client.delete(f"{a.auto}/schedules/{schedule['id']}").status_code == 200
    assert a.client.delete(f"{a.auto}/schedules/{schedule['id']}").status_code == 404
    assert a.client.patch(f"{a.auto}/schedules/nope", json={"enabled": False}).status_code == 404


@pytest.mark.parametrize("body,status", [
    ({"name": "x", "kind": "flow", "target": "search", "selector": "all"}, 400),                    # neither every nor cron
    ({"name": "x", "kind": "flow", "target": "search", "selector": "all", "every": 60, "cron": "* * * * *"}, 400),
    ({"name": "x", "kind": "flow", "target": "search", "selector": "all", "cron": "every day"}, 400),
    ({"name": "x", "kind": "flow", "target": "search", "selector": "all", "every": 1}, 400),
    ({"name": "x", "kind": "macro", "target": "search", "selector": "all", "every": 60}, 422),
    ({"name": "x", "kind": "flow", "target": "search", "selector": "all", "every": 600, "variables": {"user_password": "p"}}, 400),
    ({"name": "x", "kind": "flow", "target": "search", "selector": "all", "every": 600, "next_run": 0}, 422),
])
def test_schedules_refuse_bad_input(a, body, status):
    r = a.client.post(f"{a.auto}/schedules", json=body)
    assert r.status_code == status, r.text
    assert a.client.get(f"{a.auto}/schedules").json() == {"schedules": []}


# ------------------------------------------------------------------------- screen / llm

def test_screen_and_element_at(a):
    seen = a.client.get(f"{a.auto}/screen/1").json()
    assert seen["instance"] == 1 and seen["size"] == [480, 854] and seen["package"] == PKG
    assert any(e.get("id", "").endswith("search") for e in seen["elements"])
    assert a.devices[1].closed
    hit = a.client.get(f"{a.auto}/screen/1/element", params={"x": 100, "y": 50}).json()["element"]
    assert hit["id"].endswith("search")
    assert a.client.get(f"{a.auto}/screen/1/element", params={"x": 470, "y": 800}).json()["element"]["id"] == ""
    assert a.client.get(f"{a.auto}/screen/1/element", params={"x": "left", "y": 1}).status_code == 422
    assert a.client.get(f"{a.auto}/screen/9").status_code == 404


def test_llm_status_without_a_model(a):
    assert a.client.get(f"{a.auto}/llm").json()["provider"] == "none"


# ------------------------------------------------------------------------------ backups

def make_backup(a, name: str, *, kind: str = backup_mod.MANIFEST_KIND, secrets: bool = False, data: bytes = b"tar-bytes") -> None:
    import hashlib
    folder = a.farm.backups.directory / name
    folder.mkdir(parents=True)
    main = "data.tar.gz" if kind == backup_mod.MANIFEST_KIND else backup_mod.FARM_ARCHIVE
    (folder / main).write_bytes(data)
    manifest = {"kind": kind, "format": backup_mod.MANIFEST_FORMAT, "id": 1, "name": "Android 1", "created": time.time(),
                "reason": "manual", "data": main, "includes_secrets": secrets, "android_version": "13",
                "files": {main: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}
    (folder / backup_mod.MANIFEST).write_text(json.dumps(manifest))


def test_backups_list_verify_download_delete(a):
    assert a.client.get("/api/backups").json()["backups"] == []
    make_backup(a, "android-1/20250101-040000")
    body = a.client.get("/api/backups").json()
    assert [b["name"] for b in body["backups"]] == ["android-1/20250101-040000"]
    assert body["directory"] == str(a.farm.paths.backups) and body["schedule"]["schedule"] == ""
    assert a.client.post("/api/backups/verify", json={"archive": "android-1/20250101-040000"}).json() == {
        "archive": "android-1/20250101-040000", "ok": True}
    got = a.client.get("/api/backups/download", params={"archive": "android-1/20250101-040000"})
    assert got.status_code == 200 and got.content == b"tar-bytes"
    assert 'filename="android-1-20250101-040000-data.tar.gz"' in got.headers["content-disposition"]
    (a.farm.backups.directory / "android-1/20250101-040000/data.tar.gz").write_bytes(b"tar-bytez")
    assert a.client.post("/api/backups/verify", json={"archive": "android-1/20250101-040000"}).json()["ok"] is False
    assert a.client.request("DELETE", "/api/backups", json={"archive": "android-1/20250101-040000"}).json() == {
        "deleted": "android-1/20250101-040000"}
    assert not (a.farm.backups.directory / "android-1").exists()
    make_backup(a, "android-2/20250101-040000")
    assert a.client.delete("/api/backups", params={"archive": "android-2/20250101-040000"}).status_code == 200
    assert a.client.get("/api/backups").json()["backups"] == []


@pytest.mark.parametrize("archive", ["../../secrets", "/etc", "/", "android-1/../../secrets", "android-1", "farm/../..",
                                     "android-1/20250101-040000/../../..", "", "android-99999/20250101-040000",
                                     "..%2F..%2Fsecrets", "android-1/20250101-040000\x00"])
def test_backup_routes_accept_names_not_paths(a, archive):
    make_backup(a, "android-1/20250101-040000")
    outside = a.farm.paths.home / "elsewhere" / "android-1" / "20250101-040000"
    outside.mkdir(parents=True)
    (outside / "manifest.json").write_text("{}")
    for method, path, kw in (("POST", "/api/backups/verify", {"json": {"archive": archive}}),
                             ("POST", "/api/backups/restore", {"json": {"archive": archive}}),
                             ("DELETE", "/api/backups", {"json": {"archive": archive}}),
                             ("DELETE", "/api/backups", {"params": {"archive": archive}}),
                             ("GET", "/api/backups/download", {"params": {"archive": archive}})):
        r = a.client.request(method, path, **kw)
        assert r.status_code in (400, 404, 422), (method, path, archive, r.status_code, r.text)
    assert (a.farm.backups.directory / "android-1/20250101-040000/data.tar.gz").exists()
    assert (a.farm.paths.secrets / "api-token").exists() and outside.exists()
    assert a.farm.tasks.list() == []


def test_an_absolute_path_to_a_real_backup_elsewhere_is_still_refused(a, tmp_path):
    make_backup(a, "android-1/20250101-040000")
    real = a.farm.backups.directory / "android-1/20250101-040000"
    for archive in (str(real), str(real / "data.tar.gz")):
        assert a.client.get("/api/backups/download", params={"archive": archive}).status_code == 400
        assert a.client.request("DELETE", "/api/backups", json={"archive": archive}).status_code in (400, 422)
    assert real.exists()


def test_unknown_backups_are_404(a):
    for r in (a.client.post("/api/backups/verify", json={"archive": "android-1/20250101-040000"}),
              a.client.post("/api/backups/restore", json={"archive": "android-1/20250101-040000"}),
              a.client.delete("/api/backups", params={"archive": "farm/20250101-040000"}),
              a.client.get("/api/backups/download", params={"archive": "android-1/20250101-040000"})):
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_backup_create_is_a_task_per_selection(a):
    r = a.client.post("/api/backups", json={"selector": "all"})
    task = a.wait(r)
    assert task["kind"] == "backup.manual" and task["targets"] == [1, 2]
    assert all("skipped: no data yet" in v["detail"] for v in task["results"].values())      # never started: nothing to copy
    assert a.client.post("/api/backups", json={}).status_code == 422
    assert a.client.post("/api/backups", json={"selector": "bogus"}).status_code == 400


def test_farm_backup_leaves_secrets_out_and_one_with_secrets_is_not_served(a):
    a.client.put("/api/net/credentials/home", json={"username": "u", "password": SECRET})
    made = a.client.post("/api/backups/farm")
    assert made.status_code == 201
    name = made.json()["archive"]
    assert name.startswith("farm/") and made.json()["includes_secrets"] is False
    archive = a.farm.backups.directory / name / "farm.tar.gz"
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    import tarfile
    with tarfile.open(archive) as tar:
        members = tar.getnames()
        assert "config.yaml" in members and not any(m.startswith("secrets") for m in members)
    plain = a.client.get("/api/backups/download", params={"archive": name})
    assert plain.status_code == 200 and SECRET.encode() not in plain.content and a.token.encode() not in plain.content
    with_secrets = a.client.post("/api/backups/farm", json={"include_secrets": True}).json()
    assert with_secrets["includes_secrets"] is True
    refused = a.client.get("/api/backups/download", params={"archive": with_secrets["archive"]})
    assert refused.status_code == 403 and "not served over the API" in refused.json()["error"]["message"]
    kinds = {b["name"]: b["kind"] for b in a.client.get("/api/backups").json()["backups"]}
    assert kinds == {name: "farm", with_secrets["archive"]: "farm"}


def test_restore_runs_as_a_task_and_farm_restore_reloads_the_config(a):
    name = a.client.post("/api/backups/farm").json()["archive"]
    a.client.patch("/api/config", json={"patch": {"android": {"fps": 30}}})
    task = a.wait(a.client.post("/api/backups/restore", json={"archive": name, "force": True}))
    assert task["kind"] == "backup.restore-farm" and task["state"] == "done", task
    assert "config.yaml" in task["output"]["restored"]
    assert a.farm.config.android.fps == 20                   # the daemon runs on the restored settings at once
    assert a.client.post("/api/backups/restore", json={"archive": name, "id": 1}).status_code == 400


def test_android_restore_checks_the_target_and_reports_failure_in_the_task(a):
    make_backup(a, "android-1/20250101-040000")
    assert a.client.post("/api/backups/restore", json={"archive": "android-1/20250101-040000", "id": 9}).status_code == 404
    assert a.client.post("/api/backups/restore", json={"archive": "android-1/20250101-040000", "extra": 1}).status_code == 422
    r = a.client.post("/api/backups/restore", json={"archive": "android-1/20250101-040000", "id": 1})
    task = a.wait(r)
    assert task["kind"] == "backup.restore" and task["targets"] == [1]
    assert task["state"] == "failed" and task["error"]            # a hand-made archive is not a restorable backup


def test_prune_applies_retention(a):
    for stamp in ("20250101-040000", "20250102-040000", "20250103-040000"):
        make_backup(a, f"android-1/{stamp}")
        time.sleep(0.01)
    a.client.patch("/api/config", json={"patch": {"backup": {"keep": 1}}})
    assert sorted(a.client.post("/api/backups/prune").json()["removed"]) == ["android-1/20250101-040000", "android-1/20250102-040000"]
    assert [b["name"] for b in a.client.get("/api/backups").json()["backups"]] == ["android-1/20250103-040000"]

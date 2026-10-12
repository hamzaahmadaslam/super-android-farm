"""The audit log: what is recorded for a state-changing request, and what never is."""
from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest
from api_fakes import BASE, api
from starlette.testclient import TestClient

from superfarm.api.audit import AuditLog, note, outcome_of

SECRET = "hunter2-very-secret"      # noqa: S105 - test value that must never be written


@pytest.fixture
def a(tmp_path):
    with api(tmp_path) as made:
        yield made


def test_a_change_is_one_json_line_with_who_what_and_outcome(a):
    a.client.post("/api/instances", json={"count": 2, "name": "Phone"})
    lines = a.audit_lines()
    assert len(lines) == 1
    entry = lines[0]
    assert set(entry) == {"time", "client", "auth", "method", "path", "status", "outcome", "ms", "ids", "count"}
    assert entry["method"] == "POST" and entry["path"] == "/api/instances" and entry["status"] == 201
    assert entry["outcome"] == "ok" and entry["auth"] == "token" and entry["client"] == "testclient"
    assert entry["ids"] == [1, 2] and entry["count"] == 2
    assert entry["time"].endswith("Z") and entry["ms"] >= 0


def test_reads_are_not_audited(a):
    for path in ("/api/health", "/api/instances", "/api/config", "/api/system/capacity", "/api/tasks", "/"):
        a.client.get(path)
    assert a.audit_lines() == []
    assert not (a.farm.paths.logs / "audit.log").exists()


def test_fleet_actions_record_selector_resolved_ids_and_task(a):
    a.client.post("/api/instances", json={"count": 3, "template": {"tags": ["eu"]}})
    r = a.client.post("/api/instances/bulk", json={"action": "start", "selector": "tag:eu,!3", "force": True})
    a.wait(r)
    entry = a.audit_lines()[-1]
    assert entry["selector"] == "tag:eu,!3" and entry["ids"] == [1, 2] and entry["action"] == "start"
    assert entry["task"] == r.json()["task"]["id"] and entry["force"] is True and entry["status"] == 202
    a.wait(a.client.post("/api/instances/2/stop"))
    assert a.audit_lines()[-1]["path"] == "/api/instances/2/stop" and a.audit_lines()[-1]["ids"] == [2]


def test_refusals_and_failures_are_recorded_too(a):
    a.anonymous.post("/api/instances", json={"count": 1})
    a.anonymous.post("/api/auth/login", json={"token": SECRET})
    a.client.post("/api/instances", json={"count": 0})
    a.client.delete("/api/instances/7")
    a.client.post("/api/instances", json={"count": 1}, headers={"Origin": "http://evil.example"})
    got = [(e["path"], e["status"], e["outcome"], e["auth"]) for e in a.audit_lines()]
    assert got == [("/api/instances", 422, "error", "token"), ("/api/instances/7", 404, "error", "token"),
                   ("/api/instances", 403, "denied", "token")]
    # What nobody authenticated is kept apart, so it cannot rotate the lines above away.
    got = [(e["path"], e["status"], e["outcome"], e["auth"]) for e in a.audit_lines("audit-anonymous.log")]
    assert got == [("/api/instances", 401, "denied", "anonymous"), ("/api/auth/login", 401, "denied", "anonymous")]


def test_a_session_without_csrf_is_recorded_as_denied(a):
    client = TestClient(a.app, base_url=BASE, raise_server_exceptions=False)
    client.post("/api/auth/login", json={"token": a.token})
    client.post("/api/instances", json={"count": 1})
    login, refused = a.audit_lines()               # a login that succeeded is authenticated history
    assert (login["path"], login["status"], login["outcome"], login["auth"]) == ("/api/auth/login", 200, "ok", "token")
    assert (refused["status"], refused["outcome"], refused["auth"]) == (403, "denied", "session")


def test_a_crash_is_recorded_as_a_500(a, monkeypatch):
    monkeypatch.setattr(a.core.mgr, "create", lambda **kw: 1 / 0)
    assert a.client.post("/api/instances", json={"count": 1}).status_code == 500
    assert (a.audit_lines()[-1]["status"], a.audit_lines()[-1]["outcome"]) == (500, "error")


def test_bodies_tokens_and_query_strings_are_never_written(a):
    a.client.post("/api/instances", json={"count": 1, "template": {"notes": SECRET}})
    a.anonymous.post("/api/auth/login", json={"token": SECRET})
    a.anonymous.post("/api/auth/login", json={"token": a.token})
    a.client.put("/api/net/credentials/home", json={"username": SECRET, "password": SECRET})
    a.client.post("/api/input/text", json={"ids": [1], "value": SECRET})
    a.client.post("/api/shell", json={"selector": "9", "argv": ["echo", SECRET]})
    a.client.post("/api/device/clipboard", json={"selector": "9", "text": SECRET})
    a.client.put("/api/automation/values/shared/greeting", json={"value": SECRET})
    a.client.patch("/api/instances/1", json={"patch": {"notes": SECRET}})
    a.client.patch("/api/config", json={"patch": {"farm_name": SECRET}})
    a.client.delete("/api/instances/1", params={"keep_data": "true", "note": SECRET})
    a.client.post("/api/apps/library", params={"filename": SECRET + ".apk"}, content=b"not an apk")
    a.anonymous.get("/api/instances", headers={"Authorization": f"Bearer {SECRET}"})
    text = (a.farm.paths.logs / "audit.log").read_text() + (a.farm.paths.logs / "audit-anonymous.log").read_text()
    assert len(text.splitlines()) == 12
    assert SECRET not in text and a.token not in text
    assert "keep_data" not in text and "?" not in text
    assert all(set(json.loads(line)) <= {"time", "client", "auth", "method", "path", "status", "outcome", "ms", "ids",
                                         "selector", "task", "name", "action", "archive", "package", "app", "count",
                                         "keys", "force"} for line in text.splitlines())
    config_line = next(json.loads(line) for line in text.splitlines() if '"/api/config"' in line)
    assert config_line["keys"] == ["farm_name"]            # which settings changed, not what to


def test_the_file_is_owner_only(a):
    a.client.post("/api/instances", json={"count": 1})
    assert stat.S_IMODE((a.farm.paths.logs / "audit.log").stat().st_mode) == 0o600


def test_audit_can_be_switched_off(tmp_path):
    with api(tmp_path, {"server": {"audit_log": False}}) as a:
        a.client.post("/api/instances", json={"count": 1})
        assert not (a.farm.paths.logs / "audit.log").exists()


def test_rotation_by_size_keeps_a_bounded_number_of_files(tmp_path):
    log = AuditLog(tmp_path / "audit.log", max_bytes=400, keep=2)
    for n in range(60):
        log.write({"n": n, "path": "/api/instances/bulk", "pad": "x" * 40})
    log.close()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["audit.log", "audit.log.1", "audit.log.2"]
    assert all(p.stat().st_size <= 400 for p in tmp_path.iterdir())
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in tmp_path.iterdir())
    newest = [json.loads(line)["n"] for line in (tmp_path / "audit.log").read_text().splitlines()]
    assert newest[-1] == 59                                 # the newest entries are the ones kept


def test_rotation_settings_follow_the_config(a):
    a.client.patch("/api/config", json={"patch": {"logging": {"max_mb": 1, "keep": 2}}})
    audit = a.app.state.audit
    assert (audit._max_bytes, audit._keep) == (1024 * 1024, 2)


def test_a_log_that_cannot_be_written_does_not_fail_the_request(a, caplog):
    logs = a.farm.paths.logs
    (logs / "audit.log").mkdir(parents=True)                # a folder where the file should be
    assert a.client.post("/api/instances", json={"count": 1}).status_code == 201
    assert "audit log could not be written" in caplog.text


def test_note_keeps_only_known_short_facts():
    request = SimpleNamespace(scope={})
    note(request, selector="all", ids=range(1000), body={"password": SECRET}, token=SECRET, name="x" * 500, task=None)
    kept = request.scope["state"]["audit"]
    assert set(kept) == {"selector", "ids", "name"}
    assert len(kept["ids"]) == 200 and len(kept["name"]) == 200


@pytest.mark.parametrize("status,outcome", [(200, "ok"), (202, "ok"), (304, "ok"), (400, "error"), (401, "denied"),
                                            (403, "denied"), (404, "error"), (409, "error"), (429, "denied"), (500, "error")])
def test_outcome_of(status, outcome):
    assert outcome_of(status) == outcome


# ----------------------------------------------------- what nobody authenticated

def test_the_recorded_path_is_capped(a):
    a.anonymous.post("/api/" + "A" * 60_000)
    a.client.post("/api/instances/" + "9" * 60_000 + "/start")
    (junk,), (known,) = a.audit_lines("audit-anonymous.log"), a.audit_lines()
    assert (junk["status"], known["status"], known["auth"]) == (404, 400, "token")
    assert len(junk["path"]) == len(known["path"]) == 200 and known["path"].startswith("/api/instances/999")


def test_unauthenticated_requests_cannot_rotate_out_the_authenticated_history(a):
    for log in (a.app.state.audit, a.app.state.audit_anonymous):
        log.configure(max_bytes=2000, keep=1, enabled=True)
    a.client.post("/api/instances", json={"count": 1})                       # the line worth keeping
    for n in range(100):
        a.anonymous.post(f"/api/junk/{n}")                                   # 404, no credentials
        a.anonymous.post("/api/auth/login", json={"token": "guess"})         # 401 / 429, no credentials
        a.anonymous.post("/api/instances", headers={"Authorization": "Bearer guess"})
    logs = a.farm.paths.logs
    assert [(e["path"], e["auth"], e["ids"]) for e in a.audit_lines()] == [("/api/instances", "token", [1])]
    assert not (logs / "audit.log.1").exists()
    # The junk has a budget of its own: bounded, owner-only, and still says who and what.
    files = sorted(p.name for p in logs.iterdir() if p.name.startswith("audit-anonymous"))
    assert files == ["audit-anonymous.log", "audit-anonymous.log.1"]
    assert all((logs / f).stat().st_size <= 2000 and stat.S_IMODE((logs / f).stat().st_mode) == 0o600 for f in files)
    last = a.audit_lines("audit-anonymous.log")[-1]
    assert last["auth"] == "anonymous" and last["client"] == "testclient" and last["outcome"] == "denied"


def test_the_anonymous_log_follows_the_same_settings(a):
    a.client.patch("/api/config", json={"patch": {"logging": {"max_mb": 1, "keep": 2}}})
    other = a.app.state.audit_anonymous
    assert (other._max_bytes, other._keep, other.enabled) == (1024 * 1024, 2, True)
    a.client.patch("/api/config", json={"patch": {"server": {"audit_log": False}}})
    a.anonymous.post("/api/instances", json={"count": 1})
    assert other.enabled is False and a.audit_lines("audit-anonymous.log") == []


def test_switching_the_audit_log_off_is_the_last_thing_it_records(a, caplog):
    assert a.client.post("/api/instances", json={"count": 1}).status_code == 201
    with caplog.at_level("WARNING", logger="superfarm.api"):
        assert a.client.patch("/api/config", json={"patch": {"server": {"audit_log": False}}}).status_code == 200
    assert a.client.delete("/api/instances/1", params={"force": "true"}).status_code == 200
    created, switched = a.audit_lines()
    assert (created["method"], created["path"]) == ("POST", "/api/instances")
    assert (switched["method"], switched["path"], switched["status"]) == ("PATCH", "/api/config", 200)
    assert switched["keys"] == ["server.audit_log"] and switched["auth"] == "token"
    assert any("audit log" in r.getMessage() and "testclient" in r.getMessage() for r in caplog.records)

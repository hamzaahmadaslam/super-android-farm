"""The approvals routes and /api/mcp/info, on the real app with the core fakes behind it."""
from __future__ import annotations

import json
import threading
import time

import pytest
from api_fakes import BASE, WS, Api, api
from starlette.testclient import TestClient

from superfarm.ai.tools import TOOL_NAMES, all_tools, invoke
from superfarm.api import ws as ws_mod
from superfarm.api.routes import approvals as routes


@pytest.fixture
def a(tmp_path):
    with api(tmp_path) as made:
        assert made.client.post("/api/instances", json={"count": 3}).status_code == 201
        yield made


def ask(a: Api, tool="stop_androids", arguments=None, session="s1", caller="mcp:test-client", client=None):
    body = {"tool": tool, "arguments": {"selector": "all"} if arguments is None else arguments, "caller": caller,
            "session": session}
    return (client or a.client).post("/api/approvals", json=body)


def pending(a: Api, **kw) -> dict:
    r = ask(a, **kw)
    assert r.status_code == 201, r.text
    assert r.json()["decision"] == "ask" and r.json()["targets"] == r.json()["approval"]["targets"]
    return r.json()["approval"]


def configure(a: Api, **ai) -> None:
    assert a.client.patch("/api/config", json={"patch": {"ai": ai}}).status_code == 200


# -------------------------------------------------------------------------------- asking

def test_a_request_is_created_pending_with_a_summary_written_by_the_daemon(a):
    item = pending(a, tool="delete_androids", arguments={"selector": "1-3"})
    assert item["summary"] == "Delete 3 Androids and their data: Android 1, 2, 3"
    assert (item["state"], item["tool"], item["klass"], item["targets"]) == ("pending", "delete_androids", "destructive", [1, 2, 3])
    assert (item["caller"], item["session"], item["arguments"]) == ("mcp:test-client", "s1", {"selector": "1-3"})
    assert item["can_remember"] is False and item["expires"] == pytest.approx(item["created"] + 300)
    assert set(item) == {"id", "created", "expires", "caller", "session", "tool", "klass", "summary", "arguments",
                         "targets", "can_remember", "state", "decided_by", "decided_at", "reason", "remembered"}
    listed = a.client.get("/api/approvals").json()
    assert [x["id"] for x in listed["approvals"]] == [item["id"]] and listed["pending"] == 1 and listed["now"] > 0
    assert a.client.get(f"/api/approvals/{item['id']}").json() == {"approval": item}


def test_the_caller_cannot_supply_the_summary_or_any_other_field(a):
    body = {"tool": "delete_androids", "arguments": {"selector": "all"}, "caller": "mcp:x", "session": "s1",
            "summary": "List the Androids (read-only)"}
    r = a.client.post("/api/approvals", json=body)
    assert r.status_code == 422 and "summary" in r.text
    assert a.client.get("/api/approvals").json()["approvals"] == []
    # ... and the selector is resolved here, now: the line names the Androids that would really be hit.
    assert pending(a, tool="delete_androids", arguments={"selector": "2"})["summary"] == "Delete Android 2 and its data"
    assert pending(a, tool="tap", arguments={"selector": "all,!2", "x": 0.5, "y": 0.5})["targets"] == [1, 3]


def test_bad_requests_are_refused_before_the_operator_is_bothered(a):
    cases = [
        (ask(a, tool="format_disk"), 400, "no tool named"),
        (ask(a, tool="list_androids", arguments={}), 400, "only reads"),
        (ask(a, tool="tap", arguments={"selector": "all", "x": 7, "y": 0}), 400, "x: must be at most 1"),
        (ask(a, tool="stop_androids", arguments={"selector": "all", "force": True}), 400, "unknown argument"),
        (ask(a, tool="stop_androids", arguments={"selector": "tag:nobody"}), 400, "matches no Android"),
        (ask(a, tool="stop_androids", arguments={"selector": "what?"}), 400, "bad selector"),
        (ask(a, tool="set_android", arguments={"id": 99, "fps": 30}), 404, "no Android 99"),
        (ask(a, session="has spaces"), 400, "bad session id"),
        (ask(a, caller=""), 422, "caller"),
    ]
    for response, status, phrase in cases:
        assert response.status_code == status and phrase in response.json()["error"]["message"], response.text
    assert a.client.get("/api/approvals").json()["approvals"] == []


def test_secret_named_arguments_are_hidden_in_what_is_stored_and_shown(a):
    item = pending(a, tool="run_flow", arguments={"selector": "1", "name": "login",
                                                 "variables": {"user_password": "hunter2", "city": "Berlin"}})
    assert item["arguments"]["variables"] == {"user_password": "(hidden)", "city": "Berlin"}
    assert item["summary"] == 'Run flow "login" on Android 1 with variables city, user_password'
    stored = (a.farm.paths.logs / "approvals.jsonl").read_text()
    assert "hunter2" not in stored and "hunter2" not in a.client.get("/api/approvals").text


# ------------------------------------------------------------------------------ deciding

def test_approve_and_deny(a):
    yes, no = pending(a), pending(a, tool="start_androids")
    r = a.client.post(f"/api/approvals/{yes['id']}/approve", json={})
    done = r.json()["approval"]
    assert r.status_code == 200 and done["state"] == "approved" and done["decided_by"] == "api (testclient)"
    assert done["decided_at"] >= done["created"] and done["remembered"] is False
    r = a.client.post(f"/api/approvals/{no['id']}/deny", json={"reason": "not  now\nplease", "via": "terminal"})
    denied = r.json()["approval"]
    assert (denied["state"], denied["reason"], denied["decided_by"]) == ("denied", "not now please", "terminal (testclient)")
    listed = a.client.get("/api/approvals").json()
    assert listed["pending"] == 0 and [x["state"] for x in listed["approvals"]] == ["denied", "approved"]
    assert [x["id"] for x in a.client.get("/api/approvals", params={"state": "approved"}).json()["approvals"]] == [yes["id"]]
    assert a.client.get("/api/approvals", params={"state": "bogus"}).status_code == 400
    assert a.client.post(f"/api/approvals/{yes['id']}/approve").status_code == 200          # no body is fine too


def test_deciding_twice_is_idempotent_and_the_opposite_is_a_conflict(a):
    item = pending(a)
    first = a.client.post(f"/api/approvals/{item['id']}/approve", json={}).json()
    again = a.client.post(f"/api/approvals/{item['id']}/approve", json={"via": "terminal"})
    assert again.status_code == 200 and again.json() == first
    late = a.client.post(f"/api/approvals/{item['id']}/deny", json={"reason": "changed my mind"})
    assert late.status_code == 409 and "already approved" in late.json()["error"]["message"]
    assert a.client.get(f"/api/approvals/{item['id']}").json()["approval"]["state"] == "approved"


def test_two_operators_racing_one_wins_and_the_other_is_told(a):
    for _ in range(10):
        item = pending(a)
        codes: dict[str, int] = {}
        gate = threading.Barrier(2)

        def decide(action, item=item, codes=codes, gate=gate):
            gate.wait()
            codes[action] = a.client.post(f"/api/approvals/{item['id']}/{action}", json={}).status_code

        threads = [threading.Thread(target=decide, args=(x,)) for x in ("approve", "deny")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(codes.values()) == [200, 409], codes
        winner = next(k for k, v in codes.items() if v == 200)
        assert a.client.get(f"/api/approvals/{item['id']}").json()["approval"]["state"] == {"approve": "approved", "deny": "denied"}[winner]


def test_unknown_and_malformed_ids(a):
    assert a.client.get("/api/approvals/0123abcd").status_code == 404
    assert a.client.post("/api/approvals/0123abcd/approve", json={}).status_code == 404
    assert a.client.get("/api/approvals/..%2f..%2fetc").status_code in (400, 404)
    assert a.client.get("/api/approvals/NOT-AN-ID/wait").status_code == 400
    assert a.client.post("/api/approvals/x/deny", json={"reason": "r" * 501}).status_code == 422
    assert a.client.post("/api/approvals/x/approve", json={"remember": True, "extra": 1}).status_code == 422


def test_a_request_expires_when_nobody_answers(a):
    configure(a, approval_timeout_seconds=5)
    item = pending(a)
    a.farm.approvals._clock = lambda: time.time() + 6          # six seconds later
    seen = a.client.get(f"/api/approvals/{item['id']}").json()["approval"]
    assert (seen["state"], seen["decided_by"]) == ("expired", "timeout")
    late = a.client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert late.status_code == 409 and "expired" in late.json()["error"]["message"]


def test_cancel_and_session_end(a):
    one, two = pending(a), pending(a, tool="start_androids", session="s2")
    gone = a.client.post(f"/api/approvals/{one['id']}/cancel").json()["approval"]
    assert gone["state"] == "expired" and "stopped waiting" in gone["reason"]
    assert a.client.post(f"/api/approvals/{one['id']}/approve", json={}).status_code == 409
    assert a.client.delete("/api/approvals/sessions/s2").json() == {"ok": True, "forgotten": 0}
    assert a.client.get(f"/api/approvals/{two['id']}").json()["approval"]["state"] == "expired"
    assert a.client.delete("/api/approvals/sessions/bad session").status_code == 400


# ---------------------------------------------------------------------------- long poll

def test_wait_returns_when_the_operator_answers(a):
    item = pending(a)
    threading.Timer(0.3, lambda: a.client.post(f"/api/approvals/{item['id']}/deny", json={"reason": "no"})).start()
    began = time.monotonic()
    r = a.client.get(f"/api/approvals/{item['id']}/wait", params={"timeout": 20})
    assert r.json()["approval"]["state"] == "denied" and r.json()["approval"]["reason"] == "no"
    assert time.monotonic() - began < 10


def test_wait_times_out_with_the_request_still_pending_and_is_capped(a, monkeypatch):
    item = pending(a)
    began = time.monotonic()
    r = a.client.get(f"/api/approvals/{item['id']}/wait", params={"timeout": 0.3})
    assert r.status_code == 200 and r.json()["approval"]["state"] == "pending"
    assert 0.25 < time.monotonic() - began < 5
    monkeypatch.setattr(routes, "MAX_WAIT", 0.2)
    began = time.monotonic()
    a.client.get(f"/api/approvals/{item['id']}/wait", params={"timeout": 3600})
    assert time.monotonic() - began < 5                         # one poll never holds a connection for an hour


def test_many_waiters_do_not_use_up_the_worker_threads(a):
    items = [pending(a, session=f"s{n}") for n in range(60)]
    results: list[str] = []

    def wait(item):
        results.append(a.client.get(f"/api/approvals/{item['id']}/wait", params={"timeout": 20}).json()["approval"]["state"])

    threads = [threading.Thread(target=wait, args=(item,)) for item in items]
    for t in threads:
        t.start()
    time.sleep(0.3)
    began = time.monotonic()
    assert a.client.get("/api/health").status_code == 200       # the API still answers while 60 calls wait
    for item in items:
        assert a.client.post(f"/api/approvals/{item['id']}/approve", json={}).status_code == 200
    for t in threads:
        t.join(15)
    assert results == ["approved"] * 60 and time.monotonic() - began < 15


# -------------------------------------------------------------------------------- policy

def test_read_only_policy_refuses_and_allow_lets_through_without_a_request(a):
    configure(a, policy="read-only")
    r = ask(a)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ai_denied" and "read-only" in r.json()["error"]["message"]
    configure(a, policy="allow")
    r = ask(a)
    assert r.status_code == 200 and r.json() == {"decision": "allow", "reason": "ai.policy is allow", "approval": None,
                                                 "targets": [1, 2, 3]}
    assert a.client.get("/api/approvals").json()["approvals"] == []


def test_deny_list_auto_approve_and_ask_for(a):
    shell = {"selector": "all", "argv": ["id"]}
    r = ask(a, tool="run_shell", arguments=shell)
    assert r.status_code == 403 and "ai.deny" in r.json()["error"]["message"]          # off by default
    configure(a, deny=[], auto_approve=["tap"], ask_for=["destructive"])
    assert ask(a, tool="run_shell", arguments=shell).json()["decision"] == "ask"       # enabled: still asked, it is destructive
    assert ask(a, tool="tap", arguments={"selector": "1", "x": 0, "y": 0}).json()["reason"].startswith("pre-approved")
    assert ask(a, tool="stop_androids").json()["decision"] == "allow"                  # changing actions exempted
    assert ask(a, tool="delete_androids").json()["decision"] == "ask"
    configure(a, policy="allow", deny=["delete_androids"])
    assert ask(a, tool="delete_androids").status_code == 403                           # deny beats allow


def test_approve_and_remember_covers_the_same_tool_and_androids_for_that_session_only(a):
    args = {"selector": "1-2", "x": 0.5, "y": 0.5}
    item = pending(a, tool="tap", arguments=args)
    assert item["can_remember"] is True
    done = a.client.post(f"/api/approvals/{item['id']}/approve", json={"remember": True}).json()["approval"]
    assert done["remembered"] is True
    again = ask(a, tool="tap", arguments={"selector": "2", "x": 0.1, "y": 0.9})
    assert again.status_code == 200 and again.json() == {
        "decision": "allow", "reason": "approved earlier in this session (approve and remember)", "approval": None,
        "targets": [2]}
    assert ask(a, tool="tap", arguments={"selector": "all", "x": 0.5, "y": 0.5}).json()["decision"] == "ask"   # Android 3 too: ask
    assert ask(a, tool="type_text", arguments={"selector": "1", "text": "x"}).json()["decision"] == "ask"      # another tool
    assert ask(a, tool="tap", arguments=args, session="s2").json()["decision"] == "ask"                        # another session
    a.client.delete("/api/approvals/sessions/s1")
    assert ask(a, tool="tap", arguments=args).json()["decision"] == "ask"                                      # the session ended


def test_remember_is_ignored_for_destructive_requests(a):
    item = pending(a, tool="delete_androids", arguments={"selector": "1"})
    done = a.client.post(f"/api/approvals/{item['id']}/approve", json={"remember": True}).json()["approval"]
    assert done["state"] == "approved" and done["remembered"] is False
    assert ask(a, tool="delete_androids", arguments={"selector": "1"}).json()["decision"] == "ask"


def test_a_restart_expires_what_was_pending_and_forgets_what_was_remembered(tmp_path):
    with api(tmp_path) as first:
        first.client.post("/api/instances", json={"count": 2})
        waiting = pending(first)
        tapped = pending(first, tool="tap", arguments={"selector": "1", "x": 0, "y": 0})
        first.client.post(f"/api/approvals/{tapped['id']}/approve", json={"remember": True})
    from superfarm.api.app import create_app
    from superfarm.farm import Farm
    farm = Farm(tmp_path / "home")                              # the same farm home, a new daemon
    try:
        client = TestClient(create_app(farm, web_dir=tmp_path / "no-panel"), base_url=BASE,
                            headers={"Authorization": f"Bearer {first.token}"})
        after = client.get(f"/api/approvals/{waiting['id']}").json()["approval"]
        assert (after["state"], after["decided_by"]) == ("expired", "daemon restart")
        assert client.post(f"/api/approvals/{waiting['id']}/approve", json={}).status_code == 409
        assert client.get(f"/api/approvals/{tapped['id']}").json()["approval"]["state"] == "approved"
        assert client.get("/api/approvals").json()["pending"] == 0
    finally:
        farm.close()


# ------------------------------------------------------------------- auth, CSRF, audit

def test_every_route_needs_authentication(a):
    item = pending(a)
    for method, path in (("GET", "/api/approvals"), ("POST", "/api/approvals"), ("GET", f"/api/approvals/{item['id']}"),
                         ("GET", f"/api/approvals/{item['id']}/wait"), ("POST", f"/api/approvals/{item['id']}/approve"),
                         ("POST", f"/api/approvals/{item['id']}/deny"), ("POST", f"/api/approvals/{item['id']}/cancel"),
                         ("DELETE", "/api/approvals/sessions/s1"), ("GET", "/api/mcp/info")):
        assert a.anonymous.request(method, path, json={}).status_code == 401, (method, path)
    assert a.client.get(f"/api/approvals/{item['id']}").json()["approval"]["state"] == "pending"


def test_the_panel_decides_with_its_session_and_needs_the_csrf_header(a):
    item = pending(a)
    panel = TestClient(a.app, base_url=BASE, raise_server_exceptions=False)
    csrf = panel.post("/api/auth/login", json={"token": a.token}).json()["csrf"]
    assert [x["id"] for x in panel.get("/api/approvals", params={"state": "pending"}).json()["approvals"]] == [item["id"]]
    forged = panel.post(f"/api/approvals/{item['id']}/approve", json={})
    assert forged.status_code == 403 and forged.json()["error"]["code"] == "csrf"
    assert a.client.get(f"/api/approvals/{item['id']}").json()["approval"]["state"] == "pending"
    done = panel.post(f"/api/approvals/{item['id']}/approve", json={"remember": True}, headers={"X-SF-CSRF": csrf})
    assert done.status_code == 200 and done.json()["approval"]["decided_by"] == "panel (testclient)"


def test_audit_lines_name_the_assistant_the_tool_and_the_androids(a):
    item = pending(a, tool="stop_androids", arguments={"selector": "1-2"})
    a.client.post(f"/api/approvals/{item['id']}/approve", json={})
    a.client.post("/api/instances/bulk", json={"action": "stop", "selector": "1-2"},
                  headers={"X-SF-Caller": "mcp:test-client"})
    asked, approved, acted = a.audit_lines()[-3:]
    assert (asked["path"], asked["name"], asked["action"], asked["ids"]) == ("/api/approvals", "mcp:test-client", "stop_androids", [1, 2])
    assert (approved["action"], approved["name"], approved["ids"]) == ("approve stop_androids", "mcp:test-client", [1, 2])
    assert (acted["path"], acted["caller"], acted["selector"]) == ("/api/instances/bulk", "mcp:test-client", "1-2")
    assert all("arguments" not in line and "selector" not in line for line in (asked, approved))     # never bodies


def test_the_caller_label_in_the_audit_log_is_cleaned(a):
    a.client.post("/api/instances/bulk", json={"action": "stop", "selector": "1"},
                  headers={"X-SF-Caller": 'mcp:x" , "outcome": "forged' + "y" * 300})
    line = a.audit_lines()[-1]
    assert line["outcome"] == "ok" and len(line["caller"]) <= 80
    assert set(line["caller"]) <= set("abcdefghijklmnopqrstuvwxyz0123456789_:.@/+-")
    a.client.post("/api/instances/bulk", json={"action": "stop", "selector": "1"})
    assert "caller" not in a.audit_lines()[-1]                  # an ordinary client has no such field


def test_the_action_hits_the_androids_that_were_approved_not_whatever_matches_later(a):
    """The operator approves "Stop 2 Androids: Android 1, 2". If a third Android gets the tag
    while the request waits, it must not be stopped along with them."""
    from superfarm.cli.client import Api as HttpApi

    class Client(HttpApi):
        def __init__(self):
            super().__init__("http://127.0.0.1:1", a.token)

        def request(self, method, path, *, body=None, query=None, timeout=None):
            r = a.client.request(method, path, json=body, params={k: v for k, v in (query or {}).items() if v is not None})
            assert r.status_code < 400, r.text
            return r.json() if r.content else None

    for i in (1, 2, 3):
        a.wait(a.client.post(f"/api/instances/{i}/start"))
    for i in (1, 2):
        a.client.patch(f"/api/instances/{i}", json={"patch": {"tags": ["old"]}})

    def operator():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = a.client.get("/api/approvals", params={"state": "pending"}).json()["approvals"]
            if waiting:
                assert waiting[0]["summary"] == "Stop 2 Androids: Android 1, 2"
                a.client.patch("/api/instances/3", json={"patch": {"tags": ["old"]}})        # ... meanwhile
                a.client.post(f"/api/approvals/{waiting[0]['id']}/approve", json={})
                return
            time.sleep(0.02)

    threading.Thread(target=operator).start()
    result = invoke(Client(), "stop_androids", {"selector": "tag:old"}, caller="mcp:pin", session="pin")
    assert not result.is_error and result.structured["task"]["ok"] == 2
    states = {row["id"]: row["state"] for row in a.client.get("/api/instances").json()["instances"]}
    assert (states[1], states[2], states[3]) == ("stopped", "stopped", "booted")
    assert a.audit_lines()[-1]["selector"] == "1-2"


def test_oversized_arguments_are_refused(a):
    big = {"selector": "1", "name": "login", "variables": {f"v{n}": "x" * 1900 for n in range(40)}}
    r = ask(a, tool="run_flow", arguments=big)
    assert r.status_code == 400 and "larger than 32 kB" in r.json()["error"]["message"]
    assert a.client.get("/api/approvals").json()["approvals"] == []


# ----------------------------------------------------------------------- the whole gate

def test_invoke_end_to_end_against_the_real_routes(a, monkeypatch):
    """superfarm.ai.tools.invoke over HTTP semantics, with the test client standing in for the socket."""
    from superfarm.cli.client import Api as HttpApi

    class Client(HttpApi):
        def __init__(self):
            super().__init__("http://127.0.0.1:1", a.token)

        def request(self, method, path, *, body=None, query=None, timeout=None):
            r = a.client.request(method, path, json=body, params={k: v for k, v in (query or {}).items() if v is not None},
                                 headers={"X-SF-Caller": getattr(self, "_caller", "none")})
            if r.status_code >= 400:
                from superfarm.cli.client import _api_error
                raise _api_error(r.status_code, r.content, self)
            return r.json() if r.content else None

    def answer(action, **body):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = a.client.get("/api/approvals", params={"state": "pending"}).json()["approvals"]
            if waiting:
                a.client.post(f"/api/approvals/{waiting[0]['id']}/{action}", json=body)
                return
            time.sleep(0.02)

    for i in (1, 2, 3):
        a.wait(a.client.post(f"/api/instances/{i}/start"))
    threading.Thread(target=answer, args=("approve",)).start()
    result = invoke(Client(), "stop_androids", {"selector": "1-2"}, caller="mcp:e2e", session="e2e")
    assert not result.is_error and result.structured["task"]["state"] == "done" and result.structured["task"]["ok"] == 2
    states = {row["id"]: row["state"] for row in a.client.get("/api/instances").json()["instances"]}
    assert states[1] == states[2] == "stopped" and states[3] == "booted"

    threading.Thread(target=answer, args=("deny",), kwargs={"reason": "keep it running"}).start()
    refused = invoke(Client(), "stop_androids", {"selector": "3"}, caller="mcp:e2e", session="e2e")
    assert refused.is_error and 'declined this action: "keep it running"' in refused.text
    assert a.client.get("/api/instances/3").json()["state"] == "booted"

    blocked = invoke(Client(), "run_shell", {"selector": "3", "argv": ["id"]}, caller="mcp:e2e", session="e2e")
    assert blocked.is_error and blocked.structured["status"] == "not_allowed"
    read = invoke(Client(), "list_androids", {"state": "booted"}, caller="mcp:e2e", session="e2e")
    assert [x["id"] for x in read.structured["androids"]] == [3]
    assert {line.get("caller") for line in a.audit_lines() if line["path"] == "/api/instances/bulk"} == {"mcp:e2e"}


# --------------------------------------------------------------------------------- info

def test_mcp_info_describes_policy_tools_and_how_to_connect(a):
    info = a.client.get("/api/mcp/info").json()
    assert (info["policy"], info["ask_for"], info["deny"], info["auto_approve"]) == ("ask", ["change", "destructive"], ["run_shell"], [])
    assert (info["approval_timeout_seconds"], info["max_screenshot_px"], info["enabled"], info["http_port"]) == (300, 768, True, 7711)
    assert info["pending"] == 0 and "password" in info["forbidden_words"]
    assert [t["name"] for t in info["tools"]] == list(TOOL_NAMES)
    by_name = {t["name"]: t for t in info["tools"]}
    assert set(by_name["tap"]) == {"name", "title", "class", "decision", "reason", "description"}
    assert (by_name["list_androids"]["class"], by_name["list_androids"]["decision"]) == ("read", "allow")
    assert (by_name["tap"]["class"], by_name["tap"]["decision"]) == ("change", "ask")
    assert (by_name["delete_androids"]["class"], by_name["delete_androids"]["decision"]) == ("destructive", "ask")
    assert by_name["run_shell"]["decision"] == "deny"
    assert {t["class"] for t in info["tools"]} == {t.klass for t in all_tools()}
    stdio = info["client_config"]["stdio"]["mcpServers"]["superfarm"]
    assert stdio["args"][-3:] == ["--home", str(a.farm.paths.home), "mcp"] and stdio["command"].startswith("/")
    http = info["client_config"]["http"]["mcpServers"]["superfarm"]
    assert http["url"] == "http://127.0.0.1:7711/mcp" and "<the token" in http["headers"]["Authorization"]
    assert a.token not in json.dumps(info)                      # the snippet never carries the token
    pending(a)
    configure(a, policy="read-only", max_screenshot_px=512)
    info = a.client.get("/api/mcp/info").json()
    assert info["pending"] == 1 and info["policy"] == "read-only" and info["max_screenshot_px"] == 512
    assert {t["decision"] for t in info["tools"] if t["class"] != "read"} == {"deny"}


def test_openapi_lists_the_routes_with_their_bodies(a):
    spec = a.client.get("/api/openapi.json").json()
    for path, method in (("/api/approvals", "get"), ("/api/approvals", "post"), ("/api/approvals/{id}", "get"),
                         ("/api/approvals/{id}/wait", "get"), ("/api/approvals/{id}/approve", "post"),
                         ("/api/approvals/{id}/deny", "post"), ("/api/approvals/{id}/cancel", "post"),
                         ("/api/approvals/sessions/{session}", "delete"), ("/api/mcp/info", "get")):
        assert method in spec["paths"][path], (path, method)
    assert set(spec["components"]["schemas"]["Ask"]["properties"]) == {"tool", "arguments", "caller", "session"}
    assert spec["components"]["schemas"]["Ask"]["additionalProperties"] is False


# ------------------------------------------------------------------------------- events

def test_events_tell_the_panel_about_new_and_decided_requests(a, monkeypatch):
    monkeypatch.setattr(ws_mod, "TASK_INTERVAL", 0.02)
    before = pending(a)                                          # existed before hello: not reported
    with a.client.websocket_connect(WS + "/api/events") as ws:
        assert json.loads(ws.receive_text()) == {"type": "hello"}
        item = pending(a, tool="start_androids")
        seen = json.loads(ws.receive_text())
        assert seen["type"] == "update" and [x["id"] for x in seen["approvals"]] == [item["id"]]
        assert seen["approvals"][0]["state"] == "pending" and seen["approvals"][0]["summary"].startswith("Start 3 Androids")
        assert (seen["tasks"], seen["instances"], seen["removed"]) == ([], [], [])
        a.client.post(f"/api/approvals/{item['id']}/deny", json={"reason": "no"})
        decided = json.loads(ws.receive_text())
        assert [(x["id"], x["state"], x["reason"]) for x in decided["approvals"]] == [(item["id"], "denied", "no")]
        assert before["id"] not in json.dumps(seen)
        # An update about something else carries no "approvals" key at all.
        task = a.farm.tasks.submit("test", "a job", lambda t: None)
        other = json.loads(ws.receive_text())
        assert [t["id"] for t in other["tasks"]] == [task.id] and "approvals" not in other

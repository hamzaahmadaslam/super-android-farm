"""The in-farm AI over HTTP (/api/ai/...) and from the terminal (`sf ask`, `sf ai ...`).

The routes run on the real app with a scripted model; the CLI runs against the stub daemon,
and everything it sends is checked against the real API's own description.
"""
from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from ai_fakes import Scripted, call, say
from api_fakes import api
from automation_fakes import PACKAGE, FakeDevice, button, el, screen
from cli_fakes import Stub, api_contract, make_home, run_sf, task

from superfarm.ai.assistant import Assistant
from superfarm.ai.pilot import targets
from superfarm.ai.tools import ToolResult
from superfarm.cli.commands import ai as ai_cli
from superfarm.cli.common import Ctx
from superfarm.util.files import write_secret

HOME = screen(button("open", "Open the list", (20, 100, 460, 160)), button("noop", "Nothing", (20, 200, 460, 260)))
LIST = screen(el(id="done", text="The list", bounds=(20, 20, 300, 60)))
FAST = {"settle_seconds": 0, "human_jitter": False, "step_timeout": 1, "run_timeout": 30, "read_timeout": 5}
KEY = "sk-test-0123456789abcdef"            # noqa: S105 - a made-up key
AI = "/api/ai"


def number(nodes, text: str) -> int:
    return next(i for i, node in enumerate(targets(nodes, 60)) if node["text"] == text)


class ModelServer:
    """A one-model Ollama look-alike that passes the probe."""

    def __init__(self):
        outer = self
        self.chats = 0

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                if self.path == "/api/tags":
                    reply = {"models": [{"name": "small:3b"}]}
                else:
                    outer.chats += 1
                    last = body["messages"][-1]
                    if last["role"] == "tool":
                        reply = {"message": {"content": "The sum is 5."}}
                    elif body.get("tools"):
                        reply = {"message": {"content": "", "tool_calls": [{"function": {"name": "add", "arguments": {"a": 2, "b": 3}}}]}}
                    else:
                        reply = {"message": {"content": "ready"}}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

            def log_message(self, *_args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def model_server():
    server = ModelServer()
    yield server
    server.close()


@pytest.fixture
def a(tmp_path, model_server):
    config = {"automation": FAST, "ai": {"default_model": "local", "models": {
        "local": {"provider": "ollama", "base_url": model_server.url, "model": "small:3b"},
        "hosted": {"provider": "openai", "base_url": "http://127.0.0.1:9", "model": "big", "api_key_secret": "hosted-key"}}}}
    with api(tmp_path, config) as made:
        made.core.mgr.create(count=2)
        made.devices = {}

        def device(instance_id: int) -> FakeDevice:
            dev = FakeDevice({"home": HOME, "list": LIST}, "home", taps={("home", "open"): "list"})
            dev.serial = f"fake:{instance_id}"
            made.devices[instance_id] = dev
            return dev

        made.farm.automation.runner._device_factory = device
        made.scripts = []
        made.farm.pilot._client_factory = lambda name: made.scripts.pop(0)
        yield made


# ------------------------------------------------------------------------------- models

def test_models_are_listed_without_keys_and_tested_for_real(a, model_server):
    assert a.anonymous.get(f"{AI}/models").status_code == 401
    write_secret(a.farm.paths.secrets / "hosted-key", KEY)
    listed = a.client.get(f"{AI}/models")
    assert listed.status_code == 200 and KEY not in listed.text and listed.json()["default"] == "local"
    rows = {m["name"]: m for m in listed.json()["models"]}
    assert rows["local"]["default"] and rows["local"]["key_set"] is None and rows["hosted"]["key_set"] is True
    assert rows["hosted"]["api_key_secret"] == "hosted-key" and rows["local"]["tools"] is True

    probe = a.client.post(f"{AI}/models/local/test").json()["probe"]
    assert probe["ok"] and probe["tools"] is True and probe["model"] == "small:3b" and model_server.chats == 3
    assert [c["id"] for c in probe["checks"]] == ["reachable", "model", "reply", "tools"]
    dead = a.client.post(f"{AI}/models/hosted/test").json()["probe"]
    assert not dead["ok"] and dead["checks"][0]["kind"] == "unreachable" and "ai.models.hosted.base_url" in dead["checks"][0]["detail"]
    missing = a.client.post(f"{AI}/models/nope/test")
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "model_not_configured"
    assert any(line["path"] == f"{AI}/models/local/test" for line in a.audit_lines())


# ---------------------------------------------------------------------------- assistant

def scripted_assistant(a, *replies, invoke=None):
    seen: list = []

    def gate(client, name, args, **kw):
        seen.append({"url": client.url, "name": name, "caller": kw["caller"], "session": kw["session"]})
        return ToolResult({"ok": True}, "2 Android(s): 2 stopped.")

    script = Scripted(*replies)
    a.farm.use("assistant", Assistant(a.farm.config, a.farm.paths, a.farm.tasks, client_factory=lambda name: script,
                                      invoke=invoke or gate, status=lambda client: "2 Android(s): 2 stopped."))
    return script, seen


def test_a_conversation_over_http(a):
    script, seen = scripted_assistant(a, call("farm_status"), say("Two Androids, both stopped."))
    created = a.client.post(f"{AI}/conversations", json={})
    assert created.status_code == 201 and created.json()["conversation"]["model"] == "local"
    cid = created.json()["conversation"]["id"]
    done = a.wait(a.client.post(f"{AI}/conversations/{cid}/messages", json={"text": "how many Androids are there?"}))
    assert done["kind"] == "ai-assistant" and done["output"]["answer"] == "Two Androids, both stopped."
    # The tools reach the farm through this very daemon, on the address it really listens on.
    assert seen == [{"url": "http://127.0.0.1:80", "name": "farm_status", "caller": "assistant:local", "session": cid}]

    view = a.client.get(f"{AI}/conversations/{cid}").json()["conversation"]
    assert view["state"] == "idle" and view["last"] == 4 and view["task"] is None
    assert [e["type"] for e in view["events"]] == ["user", "tool_call", "tool_result", "final"]
    assert a.client.get(f"{AI}/conversations/{cid}", params={"after": 3}).json()["conversation"]["events"][0]["type"] == "final"
    listing = a.client.get(f"{AI}/conversations").json()["conversations"]
    assert [c["id"] for c in listing] == [cid] and listing[0]["events"] == 4 and listing[0]["title"].startswith("how many")

    assert a.client.post(f"{AI}/conversations/{cid}/cancel").json() == {"ok": True, "cancelled": False}
    assert a.client.delete(f"{AI}/conversations/{cid}").json() == {"ok": True}
    assert a.client.get(f"{AI}/conversations/{cid}").status_code == 404
    lines = [line for line in a.audit_lines() if line["path"].startswith(f"{AI}/conversations")]
    assert len(lines) == 4 and "how many" not in json.dumps(lines), "what was said is not in the audit log"


def test_conversation_errors(a):
    release, entered = threading.Event(), threading.Event()

    def slow(client, name, args, **kw):
        entered.set()
        release.wait(10)
        return ToolResult({"ok": True}, "ok")

    scripted_assistant(a, call("farm_status"), say("done"), invoke=slow)
    assert a.client.post(f"{AI}/conversations", json={"model": "nope"}).json()["error"]["code"] == "model_not_configured"
    assert a.client.post(f"{AI}/conversations", json={"surprise": 1}).status_code == 422
    cid = a.client.post(f"{AI}/conversations").json()["conversation"]["id"]
    assert a.client.post(f"{AI}/conversations/{cid}/messages", json={"text": ""}).status_code == 422
    assert a.client.post(f"{AI}/conversations/000000000000/messages", json={"text": "hi"}).status_code == 404
    assert a.client.get(f"{AI}/conversations/not-an-id").status_code == 404
    first = a.client.post(f"{AI}/conversations/{cid}/messages", json={"text": "status?"})
    assert entered.wait(5)
    busy = a.client.post(f"{AI}/conversations/{cid}/messages", json={"text": "and?"})
    assert busy.status_code == 409 and "still answering" in busy.json()["error"]["message"]
    assert a.client.get(f"{AI}/conversations/{cid}").json()["conversation"]["running_tool"]["tool"] == "farm_status"
    assert a.client.post(f"{AI}/conversations/{cid}/cancel").json()["cancelled"] is True
    release.set()
    assert a.wait(first)["output"]["ended"] == "cancelled"


# -------------------------------------------------------------------------------- pilot

def good() -> Scripted:
    return Scripted(call("tap", element=number(HOME, "Open the list"), reason="the list is behind it"))


def test_a_pilot_session_over_http_from_start_to_trained_program(a):
    a.scripts = [good(), good(), Scripted(call("done", reason="looks done"), call("done", reason="really"))]
    started = a.client.post(f"{AI}/pilot", json={"goal": "Open the list", "package": PACKAGE, "selector": "1",
                                                 "success": "text:The list", "runs": 3, "values": {"city": "Lahore"}})
    done = a.wait(started)
    sid = done["id"]
    assert done["kind"] == "ai-pilot" and done["targets"] == [1]
    assert done["output"] == {"session": sid, "dataset": "open-the-list", "runs": 3, "verified": 2, "unverified": 1,
                              "failed": 0, "recordings": done["output"]["recordings"], "model_calls": 4}

    session = a.client.get(f"{AI}/pilot/sessions/{sid}").json()["session"]
    assert session["state"] == "done" and session["goal"] == "Open the list" and session["model"] == "local"
    assert session["success"]["describe"] == "an element with text='The list' is on screen"
    assert session["counts"] == {"done": 2, "unverified": 1} and len(session["recordings"]) == 2
    device = session["devices"]["1"]
    assert device["state"] == "unverified" and device["run"] == 3 and device["runs"] == 3 and device["finished"]
    assert [r["state"] for r in device["results"]] == ["done", "done", "unverified"]
    assert device["results"][0]["verified_by"] == "operator" and device["results"][0]["model_calls"] == 1
    assert "Lahore" not in json.dumps(session), "values are named, never shown"
    assert [s["id"] for s in a.client.get(f"{AI}/pilot/sessions").json()["sessions"]] == [sid]
    assert a.client.get(f"{AI}/pilot/sessions", params={"active": "true"}).json()["sessions"] == []

    held = session["unverified"][0]
    confirmed = a.client.post(f"{AI}/pilot/sessions/{sid}/confirm", json={"instance": 1})
    assert confirmed.json() == {"confirmed": {"instance": 1, "recording": held["recording"], "state": "confirmed"}}
    assert a.client.post(f"{AI}/pilot/sessions/{sid}/confirm", json={"instance": 1}).status_code == 400

    trained = a.wait(a.client.post(f"{AI}/pilot/open-the-list/train"))
    assert trained["state"] == "done" and trained["output"]["program"]["slug"] == "open-the-list"
    summary = trained["output"]["summary"]
    assert summary["recordings"] == 3 and [s["kind"] for s in summary["outline"]] == ["launch", "tap", "assert"]
    # The confirmed run never tapped: the trainer says so instead of hiding it.
    assert summary["disagreed"] == ["p001", "p002"] and "made in 2 of 3 recordings" in " ".join(summary["text"])
    assert a.client.get(f"{AI}/pilot/open-the-list/summary").json()["summary"]["steps"] == 3

    # The trained program is an ordinary one: the automation routes run it, with no model anywhere.
    replay = a.wait(a.client.post("/api/automation/runs", json={"kind": "program", "name": "open-the-list", "selector": "2"}))
    assert replay["results"]["2"]["ok"] and a.devices[2].screen == "list"
    audit = [line for line in a.audit_lines() if line["path"] == f"{AI}/pilot"]
    assert len(audit) == 1 and audit[0]["ids"] == [1] and "Lahore" not in json.dumps(a.audit_lines())


def test_pilot_route_errors_and_steering(a):
    base = {"goal": "Open the list", "package": PACKAGE, "selector": "1"}
    for body, status in (({**base, "package": "nope"}, 400), ({**base, "selector": "99"}, 400), ({**base, "runs": 99}, 422),
                         ({**base, "model": "nope"}, 400), ({**base, "success": "@role"}, 400), ({"goal": "x"}, 422)):
        assert a.client.post(f"{AI}/pilot", json=body).status_code == status, body
    assert a.client.post(f"{AI}/pilot", json={**base, "model": "nope"}).json()["error"]["code"] == "model_not_configured"
    assert a.client.get(f"{AI}/pilot/sessions/t99").status_code == 404
    assert a.client.post(f"{AI}/pilot/no-such-dataset/train").status_code == 404
    assert a.client.post(f"{AI}/pilot/sessions/t99/cancel").status_code == 404

    a.scripts = [Scripted(call("tap", element=99))]                    # unusable three times: it asks for a person
    started = a.client.post(f"{AI}/pilot", json=base)
    sid = started.json()["task"]["id"]
    import time
    deadline = time.monotonic() + 5
    while a.client.get(f"{AI}/pilot/sessions/{sid}").json()["session"]["waiting"] != [1]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    waiting = a.client.get(f"{AI}/pilot/sessions/{sid}").json()["session"]["devices"]["1"]
    assert waiting["state"] == "needs_attention" and waiting["waiting"]["what"] == "choose the next action"
    assert a.client.post(f"{AI}/pilot/sessions/{sid}/devices/2/resolve", json={"action": "resume"}).status_code == 409
    assert a.client.post(f"{AI}/pilot/sessions/{sid}/devices/1/resolve", json={"action": "explode"}).status_code == 422
    assert a.client.post(f"{AI}/pilot/sessions/{sid}/devices/1/resolve", json={"action": "abort"}).json() == {"ok": True}
    assert a.wait(started)["results"]["1"]["ok"] is False
    assert a.client.post(f"{AI}/pilot/sessions/{sid}/cancel").json() == {"ok": True, "cancelled": True}


# ---------------------------------------------------------------------------------- CLI

@pytest.fixture(scope="module")
def contract(tmp_path_factory):
    return api_contract(tmp_path_factory.mktemp("contract-home"))


@pytest.fixture
def stub(contract):
    server = Stub()
    yield server
    server.close()
    if contract is not None:
        for request in server.requests:
            contract.check(request)


@pytest.fixture
def home(tmp_path, monkeypatch, stub):
    monkeypatch.setattr(Ctx, "sleep", lambda self, seconds: None)
    return make_home(tmp_path, monkeypatch, port=stub.port)


class Stdin(io.StringIO):
    def __init__(self, text: str = "", tty: bool = True):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def conversation(events: list[dict], **more) -> dict:
    return {"conversation": {"id": "0123456789ab", "title": "t", "model": "local", "state": "idle", "error": "",
                             "created": 1.0, "updated": 2.0, "task": None, "pending_approvals": [], "running_tool": None,
                             "usage": {}, "events": events, "last": max([e["n"] for e in events], default=0), **more}}


def test_sf_ask_prints_the_steps_the_answer_and_the_farms_record(stub, home, capsys):
    stub.on("POST", "/api/ai/conversations", (201, {"conversation": {"id": "0123456789ab", "model": "local"}}))
    stub.on("POST", "/api/ai/conversations/0123456789ab/messages", (202, {"task": task("t3", title="assistant")}))
    stub.on("DELETE", "/api/ai/conversations/0123456789ab", {"ok": True})
    stub.on("GET", "/api/ai/conversations/0123456789ab", [
        conversation([{"n": 1, "type": "user", "text": "how many?"}], state="running"),
        conversation([{"n": 2, "type": "tool_call", "tool": "farm_status", "summary": "Farm status", "klass": "read"},
                      {"n": 3, "type": "tool_result", "tool": "farm_status", "ok": True, "status": "ok",
                       "text": "2 Android(s): 1 booted, 1 stopped.\nCapacity: ..."}], state="running"),
        conversation([{"n": 4, "type": "final", "text": "One is running; there is room for 8 more.", "ended": "",
                       "actions": [], "record": "Farm record: no action that changes anything ran in this turn."}])])
    result = run_sf(capsys, "ask", "--model", "local", "how", "many?")
    assert result.code == 0 and result.out.strip() == "One is running; there is room for 8 more."
    assert "-> farm_status: Farm status" in result.err and "ok: 2 Android(s): 1 booted, 1 stopped." in result.err
    assert "Farm record: no action that changes anything ran in this turn." in result.err
    assert stub.calls("POST", "/api/ai/conversations")[0].json == {"model": "local"}
    assert stub.calls("POST", "/api/ai/conversations/0123456789ab/messages")[0].json == {"text": "how many?"}
    assert [r.query for r in stub.calls("GET", "/api/ai/conversations/0123456789ab")] == [{"after": "0"}, {"after": "1"}, {"after": "3"}]
    assert len(stub.calls("DELETE", "/api/ai/conversations/0123456789ab")) == 1


def test_sf_ask_pauses_on_an_approval_and_asks_y_or_n(stub, home, capsys, monkeypatch):
    stub.on("POST", "/api/ai/conversations", (201, {"conversation": {"id": "0123456789ab", "model": "local"}}))
    stub.on("POST", "/api/ai/conversations/0123456789ab/messages", (202, {"task": task("t3")}))
    stub.on("POST", "/api/approvals/0a1b2c3d/approve", {"approval": {"id": "0a1b2c3d", "state": "approved"}})
    stub.on("POST", "/api/approvals/0a1b2c3d/deny", {"approval": {"id": "0a1b2c3d", "state": "denied"}})
    asked = {"n": 2, "type": "approval", "approval_id": "0a1b2c3d", "state": "pending", "summary": "Start 2 Androids: Android 1, 2"}
    final = {"n": 3, "type": "final", "text": "Started.", "ended": "", "actions": [], "record": "Farm record: 1 action(s) ran: Start 2 Androids."}
    stub.on("GET", "/api/ai/conversations/0123456789ab", [conversation([asked], state="waiting_approval"), conversation([final])])
    monkeypatch.setattr(ai_cli.sys, "stdin", Stdin("y\n"))
    result = run_sf(capsys, "ask", "--keep", "start", "them")
    assert "The assistant asks to: Start 2 Androids: Android 1, 2" in result.err and "Approve? [y]es / [n]o" in result.err
    assert stub.calls("POST", "/api/approvals/0a1b2c3d/approve")[0].json == {"via": "terminal"}
    assert result.code == 0 and result.out.strip() == "Started." and stub.calls("DELETE") == []

    # No: the denial is sent. No terminal: nobody is asked, the operator is pointed at the inbox.
    stub.on("GET", "/api/ai/conversations/0123456789ab", [conversation([asked], state="waiting_approval"), conversation([final])])
    monkeypatch.setattr(ai_cli.sys, "stdin", Stdin("n\n"))
    run_sf(capsys, "ask", "--keep", "start", "them")
    assert stub.calls("POST", "/api/approvals/0a1b2c3d/deny")[0].json == {"via": "terminal", "reason": ""}
    stub.on("GET", "/api/ai/conversations/0123456789ab", [conversation([asked], state="waiting_approval"), conversation([final])])
    monkeypatch.setattr(ai_cli.sys, "stdin", Stdin("y\n", tty=False))
    piped = run_sf(capsys, "ask", "--keep", "start", "them")
    assert "sf approvals approve 0a1b2c3d" in piped.err and len(stub.calls("POST", "/api/approvals/0a1b2c3d/approve")) == 1


def test_there_is_no_flag_that_approves_in_advance(capsys):
    helped = run_sf(capsys, "ask", "--help").out
    assert "--yes" not in helped and "ai.policy" in helped
    assert run_sf(capsys, "ask", "--yes-read-only", "x").code == 2


def test_sf_ai_models(stub, home, capsys):
    stub.on("GET", "/api/ai/models", {"default": "local", "models": [
        {"name": "local", "provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "small:3b", "tools": True,
         "vision": False, "default": True, "implicit": False, "api_key_secret": "", "key_set": None, "timeout": 120},
        {"name": "hosted", "provider": "openai", "base_url": "https://api.example.com", "model": "big", "tools": True,
         "vision": True, "default": False, "implicit": False, "api_key_secret": "hosted-key", "key_set": False, "timeout": 120}]})
    listed = run_sf(capsys, "ai", "models")
    assert listed.code == 0 and "local *" in listed.out and "MISSING" in listed.out and "small:3b" in listed.out
    probe = {"name": "local", "provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "small:3b", "ok": False,
             "tools": False, "vision": None, "available": ["small:3b"], "seconds": 1.2, "checks": [
                 {"id": "reachable", "ok": True, "detail": "answers", "kind": "", "ms": 5},
                 {"id": "tools", "ok": False, "detail": "the model answered in words", "kind": "tools_unsupported", "ms": 900}]}
    stub.on("POST", "/api/ai/models/local/test", {"probe": probe})
    tested = run_sf(capsys, "ai", "models", "test", "local")
    assert tested.code == 1 and "the model answered in words" in tested.out and "NOT ready" in tested.out
    stub.on("PATCH", "/api/config", {"config": {}, "effects": {}})
    added = run_sf(capsys, "ai", "models", "add", "hosted", "--provider", "openai", "--base-url", "https://api.example.com",
                   "--model", "big", "--key-name", "hosted-key", "--vision", "--default")
    assert added.code == 0 and "sf ai models key hosted" in added.err
    assert stub.calls("PATCH", "/api/config")[0].json == {"patch": {"ai": {"default_model": "hosted", "models": {"hosted": {
        "provider": "openai", "model": "big", "base_url": "https://api.example.com", "api_key_secret": "hosted-key",
        "vision": True}}}}}
    assert run_sf(capsys, "ai", "models", "rm", "hosted").code == 0
    assert stub.calls("PATCH", "/api/config")[1].json == {"patch": {"ai": {"models": {"hosted": None}}}}


def test_sf_ai_models_key_stores_the_key_locally_and_never_prints_it(home, capsys, monkeypatch):
    from superfarm.util.files import write_yaml
    write_yaml(home.config, {"ai": {"models": {"hosted": {"provider": "openai", "api_key_secret": "hosted-key"},
                                               "local": {"provider": "ollama"}}}})
    monkeypatch.setattr(ai_cli.sys, "stdin", Stdin(KEY + "\n", tty=False))
    stored = run_sf(capsys, "ai", "models", "key", "hosted")
    assert stored.code == 0 and KEY not in stored.out + stored.err
    secret = home.secrets / "hosted-key"
    assert secret.read_text().strip() == KEY and (secret.stat().st_mode & 0o777) == 0o600
    assert run_sf(capsys, "ai", "models", "key", "local").code == 2          # names no secret
    assert run_sf(capsys, "ai", "models", "key", "nope").code == 2


def session(state="done", **more) -> dict:
    return {"id": "t5", "goal": "Open the list", "package": PACKAGE, "dataset": "open-the-list", "model": "local",
            "success": {"describe": "an element with text='The list' is on screen"}, "runs": 1, "state": state,
            "counts": {"done": 1}, "waiting": [], "recordings": ["r001-101010"], "unverified": [], "model_calls": 3,
            "devices": {"1": {"id": 1, "state": "done", "run": 1, "runs": 1, "step": 2, "steps": 25, "model_calls": 3,
                              "message": "goal reached", "results": [{"state": "done", "step": 2, "model_calls": 3,
                                                                       "elapsed": 12.0, "recording": "r001-101010",
                                                                       "message": "goal reached"}],
                              "actions": [{"n": 1, "action": "tap", "target": "'Open the list'", "outcome": "a new screen",
                                           "reason": "the list is behind it"}]}}, **more}


def test_sf_ai_pilot_starts_follows_and_trains(stub, home, capsys):
    done = task("t5", "done", current=1, total=1, results={"1": {"ok": True, "detail": "1 of 1 run(s) verified"}},
                output={"session": "t5", "dataset": "open-the-list", "runs": 1, "verified": 1, "unverified": 0, "failed": 0})
    stub.on("POST", "/api/ai/pilot", (202, {"task": task("t5", title="pilot")}))
    stub.on("GET", "/api/tasks/t5", {"task": done})
    stub.on("GET", "/api/ai/pilot/sessions/t5", {"session": session()})
    trained = task("t6", "done", output={"program": {"slug": "open-the-list", "steps": 3}, "summary": {
        "text": ["Built from 1 recording(s) made on 1 Android(s) (1).", "3 of 3 step(s) were made in every recording."],
        "warnings": ["trained from a single recording: nothing could be cross-checked."]}})
    stub.on("POST", "/api/ai/pilot/open-the-list/train", (202, {"task": trained}))
    result = run_sf(capsys, "--wait", "ai", "pilot", "Open the list", "1", "--app", PACKAGE, "--success", "text:The list",
                    "--value", "city=Lahore", "--train")
    assert result.code == 0, result.err
    assert stub.calls("POST", "/api/ai/pilot")[0].json == {"goal": "Open the list", "package": PACKAGE, "selector": "1",
                                                           "runs": 1, "success": "text:The list", "values": {"city": "Lahore"}}
    assert "program open-the-list: 3 step(s)" in result.out and "! trained from a single recording" in result.out
    assert "sf auto run program open-the-list" in result.err and "goal reached" in result.out

    assert run_sf(capsys, "ai", "pilot", "Open the list", "1").code == 2                    # which app?
    assert run_sf(capsys, "ai", "pilot", "Open the list").code == 2                         # which Androids?
    assert run_sf(capsys, "ai", "pilot", "show").code == 2


def test_sf_ai_pilot_words(stub, home, capsys):
    stub.on("GET", "/api/ai/pilot/sessions", {"sessions": [session()]})
    stub.on("GET", "/api/ai/pilot/sessions/t5", {"session": session(waiting=[1], unverified=[{"instance": 1, "recording": "r2"}])})
    listed = run_sf(capsys, "ai", "pilot", "sessions")
    assert listed.code == 0 and "open-the-list" in listed.out and "Open the list" in listed.out
    shown = run_sf(capsys, "ai", "pilot", "show", "t5", "--steps")
    assert "step 1: tap 'Open the list' -> a new screen   (the list is behind it)" in shown.out
    assert "sf ai pilot confirm t5 <android>" in shown.err and "sf ai pilot resolve t5 <android>" in shown.err
    stub.on("POST", "/api/ai/pilot/sessions/t5/cancel", {"ok": True, "cancelled": True})
    stub.on("POST", "/api/ai/pilot/sessions/t5/devices/1/resolve", {"ok": True})
    stub.on("POST", "/api/ai/pilot/sessions/t5/confirm", {"confirmed": {"instance": 1, "recording": "r2", "state": "confirmed"}})
    assert run_sf(capsys, "ai", "pilot", "cancel", "t5").code == 0
    assert run_sf(capsys, "ai", "pilot", "resolve", "t5", "1", "skip").code == 0
    assert stub.calls("POST", "/api/ai/pilot/sessions/t5/devices/1/resolve")[0].json == {"action": "skip"}
    confirmed = run_sf(capsys, "ai", "pilot", "confirm", "t5", "1")
    assert confirmed.code == 0 and "r2 now counts for training" in confirmed.err
    assert stub.calls("POST", "/api/ai/pilot/sessions/t5/confirm")[0].json == {"instance": 1}
    assert run_sf(capsys, "ai", "pilot", "resolve", "t5", "1").code == 2

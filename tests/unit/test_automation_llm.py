"""The optional model client: strict parsing, safe shapes, and both wire protocols.

The protocol tests talk to a tiny HTTP server on the loopback interface inside the
test process; nothing leaves the machine.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from superfarm.automation.llm import (
    LlmClient,
    LlmError,
    clean_annotation,
    clean_recovery,
    elements_for_model,
    make_client,
    parse_json_object,
)
from superfarm.config import LlmConfig
from superfarm.paths import Paths
from superfarm.util.files import write_secret

WORDS = ["password", "pin", "otp"]


# ------------------------------------------------------------ strict parsing
def test_only_a_json_object_is_accepted():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    for bad in ("", "Sure! Here you go: {\"a\": 1}", "[1, 2]", '"text"', "{'a': 1}", '{"a": 1} trailing',
                '```json\n{"a": 1}', "null"):
        with pytest.raises(LlmError):
            parse_json_object(bad)


def test_recovery_is_reduced_to_known_actions():
    reply = {"analysis": "a   dialog\nis in the way", "actions": [
        {"type": "tap_element", "element": 1},
        {"type": "tap_element", "element": 9},               # not in the list it was shown
        {"type": "tap_element", "element": "1"},             # not a number
        {"type": "tap_element", "element": True},
        {"type": "tap_xy", "x": 5, "y": 5},                  # coordinates it made up
        {"type": "shell", "cmd": "rm -rf /"},
        {"type": "key", "name": "BACK"},
        {"type": "key", "name": "power"},
        {"type": "swipe", "direction": "up"},
        {"type": "swipe", "direction": "sideways"},
        {"type": "wait", "seconds": 600},
        "nonsense"]}
    analysis, actions, needs_human = clean_recovery(reply, element_count=3)
    assert analysis == "a dialog is in the way" and not needs_human
    assert actions == [{"type": "tap_element", "element": 1}]          # only the first five are even looked at
    _, more, _ = clean_recovery({"analysis": "", "actions": reply["actions"][6:]}, 3)
    assert more == [{"type": "key", "name": "back"}, {"type": "swipe", "direction": "up"},
                    {"type": "wait", "seconds": 10.0}]


def test_recovery_can_ask_for_a_person():
    assert clean_recovery({"analysis": "captcha", "actions": [{"type": "tap_element", "element": 0}],
                           "needs_human": True}, 3) == ("captcha", [], True)
    assert clean_recovery({"actions": [{"type": "ask_human"}]}, 3) == ("", [], True)
    assert clean_recovery({"actions": "tap everything"}, 3) == ("", [], False)


def test_annotation_keeps_words_about_real_steps_only():
    reply = {"steps": [{"id": "p000", "intent": " open\nsearch ", "fragile": "yes", "wait_after": True},
                       {"id": "p001", "intent": 42, "wait_after": 2},
                       {"id": "p404", "intent": "invented"}, None],
             "summary": 7, "notes": "not a list"}
    clean = clean_annotation(reply, ["p000", "p001"])
    assert clean == {"steps": {"p000": {"intent": "open search"}, "p001": {"wait_after": 2.0}},
                     "summary": "", "notes": []}
    assert clean_annotation({"steps": "x"}, ["p000"])["steps"] == {}


def test_field_contents_and_secret_looking_elements_are_not_sent():
    nodes = [
        {"id": "title", "text": "Welcome back", "desc": "", "cls": "TextView", "clickable": False, "editable": False,
         "bounds": [0, 0, 10, 10]},
        {"id": "email", "text": "someone@example.org", "desc": "E-mail", "cls": "EditText", "clickable": True,
         "editable": True, "bounds": [0, 10, 10, 20]},
        {"id": "field2", "text": "hunter2", "desc": "", "cls": "EditText", "clickable": True, "editable": True,
         "password": True, "bounds": [0, 20, 10, 30]},
        {"id": "pin_hint", "text": "Your PIN is 4321", "desc": "PIN 4321", "cls": "TextView", "clickable": False,
         "editable": False, "bounds": [0, 30, 10, 40]},
    ]
    listing = elements_for_model(nodes, WORDS)
    sent = json.dumps(listing)
    assert "Welcome back" in sent and "someone@example.org" not in sent and "hunter2" not in sent
    assert "4321" not in sent
    assert listing[1] == {"n": 1, "cls": "EditText", "id": "email", "field": True, "has_text": True,
                          "desc": "E-mail", "clickable": True, "bounds": [0, 10, 10, 20]}
    assert [e["n"] for e in elements_for_model(nodes, WORDS, limit=2)] == [0, 1]


# ---------------------------------------------------------------- protocols
class _Server:
    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list[tuple[int, dict | str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                status, payload = outer.replies.pop(0) if outer.replies else (500, {"error": "no reply scripted"})
                data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
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
def server():
    s = _Server()
    yield s
    s.close()


def test_ollama_protocol(server):
    client = LlmClient(LlmConfig(provider="ollama", base_url=server.url, model="", timeout=5))
    server.replies += [(200, {"models": [{"name": "small:latest"}, {"name": "big:latest"}]}),
                       (200, {"message": {"content": '{"steps": []}'}})]
    assert client.ask("system", "user", {"type": "object"}) == {"steps": []}
    assert client.model == "small:latest"                               # the first one the server lists
    chat = server.requests[1]
    assert chat["path"] == "/api/chat" and chat["body"]["stream"] is False
    assert chat["body"]["format"] == {"type": "object"} and chat["body"]["options"]["num_ctx"] == 8192
    assert [m["role"] for m in chat["body"]["messages"]] == ["system", "user"]
    assert chat["auth"] is None


def test_ollama_falls_back_to_plain_json_mode_on_old_servers(server):
    client = LlmClient(LlmConfig(provider="ollama", base_url=server.url, model="m", timeout=5))
    server.replies += [(400, {"error": "invalid format: expected string"}),
                       (200, {"message": {"content": '{"ok": true}'}}),
                       (200, {"message": {"content": '{"ok": true}'}})]
    assert client.ask("s", "u", {"type": "object"}) == {"ok": True}
    assert server.requests[1]["body"]["format"] == "json"
    client.ask("s", "u", {"type": "object"})
    assert server.requests[2]["body"]["format"] == "json"               # remembered


def test_openai_protocol_sends_the_key_from_secrets(server, tmp_path):
    paths = Paths(tmp_path).ensure()
    write_secret(paths.secrets / "llm", "test-key-123\n")
    client = make_client(LlmConfig(provider="openai", base_url=server.url, model="gpt-like", timeout=5), paths)
    server.replies.append((200, {"choices": [{"message": {"content": '{"analysis": "x", "actions": []}'}}]}))
    assert client.ask("system", "user") == {"analysis": "x", "actions": []}
    request = server.requests[0]
    assert request["path"] == "/v1/chat/completions" and request["auth"] == "Bearer test-key-123"
    assert request["body"]["response_format"] == {"type": "json_object"} and request["body"]["model"] == "gpt-like"

    # a base URL that already ends in /v1 is used as it is; no key file means no header
    bare = LlmClient(LlmConfig(provider="openai", base_url=server.url + "/v1/", model="m", timeout=5), Paths(tmp_path / "x"))
    server.replies.append((200, {"data": [{"id": "m"}, {"id": "n"}]}))
    assert bare.models() == ["m", "n"]
    assert server.requests[1]["path"] == "/v1/models" and server.requests[1]["auth"] is None


def test_errors_are_explained_and_never_contain_the_key(server, tmp_path):
    paths = Paths(tmp_path).ensure()
    write_secret(paths.secrets / "llm", "test-key-123")
    client = LlmClient(LlmConfig(provider="openai", base_url=server.url, model="m", timeout=5), paths)
    server.replies.append((401, {"error": {"message": "invalid api key"}}))
    with pytest.raises(LlmError) as error:
        client.ask("s", "u")
    assert "401" in str(error.value) and "invalid api key" in str(error.value)
    assert "test-key-123" not in str(error.value)
    for reply in ((200, {"choices": []}), (200, {"choices": [{"message": {"content": "I think you should tap OK"}}]}),
                  (200, "<html>gateway</html>")):
        server.replies.append(reply)
        with pytest.raises(LlmError):
            client.ask("s", "u")


def test_health_and_unreachable_server(server):
    client = LlmClient(LlmConfig(provider="ollama", base_url=server.url, model="wanted", timeout=5))
    server.replies.append((200, {"models": [{"name": "other"}]}))
    ok, detail = client.health()
    assert not ok and "no model 'wanted'" in detail and "other" in detail
    server.replies.append((200, {"models": [{"name": "wanted"}]}))
    assert client.health()[0]
    empty = LlmClient(LlmConfig(provider="ollama", base_url=server.url, model="", timeout=5))
    server.replies.append((200, {"models": []}))
    with pytest.raises(LlmError, match="lists no models"):
        empty.ask("s", "u")

    dead = LlmClient(LlmConfig(provider="ollama", base_url="http://127.0.0.1:9", model="m", timeout=5))
    ok, detail = dead.health()
    assert not ok and "cannot reach the model server" in detail and "base_url" in detail


def test_configuration_is_checked():
    assert make_client(LlmConfig(provider="none")) is None
    for bad in ("file:///etc/passwd", "ftp://host/x", "localhost:11434", ""):
        with pytest.raises(ValueError, match="http"):
            LlmClient(LlmConfig(provider="ollama", base_url=bad))

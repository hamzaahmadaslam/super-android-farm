"""The model layer: profiles, both wire formats, errors that say what to check, the probe,
and the rule that nothing stored under secrets/ ever leaves in a prompt.

The protocol tests talk to a tiny HTTP server on the loopback interface inside the test
process; nothing leaves the machine.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from superfarm.ai import models, outbound
from superfarm.ai.models import ChatReply, ModelError, OllamaClient, OpenAIClient, ToolCall
from superfarm.config import Config, ModelProfile
from superfarm.paths import Paths
from superfarm.util.files import write_secret

KEY = "sk-test-0123456789abcdef"            # noqa: S105 - a made-up key that must never be echoed
TOOL = {"type": "function", "function": {"name": "list_androids", "description": "List them.",
                                         "parameters": {"type": "object", "properties": {}}}}
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 20


class Server:
    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization"),
                                       "headers": dict(self.headers)})
                reply = outer.replies.pop(0) if outer.replies else (500, {"error": "no reply scripted"})
                if callable(reply):
                    reply = reply()
                status, payload = reply
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
    s = Server()
    yield s
    s.close()


def ollama(server, paths=None, **profile) -> OllamaClient:
    return OllamaClient("local", ModelProfile(provider="ollama", base_url=server.url, model="small:3b", timeout=5, **profile),
                        paths, sleep=lambda s: None)


def openai(server, paths=None, **profile) -> OpenAIClient:
    return OpenAIClient("hosted", ModelProfile(provider="openai", base_url=server.url, model="gpt-like", timeout=5, **profile),
                        paths, sleep=lambda s: None)


def conversation() -> list[dict]:
    return [{"role": "system", "content": "be brief"},
            {"role": "user", "content": "what is on screen?", "images": [PNG]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_7", "name": "read_screen", "arguments": {"id": 3}}]},
            {"role": "tool", "tool_call_id": "call_7", "name": "read_screen", "content": "2 elements"}]


# ------------------------------------------------------------------ profiles
def test_profiles_include_the_automation_setting_as_an_implicit_one():
    empty = Config()
    assert models.profiles(empty) == {} and models.describe(empty) == []
    with pytest.raises(ModelError, match="no model is configured") as error:
        models.pick(empty)
    assert error.value.kind == "config"

    config = Config.model_validate({
        "automation": {"llm": {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "describer"}},
        "ai": {"models": {"local": {"provider": "ollama", "model": "small:3b"},
                          "hosted": {"provider": "openai", "base_url": "https://api.example.com", "api_key_secret": "hosted-key"}}}})
    known = models.profiles(config)
    assert sorted(known) == ["automation", "hosted", "local"]
    assert known["automation"].model == "describer" and known["automation"].tools is False
    with pytest.raises(ModelError, match="none is the default"):
        models.pick(config)
    with pytest.raises(ModelError, match="no model profile 'nope'"):
        models.pick(config, "nope")
    assert models.pick(config, "automation")[0] == "automation"
    chosen = Config.model_validate({**config.model_dump(), "ai": {**config.ai.model_dump(), "default_model": "hosted"}})
    assert models.pick(chosen)[0] == "hosted" and isinstance(models.make(chosen, None), OpenAIClient)
    one = Config.model_validate({"ai": {"models": {"only": {"provider": "ollama"}}},
                                 "automation": {"llm": {"provider": "ollama"}}})
    assert models.pick(one)[0] == "only", "a single explicit profile is the default without saying so"


def test_profile_names_and_addresses_are_checked(tmp_path):
    for bad in ("Has Space", "automation", "UPPER", ""):
        with pytest.raises(ValueError):
            Config.model_validate({"ai": {"models": {bad: {"provider": "ollama"}}}})
    with pytest.raises(ValueError):
        Config.model_validate({"ai": {"models": {"x": {"provider": "ollama", "api_key_secret": "../../etc/passwd"}}}})
    for address in ("file:///etc/passwd", "ftp://host/x", "localhost:11434", ""):
        with pytest.raises(ModelError, match="http") as error:
            OllamaClient("x", ModelProfile(provider="ollama", base_url=address))
        assert error.value.kind == "config"


def test_describe_never_shows_a_key_only_whether_one_is_stored(tmp_path):
    paths = Paths(tmp_path).ensure()
    config = Config.model_validate({"ai": {"default_model": "hosted", "models": {
        "hosted": {"provider": "openai", "base_url": "https://api.example.com", "api_key_secret": "hosted-key"},
        "local": {"provider": "ollama"}}}})
    rows = {r["name"]: r for r in models.describe(config, paths)}
    assert rows["hosted"]["key_set"] is False and rows["hosted"]["default"] and rows["local"]["key_set"] is None
    write_secret(paths.secrets / "hosted-key", KEY)
    shown = models.describe(config, paths)
    assert {r["name"]: r["key_set"] for r in shown}["hosted"] is True and KEY not in json.dumps(shown)


# -------------------------------------------------------------- wire formats
def test_ollama_wire_format_with_tools_and_a_picture(server):
    client = ollama(server, num_ctx=4096, extra={"keep_alive": "5m"}, headers={"X-Route": "gpu-1"})
    server.replies.append((200, {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_abc", "function": {"name": "list_androids", "arguments": {"selector": "running"}}}]},
        "prompt_eval_count": 321, "eval_count": 12}))
    reply = client.chat(conversation(), tools=[TOOL], max_tokens=200)
    assert reply.tool_calls == [ToolCall("call_abc", "list_androids", {"selector": "running"})]
    assert (reply.tokens_in, reply.tokens_out, reply.model) == (321, 12, "small:3b") and client.calls == 1
    sent = server.requests[0]
    assert sent["path"] == "/api/chat" and sent["auth"] is None and sent["headers"]["X-Route"] == "gpu-1"
    body = sent["body"]
    assert body["model"] == "small:3b" and body["stream"] is False and body["tools"] == [TOOL] and body["keep_alive"] == "5m"
    assert body["options"] == {"num_ctx": 4096, "temperature": 0.1, "num_predict": 200}
    system, user, assistant, tool = body["messages"]
    assert user["images"] == [base64.b64encode(PNG).decode()] and "images" not in system
    assert assistant["tool_calls"] == [{"function": {"name": "read_screen", "arguments": {"id": 3}}}]
    assert tool == {"role": "tool", "content": "2 elements", "tool_name": "read_screen"}
    assert reply.message() == {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_abc", "name": "list_androids", "arguments": {"selector": "running"}}]}


def test_ollama_json_schema_and_plain_reply(server):
    client = ollama(server, temperature=None, max_tokens=0)
    server.replies.append((200, {"message": {"content": '{"action": "tap"}'}}))
    reply = client.chat([{"role": "user", "content": "next?"}], json_schema={"type": "object"})
    assert reply.content == '{"action": "tap"}' and reply.tool_calls == []
    body = server.requests[0]["body"]
    assert body["format"] == {"type": "object"} and "tools" not in body and body["options"] == {"num_ctx": 16384}


def test_openai_wire_format_with_tools_a_picture_and_the_key(server, tmp_path):
    paths = Paths(tmp_path).ensure()
    write_secret(paths.secrets / "hosted-key", KEY + "\n")
    client = openai(server, paths, api_key_secret="hosted-key", extra={"seed": 7})
    server.replies.append((200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "list_androids", "arguments": '{"selector": "all"}'}},
        {"id": "call_2", "type": "function", "function": {"name": "list_androids", "arguments": "not json"}},
        {"id": "call_3", "type": "function", "function": {"name": "list_androids", "arguments": "[1, 2]"}}]}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 9}}))
    reply = client.chat(conversation(), tools=[TOOL])
    assert reply.content == "" and (reply.tokens_in, reply.tokens_out) == (50, 9)
    assert reply.tool_calls[0] == ToolCall("call_1", "list_androids", {"selector": "all"})
    # Arguments that are not a JSON object are flagged, never guessed at.
    assert [c.error for c in reply.tool_calls[1:]] == ["the arguments were not valid JSON", "the arguments must be a JSON object"]
    sent = server.requests[0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth"] == f"Bearer {KEY}"
    body = sent["body"]
    assert body["model"] == "gpt-like" and body["tools"] == [TOOL] and body["seed"] == 7
    assert body["temperature"] == 0.1 and body["max_tokens"] == 1024
    system, user, assistant, tool = body["messages"]
    assert user["content"] == [{"type": "text", "text": "what is on screen?"},
                               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()}}]
    assert assistant["tool_calls"] == [{"id": "call_7", "type": "function",
                                        "function": {"name": "read_screen", "arguments": '{"id": 3}'}}]
    assert tool == {"role": "tool", "content": "2 elements", "tool_call_id": "call_7"}

    # A base URL that already ends in /v1 is used as it is; JSON mode; a JPEG is labelled as one.
    bare = OpenAIClient("b", ModelProfile(provider="openai", base_url=server.url + "/v1/", model="m", temperature=None, max_tokens=0))
    server.replies.append((200, {"choices": [{"message": {"content": "{}"}}]}))
    bare.chat([{"role": "user", "content": "x", "images": [JPEG]}], json_schema={"type": "object"})
    body = server.requests[1]["body"]
    assert server.requests[1]["path"] == "/v1/chat/completions" and server.requests[1]["auth"] is None
    assert body["response_format"] == {"type": "json_object"} and "temperature" not in body and "max_tokens" not in body
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_model_lists_and_the_first_model_as_default(server):
    client = OllamaClient("local", ModelProfile(provider="ollama", base_url=server.url, model=""))
    server.replies += [(200, {"models": [{"name": "small:3b"}, {"name": "big:30b"}]}),
                       (200, {"message": {"content": "hi"}})]
    assert client.chat([{"role": "user", "content": "x"}]).model == "small:3b"
    assert [r["path"] for r in server.requests] == ["/api/tags", "/api/chat"]
    hosted = openai(server)
    server.replies.append((200, {"data": [{"id": "a"}, {"id": "b"}]}))
    assert hosted.models() == ["a", "b"] and server.requests[-1]["path"] == "/v1/models"
    empty = OllamaClient("local", ModelProfile(provider="ollama", base_url=server.url, model=""))
    server.replies.append((200, {"models": []}))
    with pytest.raises(ModelError, match="lists no models"):
        empty.chat([{"role": "user", "content": "x"}])


# -------------------------------------------------------------------- errors
def test_errors_say_what_to_check(server, tmp_path):
    paths = Paths(tmp_path).ensure()
    write_secret(paths.secrets / "hosted-key", KEY)
    ask = [{"role": "user", "content": "x"}]

    dead = OllamaClient("local", ModelProfile(provider="ollama", base_url="http://127.0.0.1:9", model="m", timeout=5),
                        sleep=lambda s: None)
    with pytest.raises(ModelError, match="cannot reach the model server at http://127.0.0.1:9.*ai.models.local.base_url") as e:
        dead.chat(ask)
    assert e.value.kind == "unreachable"

    client = ollama(server)
    server.replies.append((404, {"error": "model 'small:3b' not found"}))
    with pytest.raises(ModelError, match="has no model 'small:3b'.*ai.models.local.model") as e:
        client.chat(ask)
    assert e.value.kind == "model_missing" and len(server.requests) == 1, "a refusal is not retried"

    server.replies.append((400, {"error": "registry.example/library/small:3b does not support tools"}))
    with pytest.raises(ModelError, match="does not support tool calling.*ai.models.local.tools") as e:
        client.chat(ask, tools=[TOOL])
    assert e.value.kind == "tools_unsupported"

    hosted = openai(server, paths, api_key_secret="hosted-key")
    server.replies.append((401, {"error": {"message": "Incorrect API key provided"}}))
    with pytest.raises(ModelError, match="refused the credentials.*'hosted-key' was refused.*sf ai models key hosted") as e:
        hosted.chat(ask)
    assert e.value.kind == "key" and KEY not in str(e.value)
    keyless = openai(server, paths, api_key_secret="not-stored")
    server.replies.append((401, {"error": {"message": "missing key"}}))
    with pytest.raises(ModelError, match="'not-stored' is empty or missing"):
        keyless.chat(ask)

    for reply in ((200, {"choices": []}), (200, "<html>gateway</html>")):
        server.replies.append(reply)
        with pytest.raises(ModelError) as e:
            hosted.chat(ask)
        assert e.value.kind == "bad_reply"
    server.replies.append((400, {"error": {"message": "max_tokens is too large"}}))
    with pytest.raises(ModelError, match="answered HTTP 400: max_tokens is too large") as e:
        hosted.chat(ask)
    assert e.value.kind == "http"
    implicit = OllamaClient("automation", ModelProfile(provider="ollama", base_url="http://127.0.0.1:9", model="m"),
                            sleep=lambda s: None)
    with pytest.raises(ModelError, match="automation.llm.base_url"):
        implicit.chat(ask)


def test_only_what_is_worth_retrying_is_retried(server):
    naps: list[float] = []
    client = OllamaClient("local", ModelProfile(provider="ollama", base_url=server.url, model="m", timeout=5), sleep=naps.append)
    server.replies += [(503, {"error": "loading model"}), (429, {"error": "busy"}), (200, {"message": {"content": "ok"}})]
    assert client.chat([{"role": "user", "content": "x"}]).content == "ok"
    assert len(server.requests) == 3 and naps == [0.5, 2.0]
    server.replies += [(500, {"error": "boom"})] * 3
    with pytest.raises(ModelError, match="HTTP 500: boom"):
        client.chat([{"role": "user", "content": "x"}])
    assert len(server.requests) == 6, "bounded: three attempts, then the error"


def test_a_slow_model_times_out_once_and_says_so(server):
    client = OllamaClient("local", ModelProfile(provider="ollama", base_url=server.url, model="m", timeout=5), sleep=lambda s: None)

    def slow():
        time.sleep(1.5)
        return (200, {"message": {"content": "late"}})

    server.replies.append(slow)
    started = time.monotonic()
    with pytest.raises(ModelError, match="gave no answer within 0s.*ai.models.local.timeout") as e:
        client.chat([{"role": "user", "content": "x"}], timeout=0.3)
    assert e.value.kind == "timeout" and time.monotonic() - started < 1.4 and len(server.requests) == 1


# --------------------------------------------------------------------- probe
def test_probe_passes_when_everything_round_trips(server):
    client = ollama(server, vision=True)
    server.replies += [
        (200, {"models": [{"name": "small:3b"}]}),
        (200, {"message": {"content": "ready"}}),
        (200, {"message": {"content": "", "tool_calls": [{"function": {"name": "add", "arguments": {"a": 2, "b": 3}}}]}}),
        (200, {"message": {"content": "The sum is 5."}}),
        (200, {"message": {"content": "Red."}})]
    result = models.probe(client)
    assert result["ok"] and result["tools"] is True and result["vision"] is True and result["available"] == ["small:3b"]
    assert [(c["id"], c["ok"]) for c in result["checks"]] == [("reachable", True), ("model", True), ("reply", True),
                                                             ("tools", True), ("vision", True)]
    second = server.requests[3]["body"]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "tool"] and second[2]["content"] == "5"
    assert server.requests[4]["body"]["messages"][0]["images"], "the vision check really sends a picture"


def test_probe_reports_exactly_what_failed(server):
    client = ollama(server)
    server.replies += [(200, {"models": [{"name": "other:1b"}]})]
    missing = models.probe(client)
    assert not missing["ok"] and [c["id"] for c in missing["checks"]] == ["reachable", "model"]
    assert missing["checks"][1]["kind"] == "model_missing" and "it has: other:1b" in missing["checks"][1]["detail"]

    server.replies += [(200, {"models": [{"name": "small:3b"}]}), (200, {"message": {"content": "ready"}}),
                       (200, {"message": {"content": "I would add them: 5"}})]
    wordy = models.probe(client)
    assert not wordy["ok"] and wordy["tools"] is False and "answered in words instead of calling the tool" in wordy["checks"][-1]["detail"]

    server.replies += [(200, {"models": [{"name": "small:3b"}]}), (200, {"message": {"content": "ready"}}),
                       (200, {"message": {"tool_calls": [{"function": {"name": "add", "arguments": {"a": "2", "b": 3}}}]}})]
    wrong = models.probe(client)
    assert not wrong["ok"] and "instead of add(a=2, b=3)" in wrong["checks"][-1]["detail"]

    dead = models.probe(OllamaClient("d", ModelProfile(provider="ollama", base_url="http://127.0.0.1:9", model="m"),
                                     sleep=lambda s: None))
    assert not dead["ok"] and [c["id"] for c in dead["checks"]] == ["reachable"] and dead["checks"][0]["kind"] == "unreachable"
    no_tools = ollama(server, tools=False)
    server.replies += [(200, {"models": [{"name": "small:3b"}]}), (200, {"message": {"content": "ready"}})]
    plain = models.probe(no_tools)
    assert plain["ok"] and plain["tools"] is None, "a profile without tools is not tested for them"


# ----------------------------------------------------------------- redaction
def test_secret_values_and_names_never_reach_a_prompt(tmp_path):
    paths = Paths(tmp_path).ensure()
    write_secret(paths.secrets / "api-token", "farm-token-abcdef123456\n")
    write_secret(paths.secrets / "vpn-default", "vpnuser01\nvpn-pass-98765\n")
    write_secret(paths.secrets / "tiny", "abc\n")
    values = outbound.secret_values(paths.secrets)
    assert set(values) >= {"farm-token-abcdef123456", "vpnuser01", "vpn-pass-98765"} and "abc" not in values
    assert outbound.secret_values(None) == [] and outbound.secret_values(tmp_path / "missing") == []

    dirty = {"note": "curl -H 'Authorization: Bearer farm-token-abcdef123456' ... as vpnuser01",
             "password": "hunter2", "otp_code": "123456", "nested": [{"api_key": "k", "text": "pass is vpn-pass-98765"}],
             "pin": "", "count": 3, "label": "shipping address"}
    clean = outbound.for_model(dirty, words=["password", "pin", "otp"], values=[*values, "run-secret-value"])
    text = json.dumps(clean)
    for leaked in ("farm-token-abcdef123456", "vpnuser01", "vpn-pass-98765", "hunter2", "123456"):
        assert leaked not in text
    assert clean["password"] == "(hidden)" and clean["otp_code"] == "(hidden)" and clean["nested"][0]["api_key"] == "(hidden)"
    assert clean["pin"] == "" and clean["count"] == 3 and clean["label"] == "shipping address"
    assert clean["note"].count("***") == 2
    assert outbound.for_model("say run-secret-value twice: run-secret-value", values=["run-secret-value"]) == "say *** twice: ***"
    assert dirty["password"] == "hunter2", "the original is not modified"


def test_every_outgoing_message_is_cleaned_at_the_last_moment(server, tmp_path):
    paths = Paths(tmp_path).ensure()
    write_secret(paths.secrets / "api-token", "farm-token-abcdef123456")
    write_secret(paths.secrets / "hosted-key", KEY)
    messages = [{"role": "user", "content": "my token is farm-token-abcdef123456, use it", "images": [PNG]},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "name": "type_text",
                                                                      "arguments": {"password": "hunter2", "text": KEY}}]},
                {"role": "tool", "tool_call_id": "c", "name": "type_text", "content": f"typed {KEY}"}]
    for client, reply in ((ollama(server, paths, api_key_secret="hosted-key"), {"message": {"content": "ok"}}),
                          (openai(server, paths, api_key_secret="hosted-key"), {"choices": [{"message": {"content": "ok"}}]})):
        server.replies.append((200, reply))
        client.chat(messages)
        raw = json.dumps(server.requests[-1]["body"])
        assert "farm-token-abcdef123456" not in raw and KEY not in raw and "hunter2" not in raw
        assert "my token is ***, use it" in raw and "(hidden)" in raw
        assert server.requests[-1]["auth"] == f"Bearer {KEY}", "the key travels in the header, where it belongs"
    assert messages[0]["content"].endswith("use it") and "farm-token" in messages[0]["content"], "the caller's list is untouched"


# ------------------------------------------------------------------ cancelling
def test_a_waiting_caller_can_stop_waiting():
    cancelled, release = threading.Event(), threading.Event()
    assert models.interruptible(lambda: 7, cancelled) == 7
    with pytest.raises(ModelError, match="boom"):
        models.interruptible(lambda: (_ for _ in ()).throw(ModelError("boom")), cancelled)
    threading.Timer(0.1, cancelled.set).start()
    started = time.monotonic()
    with pytest.raises(models.Interrupted):
        models.interruptible(lambda: release.wait(10), cancelled, poll=0.02)
    assert time.monotonic() - started < 2
    release.set()
    assert ChatReply(content="x").message() == {"role": "assistant", "content": "x"}

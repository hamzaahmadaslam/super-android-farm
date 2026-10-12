"""Model profiles, and the one place a model is called from.

A *profile* (ai.models.<name>) says where a model lives and what it can do. Two wire
formats cover every server in use: `ollama` (its native /api/chat) and `openai` (the
chat-completions API that most hosted and local servers speak). The older
`automation.llm` setting is the implicit profile named "automation", and
automation/llm.py sends its requests through `http_json()` below, so every byte that
goes to a model leaves through this file.

Callers build messages in one neutral shape and each client translates:

    {"role": "system" | "user", "content": str, "images": [bytes, ...]}       images: user only
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments": {...}}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

Standard library only (urllib): one POST per turn, a hard timeout, a bounded number of
retries, and errors that say what to check. Every outgoing message passes
`outbound.for_model()`, so nothing stored under secrets/ can reach a model.
"""
from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..config import Config, ModelProfile
from ..paths import Paths
from ..util.files import safe_filename
from . import outbound

MAX_REPLY_BYTES = 4 * 1024 * 1024
RETRIES = 2                       # extra attempts after a connection failure or a 5xx/429
RETRY_STATUS = (429, 500, 502, 503, 504)
IMPLICIT = "automation"           # the profile made from automation.llm


class ModelError(RuntimeError):
    """A model could not be asked, or did not answer usefully. `kind` is one of:
    config, unreachable, timeout, key, model_missing, tools_unsupported, http, bad_reply."""

    def __init__(self, message: str, *, kind: str = "http", status: int = 0):
        super().__init__(message)
        self.kind, self.status = kind, status


class Unreachable(OSError):
    """No HTTP answer at all. `timed_out` tells a slow model from a dead address."""

    def __init__(self, reason: str, *, timed_out: bool = False):
        super().__init__(reason)
        self.reason, self.timed_out = reason, timed_out


# ----------------------------------------------------------------- transport
def check_url(url: str, setting: str = "base_url") -> str:
    parts = urlsplit(url or "")
    # Only plain web addresses: urllib would otherwise happily open file: URLs.
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{setting} must be an http(s) address, not {url!r}")
    return url.rstrip("/")


def http_json(url: str, payload: dict | None = None, *, headers: dict[str, str] | None = None,
              timeout: float = 60.0) -> tuple[int, Any]:
    """One request to a model server: POST when there is a payload, GET otherwise.

    Returns (status, body). The body is the parsed JSON, or {"error": <text>} when the
    server sent something else (a proxy's HTML error page). Raises Unreachable when no
    HTTP answer arrived.
    """
    send = {"Accept": "application/json", **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        send["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=send,  # noqa: S310 - callers pass check_url() addresses
                                     method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status, raw = response.status, response.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(8192)
    except TimeoutError:
        raise Unreachable(f"no answer within {timeout:.0f}s", timed_out=True) from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise Unreachable(str(reason), timed_out=isinstance(reason, TimeoutError)) from None
    if len(raw) > MAX_REPLY_BYTES:
        raise ModelError("the model server sent an implausibly large reply", kind="bad_reply")
    try:
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {"error": raw.decode("utf-8", errors="replace")[:300]}
    return status, data


def error_text(data: Any) -> str:
    error = data.get("error") if isinstance(data, dict) else data
    if isinstance(error, dict):
        error = error.get("message") or error
    return " ".join(str(error or "").split())[:300]


# ------------------------------------------------------------------- replies
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    error: str = ""               # why the arguments could not be used, when they could not

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class ChatReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0

    def message(self) -> dict:
        """This reply as the assistant message to append to the conversation."""
        out: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [c.to_dict() for c in self.tool_calls]
        return out


def _arguments(raw: Any) -> tuple[dict, str]:
    """A tool call's arguments as a mapping. Strict: a model that sends anything else is
    told so, never guessed at."""
    if isinstance(raw, dict):
        return raw, ""
    if raw in (None, ""):
        return {}, ""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}, "the arguments were not valid JSON"
        if isinstance(data, dict):
            return data, ""
    return {}, "the arguments must be a JSON object"


def _tool_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for number, item in enumerate(raw if isinstance(raw, list) else []):
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        arguments, problem = _arguments(function.get("arguments"))
        calls.append(ToolCall(id=str(item.get("id") or f"call_{number}"), name=function["name"][:120],
                              arguments=arguments, error=problem))
    return calls


# ------------------------------------------------------------------- clients
class ChatClient:
    """One profile, ready to be asked. Subclasses know one wire format each."""

    provider = ""

    def __init__(self, name: str, profile: ModelProfile, paths: Paths | None = None, *,
                 forbidden_words: Iterable[str] = (), sleep: Callable[[float], None] = time.sleep):
        self.name = name
        self.profile = profile
        try:
            self.base = check_url(profile.base_url)
        except ValueError as e:
            raise ModelError(f"model profile {name!r}: {e}", kind="config") from None
        self.model = profile.model
        self.timeout = float(profile.timeout)
        self._secrets_dir = paths.secrets if paths is not None else None
        self._key_file = (paths.secrets / safe_filename(profile.api_key_secret)
                          if paths is not None and profile.api_key_secret else None)
        self._words = tuple(forbidden_words)
        self._sleep = sleep
        self.calls = 0                 # requests that reached the model, for budgets

    # -- plumbing ----------------------------------------------------------
    def _setting(self, key: str) -> str:
        return f"automation.llm.{key}" if self.name == IMPLICIT else f"ai.models.{self.name}.{key}"

    def _api_key(self) -> str:
        if self._key_file is None:
            return ""
        try:
            return self._key_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""                  # a local server needs none; a remote one answers 401

    def _headers(self) -> dict[str, str]:
        headers = dict(self.profile.headers)
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _url(self, path: str) -> str:
        return self.base + path

    def _request(self, path: str, payload: dict | None, *, timeout: float | None = None) -> Any:
        """Send, retry what is worth retrying, and turn a refusal into words."""
        problem: ModelError | None = None
        for attempt in range(RETRIES + 1):
            if attempt:
                self._sleep(0.5 * attempt * attempt)
            try:
                status, data = http_json(self._url(path), payload, headers=self._headers(),
                                         timeout=timeout or self.timeout)
            except Unreachable as e:
                if e.timed_out:
                    # Asking again would only double the wait for a model that is busy or loading.
                    raise ModelError(
                        f"the model {self.model or 'server'} at {self.base} gave no answer within "
                        f"{timeout or self.timeout:.0f}s. It may still be loading, or too slow for this "
                        f"machine: raise {self._setting('timeout')} or use a smaller model.", kind="timeout") from None
                problem = ModelError(f"cannot reach the model server at {self.base}: {e.reason}. Check "
                                     f"{self._setting('base_url')} and that the server is running.", kind="unreachable")
                continue
            if status < 400:
                return data
            problem = self._refusal(status, error_text(data))
            if status not in RETRY_STATUS:
                break
        assert problem is not None
        raise problem

    def _refusal(self, status: int, said: str) -> ModelError:
        lowered = said.lower()
        if status in (401, 403):
            stored = "is empty or missing" if not self._api_key() else "was refused"
            secret = self.profile.api_key_secret or "(none named)"
            return ModelError(
                f"the model server at {self.base} refused the credentials (HTTP {status}: {said or 'no detail'}). "
                f"The API key secret {secret!r} {stored}; store it with: sf ai models key {self.name}",
                kind="key", status=status)
        if "does not support tools" in lowered or ("tool" in lowered and "support" in lowered):
            return ModelError(
                f"the model {self.model!r} does not support tool calling ({said}). Pick a model that does, "
                f"or set {self._setting('tools')} to false (the Assistant needs tools; the Pilot can work without).",
                kind="tools_unsupported", status=status)
        if status == 404 or ("model" in lowered and "not found" in lowered):
            return ModelError(
                f"the server at {self.base} has no model {self.model!r} (HTTP {status}: {said or 'not found'}). "
                f"Pull or load it there, or change {self._setting('model')}; `sf ai models test {self.name}` "
                "lists what the server has.", kind="model_missing", status=status)
        return ModelError(f"the model server at {self.base} answered HTTP {status}: {said or 'no detail'}",
                          kind="http", status=status)

    def _outgoing(self, messages: list[dict]) -> list[dict]:
        """The messages as they may leave: secrets removed, pictures left as they are."""
        values = outbound.secret_values(self._secrets_dir)
        clean = []
        for message in messages:
            images = message.get("images")
            safe = outbound.for_model({k: v for k, v in message.items() if k != "images"},
                                      words=self._words, values=values)
            if images:
                safe["images"] = list(images)
            clean.append(safe)
        return clean

    # -- interface ---------------------------------------------------------
    def models(self) -> list[str]:
        raise NotImplementedError

    def resolve_model(self) -> str:
        """Settle on a model the server really has (the first it lists when none is set)."""
        if not self.model:
            names = self.models()
            if not names:
                raise ModelError(f"the model server at {self.base} lists no models; pull or load one, or set "
                                 f"{self._setting('model')}", kind="model_missing")
            self.model = names[0]
        return self.model

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None, json_schema: dict | None = None,
             max_tokens: int | None = None, timeout: float | None = None) -> ChatReply:
        """One turn. `tools` are OpenAI-style function definitions; `json_schema` asks for
        a reply that is one JSON object (used when a model cannot call tools)."""
        model = self.resolve_model()
        started = time.monotonic()
        reply = self._chat(model, self._outgoing(messages), tools, json_schema,
                           self.profile.max_tokens if max_tokens is None else max_tokens, timeout)
        self.calls += 1
        reply.model, reply.seconds = model, round(time.monotonic() - started, 2)
        return reply

    def _chat(self, model: str, messages: list[dict], tools: list[dict] | None, json_schema: dict | None,
              max_tokens: int, timeout: float | None) -> ChatReply:
        raise NotImplementedError


class OllamaClient(ChatClient):
    provider = "ollama"

    def models(self) -> list[str]:
        data = self._request("/api/tags", None, timeout=min(self.timeout, 15))
        rows = data.get("models") if isinstance(data, dict) else None
        return [str(r["name"]) for r in rows or [] if isinstance(r, dict) and r.get("name")]

    def _chat(self, model, messages, tools, json_schema, max_tokens, timeout):
        wire = []
        for message in messages:
            role = message.get("role")
            entry: dict[str, Any] = {"role": role, "content": str(message.get("content") or "")}
            if message.get("images"):
                entry["images"] = [base64.b64encode(i).decode("ascii") for i in message["images"]]
            if role == "assistant" and message.get("tool_calls"):
                entry["tool_calls"] = [{"function": {"name": c["name"], "arguments": c.get("arguments") or {}}}
                                       for c in message["tool_calls"]]
            if role == "tool":
                entry["tool_name"] = str(message.get("name") or "")
            wire.append(entry)
        options: dict[str, Any] = {"num_ctx": int(self.profile.num_ctx)}
        if self.profile.temperature is not None:
            options["temperature"] = float(self.profile.temperature)
        if max_tokens:
            options["num_predict"] = int(max_tokens)
        payload: dict[str, Any] = {"model": model, "messages": wire, "stream": False, "options": options,
                                   **self.profile.extra}
        if tools:
            payload["tools"] = tools
        if json_schema is not None:
            payload["format"] = json_schema
        data = self._request("/api/chat", payload, timeout=timeout)
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
            raise ModelError("the model server's reply has no message in it", kind="bad_reply")
        return ChatReply(content=str(message.get("content") or ""), tool_calls=_tool_calls(message.get("tool_calls")),
                         tokens_in=int(data.get("prompt_eval_count") or 0), tokens_out=int(data.get("eval_count") or 0))


class OpenAIClient(ChatClient):
    """Any server that speaks the OpenAI chat-completions API."""

    provider = "openai"

    def _url(self, path: str) -> str:
        root = self.base if self.base.endswith("/v1") else self.base + "/v1"
        return root + path

    def models(self) -> list[str]:
        data = self._request("/models", None, timeout=min(self.timeout, 15))
        rows = data.get("data") if isinstance(data, dict) else None
        return [str(r["id"]) for r in rows or [] if isinstance(r, dict) and r.get("id")]

    def _chat(self, model, messages, tools, json_schema, max_tokens, timeout):
        wire = []
        for message in messages:
            role = message.get("role")
            content: Any = str(message.get("content") or "")
            if message.get("images"):
                content = [{"type": "text", "text": content}] + [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{_image_type(i)};base64,{base64.b64encode(i).decode('ascii')}"}}
                    for i in message["images"]]
            entry: dict[str, Any] = {"role": role, "content": content}
            if role == "assistant" and message.get("tool_calls"):
                entry["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments") or {})}}
                                       for c in message["tool_calls"]]
            if role == "tool":
                entry["tool_call_id"] = str(message.get("tool_call_id") or "")
            wire.append(entry)
        payload: dict[str, Any] = {"model": model, "messages": wire, **self.profile.extra}
        if self.profile.temperature is not None:
            payload["temperature"] = float(self.profile.temperature)
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if tools:
            payload["tools"] = tools
        if json_schema is not None:
            # json_object is the form every compatible server knows; the schema itself is
            # in the prompt, and the caller validates the reply anyway.
            payload["response_format"] = {"type": "json_object"}
        data = self._request("/chat/completions", payload, timeout=timeout)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            message = None
        if not isinstance(message, dict):
            raise ModelError("the model server's reply has no message in it", kind="bad_reply")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        content = message.get("content")
        return ChatReply(content=content if isinstance(content, str) else "",
                         tool_calls=_tool_calls(message.get("tool_calls")),
                         tokens_in=int(usage.get("prompt_tokens") or 0), tokens_out=int(usage.get("completion_tokens") or 0))


def _image_type(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


CLIENTS: dict[str, type[ChatClient]] = {"ollama": OllamaClient, "openai": OpenAIClient}


# ------------------------------------------------------------------ profiles
def profiles(config: Config) -> dict[str, ModelProfile]:
    """Every usable profile: ai.models, plus "automation" when automation.llm names a model server."""
    out = dict(config.ai.models)
    llm = config.automation.llm
    if llm.provider != "none":
        # Not offered for tool calling or pictures: that setting predates both, and the
        # model behind it was chosen for describing steps.
        out[IMPLICIT] = ModelProfile(provider=llm.provider, base_url=llm.base_url, model=llm.model,
                                     api_key_secret=llm.api_key_secret, timeout=llm.timeout,
                                     temperature=llm.temperature, num_ctx=llm.num_ctx, tools=False, vision=False)
    return out


def pick(config: Config, name: str | None = None) -> tuple[str, ModelProfile]:
    """The profile to use: the one named, else ai.default_model, else the only one there is."""
    known = profiles(config)
    wanted = name or config.ai.default_model
    if not wanted:
        explicit = [n for n in known if n != IMPLICIT] or list(known)
        if len(explicit) == 1:
            wanted = explicit[0]
        elif not known:
            raise ModelError("no model is configured. Add a profile under ai.models (see docs/AI.md), for example: "
                             "sf config set ai.models.local.provider ollama", kind="config")
        else:
            raise ModelError("several model profiles exist and none is the default: set ai.default_model to one of "
                             + ", ".join(sorted(known)), kind="config")
    if wanted not in known:
        raise ModelError(f"there is no model profile {wanted!r} (profiles: {', '.join(sorted(known)) or 'none'})",
                         kind="config")
    return wanted, known[wanted]


def make(config: Config, paths: Paths | None, name: str | None = None) -> ChatClient:
    chosen, profile = pick(config, name)
    return CLIENTS[profile.provider](chosen, profile, paths, forbidden_words=config.automation.forbidden_field_words)


def describe(config: Config, paths: Paths | None = None) -> list[dict]:
    """The profiles as the panel lists them. Never a key: only whether one is stored."""
    try:
        default = pick(config)[0]
    except ModelError:
        default = ""
    rows = []
    for name, profile in sorted(profiles(config).items()):
        key_set = None
        if profile.api_key_secret and paths is not None:
            key_file = paths.secrets / safe_filename(profile.api_key_secret)
            key_set = key_file.is_file() and key_file.stat().st_size > 0
        rows.append({"name": name, "provider": profile.provider, "base_url": profile.base_url, "model": profile.model,
                     "vision": profile.vision, "tools": profile.tools, "timeout": profile.timeout,
                     "default": name == default, "implicit": name == IMPLICIT,
                     "api_key_secret": profile.api_key_secret, "key_set": key_set})
    return rows


# --------------------------------------------------------------------- probe
PROBE_TOOL = {"type": "function", "function": {
    "name": "add", "description": "Add two whole numbers and return the sum.",
    "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                   "required": ["a", "b"]}}}


def _red_square() -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (220, 20, 20)).save(buffer, format="PNG")
    return buffer.getvalue()


def probe(client: ChatClient) -> dict:
    """Really test a profile: the server answers, it has the model, the model replies,
    a tool call goes there and back, and (for a vision profile) a picture is read.

    Each check is reported on its own; a check is skipped once an earlier one it depends
    on has failed. `ok` is true when every check that ran passed.
    """
    profile = client.profile
    checks: list[dict] = []

    def run(check_id: str, fn: Callable[[], str]) -> bool:
        started = time.monotonic()
        try:
            detail, passed, kind = fn(), True, ""
        except ModelError as e:
            detail, passed, kind = str(e), False, e.kind
        checks.append({"id": check_id, "ok": passed, "detail": detail, "kind": kind,
                       "ms": int((time.monotonic() - started) * 1000)})
        return passed

    listed: list[str] = []

    def reachable() -> str:
        listed.extend(client.models())
        return f"{client.base} answers and lists {len(listed)} model(s)"

    def present() -> str:
        if not client.model:
            client.resolve_model()
            return f"no model is set; using the first one listed: {client.model}"
        if listed and client.model not in listed and f"{client.model}:latest" not in listed:
            raise ModelError(f"the server has no model {client.model!r} (it has: {', '.join(listed[:8]) or 'none'})",
                             kind="model_missing")
        return f"model {client.model} is there"

    def reply() -> str:
        answer = client.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=16)
        if not answer.content.strip():
            raise ModelError("the model returned an empty reply", kind="bad_reply")
        return f"replied {answer.content.strip()[:40]!r} in {answer.seconds:.1f}s"

    def tool_round_trip() -> str:
        messages: list[dict] = [{"role": "user", "content": "Use the add tool to add 2 and 3, then tell me the sum."}]
        first = client.chat(messages, tools=[PROBE_TOOL], max_tokens=128)
        call = first.tool_calls[0] if first.tool_calls else None
        if call is None:
            raise ModelError("the model answered in words instead of calling the tool: "
                             f"{first.content.strip()[:80]!r}", kind="tools_unsupported")
        if call.name != "add" or call.error or call.arguments.get("a") != 2 or call.arguments.get("b") != 3:
            raise ModelError(f"the model called {call.name}({json.dumps(call.arguments)[:80]}) "
                             f"instead of add(a=2, b=3) {call.error}".strip(), kind="tools_unsupported")
        messages += [first.message(), {"role": "tool", "tool_call_id": call.id, "name": "add", "content": "5"}]
        final = client.chat(messages, tools=[PROBE_TOOL], max_tokens=64)
        if "5" not in final.content:
            raise ModelError("the tool call was made, but the model did not use its result: "
                             f"{final.content.strip()[:80]!r}", kind="tools_unsupported")
        return "called add(a=2, b=3) and used the result"

    def picture() -> str:
        answer = client.chat([{"role": "user", "content": "What colour fills this picture? Answer with one word.",
                               "images": [_red_square()]}], max_tokens=16)
        if "red" not in answer.content.lower():
            raise ModelError(f"shown a red square, the model said {answer.content.strip()[:60]!r}; it probably "
                             f"cannot read pictures (set {client._setting('vision')} to false)", kind="bad_reply")
        return "read a test picture correctly"

    started = time.monotonic()
    alive = run("reachable", reachable) and run("model", present) and run("reply", reply)
    if alive and profile.tools:
        run("tools", tool_round_trip)
    if alive and profile.vision:
        run("vision", picture)
    by_id = {c["id"]: c["ok"] for c in checks}
    return {"name": client.name, "provider": client.provider, "base_url": client.base, "model": client.model,
            "ok": all(by_id.values()), "tools": by_id.get("tools"), "vision": by_id.get("vision"),
            "available": listed[:50], "seconds": round(time.monotonic() - started, 1), "checks": checks}


# ---------------------------------------------------------------- cancelling
class Interrupted(Exception):
    """The caller stopped waiting for a model."""


def interruptible(fn: Callable[[], Any], cancelled: Any, *, poll: float = 0.1) -> Any:
    """Run one model request so that Stop does not have to wait for it.

    urllib cannot be interrupted, and a slow model may take minutes. The request runs
    on a helper thread; when `cancelled` (a threading.Event) is set first, this raises
    Interrupted and the abandoned request ends on its own at its timeout.
    """
    import threading

    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed to the caller below
            box["error"] = e

    thread = threading.Thread(target=work, name="sf-model-call", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(poll)
        if thread.is_alive() and cancelled is not None and cancelled.is_set():
            raise Interrupted()
    if "error" in box:
        raise box["error"]
    return box.get("value")

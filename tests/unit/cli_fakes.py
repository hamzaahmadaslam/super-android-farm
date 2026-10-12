"""A tiny in-process HTTP server standing in for the daemon, and helpers to run `sf` against it.

It listens on 127.0.0.1 with a port the kernel picks, answers from a table of canned
responses, and records every request so a test can assert what the CLI actually sent
(method, path, query, headers, body). Nothing leaves the machine.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

TOKEN = "stub-token-0123456789"


class Recorded:
    def __init__(self, method: str, path: str, query: dict, headers: dict, body: bytes):
        self.method, self.path, self.query, self.headers, self.body = method, path, query, headers, body

    @property
    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    def __repr__(self) -> str:
        return f"{self.method} {self.path}"


class Stub:
    """`stub.on("GET", "/api/instances", {...})` answers 200 with that JSON.

    A response may be: a JSON-able value (200), a `(status, value)` pair, a `(status, bytes,
    content_type)` triple, a callable taking the Recorded request and returning any of those,
    or a list of the above used one per call (the last one repeats)."""

    def __init__(self, *, token: str | None = TOKEN):
        self.token = token
        self.routes: dict[tuple[str, str], Any] = {}
        self.requests: list[Recorded] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = unquote(parts.path)              # as a real server hands it to its routes
                request = Recorded(self.command, path, dict(parse_qsl(parts.query)),
                                   {k.lower(): v for k, v in self.headers.items()}, body)
                stub.requests.append(request)
                if stub.token is not None and request.headers.get("authorization") != f"Bearer {stub.token}":
                    return self._send(401, {"error": {"code": "unauthorized", "message": "missing or wrong token"}})
                answer = stub.routes.get((self.command, path))
                if answer is None:
                    return self._send(404, {"error": {"code": "not_found", "message": f"no route {path}"}})
                if isinstance(answer, list):
                    answer = answer.pop(0) if len(answer) > 1 else answer[0]
                if callable(answer):
                    answer = answer(request)
                if isinstance(answer, tuple) and len(answer) == 3:
                    return self._raw(*answer)
                if isinstance(answer, tuple):
                    return self._send(*answer)
                return self._send(200, answer)

            def _send(self, status: int, value: Any) -> None:
                self._raw(status, b"" if value is None else json.dumps(value).encode(), "application/json")

            def _raw(self, status: int, data: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def on(self, method: str, path: str, answer: Any) -> None:
        self.routes[(method, path)] = answer

    def calls(self, method: str | None = None, path: str | None = None) -> list[Recorded]:
        return [r for r in self.requests if (method is None or r.method == method) and (path is None or r.path == path)]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


def task(task_id: str = "t1", state: str = "running", *, current: int = 0, total: int = 0,
         results: dict | None = None, title: str = "Start Androids", **more: Any) -> dict:
    """A task dict shaped like superfarm.core.tasks.Task.to_dict()."""
    results = results or {}
    return {"id": task_id, "kind": "bulk", "title": title, "targets": [int(k) for k in results], "state": state,
            "current": current, "total": total, "message": "", "error": "", "results": results,
            "failed": sum(1 for r in results.values() if not r["ok"]), "output": None, "created": 1.0,
            "started": 1.0, "finished": 0.0, **more}


def instance(instance_id: int, *, state: str = "stopped", tags: tuple[str, ...] = (), name: str = "") -> dict:
    """A row shaped like InstanceManager.list_status()."""
    running = state in ("running", "booted")
    return {"id": instance_id, "name": name or f"Android {instance_id}", "tags": list(tags), "notes": "",
            "state": state, "busy": "",
            "containers": {"android": {"name": f"sf-android-{instance_id}", "state": "running" if running else "exited",
                                       "status": ""},
                           "net": {"name": f"sf-net-{instance_id}", "state": "running" if running else "exited",
                                   "status": ""}},
            "adb_port": 5555 + instance_id, "serial": f"127.0.0.1:{5555 + instance_id}",
            "data_volume": f"sf-data-{instance_id}", "adopted": False, "autostart": False, "device_profile": "none",
            "uptime": None, "last_error": "", "last_start": None, "android_version": "13",
            "image": "superfarm/android:13", "screen": {"width": 480, "height": 854, "dpi": 160, "fps": 20},
            "gpu": {"requested": "auto", "mode": "intel" if running else "", "node": ""},
            "net": {"mode": "direct", "profile": "", "gateway": "", "proxy": "", "killswitch": True}}


Sleep = Callable[[float], None]


class Run:
    """Result of one `sf` invocation in a test."""

    def __init__(self, code: int, out: str, err: str):
        self.code, self.out, self.err = code, out, err

    @property
    def json(self) -> Any:
        return json.loads(self.out)


def make_home(tmp_path: Any, monkeypatch: Any, *, port: int | None = None, token: str | None = TOKEN) -> Any:
    """A throwaway farm home whose config points at `port`, with the API token in place."""
    from superfarm.paths import get_paths
    from superfarm.util.files import write_secret, write_yaml
    home = tmp_path / "farm"
    monkeypatch.setenv("SUPERFARM_HOME", str(home))
    # The developer's own shell may export these; a test must see the documented defaults.
    for name in ("NO_COLOR", "SUPERFARM_USER", "SUPERFARM_GROUP", "SUDO_USER", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
    paths = get_paths().ensure()
    if port is not None:
        write_yaml(paths.config, {"server": {"port": port}})
    if token is not None:
        write_secret(paths.secrets / "api-token", token + "\n")
    return paths


def run_sf(capsys: Any, *argv: str) -> Run:
    """Call the CLI's main() in-process and capture what it printed."""
    from superfarm.cli.main import main
    capsys.readouterr()
    try:
        code = main(list(argv))
    except SystemExit as e:          # argparse exits by itself on usage errors and --help
        code = e.code if isinstance(e.code, int) else 1
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


class Contract:
    """Checks what the CLI sent against the real API's own description (its OpenAPI document).

    The stub answers whatever a test tells it to, so by itself it cannot notice the CLI
    calling a route that does not exist or sending a field the API would reject. This does:
    route and method must exist, path choices and query names must be declared, and a JSON
    body must fit the route's request model (known fields only, required ones present,
    plain types right). No request is ever executed against the real application."""

    _TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}

    def __init__(self, spec: dict):
        import re
        self.schemas = spec.get("components", {}).get("schemas", {})
        self.exact = spec["paths"]
        self.patterns = []
        for template, item in spec["paths"].items():
            names = re.findall(r"\{([^}]+)\}", template)
            if names:
                escaped = re.escape(template)
                strict = re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "([^/]+)", escaped) + "$")
                # A `{name:path}` parameter (flow names) may hold slashes. OpenAPI does not say
                # which parameters are of that kind, so a template *ending* in a parameter gets
                # a second, greedy reading of that last one.
                loose = None
                if template.endswith("}"):
                    head = escaped[:escaped.rindex("\\{")]
                    loose = re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "([^/]+)", head) + "(.+)$")
                self.patterns.append((len(names), template, strict, loose, names, item))
        self.patterns.sort(key=lambda p: p[0])

    def _operation(self, method: str, path: str) -> tuple[str, dict, dict]:
        method = method.lower()
        if path in self.exact and method in self.exact[path]:
            return path, self.exact[path][method], {}
        for attempt in (2, 3):
            for entry in self.patterns:
                names, item = entry[4], entry[5]
                found = entry[attempt].match(path) if entry[attempt] is not None else None
                if found and method in item:
                    return entry[1], item[method], dict(zip(names, found.groups(), strict=True))
        raise AssertionError(f"the CLI called {method.upper()} {path}, which the API does not have")

    def _resolve(self, schema: dict) -> dict:
        if "$ref" in schema:
            return self.schemas[schema["$ref"].rsplit("/", 1)[-1]]
        for option in schema.get("anyOf", []):
            if "$ref" in option:
                return self._resolve(option)
        return schema

    def _fits(self, value: Any, schema: dict) -> bool:
        options = schema.get("anyOf") or [schema]
        for option in options:
            option = self._resolve(option) if "$ref" in option else option
            kind = option.get("type")
            if kind == "null" and value is None:
                return True
            if "enum" in option and value not in option["enum"]:
                continue
            expected = self._TYPES.get(kind)
            if expected is None or (isinstance(value, expected) and not (kind in ("integer", "number")
                                                                         and isinstance(value, bool))):
                return True
        return False

    def check(self, request: Recorded) -> None:
        template, operation, path_values = self._operation(request.method, request.path)
        where = f"{request.method} {template}"
        declared = {p["name"]: p for p in operation.get("parameters", [])}
        for name, value in path_values.items():
            choices = declared.get(name, {}).get("schema", {}).get("enum")
            assert not choices or value in choices, f"{where}: {name}={value!r} is not one of {choices}"
        for name in request.query:
            assert declared.get(name, {}).get("in") == "query", f"{where}: unknown query parameter {name!r}"
        if not request.headers.get("content-type", "").startswith("application/json") or not request.body:
            return
        body = request.json
        content = operation.get("requestBody", {}).get("content", {}).get("application/json")
        if content is None:
            assert body == {}, f"{where} takes no JSON body, the CLI sent {sorted(body)}"
            return
        schema = self._resolve(content["schema"])
        properties = schema.get("properties")
        if properties is None:
            return
        assert isinstance(body, dict), f"{where}: body must be an object"
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(body) - set(properties))
            assert not unknown, f"{where}: the API does not accept field(s) {unknown} (it has {sorted(properties)})"
        missing = sorted(set(schema.get("required", [])) - set(body))
        assert not missing, f"{where}: required field(s) {missing} were not sent"
        for name, value in body.items():
            if name in properties:
                assert self._fits(value, properties[name]), f"{where}: field {name!r} has the wrong type ({value!r})"


def api_contract(home: Any) -> Contract | None:
    """The contract of the real API, or None when the API package cannot be imported."""
    try:
        from superfarm.api.app import create_app
        from superfarm.farm import Farm
    except ImportError:
        return None
    farm = Farm(str(home))
    try:
        return Contract(create_app(farm).openapi())
    finally:
        farm.close()

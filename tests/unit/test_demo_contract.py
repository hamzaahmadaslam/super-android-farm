"""The public demo cannot drift from the panel or from the real API unnoticed.

The demo (demo/) serves the real panel files over a fake backend that runs in the browser
(demo/mock/). Three things keep the two in step, and all of them run in the local gate:

    generated files    demo/mock/generated/ is written from the real code by
                       demo/scripts/generate.py; stale files fail here
    routes             every path the panel calls must have an entry in the mock's route
                       table, with the method the panel uses
    answers            for the GET routes the panel reads, the mock's answer is compared with
                       the real application's answer to the same request (the real app runs on
                       the unit-test fakes): every field the real answer has must be in the
                       mock's, with the same kind of value

When this file fails after a change to the panel or the API, the message says what to add
and where. docs/DEMO.md ("Add a route") has the steps.

The real application publishes an OpenAPI schema, and the mock's answers are checked against
it too. For most routes that schema only says "a JSON object" (the routes return plain
dictionaries), which is why the comparison with real answers exists.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import time
from pathlib import Path

import pytest
import test_web_static as web
from api_fakes import api
from automation_fakes import FakeDevice, button, field, screen

from superfarm.core import backup as backup_mod

ROOT = Path(__file__).resolve().parents[2]
MOCK = ROOT / "demo" / "mock"
needs_node = web.needs_node
ADVICE = ("Add it with route(...) in the file under demo/mock/routes/ that fits (or a new file imported in "
          "demo/mock/index.js); docs/DEMO.md, section \"Add a route\", shows how.")


# ------------------------------------------------------------------- generated files

def _generator():
    spec = importlib.util.spec_from_file_location("demo_generate", ROOT / "demo" / "scripts" / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_files_match_the_code_they_come_from():
    """The settings schema, the defaults, the AI tool list and the bundled flows in the demo are
    copies of the real ones. They are committed (the demo is built without Python), so a
    change to the code must be followed by regenerating them."""
    generate = _generator()
    stale, expected = [], generate.build()
    for name, value in expected.items():
        path = generate.OUT / f"{name}.js"
        if not path.is_file() or path.read_text(encoding="utf-8") != generate.render(value):
            stale.append(path.relative_to(ROOT).as_posix())
    extra = sorted(p.name for p in generate.OUT.glob("*.js") if p.stem not in expected)
    assert stale == [], f"out of date: {stale}. Run: .venv/bin/python demo/scripts/generate.py"
    assert extra == [], f"files in demo/mock/generated that the generator does not write: {extra}"


# ---------------------------------------------------------------------------- routes

def _node_json(tmp_path: Path, body: str) -> dict:
    """Run a few lines of JavaScript against the mock (imported as `m`) and return what they print."""
    script = tmp_path / "mock.mjs"
    script.write_text(f"const m = await import({json.dumps((MOCK / 'index.js').as_uri())});\n{body}\n", encoding="utf-8")
    result = subprocess.run([web.node, str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


METHODS = {"get": "GET", "post": "POST", "postQuiet": "POST", "upload": "POST", "put": "PUT", "patch": "PATCH", "del": "DELETE",
           "mediaUrl": "GET", "streamInto": "GET", "openSocket": "WS"}


def panel_calls() -> list[tuple[str | None, str, str]]:
    """(method or None, path, file) for every '/api/...' the panel builds. The path is what
    test_web_static extracts; the method is read from the function the path is handed to."""
    out = []
    for path in web.JS:
        text = web.read(path)
        expanded = text
        for name, value in re.findall(r"const (\w+) = '(/api/[^']*)';", text):
            expanded = expanded.replace("`${" + name + "}", "`" + value)
        starts = list(re.finditer(r"""(['"`])(/api/)""", expanded))
        found = web.api_paths(text)
        assert len(starts) == len(found), f"{path.name}: the path extraction and this test disagree"
        for match, api_path in zip(starts, found, strict=True):
            before = expanded[max(0, match.start() - 60):match.start()]
            if re.search(r"const \w+ = $", before):
                continue                                   # a module's own prefix constant, not a call
            called = re.search(r"(\w+)\(\s*$", before)
            out.append((METHODS.get(called.group(1)) if called else None, api_path, path.name))
    return out


def _answers(route: dict, method: str | None, path: str) -> bool:
    if method is not None and route["method"] != method:
        return False
    want, have = route["pattern"].strip("/").split("/"), path.strip("/").split("/")
    if re.fullmatch(r"(\{\}){2,}", have[-1]):             # `${id}${rest}`: an id and then more of the path
        return len(want) >= len(have) and all(w.startswith("{") or w == h or h.startswith("{}") for w, h in zip(want, have, strict=False))
    if want[-1].endswith("*}"):                            # {name*} takes the rest of the path
        return len(have) >= len(want) and all(w.startswith("{") or w == h or h == "{}" for w, h in zip(want[:-1], have, strict=False))
    return len(want) == len(have) and all(w.startswith("{") or h == "{}" or w == h for w, h in zip(want, have, strict=True))


@needs_node
def test_every_route_the_panel_calls_is_answered_by_the_mock(tmp_path):
    routes = _node_json(tmp_path, "console.log(JSON.stringify(m.listRoutes()));")
    assert len(routes) > 120, "the mock's route table is suspiciously small"
    calls = panel_calls()
    assert len(calls) > 80, "suspiciously few calls were found in the panel"
    missing = sorted({f"{method or 'ANY'} {path}   (called in superfarm/web/js/.../{name})"
                      for method, path, name in calls if not any(_answers(r, method, path) for r in routes)})
    assert missing == [], ("the panel calls routes the demo's fake backend does not answer:\n  " + "\n  ".join(missing)
                           + f"\n{ADVICE}")


@needs_node
def test_the_mock_only_answers_routes_the_real_api_has(tmp_path):
    """A route invented in the mock would let the demo show something no farm can do."""
    routes = _node_json(tmp_path, "console.log(JSON.stringify(m.listRoutes()));")
    unknown = []
    for route in routes:
        shape = re.sub(r"\{\w+\*?\}", "{}", route["pattern"])
        if not web.in_contract(shape) and not shape.startswith("/api/automation/") and shape not in EXTRA_REAL_ROUTES:
            unknown.append(f"{route['method']} {route['pattern']}")
    assert unknown == [], f"mock routes that are not in the HTTP contract: {unknown}"


# Real routes the panel does not call (so they are not in the panel's contract list) that the
# mock answers anyway, to behave like the API where a page might reach them later.
EXTRA_REAL_ROUTES = {"/api/media/{}", "/api/backups/prune"}


# --------------------------------------------------------------------------- answers

PKG = "com.example.app"
FLOW = """description: open the search form
package: com.example.app
variables: {query: earbuds}
steps:
  - tap: {id: search}
  - type: {text: "{query}", into: {id: query}}
"""
# (name, the request to the real app, the same request to the mock). The ids differ because
# each side has its own data: the real app is filled by `real_answers`, the mock by its seed.
COMPARED = [
    ("health", "/api/health", "/api/health"),
    ("me", "/api/auth/me", "/api/auth/me"),
    ("system", "/api/system", "/api/system"),
    ("capacity", "/api/system/capacity", "/api/system/capacity"),
    ("doctor", "/api/system/doctor", "/api/system/doctor"),
    ("metrics", "/api/system/metrics", "/api/system/metrics"),
    ("gpus", "/api/system/gpus", "/api/system/gpus"),
    ("device-profiles", "/api/device-profiles", "/api/device-profiles"),
    ("config", "/api/config", "/api/config"),
    ("images", "/api/images", "/api/images"),
    ("tasks", "/api/tasks", "/api/tasks"),
    ("task", "/api/tasks/{task}", "/api/tasks/t1"),
    ("instances", "/api/instances", "/api/instances"),
    ("instance", "/api/instances/1", "/api/instances/1"),
    ("instance net", "/api/instances/1/net", "/api/instances/1/net"),
    ("instance logs", "/api/instances/1/logs", "/api/instances/1/logs"),
    ("instance apps", "/api/instances/1/apps", "/api/instances/1/apps"),
    ("instance logcat", "/api/instances/1/logcat", "/api/instances/1/logcat"),
    ("net profiles", "/api/net/profiles", "/api/net/profiles"),
    ("net credentials", "/api/net/credentials", "/api/net/credentials"),
    ("net gateways", "/api/net/gateways", "/api/net/gateways"),
    ("apps library", "/api/apps/library", "/api/apps/library"),
    ("apps matrix", "/api/apps/matrix?selector=all&detail=true", "/api/apps/matrix?selector=all&detail=true"),
    ("apps matrix plain", "/api/apps/matrix", "/api/apps/matrix"),
    ("windows", "/api/windows", "/api/windows"),
    ("camera", "/api/camera", "/api/camera"),
    ("camera sources", "/api/camera/sources", "/api/camera/sources"),
    ("listen", "/api/listen", "/api/listen"),
    ("datasets", "/api/automation/datasets", "/api/automation/datasets"),
    ("dataset", "/api/automation/datasets/search-flow", "/api/automation/datasets/new-note"),
    ("recording", "/api/automation/datasets/search-flow/recordings/{recording}", "/api/automation/datasets/new-note/recordings/r001-101512"),
    ("record", "/api/automation/record", "/api/automation/record"),
    ("programs", "/api/automation/programs", "/api/automation/programs"),
    ("program", "/api/automation/programs/search-flow", "/api/automation/programs/new-note"),
    ("flows", "/api/automation/flows", "/api/automation/flows"),
    ("flow", "/api/automation/flows/search", "/api/automation/flows/smoke-test"),
    ("example flow", "/api/automation/flows/examples/open-app", "/api/automation/flows/examples/open-app"),
    ("profiles", "/api/automation/profiles", "/api/automation/profiles"),
    ("values", "/api/automation/values", "/api/automation/values"),
    ("instance values", "/api/automation/values/instances/1", "/api/automation/values/instances/1"),
    ("pools", "/api/automation/values/pools", "/api/automation/values/pools"),
    ("pool", "/api/automation/values/pools/codes", "/api/automation/values/pools/invite-codes"),
    ("runs", "/api/automation/runs", "/api/automation/runs"),
    ("run", "/api/automation/runs/{run}", "/api/automation/runs/r0"),
    ("schedules", "/api/automation/schedules", "/api/automation/schedules"),
    ("screen", "/api/automation/screen/1", "/api/automation/screen/1"),
    ("element", "/api/automation/screen/1/element?x=100&y=50", "/api/automation/screen/1/element?x=100&y=120"),
    ("llm", "/api/automation/llm", "/api/automation/llm"),
    ("backups", "/api/backups", "/api/backups"),
    ("approvals", "/api/approvals", "/api/approvals"),
    ("approval", "/api/approvals/{approval}", "/api/approvals/3c1d9a70"),
    ("mcp info", "/api/mcp/info", "/api/mcp/info"),
    ("ai models", "/api/ai/models", "/api/ai/models"),
    ("ai conversations", "/api/ai/conversations", "/api/ai/conversations"),
    ("pilot sessions", "/api/ai/pilot/sessions", "/api/ai/pilot/sessions"),
]
# GET routes of the mock that are not compared, each with the reason. A new GET route must
# be added to COMPARED or here; the test below says so.
NOT_COMPARED = {
    "/api/config/schema": "an exact copy of the real schema (see the generated-files test)",
    "/api/shell/{}/output": "needs a finished shell task; the fakes have no shell to run one in",
    "/api/instances/{}/files": "the fake device of the unit tests has no file listing",
    "/api/instances/{}/info": "the fake device of the unit tests has no property dump",
    "/api/automation/record/{}": "the same shape as an entry of /api/automation/record",
    "/api/ai/conversations/{}": "the demo has no model, so no conversation exists to compare",
    "/api/ai/pilot/sessions/{}": "the demo has no model, so no pilot session exists to compare",
    "/api/ai/pilot/{}/summary": "the demo has no model, so no pilot recording exists to compare",
    "/api/backups/download": "a file, served in the demo as a blob by transport.js",
    "/api/instances/{}/frame.jpg": "a picture, drawn in the page by screen.js",
    "/api/instances/{}/stream.mjpg": "a picture stream, drawn in the page by screen.js",
    "/api/instances/{}/screenshot": "a picture, drawn in the page by screen.js",
    "/api/instances/{}/files/pull": "a file, served in the demo as a blob by transport.js",
}
# Parts of an answer that depend on the machine the test runs on (its GPUs, sensors and
# camera devices) or that exist only on a real host. They are left out of the comparison.
HOST_DEPENDENT = {
    "system": {"gpus", "host", "capacity.estimate", "home"},
    "capacity": {"estimate"},                 # the fakes return a stub; the demo's estimate is the real estimator's (generated/capacity.js)
    "metrics": {"gpus", "host.temps"},
    "gpus": {"gpus", "relay.relays", "relay.runtime.files", "relay.runtime.relay"},
    "camera": {"available.devices", "available.tools", "available.overlay", "available.reasons", "status.instances"},
    "images": {"overlays", "features.camera.reason", "features.nvidia.reason"},
    "windows": {"available"},
    "instance": {"net"},                      # the fake network backend answers with a stub
    "instance net": {"killswitch.problems"},
    "mcp info": {"sdk_version"},
}
# Dictionaries whose keys are data (an id, a name): one value of each side is compared.
KEYED = {"components", "profiles", "images", "gateways", "instances", "results", "devices", "packages", "variables", "counts", "props", "extra_props", "models",
         "values", "limits", "overlay", "matrix"}


# Values whose fields depend on what they describe (a task's output on its kind, a selector on
# the element, a tool's arguments on the tool): only their kind is compared.
FREE_FORM = {"output", "selector", "arguments"}


def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return {str: "string", list: "list", dict: "object"}[type(value)]


def differences(real, mock, path: str, skip: set[str], out: list[str]) -> list[str]:
    """What the real answer has that the mock's does not, and values of a different kind.
    A field the mock has in addition is fine: nothing reads it."""
    if path in skip or real is None or mock is None:
        return out                                           # an optional value: either side may leave it empty
    if _kind(real) != _kind(mock):
        out.append(f"{path or 'the answer'}: the real API has a {_kind(real)}, the mock a {_kind(mock)}")
    elif isinstance(real, dict):
        name = path.rsplit(".", 1)[-1]
        if name in FREE_FORM:
            return out
        if name in KEYED or (real and all(re.fullmatch(r"\d+", key) for key in real)):
            if real and mock:
                differences(next(iter(real.values())), next(iter(mock.values())), path, skip, out)
            return out
        for key, value in real.items():
            inner = f"{path}.{key}" if path else key
            if inner in skip:
                continue
            if key not in mock:
                out.append(f"{inner}: missing in the mock (the real API returns a {_kind(value)})")
            else:
                differences(value, mock[key], inner, skip, out)
    elif isinstance(real, list) and real and mock:
        # Entries that name themselves are paired by that name, so that like is compared with like.
        partner = mock[0]
        if isinstance(real[0], dict) and "id" in real[0]:
            partner = next((m for m in mock if isinstance(m, dict) and m.get("id") == real[0]["id"]), mock[0])
        differences(real[0], partner, path, skip, out)
    return out


def _fill_real(a) -> dict[str, str]:
    """Give the real application (on the unit-test fakes) one of everything the demo shows,
    through its own API, and return the ids that were made."""
    c, auto = a.client, "/api/automation"
    a.core.mgr.create(count=2, template={"tags": ["eu"]})

    def device(instance_id: int) -> FakeDevice:
        home = screen(button("search", "Search", (20, 20, 220, 80)))
        form = screen(field("query", "", (20, 20, 460, 80), focused=True))
        made = FakeDevice({"home": home, "form": form}, "home", taps={("home", "search"): "form"})
        made.serial = f"fake:{instance_id}"
        return made

    a.farm.automation.runner._device_factory = device
    ids = {"task": a.wait(c.post("/api/instances/1/start", json={}))["id"]}
    c.post("/api/net/profiles", files={"file": ("example.ovpn", b"client\ndev tun\nproto udp\nremote 198.51.100.10 1194\n")})
    c.put("/api/net/credentials/default", json={"username": "someone", "password": "not-a-secret"})
    assert c.post(f"{auto}/datasets", json={"name": "Search flow", "package": PKG}).status_code == 201
    assert c.post(f"{auto}/record/start", json={"dataset": "search-flow", "id": 1}).status_code == 200
    c.post(f"{auto}/record/1/tap", json={"x": 0.25, "y": 0.06})
    c.post(f"{auto}/record/1/text", json={"value": "earbuds"})
    assert c.post(f"{auto}/record/1/stop").status_code == 200
    ids["recording"] = c.get(f"{auto}/datasets/search-flow").json()["dataset"]["recordings_list"][0]["id"]
    a.wait(c.post(f"{auto}/datasets/search-flow/train"))
    ids["run"] = a.wait(c.post(f"{auto}/runs", json={"kind": "program", "name": "search-flow", "selector": "2"}), 30)["id"]
    assert c.put(f"{auto}/flows/search", json={"text": FLOW}).status_code == 200
    c.put(f"{auto}/values/shared/greeting", json={"value": "hello"})
    c.put(f"{auto}/values/instances/1/nickname", json={"value": "one"})
    c.post(f"{auto}/values/pools/codes", json={"lines": ["A1", "B2"]})
    assert c.post(f"{auto}/schedules", json={"name": "nightly", "kind": "flow", "target": "search", "selector": "all", "cron": "0 4 * * *"}).status_code == 201
    folder = a.farm.backups.directory / "android-1" / "20250101-040000"
    folder.mkdir(parents=True)
    (folder / "data.tar.gz").write_bytes(b"tar-bytes")
    (folder / backup_mod.MANIFEST).write_text(json.dumps({
        "kind": backup_mod.MANIFEST_KIND, "format": backup_mod.MANIFEST_FORMAT, "id": 1, "name": "Android 1", "created": time.time(), "reason": "manual",
        "data": "data.tar.gz", "includes_secrets": False, "android_version": "13",
        "files": {"data.tar.gz": {"size": 9, "sha256": hashlib.sha256(b"tar-bytes").hexdigest()}}}))
    asked = c.post("/api/approvals", json={"tool": "restart_androids", "arguments": {"selector": "1"}, "caller": "mcp:test", "session": "session-1"})
    assert asked.status_code == 201, asked.text
    ids["approval"] = asked.json()["approval"]["id"]
    return ids


@pytest.fixture(scope="module")
def answers(tmp_path_factory):
    """{name: (real status, real body, mock status, mock body)} for every entry of COMPARED."""
    if web.node is None:
        pytest.skip("needs node 22.7 or newer")
    tmp = tmp_path_factory.mktemp("demo")
    fast = {"settle_seconds": 0, "human_jitter": False, "step_timeout": 1, "run_timeout": 30, "read_timeout": 5}
    real = {}
    with api(tmp / "farm", {"automation": fast}) as a:
        ids = _fill_real(a)
        for name, url, _ in COMPARED:
            response = a.client.get(url.format(**ids))
            real[name] = (response.status_code, response.json())
        schema = a.app.openapi()
    urls = json.dumps({name: mock_url for name, _, mock_url in COMPARED})
    mock = _node_json(tmp, f"""
m.handle('POST', '/api/auth/login', {{}});
const out = {{}};
for (const [name, url] of Object.entries({urls})) out[name] = m.handle('GET', url);
console.log(JSON.stringify(out));""")
    return {name: (*real[name], mock[name]["status"], mock[name]["body"]) for name, _, _ in COMPARED}, schema


@pytest.mark.parametrize("name", [entry[0] for entry in COMPARED])
def test_the_mock_answers_like_the_real_api(answers, name):
    real_status, real, mock_status, mock = answers[0][name]
    assert real_status == 200, f"the real application did not answer this request: {real_status} {real}"
    assert mock_status == 200, f"the mock did not answer this request: {mock_status} {mock}"
    found = differences(real, mock, "", HOST_DEPENDENT.get(name, set()), [])
    assert found == [], (f"the demo's answer to {dict((n, m) for n, _, m in COMPARED)[name]} no longer has the shape of the real API's:\n  "
                         + "\n  ".join(found) + "\nChange the route in demo/mock/routes/ (or the seed in demo/mock/seed.js) to match.")


def _valid(value, rule: dict, defs: dict) -> bool:
    """The small part of JSON Schema the application's OpenAPI document uses for answers."""
    if "$ref" in rule:
        return _valid(value, defs[rule["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in rule:
        return any(_valid(value, option, defs) for option in rule["anyOf"])
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float), "null": type(None)}
    if "type" in rule and not isinstance(value, kinds[rule["type"]]):
        return False
    if isinstance(value, dict):
        if any(key not in value for key in rule.get("required", [])):
            return False
        return all(_valid(value[key], inner, defs) for key, inner in rule.get("properties", {}).items() if key in value)
    if isinstance(value, list) and "items" in rule:
        return all(_valid(item, rule["items"], defs) for item in value)
    return True


def test_the_mock_answers_satisfy_the_openapi_schema(answers):
    by_name, schema = answers
    defs = schema.get("components", {}).get("schemas", {})
    checked = 0
    for name, real_url, _ in COMPARED:
        path = re.sub(r"\{(task|run|approval|recording)\}", "{}", real_url.split("?")[0])
        for template, item in schema["paths"].items():
            want, have = template.strip("/").split("/"), path.strip("/").split("/")
            if len(want) != len(have) or not all(w.startswith("{") or w == h for w, h in zip(want, have, strict=True)):
                continue
            rule = item.get("get", {}).get("responses", {}).get("200", {}).get("content", {}).get("application/json", {}).get("schema")
            if rule is None:
                continue
            checked += 1
            assert _valid(by_name[name][3], rule, defs), f"the mock's answer to {name} does not satisfy the OpenAPI schema of GET {template}"
            break
    assert checked >= 40, f"only {checked} answers had an OpenAPI schema to check against"


@needs_node
def test_every_get_route_of_the_mock_is_compared_or_explained(tmp_path):
    routes = _node_json(tmp_path, "console.log(JSON.stringify(m.listRoutes()));")
    compared = {mock_url.split("?")[0] for _, _, mock_url in COMPARED}
    loose = []
    for route in routes:
        if route["method"] != "GET":
            continue
        shape = re.sub(r"\{\w+\*?\}", "{}", route["pattern"])
        want = route["pattern"].strip("/").split("/")
        hit = any(len(want) <= len(url.strip("/").split("/")) and all(w.startswith("{") or w == h for w, h in zip(want, url.strip("/").split("/"), strict=False))
                  and (len(want) == len(url.strip("/").split("/")) or want[-1].endswith("*}")) for url in compared)
        if not hit and shape not in NOT_COMPARED:
            loose.append(route["pattern"])
    assert loose == [], (f"GET routes of the mock whose answers are not compared with the real API: {loose}. Add each to COMPARED in this file "
                         "(with the matching request to the real application), or to NOT_COMPARED with the reason.")
    stale = sorted(shape for shape in NOT_COMPARED if not any(re.sub(r"\{\w+\*?\}", "{}", r["pattern"]) == shape for r in routes))
    assert stale == [], f"NOT_COMPARED lists routes the mock no longer has: {stale}"

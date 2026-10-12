"""The application as a whole: the panel's files, the route table against the spec, strict
request bodies, start-up and shut-down."""
from __future__ import annotations

import asyncio
import re
import threading

import httpx
import pytest
from api_fakes import BASE, Api, api
from starlette.testclient import TestClient

from superfarm.api.app import create_app

# Every route of the API contract (spec, Addendum 5).
CONTRACT = """
GET /api/health
POST /api/auth/login
POST /api/auth/logout
GET /api/auth/me
GET /api/system
GET /api/system/capacity
GET /api/system/doctor
GET /api/system/metrics
GET /api/system/gpus
GET /api/config
PATCH /api/config
GET /api/config/schema
GET /api/images
POST /api/images/{version}/ensure
POST /api/images/{version}/build
GET /api/instances
POST /api/instances
GET /api/instances/{id}
PATCH /api/instances/{id}
DELETE /api/instances/{id}
POST /api/instances/{id}/{action}
POST /api/instances/{id}/clone
POST /api/instances/bulk
GET /api/instances/{id}/logs
GET /api/device-profiles
GET /api/tasks
GET /api/tasks/{task_id}
POST /api/tasks/{task_id}/cancel
GET /api/net/profiles
POST /api/net/profiles
DELETE /api/net/profiles/{name}
GET /api/net/credentials
PUT /api/net/credentials/{name}
DELETE /api/net/credentials/{name}
GET /api/net/gateways
PUT /api/net/gateways/{name}
DELETE /api/net/gateways/{name}
POST /api/net/gateways/{name}/{action}
GET /api/instances/{id}/net
GET /api/apps/library
POST /api/apps/library
DELETE /api/apps/library/{app_id}
POST /api/apps/install
POST /api/apps/ensure
POST /api/apps/uninstall
POST /api/apps/launch
POST /api/apps/stop
POST /api/apps/clear
POST /api/apps/enable
POST /api/apps/disable
POST /api/apps/grant
POST /api/apps/revoke
GET /api/apps/matrix
GET /api/instances/{id}/apps
POST /api/files/push
POST /api/files/gallery
GET /api/instances/{id}/files
GET /api/instances/{id}/files/pull
POST /api/device/locale
POST /api/device/timezone
POST /api/device/geo
POST /api/device/proxy
POST /api/device/clipboard
POST /api/device/animations
POST /api/device/dark-mode
POST /api/device/font-scale
POST /api/device/open-url
POST /api/device/wake
POST /api/device/reboot
GET /api/instances/{id}/info
GET /api/instances/{id}/screenshot
GET /api/instances/{id}/logcat
POST /api/shell
GET /api/shell/{task_id}/output
GET /api/instances/{id}/frame.jpg
GET /api/instances/{id}/stream.mjpg
POST /api/input/tap
POST /api/input/swipe
POST /api/input/long-press
POST /api/input/text
POST /api/input/key
POST /api/windows/{action}
POST /api/windows/close-all
GET /api/windows
GET /api/camera
POST /api/camera/play
POST /api/camera/stop
POST /api/camera/mic
POST /api/camera/attach
POST /api/camera/install-hal
GET /api/camera/sources
POST /api/media
GET /api/listen
POST /api/listen/{id}
DELETE /api/listen
GET /api/backups
POST /api/backups
POST /api/backups/farm
POST /api/backups/verify
POST /api/backups/restore
DELETE /api/backups
GET /api/backups/download
GET /api/automation/datasets
GET /api/automation/datasets/{slug}/recordings/{rid}
GET /api/automation/datasets/{slug}/recordings/{rid}/shots/{relative}
POST /api/automation/record/start
POST /api/automation/record/{id}/tap
POST /api/automation/record/{id}/swipe
POST /api/automation/record/{id}/text
POST /api/automation/record/{id}/key
POST /api/automation/record/{id}/launch
POST /api/automation/record/{id}/wait
POST /api/automation/record/{id}/stop
POST /api/automation/datasets/{slug}/train
GET /api/automation/programs
GET /api/automation/flows
PUT /api/automation/flows/{name}
DELETE /api/automation/flows/{name}
POST /api/automation/flows/validate
GET /api/automation/profiles
PUT /api/automation/profiles/{package}/roles/{role}
POST /api/automation/profiles/{package}/overlays
POST /api/automation/profiles/{package}/teach
GET /api/automation/values
PUT /api/automation/values/shared/{name}
PUT /api/automation/values/instances/{id}/{name}
POST /api/automation/values/pools/{name}
POST /api/automation/runs
GET /api/automation/runs/{task_id}
POST /api/automation/runs/{task_id}/cancel
POST /api/automation/runs/{task_id}/resolve
GET /api/automation/schedules
POST /api/automation/schedules
PATCH /api/automation/schedules/{schedule_id}
DELETE /api/automation/schedules/{schedule_id}
GET /api/automation/screen/{id}
GET /api/automation/screen/{id}/element
GET /api/automation/llm
"""


@pytest.fixture
def a(tmp_path):
    with api(tmp_path) as made:
        yield made


@pytest.fixture
def schema(a):
    r = a.client.get("/api/openapi.json")
    assert r.status_code == 200
    return r.json()


def test_every_route_of_the_contract_exists(schema):
    have = {(method.upper(), path) for path, item in schema["paths"].items() for method in item}
    want = {tuple(line.split()) for line in CONTRACT.strip().splitlines()}
    assert sorted(want - have) == []


def test_every_public_method_of_the_automation_facade_has_a_route(a):
    from superfarm.api.routes import automation as routes
    from superfarm.automation import Automation
    source = open(routes.__file__, encoding="utf-8").read()
    facade = [n for n, v in vars(Automation).items() if callable(v) and not n.startswith("_")
              and n not in ("reload_config", "device", "tick")]       # internal wiring, not operations
    assert len(facade) > 30
    assert [n for n in facade if f".{n}(" not in source] == []


def test_websocket_routes_exist(a):
    paths = {getattr(r, "path", "") for r in __import__("superfarm.api.ws", fromlist=["router"]).router.routes}
    assert paths == {"/api/events", "/api/instances/{id}/h264"}


def test_every_request_body_refuses_unknown_fields(schema):
    bodies = set()
    for item in schema["paths"].values():
        for operation in item.values():
            content = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
            ref = content.get("schema", {})
            for candidate in [ref, *ref.get("anyOf", [])]:
                if "$ref" in candidate:
                    bodies.add(candidate["$ref"].rsplit("/", 1)[1])
    assert len(bodies) > 40
    loose = [name for name in sorted(bodies) if schema["components"]["schemas"][name].get("additionalProperties") is not False]
    assert loose == []


def test_the_schema_itself_needs_authentication_and_no_docs_page_is_served(a):
    assert a.anonymous.get("/api/openapi.json").status_code == 401
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert a.anonymous.get(path).status_code == 404


def test_everything_except_health_and_login_needs_authentication(a, schema):
    open_routes = {("GET", "/api/health"), ("POST", "/api/auth/login"), ("POST", "/api/auth/logout")}
    for path, item in schema["paths"].items():
        for method in item:
            if (method.upper(), path) in open_routes:
                continue
            url = re.sub(r"\{[a-z_]+\}", "1", path)
            r = a.anonymous.request(method.upper(), url)
            assert r.status_code == 401, (method, path, r.status_code)


# -------------------------------------------------------------------------------- panel

@pytest.fixture
def panel(tmp_path):
    web = tmp_path / "web"
    (web / "js").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>Panel</title><script type=module src=js/app.js></script>")
    (web / "js" / "app.js").write_text("console.log('panel')")
    (tmp_path / "outside.txt").write_text("not part of the panel")
    a = Api(tmp_path, web_dir=web)
    yield a
    a.close()


def test_the_panel_is_served_from_the_root_without_authentication(panel):
    index = panel.anonymous.get("/")
    assert index.status_code == 200 and "<title>Panel</title>" in index.text
    assert index.headers["content-type"].startswith("text/html")
    assert "default-src 'self'" in index.headers["content-security-policy"]
    script = panel.anonymous.get("/js/app.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]


@pytest.mark.parametrize("path", ["/../outside.txt", "/..%2foutside.txt", "/js/..%2f..%2foutside.txt", "/%2e%2e/outside.txt",
                                  "/js/../../home/secrets/api-token", "/..%2fhome%2fsecrets%2fapi-token"])
def test_the_panel_serves_nothing_outside_its_folder(panel, path):
    r = panel.anonymous.get(path)
    assert r.status_code in (400, 404), (path, r.status_code)
    assert "not part of the panel" not in r.text and panel.token not in r.text


def test_unknown_api_paths_are_json_404s_even_with_a_panel(panel):
    r = panel.client.get("/api/does-not-exist")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = panel.client.put("/api/health")
    assert r.status_code == 405 and r.headers["allow"] == "GET" and r.json()["error"]["code"] == "method_not_allowed"
    assert panel.anonymous.post("/js/app.js").status_code == 405


def test_without_a_panel_the_root_says_so(a):
    r = a.anonymous.get("/")
    assert r.status_code == 200 and "panel is not installed" in r.text
    assert a.anonymous.get("/index.html").status_code == 404


def test_the_shipped_panel_is_used_when_present(a):
    from superfarm.api import app as app_mod
    app = create_app(a.farm)
    client = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    r = client.get("/")
    assert r.status_code == 200
    assert ("panel is not installed" in r.text) != (app_mod.WEB_DIR / "index.html").is_file()
    app.state.audit.close()


# ------------------------------------------------------------------- start-up, shut-down

def test_start_sweeps_stale_uploads_and_stop_closes_live_sessions(a):
    stale = a.farm.paths.run / "uploads" / "up-left-behind"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    closed: list[str] = []

    class Streams:
        async def close_all(self) -> None:
            closed.append("h264")

    a.farm.use("h264", Streams())
    with TestClient(a.app, base_url=BASE) as running:
        assert not stale.exists()
        assert running.get("/api/health").status_code == 200
        assert closed == []
    assert closed == ["h264"]


def test_shutdown_does_not_build_what_was_never_used(a, monkeypatch):
    from superfarm.farm import Farm
    monkeypatch.setitem(Farm._FACTORIES, "h264", lambda self: pytest.fail("built at shutdown"))
    with TestClient(a.app, base_url=BASE) as running:
        running.get("/api/health")


async def test_blocking_farm_calls_do_not_stall_the_event_loop(a):
    """A slow `docker ps` behind one request must not delay another request."""
    release = threading.Event()
    real = a.core.mgr.list_status
    a.core.mgr.list_status = lambda: (release.wait(5), real())[1]
    transport = httpx.ASGITransport(app=a.app)
    headers = {"Authorization": f"Bearer {a.token}"}
    async with httpx.AsyncClient(transport=transport, base_url=BASE, headers=headers) as client:
        slow = asyncio.create_task(client.get("/api/instances"))
        await asyncio.sleep(0.05)
        began = asyncio.get_running_loop().time()
        quick = await asyncio.wait_for(client.get("/api/health"), 2)
        assert quick.status_code == 200 and asyncio.get_running_loop().time() - began < 1
        assert not slow.done()
        release.set()
        assert (await asyncio.wait_for(slow, 5)).status_code == 200


# -------------------------------------------------------------------------- body limits

def test_an_oversized_json_body_is_refused_before_it_is_read_or_authenticated(a):
    from superfarm.api.app import MAX_JSON_BODY
    huge = b'{"count": 1, "name": "' + b"x" * MAX_JSON_BODY + b'"}'
    r = a.anonymous.post("/api/instances", content=huge, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"       # not 401: nothing was buffered

    def chunks():                                    # the same body with no Content-Length to go by
        for start in range(0, len(huge), 256 * 1024):
            yield huge[start:start + 256 * 1024]

    r = a.client.post("/api/instances", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.headers["x-content-type-options"] == "nosniff"
    assert a.farm.store.ids() == []
    ok = b'{"count": 1, "name": "' + b"x" * 40 + b'"}'
    assert a.client.post("/api/instances", content=ok, headers={"content-type": "application/json"}).status_code == 201


def test_only_the_upload_routes_take_large_bodies(schema):
    from superfarm.api.app import UPLOAD_PATHS
    without_json_body = {path for path, item in schema["paths"].items() if "post" in item
                         and "requestBody" not in item["post"] and not path.endswith("}")}
    assert UPLOAD_PATHS <= without_json_body         # each is a real POST route that reads its body as a stream

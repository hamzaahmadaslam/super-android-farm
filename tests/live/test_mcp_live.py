"""Live test of the MCP server: the real daemon, `sf mcp` as a real subprocess, the SDK's own
client on the other end of the pipe.

    protocol   initialize, list tools / resources / prompts, read-only tools, a resource and a
               prompt; a changing tool under the `ask` policy approved through the API from a
               second thread (the call returns the real result); one denied (the model-facing
               message); one that nobody answers (expires); the same server over Streamable
               HTTP with the bearer token; audit lines that name the client
    android    ONE real Android: create -> start (approved) -> screenshot is a real picture ->
               read_screen lists elements -> tap and type_text change the screen -> a bundled
               example flow runs -> delete (approved)

Run:  SF_LIVE=1 .venv/bin/python -m pytest -o addopts="" tests/live/test_mcp_live.py -v
Needs the MCP SDK (`pip install "superfarm[mcp]"`), Docker, adb, the binder module, the
sidecar image and a local Android image (SF_LIVE_IMAGE, default redroid:13-noflag). The
Android has no network at all (mode `none`). Everything created carries the prefix `sftmcp`
and is removed again, also on failure.
"""
from __future__ import annotations

import base64
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from superfarm.core.docker import Docker

pytestmark = pytest.mark.live

pytest.importorskip("mcp", reason="the optional MCP SDK is not installed (pip install 'superfarm[mcp]')")

import anyio  # noqa: E402
import mcp_types as types  # noqa: E402
from mcp import Client, StdioServerParameters, stdio_client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: E402

PREFIX = "sftmcp"
PORT = 18310
MCP_HTTP_PORT = 18311
ADB_BASE = 18400
BASE = f"http://127.0.0.1:{PORT}"
IMAGE = os.environ.get("SF_LIVE_IMAGE", "redroid:13-noflag")
SETTINGS = "com.android.settings"
CLIENT = types.Implementation(name="Live Test Client", version="1.0")
CALLER = "mcp:Live_Test_Client"


def config(timeout: int) -> str:
    return f"""\
server: {{port: {PORT}}}
docker: {{name_prefix: {PREFIX}}}
gpu: {{mode: software}}
network: {{mode: none, adb_port_base: {ADB_BASE}}}
android:
  images:
    "13": {{base: "{IMAGE}", tag: "{IMAGE}"}}
  extra_boot_args: ["ro.farm.redroid_fps=20"]
backup: {{auto_before_risky: false}}
performance: {{trim_packages: false}}
ai: {{approval_timeout_seconds: {timeout}}}
"""
# trim_packages is off because the default trim list disables com.android.settings.intelligence,
# the app behind the Settings search bar this test uses.


def cleanup(docker: Docker) -> None:
    """Remove what this test can have created, selected by its own prefix, and nothing else."""
    names = [c["name"] for c in docker.ps(all=True, labels={"io.superfarm.farm": PREFIX})
             if c["name"].startswith(f"{PREFIX}-")]
    if names:
        docker.rm(*names)
    for volume in docker.volumes(f"{PREFIX}-"):
        docker.volume_rm(volume)


class Farm:
    """The daemon process and an HTTP client that plays the operator."""

    def __init__(self, home: Path):
        self.home = home
        self.process: subprocess.Popen | None = None
        self.api: httpx.Client | None = None

    def start(self) -> None:
        env = {**os.environ, "SUPERFARM_HOME": str(self.home)}
        code = "import sys; from superfarm.api.server import serve; sys.exit(serve())"
        self.process = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"the daemon exited at once with code {self.process.returncode}; see "
                                     f"{self.home}/logs/daemon.log")
            try:
                if httpx.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.2)
        else:
            raise AssertionError("the daemon did not answer within 30 s")
        self.token = (self.home / "secrets" / "api-token").read_text().strip()
        self.api = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {self.token}"})

    def stop(self) -> None:
        if self.api is not None:
            self.api.close()
        if self.process is not None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(10)
            self.process = None

    def pending(self) -> list[dict]:
        return self.api.get("/api/approvals", params={"state": "pending"}).json()["approvals"]

    def answer(self, action: str, seen: list[dict], *, timeout: float = 60, **body) -> threading.Thread:
        """In a second thread, wait for the next approval request and answer it through the API."""
        def work() -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                waiting = self.pending()
                if waiting:
                    seen.append(waiting[-1])
                    r = self.api.post(f"/api/approvals/{waiting[-1]['id']}/{action}", json=body)
                    seen.append(r.json()["approval"])
                    return
                time.sleep(0.1)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread

    def audit(self) -> list[dict]:
        path = self.home / "logs" / "audit.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Operator:
    """Approves every request as it arrives, and keeps what it approved."""

    def __init__(self, farm: Farm):
        self.farm, self.approved, self._stop = farm, [], threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            for item in self.farm.pending():
                self.farm.api.post(f"/api/approvals/{item['id']}/approve", json={"via": "terminal"})
                self.approved.append(item)
            time.sleep(0.1)

    def __enter__(self) -> Operator:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(10)


def make_farm(tmp_path, *, timeout: int, need_image: bool):
    docker = Docker()
    ok, why = docker.available()
    if not ok:
        pytest.skip(f"Docker is not usable: {why}")
    if need_image and not docker.image_exists(IMAGE):
        pytest.skip(f"the Android image {IMAGE} is not on this machine (set SF_LIVE_IMAGE)")
    cleanup(docker)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(config(timeout))
    farm = Farm(home)
    farm.docker = docker
    return farm


@pytest.fixture
def farm(tmp_path, request):
    made = make_farm(tmp_path, timeout=getattr(request, "param", {}).get("timeout", 300),
                     need_image=getattr(request, "param", {}).get("image", False))
    made.start()
    try:
        yield made
    finally:
        made.stop()
        cleanup(made.docker)


def stdio(farm: Farm, mode: str = "legacy"):
    """A client connected to `sf mcp` running as a subprocess for this farm home."""
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "superfarm.cli.main", "--home", str(farm.home), "mcp"],
                                   env={"PYTHONPATH": str(Path(__file__).resolve().parents[2])}, cwd=str(farm.home))
    errlog = open(farm.home / "logs" / "mcp-stderr.log", "a")          # noqa: SIM115 - lives as long as the client
    return Client(stdio_client(params, errlog=errlog), mode=mode, client_info=CLIENT, cache=None)


def text(result) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


async def call(client: Client, name: str, arguments: dict | None = None):
    return await client.call_tool(name, arguments or {})


async def finished(client: Client, result, timeout: float = 420):
    """Follow a task-returning tool result with wait_for_task until the task has ended."""
    deadline = time.monotonic() + timeout
    while not result.structured_content["task"]["finished"]:
        assert time.monotonic() < deadline, f"task did not finish: {text(result)}"
        result = await call(client, "wait_for_task", {"task_id": result.structured_content["task"]["id"], "timeout_seconds": 60})
    return result


# ------------------------------------------------------------------------------ protocol

@pytest.mark.parametrize("farm", [{"timeout": 6}], indirect=True)
@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_protocol_and_the_operators_yes_or_no(farm, mode):
    async def main() -> None:
        async with stdio(farm, mode) as client:
            # ---- handshake and listings
            assert client.protocol_version == ("2025-11-25" if mode == "legacy" else "2026-07-28")
            assert "DEVICE-CONTENT" in client.instructions
            if mode == "legacy":
                assert client.server_info.name == "superfarm"
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert len(tools) == 36 and "run_shell" not in tools             # denied by default
            assert tools["list_androids"].annotations.read_only_hint is True
            assert tools["delete_androids"].annotations.destructive_hint is True
            assert tools["tap"].input_schema["required"] == ["selector", "x", "y"]
            assert [r.uri for r in (await client.list_resources()).resources] == ["farm://status", "farm://androids"]
            assert [t.uri_template for t in (await client.list_resource_templates()).resource_templates] == ["farm://android/{id}"]
            assert [p.name for p in (await client.list_prompts()).prompts] == ["check_farm_health", "roll_out_app", "diagnose_boot"]

            # ---- read-only tools run at once, with no request for the operator
            status = await call(client, "farm_status")
            assert status.is_error is False and status.structured_content["androids"] == {"total": 0, "by_state": {}}
            assert status.structured_content["capacity"]["estimated_max"] >= 1
            assert "Capacity:" in text(status)
            doctor = await call(client, "system_doctor")
            assert doctor.is_error is False and len(doctor.structured_content["checks"]) > 5
            flows = await call(client, "list_flows")
            assert "examples/settings-open-entry" in [f["name"] for f in flows.structured_content["flows"]]
            assert (await call(client, "list_backups")).structured_content["count"] == 0
            assert (await call(client, "list_tasks")).is_error is False
            assert farm.pending() == [] and farm.api.get("/api/approvals").json()["approvals"] == []
            resource = await client.read_resource("farm://status")
            assert json.loads(resource.contents[0].text)["androids"]["total"] == 0
            prompt = await client.get_prompt("diagnose_boot", {"id": "1"})
            assert "get_android" in prompt.messages[0].content.text

            # ---- a changing tool waits; approved from a second thread, it returns the real result
            seen: list[dict] = []
            thread = farm.answer("approve", seen)
            began = time.monotonic()
            created = await call(client, "create_androids", {"count": 2, "name": "live", "tags": ["mcp"]})
            thread.join(10)
            assert created.is_error is False and created.structured_content == {"ok": True, "created": [1, 2]}
            asked, decided = seen
            assert asked["summary"] == 'Create 2 new Androids (name="live", tags=["mcp"])'
            assert (asked["caller"], asked["tool"], asked["klass"], asked["state"]) == (CALLER, "create_androids", "change", "pending")
            assert decided["state"] == "approved" and decided["decided_by"].startswith("api (")
            assert time.monotonic() - began < 30
            rows = farm.api.get("/api/instances").json()["instances"]
            assert [(r["id"], r["name"], r["tags"]) for r in rows] == [(1, "live 1", ["mcp"]), (2, "live 2", ["mcp"])]
            listed = await call(client, "list_androids", {"tag": "mcp"})
            assert [a["id"] for a in listed.structured_content["androids"]] == [1, 2]

            # ---- denied: the model is told so, with the operator's reason, and nothing changed
            seen = []
            thread = farm.answer("deny", seen, reason="keep the name")
            denied = await call(client, "set_android", {"id": 1, "name": "renamed", "fps": 30})
            thread.join(10)
            assert denied.is_error is True and denied.structured_content["status"] == "declined"
            assert text(denied) == ('The operator declined this action: "keep the name". Nothing was done. '
                                    "Do not retry unless the operator asks for it.")
            assert seen[0]["summary"] == 'Change settings of Android 1: fps=30, name="renamed"'
            assert farm.api.get("/api/instances/1").json()["name"] == "live 1"

            # ---- nobody answers: the request expires after ai.approval_timeout_seconds (6 here)
            began = time.monotonic()
            ignored = await call(client, "delete_androids", {"selector": "all"})
            waited = time.monotonic() - began
            assert ignored.is_error is True and ignored.structured_content["status"] == "no_answer"
            assert text(ignored).startswith("The operator did not answer within 6 seconds, so nothing was done.")
            assert 5 <= waited < 20, waited
            expired = farm.api.get(f"/api/approvals/{ignored.structured_content['approval_id']}").json()["approval"]
            assert (expired["state"], expired["decided_by"], expired["klass"]) == ("expired", "timeout", "destructive")
            assert expired["summary"] == "Delete 2 Androids and their data: Android 1, 2"
            assert farm.api.post(f"/api/approvals/{expired['id']}/approve", json={}).status_code == 409      # too late
            assert len(farm.api.get("/api/instances").json()["instances"]) == 2

            # ---- what the policy denies is refused at once, and invalid arguments never reach the operator
            shell = await call(client, "run_shell", {"selector": "all", "argv": ["id"]})
            assert shell.is_error and "switched off for AI assistants" in text(shell)
            invalid = await call(client, "tap", {"selector": "all", "x": 3, "y": 0})
            assert invalid.is_error and text(invalid) == "Invalid arguments for tap: x: must be at most 1"
            nothing = await call(client, "stop_androids", {"selector": "tag:nobody"})
            assert nothing.is_error and "matches no Android" in text(nothing)
            assert farm.pending() == []

            # ---- approve and remember: the second call of the same tool on the same Android does not ask
            seen = []
            thread = farm.answer("approve", seen, remember=True)
            first = await call(client, "set_android", {"id": 2, "fps": 25})
            thread.join(10)
            assert first.is_error is False and seen[1]["remembered"] is True
            second = await call(client, "set_android", {"id": 2, "fps": 30})
            assert second.is_error is False and farm.pending() == []
            assert farm.api.get("/api/instances/2").json()["spec"]["fps"] == 30

    anyio.run(main)

    # ---- the session ended with the client: nothing is remembered for it any more
    states = [a["state"] for a in farm.api.get("/api/approvals").json()["approvals"]]
    assert sorted(states) == ["approved", "approved", "denied", "expired"]
    lines = farm.audit()
    asked = [line for line in lines if line["path"] == "/api/approvals"]
    assert asked and all(line["caller"] == CALLER and line["name"] == CALLER for line in asked)
    acted = [line for line in lines if line["path"] == "/api/instances" and line["method"] == "POST"]
    assert [line["caller"] for line in acted] == [CALLER] and acted[0]["ids"] == [1, 2]
    assert any(line["path"].startswith("/api/approvals/sessions/mcp-") and line["method"] == "DELETE" for line in lines)
    assert farm.token not in (farm.home / "logs" / "audit.log").read_text()
    assert farm.token not in (farm.home / "logs" / "approvals.jsonl").read_text()
    assert (farm.home / "logs" / "mcp-stderr.log").read_text() == ""

    # ---- a daemon restart approves nothing: a request still waiting is expired by it
    waiting = farm.api.post("/api/approvals", json={"tool": "stop_androids", "arguments": {"selector": "all"},
                                                    "caller": "mcp:left-waiting", "session": "restart-test"}).json()["approval"]
    assert waiting["state"] == "pending"
    farm.stop()
    farm.start()
    after = farm.api.get(f"/api/approvals/{waiting['id']}").json()["approval"]
    assert (after["state"], after["decided_by"]) == ("expired", "daemon restart")
    for i in (1, 2):
        assert farm.api.delete(f"/api/instances/{i}").status_code == 200


def test_streamable_http_with_the_bearer_token(farm):
    env = {**os.environ, "SUPERFARM_HOME": str(farm.home)}
    server = subprocess.Popen([sys.executable, "-m", "superfarm.cli.main", "mcp", "--http", "--port", str(MCP_HTTP_PORT)],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{MCP_HTTP_PORT}/mcp"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", MCP_HTTP_PORT)) == 0:
                    break
            assert server.poll() is None, "sf mcp --http exited at once"
            time.sleep(0.2)
        # Loopback only: the listening socket is bound to 127.0.0.1, not to every interface.
        listening = subprocess.run(["ss", "-ltnH", f"sport = :{MCP_HTTP_PORT}"], capture_output=True, text=True).stdout
        assert [line.split()[3] for line in listening.splitlines()] == [f"127.0.0.1:{MCP_HTTP_PORT}"], listening
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}}
        accept = {"Accept": "application/json, text/event-stream"}
        assert httpx.post(url, json=body, headers=accept).status_code == 401
        assert httpx.post(url, json=body, headers={**accept, "Authorization": "Bearer wrong"}).status_code == 401
        assert httpx.post(url, json=body, headers={**accept, "Authorization": f"Bearer {farm.token}",
                                                  "Host": "evil.example"}).status_code in (400, 403, 421)

        async def main() -> None:
            async with create_mcp_http_client(headers={"Authorization": f"Bearer {farm.token}"}) as http, \
                    Client(streamable_http_client(url, http_client=http), mode="legacy", client_info=CLIENT, cache=None) as client:
                assert client.server_info.name == "superfarm"
                assert len((await client.list_tools()).tools) == 36
                status = await call(client, "farm_status")
                assert status.is_error is False and status.structured_content["androids"]["total"] == 0
                seen: list[dict] = []
                thread = farm.answer("approve", seen)
                created = await call(client, "create_androids", {"name": "over-http"})
                thread.join(10)
                assert created.structured_content == {"ok": True, "created": [1]} and seen[0]["caller"] == CALLER

        anyio.run(main)
        assert farm.api.delete("/api/instances/1").status_code == 200
    finally:
        server.send_signal(signal.SIGINT)
        try:
            server.wait(15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(10)


# ------------------------------------------------------------------------------- android

@pytest.mark.parametrize("farm", [{"image": True}], indirect=True)
def test_one_real_android_seen_and_driven_through_mcp(farm):
    async def main(operator: Operator) -> None:
        async with stdio(farm) as client:
            # ---- create and start (both approved by the operator)
            created = await call(client, "create_androids", {"name": "pilot"})
            assert created.structured_content == {"ok": True, "created": [1]}
            started = await finished(client, await call(client, "start_androids", {"selector": "1"}))
            assert started.is_error is False, text(started)
            assert started.structured_content["task"]["state"] == "done" and started.structured_content["task"]["ok"] == 1
            assert "<<<DEVICE-CONTENT" in text(started)                     # per-device details are marked as device text
            android = (await call(client, "get_android", {"id": 1})).structured_content["android"]
            assert android["state"] == "booted" and android["rendering"] == "software" and android["network"] == "none"
            names = {c["name"] for c in farm.docker.ps(all=True, labels={"io.superfarm.farm": PREFIX})}
            assert names == {f"{PREFIX}-android-1", f"{PREFIX}-net-1"}
            assert (await call(client, "network_status", {"id": 1})).structured_content["network"]["mode"] == "none"

            # ---- a screenshot is a real picture, within the configured size
            shot = await call(client, "screenshot", {"id": 1})
            assert shot.is_error is False, text(shot)
            image = next(c for c in shot.content if c.type == "image")
            assert image.mime_type == "image/jpeg"
            with Image.open(io.BytesIO(base64.b64decode(image.data))) as picture:
                picture.load()
                assert picture.format == "JPEG" and max(picture.size) <= 768 and min(picture.size) >= 240
                assert picture.size == (shot.structured_content["width"], shot.structured_content["height"])
                assert len(picture.convert("L").resize((16, 16)).getcolors(256)) > 1          # not one flat colour
            small = await call(client, "screenshot", {"id": 1, "max_px": 200})
            assert max(small.structured_content["width"], small.structured_content["height"]) == 200

            # ---- open Settings, read the screen: a numbered list of elements inside the device block
            launched = await finished(client, await call(client, "launch_app", {"selector": "1", "package": SETTINGS}))
            assert launched.is_error is False, text(launched)
            screen, bar = None, None
            deadline = time.monotonic() + 90
            while bar is None and time.monotonic() < deadline:
                screen = await call(client, "read_screen", {"id": 1})
                if not screen.is_error:
                    bar = next((e for e in screen.structured_content["device_content"]["elements"]
                                if e["id"] == "search_action_bar"), None)
                if bar is None:
                    await anyio.sleep(2)
            assert bar is not None, text(screen)
            before = screen.structured_content["device_content"]
            assert before["package"] == SETTINGS and len(before["elements"]) >= 5
            assert bar["clickable"] and 0 < bar["center"][0] < 1 and 0 < bar["center"][1] < 1
            assert screen.structured_content["size"] == [480, 854]
            body = text(screen)
            assert body.startswith("Screen of Android 1 (480x854 px)") and "<<<DEVICE-CONTENT" in body
            assert body.rstrip().splitlines()[-1].startswith("<<<END DEVICE-CONTENT ")

            # ---- tap the search bar: the screen changes to the search box
            tapped = await call(client, "tap", {"selector": "1", "x": bar["center"][0], "y": bar["center"][1]})
            assert tapped.is_error is False and tapped.structured_content == {"ok": True, "androids": 1, "failed": 0}
            box, after = None, None
            deadline = time.monotonic() + 60
            while box is None and time.monotonic() < deadline:
                await anyio.sleep(1.5)
                reading = await call(client, "read_screen", {"id": 1})
                if not reading.is_error:
                    after = reading.structured_content["device_content"]
                    box = next((e for e in after["elements"] if e.get("editable")), None)
            assert box is not None, "tapping the search bar did not open an editable search box"
            assert [e["id"] for e in after["elements"]] != [e["id"] for e in before["elements"]]

            # ---- type: the text appears in the box
            typed = await call(client, "type_text", {"selector": "1", "text": "battery"})
            assert typed.is_error is False, text(typed)
            shown = False
            deadline = time.monotonic() + 60
            while not shown and time.monotonic() < deadline:
                await anyio.sleep(1.5)
                reading = await call(client, "read_screen", {"id": 1})
                if not reading.is_error:
                    shown = any(e.get("editable") and "battery" in e["text"].lower()
                                for e in reading.structured_content["device_content"]["elements"])
            assert shown, "the typed text did not appear in the search box"
            assert (await call(client, "press_key", {"selector": "1", "key": "HOME"})).is_error is False

            # ---- a bundled example flow runs to the end
            run = await call(client, "run_flow", {"selector": "1", "name": "examples/settings-open-entry",
                                                  "variables": {"entry": "Display"}})
            assert run.is_error is False, text(run)
            task_id = run.structured_content["task"]["id"]
            run = await finished(client, run, timeout=300)
            assert run.structured_content["task"]["state"] == "done" and run.structured_content["task"]["failed"] == 0, text(run)
            status = await call(client, "run_status", {"task_id": task_id})
            assert status.structured_content["run"]["name"] == "examples/settings-open-entry"
            assert status.structured_content["device_content"]["devices"]["1"]["state"] == "done", text(status)

            # ---- apps on the device are listed as device content
            apps = await call(client, "list_apps", {"id": 1, "include_system": True})
            assert SETTINGS in [a["package"] for a in apps.structured_content["device_content"]["installed"]]

            # ---- delete (approved): containers and data are gone
            deleted = await finished(client, await call(client, "delete_androids", {"selector": "1"}))
            assert deleted.is_error is False, text(deleted)
            assert (await call(client, "list_androids")).structured_content["count"] == 0

    with Operator(farm) as operator:
        anyio.run(main, operator)

    summaries = [a["summary"] for a in operator.approved]
    assert summaries[0] == 'Create 1 new Android (name="pilot")' and summaries[1] == "Start Android 1"
    assert f'Launch "{SETTINGS}" on Android 1' in summaries and 'Type "battery" on Android 1' in summaries
    assert any(s.startswith("Tap at (") and s.endswith("on the screen of Android 1") for s in summaries)
    assert 'Run flow "examples/settings-open-entry" on Android 1 with variables entry' in summaries
    assert summaries[-1] == "Delete Android 1 and its data"
    assert {a["klass"] for a in operator.approved} == {"change", "destructive"}
    assert all(a["caller"] == CALLER for a in operator.approved)
    assert not farm.docker.ps(all=True, labels={"io.superfarm.farm": PREFIX})
    assert not farm.docker.volumes(f"{PREFIX}-")
    acted = [line for line in farm.audit() if line.get("caller") == CALLER and line["method"] != "GET"
             and not line["path"].startswith("/api/approvals")]
    assert {line["path"] for line in acted} >= {"/api/instances", "/api/instances/bulk", "/api/input/tap",
                                                "/api/input/text", "/api/apps/launch", "/api/automation/runs"}

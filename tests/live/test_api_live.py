"""Live test of the daemon: the real server process, a real Android, driven over HTTP only.

    start `serve` as a process on 127.0.0.1:17701 with a throwaway farm home
    health -> login (cookie + CSRF) -> system, capacity, gpus
    create and start one software-rendered Android through the API
    frame.jpg is a JPEG of the configured size; a tap goes through /api/input/tap
    the MJPEG stream gives its viewer back when the client hangs up; the event socket works
    the H.264 socket relays key frames and takes control messages (when scrcpy is installed)
    SIGTERM stops the daemon cleanly and the Android keeps running; a new daemon sees it
    stop and delete through the API; nothing is left behind

Run:  SF_LIVE=1 .venv/bin/python -m pytest tests/live/test_api_live.py -v
Needs Docker, the binder module, adb, the sidecar image and a local Android image
(SF_LIVE_IMAGE, default redroid:13-noflag). The Android has no network at all (mode `none`).
Everything it creates carries the prefix `sftapi` and is removed again, also on failure.
"""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from superfarm.core.docker import Docker

pytestmark = pytest.mark.live

PREFIX = "sftapi"
PORT = 17701
ADB_BASE = 17710
BASE = f"http://127.0.0.1:{PORT}"
IMAGE = os.environ.get("SF_LIVE_IMAGE", "redroid:13-noflag")
WIDTH, HEIGHT = 480, 854

CONFIG = f"""\
server: {{port: {PORT}}}
docker: {{name_prefix: {PREFIX}}}
gpu: {{mode: software}}
network: {{mode: none, adb_port_base: {ADB_BASE}}}
android:
  width: {WIDTH}
  height: {HEIGHT}
  images:
    "13": {{base: "{IMAGE}", tag: "{IMAGE}"}}
backup: {{auto_before_risky: false}}
"""


def cleanup(docker: Docker) -> None:
    """Remove what this test can have created, selected by its own prefix, and nothing else."""
    names = [c["name"] for c in docker.ps(all=True, labels={"io.superfarm.farm": PREFIX})
             if c["name"].startswith(f"{PREFIX}-")]
    if names:
        docker.rm(*names)
    for volume in docker.volumes(f"{PREFIX}-"):
        docker.volume_rm(volume)


class Daemon:
    def __init__(self, home: Path):
        self.home = home
        self.process: subprocess.Popen | None = None

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
                    return
            except httpx.TransportError:
                time.sleep(0.2)
        raise AssertionError("the daemon did not answer within 30 s")

    def stop(self) -> int:
        assert self.process is not None
        self.process.send_signal(signal.SIGTERM)
        try:
            return self.process.wait(20)
        finally:
            if self.process.poll() is None:
                self.process.kill()
            self.process = None


@pytest.fixture
def daemon(tmp_path):
    docker = Docker()
    ok, why = docker.available()
    if not ok:
        pytest.skip(f"Docker is not usable: {why}")
    if not docker.image_exists(IMAGE):
        pytest.skip(f"the Android image {IMAGE} is not on this machine (set SF_LIVE_IMAGE)")
    cleanup(docker)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(CONFIG)
    d = Daemon(home)
    d.docker = docker
    d.start()
    try:
        yield d
    finally:
        if d.process is not None:
            d.process.kill()
            d.process.wait(10)
        cleanup(docker)


def follow(client: httpx.Client, response: httpx.Response, timeout: float = 300) -> dict:
    assert response.status_code == 202, response.text
    task = response.json()["task"]
    deadline = time.monotonic() + timeout
    while task["state"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(1)
        task = client.get(f"/api/tasks/{task['id']}").json()["task"]
    assert task["state"] == "done", task
    return task


def test_daemon_end_to_end(daemon):
    docker = daemon.docker
    token = (daemon.home / "secrets" / "api-token").read_text().strip()
    assert (daemon.home / "secrets" / "api-token").stat().st_mode & 0o777 == 0o600

    # ---- health, login, the browser's way in
    assert httpx.get(f"{BASE}/api/health").json()["ok"] is True
    assert httpx.get(f"{BASE}/api/system").status_code == 401
    client = httpx.Client(base_url=BASE, timeout=60)
    assert client.post("/api/auth/login", json={"token": "wrong"}).status_code == 401
    login = client.post("/api/auth/login", json={"token": token})
    assert login.status_code == 200 and "sf_session" in client.cookies
    assert client.get("/api/auth/me").json()["method"] == "session"
    assert client.post("/api/instances", json={"count": 1}).status_code == 403          # no CSRF header yet
    client.headers["X-SF-CSRF"] = login.json()["csrf"]

    # ---- system
    system = client.get("/api/system").json()
    assert system["version"] and system["host"]["kernel"] and system["host"]["ram_mb"] > 1000
    assert system["components"]["instances"]["available"] is True
    capacity = client.get("/api/system/capacity").json()
    assert capacity["running"] == 0 and isinstance(capacity["estimate"].get("max_instances"), int)
    gpus = client.get("/api/system/gpus")
    assert gpus.status_code == 200, gpus.text
    assert any(o["value"] == "software" and o["available"] for o in gpus.json()["options"])

    # ---- create and start
    created = client.post("/api/instances", json={"count": 1, "name": "live test"})
    assert created.status_code == 201, created.text
    n = created.json()["instances"][0]["id"]
    try:
        task = follow(client, client.post(f"/api/instances/{n}/start", json={}))
        assert task["results"][str(n)]["ok"], task
        status = client.get(f"/api/instances/{n}").json()
        assert status["state"] == "booted" and status["adb_port"] == ADB_BASE + n
        assert status["gpu"]["mode"] == "software" and status["net"]["mode"] == "none"
        assert client.get("/api/system/capacity").json()["running"] == 1

        # ---- see it, touch it
        frame = client.get(f"/api/instances/{n}/frame.jpg")
        assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"
        assert frame.content[:3] == b"\xff\xd8\xff"
        assert Image.open(io.BytesIO(frame.content)).size == (WIDTH, HEIGHT)
        tap = client.post("/api/input/tap", json={"ids": [n], "x": 0.5, "y": 0.5}).json()
        assert tap["ok"] is True and tap["results"][str(n)]["x"] == WIDTH // 2 and tap["results"][str(n)]["y"] == HEIGHT // 2

        # ---- a live view gives its slot back when the client hangs up
        with client.stream("GET", f"/api/instances/{n}/stream.mjpg", params={"fps": 5}) as stream:
            assert stream.headers["content-type"].startswith("multipart/x-mixed-replace")
            seen, viewers = b"", 0
            for chunk in stream.iter_bytes():
                seen += chunk
                if seen.count(b"--sfframe") >= 2:
                    # asked while the stream is still being read: leaving the loop closes it
                    viewers = client.get("/api/stream/capabilities").json()["screen"]["viewers"]
                    break
            assert viewers == 1 and b"Content-Type: image/jpeg" in seen
        deadline = time.monotonic() + 10
        while client.get("/api/stream/capabilities").json()["screen"]["viewers"] and time.monotonic() < deadline:
            time.sleep(0.2)
        assert client.get("/api/stream/capabilities").json()["screen"]["viewers"] == 0

        # ---- the event socket, authenticated by a first message
        from websockets.sync.client import connect
        with connect(f"ws://127.0.0.1:{PORT}/api/events") as ws:
            ws.send(json.dumps({"auth": token}))
            assert json.loads(ws.recv(timeout=10)) == {"type": "hello"}

        # ---- the H.264 view: relayed when this host offers it, refused with the reason when not
        offered = client.get("/api/stream/capabilities").json()["h264"]
        with connect(f"ws://127.0.0.1:{PORT}/api/instances/{n}/h264") as ws:
            ws.send(json.dumps({"auth": token}))
            first = json.loads(ws.recv(timeout=30))
            if offered["enabled"]:
                assert first["type"] == "info" and first["codec"].startswith("avc1.") and first["id"] == n
                frame = ws.recv(timeout=30)
                assert isinstance(frame, bytes) and len(frame) > 13
                flags, width, height = frame[0], int.from_bytes(frame[9:11], "big"), int.from_bytes(frame[11:13], "big")
                assert flags & 0x01 and flags & 0x02                 # a viewer starts at a key frame with its configuration
                assert [width, height] == first["size"] and frame[13:17] in (b"\x00\x00\x00\x01", b"\x00\x00\x01\x65")
                ws.send(json.dumps({"type": "tap", "x": 0.5, "y": 0.5}))
                ws.send(json.dumps({"type": "nonsense"}))
                while True:                                          # video keeps coming; the refusal arrives between frames
                    message = ws.recv(timeout=30)
                    if isinstance(message, str):
                        assert json.loads(message)["code"] == "bad_request"
                        break
            else:
                assert first["type"] == "error" and first["code"] == "h264_unavailable" and offered["reason"] in first["message"]
        on_device = ["sh", "-c", "ps -A -o ARGS | grep -c '[s]crcpy' || true"]
        deadline = time.monotonic() + 10
        while docker.exec(f"{PREFIX}-android-{n}", on_device, check=False).out != "0" and time.monotonic() < deadline:
            time.sleep(0.5)
        assert docker.exec(f"{PREFIX}-android-{n}", on_device, check=False).out == "0"     # nothing left running on the device

        # ---- stopping the daemon leaves the Android running
        assert daemon.stop() == 0
        android = f"{PREFIX}-android-{n}"
        assert docker.running(android) and docker.running(f"{PREFIX}-net-{n}")
        daemon.start()
        client = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": f"Bearer {token}"})
        assert client.get(f"/api/instances/{n}").json()["state"] in ("running", "booted")
        log = (daemon.home / "logs" / "daemon.log").read_text()
        assert "daemon stopped" in log and token not in log

        # ---- stop and delete
        follow(client, client.post(f"/api/instances/{n}/stop"))
        assert not docker.running(android)
        deleted = client.delete(f"/api/instances/{n}")
        assert deleted.status_code == 200 and deleted.json()["deleted"] == n
    finally:
        cleanup(docker)
    assert client.get("/api/instances").json() == {"instances": []}
    assert not docker.exists(f"{PREFIX}-android-{n}") and docker.volumes(f"{PREFIX}-") == []
    audit = [json.loads(line) for line in (daemon.home / "logs" / "audit.log").read_text().splitlines()]
    assert [e["path"] for e in audit if e["status"] == 202] == [f"/api/instances/{n}/start", f"/api/instances/{n}/stop"]
    assert token not in (daemon.home / "logs" / "audit.log").read_text()

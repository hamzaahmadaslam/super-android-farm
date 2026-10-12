"""Live test of duplicate and simultaneous requests: the real daemon, one real Android, HTTP only.

People double-click, and two tabs or a script and the panel can ask for the same thing at the
same moment. Every pair below is sent truly in parallel (two threads released together):

    create with one Idempotency-Key         one Android, the same answer twice
    two starts of the same Android          one boot, one pair of containers, the other 409 busy
    delete / stop during that start         409 busy naming the start's task; the start completes
    start + stop at the same moment         one wins, the state matches the winner
    two settings saves                      both land, in memory and in config.yaml
    two uploads of the same app file        one app in the library
    two bulk actions on the same Android    one task, the other 409 busy
    cancel of a finished task               409 "already finished"; of a running one, twice: fine
    two restores over the same Android      one restore, with the safety backup it made in its result
    two deletes                             deleted once; the other is told there is no such Android

The rule behind the 409s is in superfarm/core/tasks.py: one operator request per Android at a
time; a second one is refused and told which task has the Android.

Run:  SF_LIVE=1 .venv/bin/python -m pytest tests/live/test_api_double_requests.py -v
Needs Docker, the binder module, adb, the sidecar image and a local Android image
(SF_LIVE_IMAGE, default redroid:13-noflag). The Android has no network at all (mode `none`)
and is rendered in software. Everything it creates carries the prefix `sftfix` and is removed
again, also on failure. Takes about a minute and a half: two boots, one backup, one restore.
"""
from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
import yaml

from superfarm.core.docker import Docker

pytestmark = pytest.mark.live

PREFIX = "sftfix"
PORT = 18210
ADB_BASE = 18220
BASE = f"http://127.0.0.1:{PORT}"
IMAGE = os.environ.get("SF_LIVE_IMAGE", "redroid:13-noflag")

CONFIG = f"""\
server: {{port: {PORT}}}
docker: {{name_prefix: {PREFIX}}}
gpu: {{mode: software}}
network: {{mode: none, adb_port_base: {ADB_BASE}}}
android:
  width: 480
  height: 854
  extra_boot_args: ["ro.farm.redroid_fps=20"]
  images:
    "13": {{base: "{IMAGE}", tag: "{IMAGE}"}}
"""


def cleanup(docker: Docker) -> None:
    """Remove what this test can have created, selected by its own prefix, and nothing else."""
    names = [c["name"] for c in docker.ps(all=True, labels={"io.superfarm.farm": PREFIX})
             if c["name"].startswith(f"{PREFIX}-")]
    if names:
        docker.rm(*names)
    for volume in docker.volumes(f"{PREFIX}-"):
        docker.volume_rm(volume)


def ours(docker: Docker) -> list[str]:
    return sorted(c["name"] for c in docker.ps(all=True, labels={"io.superfarm.farm": PREFIX}))


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


class Caller:
    """The API as two people at once would use it."""

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = httpx.Client(base_url=BASE, timeout=120, headers=self.headers)

    def __getattr__(self, verb):
        return getattr(self.client, verb)

    def together(self, *requests: tuple) -> list[httpx.Response]:
        """Send the requests at the same moment, each on a connection and a thread of its own.
        A request is (method, path) or (method, path, keyword arguments)."""
        barrier = threading.Barrier(len(requests))
        out: list = [None] * len(requests)

        def send(n: int, method: str, path: str, kw: dict | None = None) -> None:
            with httpx.Client(base_url=BASE, timeout=120, headers=self.headers) as own:
                own.get("/api/health")                       # the connection is open before the start signal
                barrier.wait(30)
                out[n] = own.request(method, path, **(kw or {}))

        threads = [threading.Thread(target=send, args=(n, *request)) for n, request in enumerate(requests)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(150)
        assert all(isinstance(r, httpx.Response) for r in out), out
        return out

    def follow(self, response: httpx.Response, timeout: float = 300) -> dict:
        assert response.status_code == 202, response.text
        task = response.json()["task"]
        deadline = time.monotonic() + timeout
        while task["state"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.5)
            task = self.client.get(f"/api/tasks/{task['id']}").json()["task"]
        assert task["state"] not in ("queued", "running"), task
        return task

    def state(self, n: int) -> str:
        return self.client.get(f"/api/instances/{n}").json()["state"]


def one_of_two(first: httpx.Response, second: httpx.Response) -> tuple[httpx.Response, httpx.Response]:
    """(accepted, refused) of two requests of which exactly one may be taken."""
    assert sorted((first.status_code, second.status_code)) == [202, 409], (first.text, second.text)
    return (first, second) if first.status_code == 202 else (second, first)


def refused_by(response: httpx.Response, task_id: str, ids: list[int]) -> None:
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "busy" and error["detail"] == {"task": task_id, "ids": ids}, error
    assert f"task {task_id}" in error["message"]


def small_apk() -> bytes:
    """An app file the library accepts: the unit tests' own minimal APK."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
    try:
        import test_control_support
        return test_control_support.make_apk()
    finally:
        sys.path.pop(0)


def test_double_and_simultaneous_requests(daemon):
    docker = daemon.docker
    api = Caller((daemon.home / "secrets" / "api-token").read_text().strip())
    android, net, volume = f"{PREFIX}-android-1", f"{PREFIX}-net-1", f"{PREFIX}-data-1"

    # ---- create, sent twice with one Idempotency-Key: one Android
    key = {"headers": {"Idempotency-Key": "live-create-1"}, "json": {"count": 1, "name": "double click"}}
    first, second = api.together(("POST", "/api/instances", key), ("POST", "/api/instances", key))
    assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
    assert first.json() == second.json() and [s["id"] for s in first.json()["instances"]] == [1]
    assert sorted("idempotency-replayed" in r.headers for r in (first, second)) == [False, True]
    assert [row["id"] for row in api.get("/api/instances").json()["instances"]] == [1]

    try:
        # ---- two starts at the same moment: one boot
        start, refused = one_of_two(*api.together(("POST", "/api/instances/1/start"), ("POST", "/api/instances/1/start")))
        booting = start.json()["task"]["id"]
        refused_by(refused, booting, [1])
        # ---- while it boots: no delete, no stop, no restart, no second bulk start
        began = time.monotonic()
        refused_by(api.delete("/api/instances/1"), booting, [1])
        refused_by(api.post("/api/instances/1/stop"), booting, [1])
        refused_by(api.post("/api/instances/1/restart"), booting, [1])
        refused_by(api.post("/api/instances/bulk", json={"action": "start", "selector": "all"}), booting, [1])
        assert time.monotonic() - began < 5                         # answers, not waits
        task = api.follow(start)
        assert task["state"] == "done" and task["results"]["1"]["ok"], task
        assert api.state(1) == "booted"
        assert ours(docker) == [android, net]                       # one pair, no leftovers of a second attempt
        started = [t for t in api.get("/api/tasks").json()["tasks"] if t["kind"] == "instance.start"]
        assert [t["id"] for t in started] == [booting]
        created = docker.run(["container", "inspect", "--format", "{{.Created}} {{.RestartCount}}", android]).out

        # ---- start + stop at the same moment on the running Android
        won, lost = one_of_two(*api.together(("POST", "/api/instances/1/start"), ("POST", "/api/instances/1/stop")))
        refused_by(lost, won.json()["task"]["id"], [1])
        outcome = api.follow(won)
        assert outcome["results"]["1"]["ok"], outcome
        stop_won = won.json()["task"]["kind"] == "instance.stop"
        assert docker.running(android) is not stop_won and api.state(1) == ("stopped" if stop_won else "booted")
        # Whoever won, nothing was built a second time.
        assert docker.run(["container", "inspect", "--format", "{{.Created}} {{.RestartCount}}", android]).out == created

        # ---- two settings saves at the same moment: both land
        a, b = api.together(("PATCH", "/api/config", {"json": {"patch": {"farm_name": "Double Farm"}}}),
                            ("PATCH", "/api/config", {"json": {"patch": {"stream": {"jpeg_quality": 61}}}}))
        assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
        assert (a.json()["changed"], b.json()["changed"]) == (["farm_name"], ["stream.jpeg_quality"])
        config = api.get("/api/config").json()["config"]
        assert (config["farm_name"], config["stream"]["jpeg_quality"]) == ("Double Farm", 61)
        saved = yaml.safe_load((daemon.home / "config.yaml").read_text())
        assert (saved["farm_name"], saved["stream"]["jpeg_quality"]) == ("Double Farm", 61)

        # ---- two uploads of the same app file: one app
        apk = small_apk()
        assert zipfile.is_zipfile(io.BytesIO(apk))
        upload = ("POST", "/api/apps/library", {"params": {"filename": "double.apk"}, "content": apk,
                                                "headers": {"Content-Type": "application/vnd.android.package-archive"}})
        a, b = api.together(upload, upload)
        assert (a.status_code, b.status_code) == (201, 201), (a.text, b.text)
        assert a.json()["app"]["id"] == b.json()["app"]["id"]
        assert [x["id"] for x in api.get("/api/apps/library").json()["apps"]] == [a.json()["app"]["id"]]
        assert list((daemon.home / "run" / "uploads").iterdir()) == []

        # ---- two bulk actions that name the same Android: one task
        if not docker.running(android):                             # the stop won above: bring it back first
            assert api.follow(api.post("/api/instances/1/start"))["results"]["1"]["ok"]
        won, lost = one_of_two(*api.together(
            ("POST", "/api/instances/bulk", {"json": {"action": "stop", "selector": "all"}}),
            ("POST", "/api/instances/bulk", {"json": {"action": "stop", "selector": "1"}})))
        refused_by(lost, won.json()["task"]["id"], [1])
        stopped = api.follow(won)
        assert stopped["results"]["1"]["ok"] and not docker.running(android) and api.state(1) == "stopped"

        # ---- cancel of a task that has finished: it was not cancelled, and says so
        late = api.post(f"/api/tasks/{stopped['id']}/cancel")
        assert late.status_code == 409 and "already finished (done)" in late.json()["error"]["message"]
        assert api.get(f"/api/tasks/{stopped['id']}").json()["task"]["state"] == "done"

        # ---- a backup, then two restores over the same (stopped) Android at the same moment
        a, b = api.together(("POST", "/api/backups", {"json": {"selector": "1"}}),
                            ("POST", "/api/backups", {"json": {"selector": "1"}}))
        won, lost = one_of_two(a, b)
        refused_by(lost, won.json()["task"]["id"], [1])
        backed_up = api.follow(won)
        assert backed_up["state"] == "done" and backed_up["results"]["1"]["ok"], backed_up
        (backup,) = api.get("/api/backups").json()["backups"]          # one backup, not two
        restore = ("POST", "/api/backups/restore", {"json": {"archive": backup["name"], "id": 1, "force": True}})
        won, lost = one_of_two(*api.together(restore, restore))
        refused_by(lost, won.json()["task"]["id"], [1])
        refused_by(api.post("/api/instances/1/start"), won.json()["task"]["id"], [1])     # not while its data is replaced
        restored = api.follow(won)
        assert restored["state"] == "done" and restored["results"]["1"]["ok"], restored
        output = restored["output"]
        assert (output["id"], output["archive"], output["new"]) == (1, backup["name"], False)
        # The data that was replaced was copied aside first, and the result says where.
        safety = Path(output["safety_backup"])
        assert safety.is_dir() and safety.parent.name == "android-1" and (safety / "manifest.json").is_file()
        names = sorted(b["name"] for b in api.get("/api/backups").json()["backups"])
        assert names == sorted([backup["name"], f"android-1/{safety.name}"])           # one restore made one safety backup
        assert sorted(t["kind"] for t in api.get("/api/tasks").json()["tasks"] if t["kind"].startswith("backup.")) == [
            "backup.manual", "backup.restore"]

        # ---- cancelling a running task twice at the same moment, and once more when it has ended
        boot = api.post("/api/instances/1/start")
        assert boot.status_code == 202, boot.text
        cancel = ("POST", f"/api/tasks/{boot.json()['task']['id']}/cancel")
        a, b = api.together(cancel, cancel)
        assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
        ended = api.follow(boot)
        assert ended["state"] == "cancelled", ended
        again = api.post(cancel[1])
        assert again.status_code == 200 and again.json()["task"]["state"] == "cancelled"

        # ---- two deletes at the same moment: deleted once
        assert api.patch("/api/config", json={"patch": {"backup": {"auto_before_risky": False}}}).status_code == 200
        a, b = api.together(("DELETE", "/api/instances/1"), ("DELETE", "/api/instances/1"))
        assert sorted((a.status_code, b.status_code)) == [200, 404], (a.text, b.text)
        done, missing = (a, b) if a.status_code == 200 else (b, a)
        assert (done.json()["deleted"], done.json()["data"]) == (1, "removed")
        assert missing.json()["error"] == {"code": "not_found", "message": "there is no Android 1"}
        assert api.get("/api/instances").json()["instances"] == []
        assert ours(docker) == [] and docker.volumes(f"{PREFIX}-") == [] and not docker.volume_exists(volume)
        assert not (daemon.home / "instances" / "1" / "instance.yaml").exists()
        assert not (daemon.home / "instances" / "1" / "state.json").exists()
    finally:
        log = (daemon.home / "logs" / "daemon.log").read_text()

    # ---- none of it was an accident waiting in the log
    assert "Traceback" not in log and "unhandled error" not in log, log[-3000:]
    assert daemon.stop() == 0
    assert ours(docker) == [] and docker.volumes(f"{PREFIX}-") == []

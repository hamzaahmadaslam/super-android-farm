"""Live test of the in-farm AI: a real model, the real daemon, real Androids, over HTTP only.

    1. the model profile passes the probe (server, model, a reply, a tool call there and back)
    2. Assistant: "how many are running, how many can this machine run?" is answered from tool results
    3. Assistant: "create one Android named pilot-test and start it" -> approval requests, approved
       through the API like an operator would; the Android really boots
    4. Pilot: one goal in Android's own Settings app, three runs on that Android -> train -> the
       trained program replays with the model server gone
    5. Pilot: the same goal on two Androids at once -> train   (only with more than 6 GB of free memory)

Run:  SF_LIVE=1 .venv/bin/python -m pytest tests/live/test_ai_live.py -v -s

A model server is needed. Either one is already running:

    SF_LIVE_MODEL_URL    its address          (default http://127.0.0.1:18511)
    SF_LIVE_MODEL        the model to use     (default: the first one the server lists)
    SF_LIVE_MODEL_FORMAT ollama | openai      (default ollama)

or the test starts an Ollama server itself on that address, and then really stops it before the replay:

    SF_LIVE_OLLAMA_BIN     path of the `ollama` binary
    SF_LIVE_OLLAMA_MODELS  its model folder (OLLAMA_MODELS)

With a server it does not own, the replay runs with the profile pointed at a closed port instead,
which proves the same thing: nothing asks a model. The test is skipped when no model server answers.

Needs Docker, the binder module, adb, the sidecar image and a local Android image (SF_LIVE_IMAGE,
default redroid:13-noflag). The Androids have no network (mode `none`) and render on the CPU.
Everything created carries the prefix `sftai` and is removed again, also on failure. The numbers
the run measured are printed at the end (-s) and are what docs/AI.md quotes.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from superfarm.core.docker import Docker

pytestmark = pytest.mark.live

PREFIX = "sftai"
PORT = 18500
ADB_BASE = 18520
BASE = f"http://127.0.0.1:{PORT}"
IMAGE = os.environ.get("SF_LIVE_IMAGE", "redroid:13-noflag")
MODEL_URL = os.environ.get("SF_LIVE_MODEL_URL", "http://127.0.0.1:18511")
MODEL = os.environ.get("SF_LIVE_MODEL", "")
FORMAT = os.environ.get("SF_LIVE_MODEL_FORMAT", "ollama")
OLLAMA_BIN = os.environ.get("SF_LIVE_OLLAMA_BIN", "")
OLLAMA_MODELS = os.environ.get("SF_LIVE_OLLAMA_MODELS", "")
SETTINGS = "com.android.settings"
# Three levels deep and below the first screenful: the run has to scroll, pick the right
# section and then the right entry. The check is a label only that last screen has.
GOAL = "Open the Date & time settings"
SUCCESS = "text:Set time automatically"
MIN_PARALLEL_MB = 6000

CONFIG = """\
farm_name: AI live test
server: {{port: {port}}}
docker: {{name_prefix: {prefix}}}
gpu: {{mode: software}}
network: {{mode: none, adb_port_base: {adb}}}
resources: {{memory_mb: 1536}}
android:
  extra_boot_args: ["ro.farm.redroid_fps=20"]
  images:
    "13": {{base: "{image}", tag: "{image}"}}
backup: {{auto_before_risky: false}}
ai:
  default_model: live
  models:
    live: {{provider: {format}, base_url: "{url}", model: "{model}", num_ctx: 16384, timeout: 300}}
"""

REPORT: dict[str, dict] = {}
STATE: dict = {}


def available_mb() -> int:
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def cleanup(docker: Docker) -> None:
    """Remove what this test can have created, selected by its own prefix, and nothing else."""
    names = [c["name"] for c in docker.ps(all=True, labels={"io.superfarm.farm": PREFIX})
             if c["name"].startswith(f"{PREFIX}-")]
    if names:
        docker.rm(*names)
    for volume in docker.volumes(f"{PREFIX}-"):
        docker.volume_rm(volume)


class ModelServer:
    """The model server of this run: one we start (and can really stop), or one already there."""

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.owned = bool(OLLAMA_BIN)

    def answers(self) -> bool:
        try:
            return httpx.get(MODEL_URL + ("/api/tags" if FORMAT == "ollama" else "/v1/models"), timeout=3).status_code == 200
        except httpx.TransportError:
            return False

    def start(self) -> None:
        if not self.owned or self.answers():
            self.owned = self.owned and self.process is not None
            return
        parts = urlsplit(MODEL_URL)
        env = {**os.environ, "OLLAMA_HOST": f"{parts.hostname}:{parts.port}", "OLLAMA_NUM_PARALLEL": "2",
               "OLLAMA_KEEP_ALIVE": "10m"}
        if OLLAMA_MODELS:
            env["OLLAMA_MODELS"] = OLLAMA_MODELS
            env["HOME"] = str(Path(OLLAMA_MODELS).parent / "home")     # its key pair, next to the models
        self.process = subprocess.Popen([OLLAMA_BIN, "serve"], env=env, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        self.owned = True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not self.answers():
            time.sleep(0.3)

    def stop(self) -> bool:
        """Stop it if it is ours. True when the server is really gone."""
        if self.process is None:
            return False
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.process = None
        return not self.answers()


class Farm:
    def __init__(self, home: Path):
        self.home = home
        self.process: subprocess.Popen | None = None
        self.http: httpx.Client | None = None

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
        token = (self.home / "secrets" / "api-token").read_text().strip()
        self.http = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {token}"}, timeout=120)

    def stop(self) -> None:
        if self.http is not None:
            self.http.close()
        if self.process is not None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(20)
            except subprocess.TimeoutExpired:
                self.process.kill()

    # -- small helpers over the API --------------------------------------------------
    def get(self, path: str, **params) -> dict:
        response = self.http.get(path, params=params)
        assert response.status_code == 200, (path, response.status_code, response.text[:300])
        return response.json()

    def post(self, path: str, body: dict | None = None, *, ok=(200, 201, 202)) -> dict:
        response = self.http.post(path, json=body or {})
        assert response.status_code in ok, (path, response.status_code, response.text[:300])
        return response.json()

    def wait(self, task: dict, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while task["state"] in ("queued", "running"):
            assert time.monotonic() < deadline, f"task {task['id']} ({task['title']}) did not finish in {timeout:.0f}s"
            time.sleep(0.5)
            task = self.get(f"/api/tasks/{task['id']}")["task"]
        return task

    def ask(self, text: str, *, approve: bool = False, timeout: float = 420) -> dict:
        """One message to the Assistant. With `approve`, every approval request it raises is
        answered Yes through the approvals API, as an operator would in the panel."""
        conversation = self.post("/api/ai/conversations")["conversation"]["id"]
        started = time.monotonic()
        self.post(f"/api/ai/conversations/{conversation}/messages", {"text": text})
        approved: list[dict] = []
        while True:
            view = self.get(f"/api/ai/conversations/{conversation}")["conversation"]
            for approval_id in view["pending_approvals"]:
                if approve and approval_id not in [a["id"] for a in approved]:
                    approved.append(self.post(f"/api/approvals/{approval_id}/approve", {"via": "panel"})["approval"])
            if view["state"] == "idle" and view["events"] and view["events"][-1]["type"] == "final":
                break
            assert time.monotonic() - started < timeout, f"the assistant did not finish: {json.dumps(view)[:1500]}"
            time.sleep(0.3)
        return {"view": view, "final": view["events"][-1], "approved": approved, "seconds": round(time.monotonic() - started, 1),
                "tools": [e["tool"] for e in view["events"] if e["type"] == "tool_call"],
                "results": [e for e in view["events"] if e["type"] == "tool_result"]}

    def screen_texts(self, instance: int) -> list[str]:
        return [e["text"] for e in self.get(f"/api/automation/screen/{instance}")["elements"] if e.get("text")]


@pytest.fixture(scope="module")
def model():
    server = ModelServer()
    server.start()
    if not server.answers():
        pytest.skip(f"no model server answers at {MODEL_URL} (set SF_LIVE_MODEL_URL, or SF_LIVE_OLLAMA_BIN to start one)")
    yield server
    server.stop()


@pytest.fixture(scope="module")
def farm(tmp_path_factory, model):
    docker = Docker()
    ok, why = docker.available()
    if not ok:
        pytest.skip(f"Docker is not usable: {why}")
    if not docker.image_exists(IMAGE):
        pytest.skip(f"the Android image {IMAGE} is not on this machine (set SF_LIVE_IMAGE)")
    if available_mb() < 3000:
        pytest.skip("less than 3 GB of memory available; not starting a test Android")
    cleanup(docker)
    home = tmp_path_factory.mktemp("ai-home")
    (home / "config.yaml").write_text(CONFIG.format(port=PORT, prefix=PREFIX, adb=ADB_BASE, image=IMAGE, format=FORMAT,
                                                    url=MODEL_URL, model=MODEL))
    made = Farm(home)
    made.start()
    try:
        yield made
    finally:
        made.stop()
        cleanup(docker)
        if REPORT:
            print("\n\n==== measured in this run ====")
            for name, facts in REPORT.items():
                print(f"{name}: {json.dumps(facts)}")


def pilot_numbers(session: dict) -> dict:
    runs = [r for device in session["devices"].values() for r in device["results"]]
    return {"runs": len(runs), "verified": sum(1 for r in runs if r["state"] == "done"),
            "states": [r["state"] for r in runs], "steps": [r["step"] for r in runs],
            "model_calls": [r["model_calls"] for r in runs], "rejected_answers": [r["rejected"] for r in runs],
            "seconds": [r["elapsed"] for r in runs], "tokens_in": sum(r["tokens_in"] for r in runs),
            "why_not": [{"reason": r["message"][:160],
                         "trail": [f"{a['action']} {a['target']} -> {a['outcome']}" for a in r.get("actions") or []]}
                        for r in runs if r["state"] != "done"]}


# ------------------------------------------------------------------------------- 1. probe

def test_1_the_profile_passes_the_probe(farm):
    listed = farm.get("/api/ai/models")
    assert [m["name"] for m in listed["models"]] == ["live"] and listed["models"][0]["default"]
    response = farm.http.post("/api/ai/models/live/test", timeout=600)
    probe = response.json()["probe"]
    REPORT["probe"] = {"model": probe["model"], "ok": probe["ok"], "seconds": probe["seconds"],
                       "checks": {c["id"]: [c["ok"], round(c["ms"] / 1000, 1)] for c in probe["checks"]}}
    assert probe["ok"], probe["checks"]
    assert [c["id"] for c in probe["checks"]] == ["reachable", "model", "reply", "tools"] and probe["tools"] is True


# --------------------------------------------------------------------------- 2. assistant

def test_2_the_assistant_answers_from_tool_results(farm):
    capacity = farm.get("/api/system/capacity")
    estimate = capacity["estimate"]["max_instances"]
    asked = farm.ask("How many Androids are running right now, and how many can this machine run?")
    final = asked["final"]
    REPORT["assistant question"] = {"seconds": asked["seconds"], "tools": asked["tools"], "answer": final["text"][:300],
                                    "estimate": estimate, "usage": asked["view"]["usage"]}
    assert asked["tools"], "the model answered without looking"
    assert all(r["ok"] for r in asked["results"]) and set(asked["tools"]) <= {"farm_status", "list_androids", "system_doctor"}
    assert final["actions"] == [] and final["record"].startswith("Farm record: no action")
    assert str(estimate) in final["text"], "the capacity estimate in the answer is the one the tool reported"
    assert farm.get("/api/instances")["instances"] == [], "a question changed nothing"


def test_3_the_assistant_creates_and_starts_an_android_after_approval(farm):
    asked = farm.ask("Create one Android named pilot-test and start it.", approve=True, timeout=600)
    final = asked["final"]
    rows = farm.get("/api/instances")["instances"]
    REPORT["assistant action"] = {"seconds": asked["seconds"], "tools": asked["tools"], "approved": [a["summary"] for a in asked["approved"]],
                                  "record": final["record"], "answer": final["text"][:300],
                                  "androids": [[r["id"], r["name"], r["state"]] for r in rows], "usage": asked["view"]["usage"]}
    assert len(asked["approved"]) >= 2, "creating and starting are two approvals under policy `ask`"
    assert all(a["caller"] == "assistant:live" and a["state"] == "approved" for a in asked["approved"])
    assert [a["tool"] for a in asked["approved"]][:2] == ["create_androids", "start_androids"]
    assert len(rows) == 1 and rows[0]["name"] == "pilot-test" and rows[0]["state"] in ("starting", "running", "booted"), rows
    ran = [a for a in final["actions"] if a["ok"]]
    assert [a["tool"] for a in ran][:2] == ["create_androids", "start_androids"] and "2 action(s) ran" in final["record"]
    # The same inbox and the same audit log as any other AI caller.
    inbox = farm.get("/api/approvals")["approvals"]
    assert {a["id"] for a in asked["approved"]} <= {a["id"] for a in inbox}
    audit = (farm.home / "logs" / "audit.log").read_text()
    assert "assistant:live" in audit and "/api/instances" in audit
    STATE["android"] = rows[0]["id"]
    deadline = time.monotonic() + 240
    while farm.get(f"/api/instances/{rows[0]['id']}")["state"] != "booted":
        assert time.monotonic() < deadline, "the Android the assistant started did not finish booting"
        time.sleep(2)


# ------------------------------------------------------------------------------- 4. pilot

def test_4_pilot_three_runs_train_and_replay_without_a_model(farm, model):
    android = STATE.get("android")
    if android is None:                                    # the assistant test did not get that far
        android = farm.post("/api/instances", {"count": 1, "name": "pilot-test"})["instances"][0]["id"]
        farm.wait(farm.post(f"/api/instances/{android}/start")["task"], 300)
        STATE["android"] = android
    started = time.monotonic()
    task = farm.post("/api/ai/pilot", {"goal": GOAL, "package": SETTINGS, "selector": str(android), "success": SUCCESS,
                                       "runs": 3, "dataset": "date-time"})["task"]
    task = farm.wait(task, 1500)
    session = farm.get(f"/api/ai/pilot/sessions/{task['id']}")["session"]
    numbers = pilot_numbers(session)
    REPORT["pilot, 3 runs on 1 Android"] = {**numbers, "wall_seconds": round(time.monotonic() - started, 1)}
    assert task["state"] == "done" and numbers["runs"] == 3
    assert numbers["verified"] >= 2, f"too few verified runs to train on: {numbers}"
    assert len(farm.get("/api/automation/datasets/date-time")["dataset"]["recordings_list"]) == numbers["verified"]

    trained = farm.wait(farm.post("/api/ai/pilot/date-time/train")["task"], 300)
    assert trained["state"] == "done", trained
    summary, program = trained["output"]["summary"], trained["output"]["program"]
    REPORT["training"] = {"steps": [[s["kind"], s["label"], s["seen_in"]] for s in summary["outline"]],
                          "text": summary["text"], "warnings": summary["warnings"]}
    kinds = [s["kind"] for s in summary["outline"]]
    assert kinds[0] == "launch" and kinds[-1] == "assert" and "tap" in kinds and program["slug"] == "date-time"

    # The replay must not need a model: stop the server if it is ours, otherwise point the
    # profile at a closed port. Either way a model request could only fail.
    really_stopped = model.stop()
    if not really_stopped:
        farm.http.patch("/api/config", json={"patch": {"ai": {"models": {"live": {"base_url": "http://127.0.0.1:9"}}}}})
    probe = farm.http.post("/api/ai/models/live/test", timeout=120).json()["probe"]
    assert not probe["ok"] and probe["checks"][0]["kind"] == "unreachable", "the model is still reachable"

    farm.wait(farm.post("/api/apps/stop", {"selector": str(android), "package": SETTINGS})["task"], 60)
    replay_started = time.monotonic()
    replay = farm.wait(farm.post("/api/automation/runs", {"kind": "program", "name": "date-time", "selector": str(android)})["task"], 300)
    status = farm.get(f"/api/automation/runs/{replay['id']}")["run"]["devices"][str(android)]
    REPORT["replay without a model"] = {"model_server_stopped": really_stopped, "ok": replay["results"][str(android)]["ok"],
                                        "seconds": round(time.monotonic() - replay_started, 1), "state": status["state"],
                                        "steps_done": status["done"], "recovered": status["recovered"]}
    assert replay["results"][str(android)]["ok"], status
    assert "Set time automatically" in farm.screen_texts(android), "the replay really ended on the Date & time screen"

    model.start()                                          # for the tests that follow
    if not really_stopped:
        farm.http.patch("/api/config", json={"patch": {"ai": {"models": {"live": {"base_url": MODEL_URL}}}}})


def test_5_pilot_on_two_androids_in_parallel(farm):
    if available_mb() < MIN_PARALLEL_MB:
        pytest.skip(f"less than {MIN_PARALLEL_MB} MB of memory available; not starting a second Android")
    first = STATE["android"]
    second = farm.post("/api/instances", {"count": 1, "name": "pilot-test-2"})["instances"][0]["id"]
    assert farm.wait(farm.post(f"/api/instances/{second}/start")["task"], 300)["results"][str(second)]["ok"]
    time.sleep(5)                                          # the launcher needs a moment after boot
    started = time.monotonic()
    task = farm.post("/api/ai/pilot", {"goal": GOAL, "package": SETTINGS, "selector": f"{first},{second}",
                                       "success": SUCCESS, "runs": 1, "dataset": "date-time-pair"})["task"]
    task = farm.wait(task, 900)
    session = farm.get(f"/api/ai/pilot/sessions/{task['id']}")["session"]
    numbers = pilot_numbers(session)
    wall = round(time.monotonic() - started, 1)
    REPORT["pilot, 2 Androids in parallel"] = {**numbers, "wall_seconds": wall,
                                               "ran_at_the_same_time": wall < sum(numbers["seconds"]) * 0.9}
    assert numbers["runs"] == 2 and numbers["verified"] >= 1, numbers
    trained = farm.wait(farm.post("/api/ai/pilot/date-time-pair/train")["task"], 300)
    assert trained["state"] == "done", trained
    summary = trained["output"]["summary"]
    REPORT["training on the pair"] = {"recordings": summary["recordings"], "instances": summary["instances"],
                                      "steps": [[s["kind"], s["label"], s["seen_in"]] for s in summary["outline"]],
                                      "warnings": summary["warnings"]}
    assert summary["recordings"] == numbers["verified"]

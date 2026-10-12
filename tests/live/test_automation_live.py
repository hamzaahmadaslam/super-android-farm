"""Automation against a real Android (opt-in: SF_LIVE=1).

Uses one throwaway Android container (`sftest-auto-android`, ADB on 127.0.0.1:17501,
no data volume, capped at 1.5 GB) and Android's own Settings app as the app under
test. The container is created here when it is not already running and removed again
afterwards; nothing else on the machine is touched. The test is skipped when the
machine has little memory to spare, so it can never squeeze a farm that is in use.

    SF_LIVE=1 .venv/bin/python -m pytest tests/live/test_automation_live.py -q
"""
from __future__ import annotations

import os
import time

import pytest

from superfarm.automation import Automation
from superfarm.automation.flows import FlowLibrary, FlowRun, parse_flow_text
from superfarm.automation.program import Program
from superfarm.automation.recorder import RecordingSession
from superfarm.automation.runtime import ProgramRun
from superfarm.automation.store import AutomationStore
from superfarm.automation.trainer import train
from superfarm.automation.ui import REMOTE_DIR, Device, DumpReader, UiError, element_at, read_nodes
from superfarm.automation.values import Resolver
from superfarm.config import AutomationConfig, Config
from superfarm.core.adb import Adb
from superfarm.core.tasks import TaskManager
from superfarm.paths import Paths
from superfarm.util.proc import run, which

pytestmark = pytest.mark.live

CONTAINER = "sftest-auto-android"
PORT = 17501
IMAGE = os.environ.get("SF_LIVE_IMAGE", "redroid:13-noflag")
SETTINGS = "com.android.settings"
SEARCH = "com.android.settings.intelligence"
MIN_FREE_MB = 3000


def _available_mb() -> int:
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


@pytest.fixture(scope="module")
def adb():
    if not which("docker") or not which("adb"):
        pytest.skip("needs docker and adb")
    existed = run(["docker", "container", "inspect", "--format", "{{.State.Running}}", CONTAINER],
                  timeout=30, check=False).out == "true"
    if not existed:
        if _available_mb() < MIN_FREE_MB:
            pytest.skip(f"less than {MIN_FREE_MB} MB of memory available; not starting a test Android")
        if not run(["docker", "image", "inspect", IMAGE], timeout=30, check=False).ok:
            pytest.skip(f"image {IMAGE} is not present locally (set SF_LIVE_IMAGE)")
        run(["docker", "rm", "-f", "-v", CONTAINER], timeout=60, check=False)
        run(["docker", "run", "-d", "--privileged", "--name", CONTAINER, "--memory", "1536m", "--cpus", "2",
             "-p", f"127.0.0.1:{PORT}:5555", IMAGE, "androidboot.redroid_width=480",
             "androidboot.redroid_height=854", "androidboot.redroid_dpi=160"], timeout=120)
    device = Adb.for_port(PORT)
    try:
        assert device.wait_boot(timeout=180), "the test Android did not boot"
        time.sleep(3)                       # the launcher needs a moment after boot_completed
        yield device
    finally:
        device.disconnect()
        if not existed:
            run(["docker", "rm", "-f", "-v", CONTAINER], timeout=60, check=False)


@pytest.fixture
def device(adb):
    dev = Device(adb, reader=DumpReader(adb), read_timeout=30)
    yield dev
    dev.close()


def settings() -> AutomationConfig:
    return AutomationConfig(step_timeout=30, run_timeout=240)


def library(tmp_path) -> FlowLibrary:
    return FlowLibrary(AutomationStore(tmp_path / "automation"))


def run_flow(tmp_path, device, name_or_text: str, variables: dict | None = None, **kw) -> dict:
    lib = library(tmp_path)
    flow = lib.get(name_or_text) if name_or_text.startswith("examples/") else parse_flow_text(name_or_text)
    lines: list[str] = []
    result = FlowRun(flow, device, settings(), library=lib, resolver=Resolver(variables=variables),
                     log=lines.append, **kw).run()
    result["lines"] = lines
    return result


# ------------------------------------------------------------------- reading
def test_reads_are_fresh_bounded_and_leave_nothing_behind(adb, device):
    device.key("home")
    time.sleep(2)
    started = time.monotonic()
    home = device.nodes()
    assert time.monotonic() - started < 30 and len(home) > 5
    device.launch(SETTINGS)
    time.sleep(4)
    nodes = device.nodes()
    assert any(n["package"] == SETTINGS for n in nodes) and device.current()[0] == SETTINGS
    assert {n["id"] for n in nodes} != {n["id"] for n in home}            # not the previous screen again
    assert adb.shell(["sh", "-c", f"ls {REMOTE_DIR}/sf-ui-* 2>/dev/null | wc -l"]) == "0"
    assert len(read_nodes(adb, timeout=30)) == len(nodes)
    bar = next(n for n in nodes if n["id"] == "search_action_bar")
    element = element_at(nodes, *bar["center"])
    assert element["id"] == "search_action_bar" and element["text"] == "Search settings" and element["clickable"]


def test_a_read_is_cut_off_at_its_time_limit(adb):
    started = time.monotonic()
    with pytest.raises(UiError):
        DumpReader(adb).read(timeout=0.2)             # far too short for any dump
    assert time.monotonic() - started < 8


# ------------------------------------------------------------- example flows
def test_example_open_app(tmp_path, device):
    device.key("home")
    result = run_flow(tmp_path, device, "examples/open-app", {"package": SETTINGS})
    assert result["state"] == "done", result
    assert device.current()[0] == SETTINGS


def test_example_settings_search(tmp_path, device):
    result = run_flow(tmp_path, device, "examples/settings-search", {"query": "display"})
    assert result["state"] == "done" and result["done"] == 6, result
    nodes = Device(device.adb).nodes()
    box = next(n for n in nodes if n["editable"])
    assert box["text"] == "display" and box["package"] == SEARCH
    assert any("display" in n["text"].lower() for n in nodes if not n["editable"])


def test_example_type_into_focused_field(tmp_path, device):
    # the search box of the previous test still holds text; this must replace it
    run_flow(tmp_path, device, "examples/settings-search", {"query": "sound"})
    result = run_flow(tmp_path, device, "examples/type-into-focused-field", {"text": "wifi"})
    assert result["state"] == "done", result
    box = next(n for n in Device(device.adb).nodes() if n["editable"])
    assert box["text"] == "wifi"


def test_example_scroll_until_visible(tmp_path, device):
    run_flow(tmp_path, device, "examples/open-app", {"package": SETTINGS})
    assert not any(n["text"] == "System" for n in Device(device.adb).nodes())     # below the fold at first
    result = run_flow(tmp_path, device, "examples/scroll-until-visible", {"text": "System"})
    assert result["state"] == "done", result
    assert any(line.endswith(")") and "swiped" in line for line in result["lines"])
    assert any(n["text"] == "System" for n in Device(device.adb).nodes())

    missing = run_flow(tmp_path, device, "on_unknown: continue\nsteps: [{scroll_to: {text: No Such Setting}, max_swipes: 12}]")
    assert missing["state"] == "done" and "end of the list" in missing["issues"][0], missing


def test_example_permission_dialog_is_a_no_op_without_a_dialog(tmp_path, device):
    result = run_flow(tmp_path, device, "examples/dismiss-permission-dialog")
    assert result["state"] == "done" and not any("tap" in line for line in result["lines"])


# -------------------------------------------------------- rules on a real app
def test_dry_run_and_never_tap_change_nothing(tmp_path, device):
    run_flow(tmp_path, device, "examples/open-app", {"package": SETTINGS})
    before = [(n["id"], n["text"]) for n in Device(device.adb).nodes()]

    practice = run_flow(tmp_path, device, f"package: {SETTINGS}\nsteps: [{{tap: {{text: Apps}}}}]", dry_run=True)
    assert practice["state"] == "done" and "nothing was sent" in practice["message"]
    assert any("would tap 'Apps'" in line for line in practice["lines"])

    cfg = settings().model_copy(update={"stop_on_unknown_screen": False})
    flow = parse_flow_text(f"package: {SETTINGS}\nnever_tap: [{{text: Apps}}]\nsteps: [{{tap: {{text: Apps}}}}]")
    refused = FlowRun(flow, device, cfg).run()
    assert refused["state"] == "failed" and "never-tap" in refused["error"]

    assert [(n["id"], n["text"]) for n in Device(device.adb).nodes()] == before


# ------------------------------------------------- record -> train -> replay
def test_record_train_and_replay(tmp_path, adb):
    store = AutomationStore(tmp_path / "automation")
    dataset = store.create_dataset("Search settings", "open Settings and search for something", SETTINGS)
    cfg = settings()

    for _ in range(3):
        dev = Device(adb, reader=DumpReader(adb), read_timeout=30)
        dev.stop_app(SETTINGS)
        session = RecordingSession(dev, dataset, cfg, instance_id=1)
        session.launch(SETTINGS)
        time.sleep(4)
        tapped = session.tap(240, 227, normalized=False)                 # the search bar
        assert tapped["element"]["id"] == "search_action_bar", tapped
        assert tapped["shot"] and tapped["activity"].startswith(SETTINGS + "/")
        time.sleep(3)
        typed = session.text("storage")
        assert typed["element"]["editable"] and typed["element"]["package"] == SEARCH
        summary = session.stop()
        assert summary["steps"] == 3 and summary["named_taps"] == 1

    program = train(dataset, store.programs_dir)
    assert [s.kind for s in program.steps] == ["launch", "tap", "type"]
    assert program.steps[1].selector["id"] == "search_action_bar" and program.steps[1].seen_in == 3
    assert program.steps[1].shot and program.shot_path(program.steps[1].shot).is_file()
    assert not any("left out" in w for w in program.warnings)

    dev = Device(adb, reader=DumpReader(adb), read_timeout=30)
    dev.stop_app(SETTINGS)
    dev.key("home")
    loaded = Program.load(store.programs_dir, "search-settings")
    result = ProgramRun(loaded, dev, cfg).run()
    assert result["state"] == "done" and result["done"] == 3, result
    nodes = Device(adb).nodes()
    assert next(n for n in nodes if n["editable"])["text"] == "storage"

    # the same program in practice mode sends nothing
    Device(adb).key("home")
    time.sleep(2)
    practice = ProgramRun(loaded, Device(adb, reader=DumpReader(adb)), cfg, dry_run=True).run()
    assert practice["state"] == "done" and Device(adb).current()[0] != SETTINGS


# ------------------------------------------------- the facade, end to end
class OneAndroid:
    """The two things automation asks of the instance manager, for one instance."""

    def __init__(self, adb: Adb):
        self._adb = adb

    def select(self, selector: str) -> list[int]:
        return [1] if selector in ("1", "all") else []

    def adb(self, instance_id: int) -> Adb:
        return self._adb


def test_fleet_run_recording_and_teaching_through_the_facade(tmp_path, adb):
    tasks = TaskManager()
    config = Config.model_validate({"automation": {"hierarchy_reader": "uiautomator", "step_timeout": 30}})
    auto = Automation(config, Paths(tmp_path / "home").ensure(), OneAndroid(adb), tasks)
    try:
        # a flow as a Task, with per-device state
        task = auto.run("flow", "examples/settings-search", "all", variables={"query": "apps"})
        tasks.wait(task, timeout=180)
        status = auto.run_status(task.id)
        assert task.state == "done" and task.results[1]["ok"], status
        assert status["devices"]["1"]["state"] == "done" and status["devices"]["1"]["done"] == 6

        # the live screen, and a role taught from it
        run_flow(tmp_path, Device(adb), "examples/open-app", {"package": SETTINGS})
        seen = auto.read_screen(1)
        assert seen["package"] == SETTINGS and seen["size"] == [480, 854]
        bar = next(e for e in seen["elements"] if e["id"] == "search_action_bar")
        x, y = (bar["bounds"][0] + bar["bounds"][2]) // 2, (bar["bounds"][1] + bar["bounds"][3]) // 2
        taught = auto.teach_role(1, SETTINGS, "search", x, y, with_text=False)
        assert taught == {"role": "search", "selector": {"id": "search_action_bar"}}
        auto.profiles.add_never_tap(SETTINGS, {"text": "Apps"})
        auto.flows.save("by-role", f"package: {SETTINGS}\nsteps:\n  - tap: '@search'\n"
                                   "  - wait_for: {editable: true, focused: true}\n"
                                   "  - launch: {fresh: true}\n  - wait_for: {text: Apps}\n"
                                   "  - tap: {text: Apps}\n")
        task = auto.run("flow", "by-role", "1")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not (auto.run_status(task.id) or {}).get("waiting"):
            time.sleep(0.5)
        status = auto.run_status(task.id)
        # the profile's never-tap rule stopped the last step, and this device waits for a person
        assert status["waiting"] == [1] and "never-tap" in status["devices"]["1"]["waiting"]["reason"], status
        assert status["devices"]["1"]["step"] == 4
        assert auto.resolve(task.id, 1, "skip")
        tasks.wait(task, timeout=60)
        assert auto.run_status(task.id)["devices"]["1"]["state"] == "done"
        assert Device(adb).current()[0] == SETTINGS                 # still on the home page: Apps was never opened

        # recording through the facade, as the panel would drive it
        auto.create_dataset("Open search", "open the settings search", SETTINGS)
        auto.start_recording("open-search", 1)
        step = auto.record_tap(1, 0.5, 227 / 854)
        assert step["element"]["id"] == "search_action_bar"
        auto.record_key(1, "back")
        assert auto.stop_recording(1)["steps"] == 2
        training = auto.train("open-search")
        tasks.wait(training, timeout=60)
        assert training.state == "done" and training.output["steps"] == 2
        practice = auto.run("program", "open-search", "1", dry_run=True)
        tasks.wait(practice, timeout=120)
        assert auto.run_status(practice.id)["devices"]["1"]["state"] == "done"
    finally:
        tasks.shutdown()

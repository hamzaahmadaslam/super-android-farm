"""Every bundled example flow, on an Android set up the way the farm sets it up by default.

The default farm trims a list of system packages once after first boot
(`performance.trim_packages`, lists in superfarm/data/trim/). An example that quietly depends on
one of them would pass on a bare Android and fail on a real farm, so this test boots one
Android through the instance manager with the default configuration, confirms the trim was
applied, and runs every example in superfarm/data/flows/examples/.

Opt-in (SF_LIVE=1). One Android, software rendering, no route to the internet, 1.5 GB, removed
afterwards; everything carries the test prefix `sftex`.

    SF_LIVE=1 .venv/bin/python -m pytest tests/live/test_examples_live.py -q

SF_EXAMPLES_TRIM=off boots the same Android with trimming off, to compare.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import pytest

from superfarm.automation.flows import EXAMPLES_DIR, FlowLibrary, FlowRun, parse_flow_text
from superfarm.automation.store import AutomationStore
from superfarm.automation.ui import Device, DumpReader
from superfarm.automation.values import Resolver
from superfarm.config import AutomationConfig, Config
from superfarm.core.docker import Docker
from superfarm.core.instances import InstanceManager, mem_available_mb, trim_list
from superfarm.core.store import Store
from superfarm.core.tasks import TaskManager
from superfarm.paths import Paths

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("SF_LIVE") != "1", reason="set SF_LIVE=1 to run live tests")]

PREFIX = "sftex"
PORT_BASE = 18600
IMAGE = os.environ.get("SF_LIVE_IMAGE", "redroid:13-noflag")
HELPER_BASE = os.environ.get("SF_LIVE_HELPER", "alpine:latest")
TOOLS_TAG = "sftest-ex-tools:1"
TRIMMED = os.environ.get("SF_EXAMPLES_TRIM", "on") != "off"
SETTINGS = "com.android.settings"

# Where an example expects to start. Most do not care; these two work on what is already on screen.
OPEN_SETTINGS = {"flow": "examples/open-app", "variables": {"package": SETTINGS}}
OPEN_DRAWER_SEARCH = """
steps:
  - swipe: {direction: up}
  - tap: {id: search_container_all_apps}
  - wait_for: {editable: true, focused: true}
"""
SETUP = {"scroll-until-visible": OPEN_SETTINGS, "type-into-focused-field": OPEN_DRAWER_SEARCH}

# What each example needs to run. Every file in the examples folder must have an entry, so a
# new example cannot be added without being run here.
VARIABLES: dict[str, dict] = {
    "dismiss-permission-dialog": {},
    "open-app": {"package": SETTINGS},
    "scroll-until-visible": {"text": "System"},
    "settings-open-entry": {},
    "type-into-focused-field": {"text": "wifi"},
}


@dataclass
class SoftwareGpu:
    mode: str = "software"
    node: str = ""
    run_args: list[str] = field(default_factory=list)
    boot_args: list[str] = field(default_factory=lambda: ["androidboot.redroid_gpu_mode=guest"])
    reason: str = "live test"


def offline_sidecar(r, config, paths, store, docker) -> list[str]:
    """Network namespace owner for the test: ADB published on loopback, default route removed."""
    labels = [x for k, v in r.names.labels("net").items() for x in ("--label", f"{k}={v}")]
    return ["--name", r.names.net, *labels, "--memory", "64m", "--cap-add", "NET_ADMIN", "--stop-signal", "SIGKILL",
            "-p", f"127.0.0.1:{r.adb_port}:5555", HELPER_BASE,
            "sh", "-c", "ip route del default 2>/dev/null; exec sleep 86400"]


def cleanup(docker: Docker) -> None:
    rows = docker.ps(all=True, labels={"io.superfarm.farm": PREFIX})
    docker.rm(*[c["name"] for c in rows if c["name"].startswith(PREFIX + "-")])
    for volume in docker.volumes(PREFIX + "-data-"):
        docker.volume_rm(volume)


@pytest.fixture(scope="module")
def android(tmp_path_factory):
    docker = Docker()
    ok, why = docker.available()
    if not ok:
        pytest.skip(f"Docker is not usable: {why}")
    for image in (IMAGE, HELPER_BASE):
        if not docker.image_exists(image):
            pytest.skip(f"image {image} is not on this machine (set SF_LIVE_IMAGE / SF_LIVE_HELPER)")
    if 0 <= mem_available_mb() < 4000:
        pytest.skip("less than 4 GB of RAM available; not adding an Android to this machine now")
    cleanup(docker)
    # Everything else stays at its default (1536 MB, trimming on), which is the point of this test.
    performance = {"boot_timeout": 240, "memory_guard_mb": 2500}
    if not TRIMMED:
        performance["trim_packages"] = False
    config = Config.model_validate({
        "docker": {"name_prefix": PREFIX},
        "network": {"adb_port_base": PORT_BASE},
        "android": {"images": {"13": {"base": IMAGE, "tag": IMAGE}}},
        "performance": performance,
        "backup": {"auto_before_risky": False},
    })
    paths = Paths(tmp_path_factory.mktemp("home")).ensure()
    tasks = TaskManager(workers=2)
    mgr = InstanceManager(config, paths, docker, Store(paths), tasks)
    mgr.images.tools_tag, mgr.images.tools_base = TOOLS_TAG, HELPER_BASE
    b = mgr.backends
    b.gpu_choose = lambda r, config, paths, *, counts=None: SoftwareGpu()
    b.sidecar_run_args = offline_sidecar
    b.net_wait_ready = lambda docker, r, timeout=90: {"ready": docker.running(r.names.net), "state": "up", "detail": ""}
    b.net_status = lambda docker, r: {"state": "offline (test)"}
    try:
        mgr.create(name="examples")
        started = mgr.start(1)
        assert started["state"] == "booted", started
        assert started["warnings"] == [], started["warnings"]
        time.sleep(3)                       # the launcher needs a moment after boot_completed
        yield mgr.adb(1)
    finally:
        tasks.shutdown()
        for i in mgr.store.ids():
            try:
                mgr.adb(i).disconnect()
            except Exception:  # noqa: BLE001 - best effort
                pass
        cleanup(docker)


def run_flow(tmp_path, adb, flow: str | None = None, text: str | None = None, variables: dict | None = None) -> dict:
    """Run a bundled example (`flow`) or a flow given as YAML text, and fail instead of waiting for a person."""
    lib = FlowLibrary(AutomationStore(tmp_path / "automation"))
    device = Device(adb, reader=DumpReader(adb), read_timeout=30)
    lines: list[str] = []
    config = AutomationConfig(step_timeout=30, run_timeout=240, stop_on_unknown_screen=False)
    try:
        result = FlowRun(lib.get(flow) if flow else parse_flow_text(text), device, config, library=lib,
                         resolver=Resolver(variables=variables), log=lines.append).run()
    finally:
        device.close()
    result["lines"] = lines
    return result


def test_every_example_has_a_case_here():
    assert sorted(p.stem for p in EXAMPLES_DIR.glob("*.yaml")) == sorted(VARIABLES)


def test_the_android_has_the_default_trim(android):
    disabled = {line.removeprefix("package:").strip() for line in android.shell("pm list packages -d").splitlines()}
    wanted = set(trim_list("13"))
    if TRIMMED:
        assert wanted and wanted <= disabled, sorted(wanted - disabled)
    else:
        # (the image itself ships a few packages disabled, com.android.nfc among them)
        assert not {"com.android.settings.intelligence", "com.android.egg", "com.android.bluetooth"} & disabled


@pytest.mark.parametrize("name", sorted(VARIABLES))
def test_example_succeeds(tmp_path, android, name):
    # Start from the home screen, then from whatever screen the example works on.
    Device(android).key("home")
    time.sleep(1)
    setup = SETUP.get(name)
    if isinstance(setup, dict):
        assert run_flow(tmp_path, android, setup["flow"], variables=setup["variables"])["state"] == "done"
    elif setup:
        assert run_flow(tmp_path, android, text=setup)["state"] == "done"
    started = time.monotonic()
    result = run_flow(tmp_path, android, f"examples/{name}", variables=VARIABLES[name])
    assert result["state"] == "done", result
    print(f"{name}: {result['done']} step(s) in {time.monotonic() - started:.1f} s")


def test_typed_text_replaces_what_the_field_held(tmp_path, android):
    Device(android).key("home")
    time.sleep(1)
    assert run_flow(tmp_path, android, text=OPEN_DRAWER_SEARCH)["state"] == "done"
    for text in ("wifi", "sound"):
        assert run_flow(tmp_path, android, "examples/type-into-focused-field", variables={"text": text})["state"] == "done"
        box = next(n for n in Device(android).nodes() if n["editable"])
        assert box["text"] == text

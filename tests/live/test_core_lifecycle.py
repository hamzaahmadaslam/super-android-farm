"""Live lifecycle of a real Android container: create -> start -> stop -> start -> backup ->
restore -> clone -> delete.

Opt-in: needs Docker, the binder module and a local Android image. Run with

    SF_LIVE=1 SF_LIVE_IMAGE=redroid/redroid:13.0.0-latest python -m pytest tests/live/test_core_lifecycle.py -q

Only the instance manager is under test. The GPU and network modules are replaced by the
smallest possible stand-ins (software rendering; a sidecar that owns the network namespace,
publishes ADB on 127.0.0.1 and has no route to the internet), so the test neither depends
on those modules nor ever puts a test Android online. Never more than one Android runs at
a time, and everything created carries the test prefix and is removed at the end.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

import pytest

from superfarm.config import Config
from superfarm.core.docker import Docker
from superfarm.core.instances import InstanceManager, mem_available_mb
from superfarm.core.store import Store
from superfarm.core.tasks import TaskManager
from superfarm.paths import Paths

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("SF_LIVE") != "1", reason="set SF_LIVE=1 to run live tests")]

PREFIX = "sftcore"
PORT_BASE = 17000
IMAGE = os.environ.get("SF_LIVE_IMAGE", "redroid:13-noflag")
HELPER_BASE = os.environ.get("SF_LIVE_HELPER", "alpine:latest")
TOOLS_TAG = "sftest-core-tools:1"
BUILD_VERSION = "sftest13"
BUILT_TAG = f"superfarm/android:{BUILD_VERSION}"


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
    """Remove everything this test can have created, and nothing else: selection is by the
    test prefix in the farm label / name."""
    rows = docker.ps(all=True, labels={"io.superfarm.farm": PREFIX})
    docker.rm(*[c["name"] for c in rows if c["name"].startswith(PREFIX + "-")])
    for volume in docker.volumes(PREFIX + "-data-"):
        docker.volume_rm(volume)
    for tag in (TOOLS_TAG, BUILT_TAG):
        if docker.image_exists(tag):
            docker.run(["image", "rm", tag], check=False)


@pytest.fixture
def farm(tmp_path):
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
    config = Config.model_validate({
        "docker": {"name_prefix": PREFIX},
        "network": {"adb_port_base": PORT_BASE},
        "android": {"images": {"13": {"base": IMAGE, "tag": IMAGE}, BUILD_VERSION: {"base": IMAGE}}},
        "device": {"profile": "samsung-s23", "timezone": "Europe/Berlin"},
        "performance": {"boot_timeout": 240, "memory_guard_mb": 2500},
        # Safety backups before a delete have their own scenario in test_e2e_farm.py.
        "backup": {"auto_before_risky": False},
    })
    paths = Paths(tmp_path / "home").ensure()
    tasks = TaskManager(workers=2)
    mgr = InstanceManager(config, paths, docker, Store(paths), tasks)
    mgr.images.tools_tag, mgr.images.tools_base = TOOLS_TAG, HELPER_BASE
    b = mgr.backends
    b.gpu_choose = lambda r, config, paths, *, counts=None: SoftwareGpu()
    b.sidecar_run_args = offline_sidecar
    b.net_wait_ready = lambda docker, r, timeout=90: {"ready": docker.running(r.names.net), "state": "up", "detail": ""}
    b.net_status = lambda docker, r: {"state": "offline (test)"}
    try:
        yield mgr
    finally:
        tasks.shutdown()
        for i in mgr.store.ids():
            try:
                mgr.adb(i).disconnect()
            except Exception:  # noqa: BLE001 - best effort
                pass
        cleanup(docker)


def in_volume(mgr: InstanceManager, id: int, volume: str, command: list[str]) -> str:
    return mgr._helper_run(id, command, mounts=[f"{volume}:/data:ro"])


def test_lifecycle(farm: InstanceManager):
    mgr, docker = farm, farm.docker
    names = mgr.names(1)

    # ---- create: nothing runs yet
    spec = mgr.create(name="lifecycle")[0]
    assert spec.id == 1 and spec.device.profile == "samsung-s23"
    assert mgr.status(1)["state"] == "missing"

    # ---- first start: pair created network first, Android boots, post-boot settings land
    first = mgr.start(1)
    assert first["state"] == "booted" and first["recreated"] and first["gpu"] == "software"
    assert first["warnings"] == [], first["warnings"]
    assert docker.running(names.net) and docker.running(names.android)
    assert docker.label(names.android, "io.superfarm.plan") == docker.label(names.net, "io.superfarm.plan") != ""
    adb = mgr.adb(1)
    assert adb.getprop("sys.boot_completed") == "1"
    assert adb.getprop("ro.product.model") == "SM-S911B" and adb.getprop("ro.product.brand") == "samsung"
    assert adb.getprop("ro.serialno") == spec.device.serial
    assert adb.setting_get("secure", "android_id") == spec.device.android_id
    assert adb.getprop("persist.sys.timezone") == "Europe/Berlin"
    assert adb.setting_get("global", "window_animation_scale") in ("0", "0.0")
    assert adb.size() == (480, 854)
    disabled = adb.shell("pm list packages -d")
    assert "com.android.egg" in disabled and "com.android.bluetooth" in disabled
    assert adb.shell(["cat", "/data/local/tmp/.superfarm_trim"]).splitlines()[0] == mgr.store.state_get(1, "trim")["hash"]
    # offline by construction: the sidecar has no default route
    assert adb.shell("ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; echo $?") != "0"

    status = mgr.status(1)
    assert status["state"] == "booted" and status["uptime"] is not None and status["serial"] == f"127.0.0.1:{PORT_BASE + 1}"
    assert [s["state"] for s in mgr.list_status()] == ["booted"]

    # ---- data survives stop + start, and the containers are reused
    adb.shell(["sh", "-c", "echo kept-across-restarts > /data/local/tmp/keep.txt"])
    android_id = docker.run(["container", "inspect", "--format", "{{.Id}}", names.android]).out
    mgr.stop(1)
    assert not docker.running(names.android) and not docker.running(names.net)
    assert mgr.status(1)["state"] == "stopped"
    second = mgr.start(1)
    assert second["state"] == "booted" and not second["recreated"]
    assert docker.run(["container", "inspect", "--format", "{{.Id}}", names.android]).out == android_id
    assert mgr.adb(1).shell(["cat", "/data/local/tmp/keep.txt"]) == "kept-across-restarts"

    # ---- mock location (needs root inside Android)
    mgr.update(1, {"device": {"geo": "52.52,13.405"}})
    again = mgr.start(1)                                   # already running: only post-boot settings are applied
    assert again["already_running"] and again["warnings"] == [], again["warnings"]
    assert "52.52" in docker.exec(names.android, ["dumpsys", "location"], timeout=30).stdout

    # ---- backup: Android is stopped for the copy and started again
    backups = mgr.backups
    archive = backups.create(1)
    assert docker.running(names.android)
    manifest = backups.manifest(archive)
    data = backups.data_file(archive)
    assert manifest["consistent"] and manifest["files"][data.name]["size"] == data.stat().st_size > 1_000_000
    assert sorted(p.name for p in archive.iterdir()) == sorted([data.name, "instance.yaml", "manifest.json"])
    assert backups.verify(archive, deep=True)
    assert [b["dir"] for b in backups.list()] == [str(archive)]

    # ---- a changed renderer clears compiled shader caches (checked on the stopped volume)
    mgr.stop(1)
    find_caches = ["find", "/data/user_de", "-name", "*.shaders_cache"]
    assert in_volume(mgr, 1, names.volume, find_caches) != ""
    assert mgr._check_renderer(mgr.resolved(1), "other-renderer", False) == []
    assert in_volume(mgr, 1, names.volume, find_caches) == ""
    assert in_volume(mgr, 1, names.volume, ["cat", "/data/.superfarm_renderer"]) == "other-renderer"

    # ---- restore as a new Android: same identity, so it is created with networking off
    new_id = backups.restore(archive)
    restored = mgr.store.get(new_id)
    assert new_id == 2 and restored.device == mgr.store.get(1).device and restored.network.mode == "none"
    new_volume = mgr.names(new_id).volume
    assert in_volume(mgr, new_id, new_volume, ["cat", "/data/local/tmp/keep.txt"]) == "kept-across-restarts"
    owner = ["stat", "-c", "%u:%g %a", "/data", "/data/local/tmp", "/data/system"]
    assert in_volume(mgr, new_id, new_volume, owner) == in_volume(mgr, 1, names.volume, owner)
    # the restored Android boots with the data and identity of the original (the original stays stopped)
    booted = mgr.start(new_id)
    assert booted["state"] == "booted"
    assert mgr.adb(new_id).shell(["cat", "/data/local/tmp/keep.txt"]) == "kept-across-restarts"
    assert mgr.adb(new_id).setting_get("secure", "android_id") == spec.device.android_id
    assert "com.android.egg" in mgr.adb(new_id).shell("pm list packages -d")
    with pytest.raises(Exception, match="replaces everything that Android holds now"):
        backups.restore(archive, new_id)                   # over an existing Android: only with force
    with pytest.raises(Exception, match="never restored over a running Android"):
        backups.restore(archive, new_id, force=True)       # and never while it runs
    mgr.stop(new_id)

    # ---- clone with data: a third volume with the same content, a new identity
    clone = mgr.clone(1, with_data=True)
    assert clone.device.android_id != spec.device.android_id
    assert in_volume(mgr, clone.id, mgr.names(clone.id).volume, ["cat", "/data/local/tmp/keep.txt"]) == "kept-across-restarts"

    # ---- delete: containers, volumes and settings go; nothing with the test prefix is left
    for i in (clone.id, new_id, 1):
        mgr.delete(i)
    assert mgr.store.ids() == []
    assert docker.ps(all=True, labels={"io.superfarm.farm": PREFIX}) == []
    assert docker.volumes(PREFIX + "-data-") == []


def test_image_build_with_overlay(farm: InstanceManager):
    """`COPY overlay/ /` really lands files in the Android image, with their modes."""
    mgr, docker = farm, farm.docker
    target = mgr.images.overlay_dir(BUILD_VERSION) / "system" / "etc" / "sftest-marker.txt"
    target.parent.mkdir(parents=True)
    target.write_text("overlay works\n")
    os.chmod(target, 0o640)
    assert mgr.images.ensure(BUILD_VERSION, pull=False) == BUILT_TAG
    row = next(r for r in mgr.images.status() if r["version"] == BUILD_VERSION)
    assert row["built"] and not row["stale"] and row["overlays"] == ["system/etc/sftest-marker.txt"]
    name = f"{PREFIX}-android-9999"
    try:
        docker.run(["create", "--name", name, "--label", f"io.superfarm.farm={PREFIX}", BUILT_TAG])
        listing = subprocess.run([docker.binary, "cp", f"{name}:/system/etc/sftest-marker.txt", "-"],
                                 capture_output=True, timeout=60, check=True).stdout
        assert b"overlay works" in listing
    finally:
        docker.rm(name)
    built_before = docker.image_id(BUILT_TAG)
    assert mgr.images.ensure(BUILD_VERSION, pull=False) == BUILT_TAG and docker.image_id(BUILT_TAG) == built_before

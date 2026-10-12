"""End to end: the instance manager with the *real* host (GPU, relay, capacity) and network
(sidecar) modules, against real Android containers.

    SF_LIVE=1 python -m pytest -o addopts="" tests/live/test_e2e_farm.py -v -s

Scenarios, one Android at a time (never two running together):

    software + direct   create -> start -> ADB -> outbound internet -> stop -> delete
    network none        boots, ADB works, nothing leaves
    overrides           cpus / memory / cpuset / fps visible in `docker inspect` and in Android
    capacity            over capacity warns; with enforce_capacity it refuses; force overrides;
                        a restart is never refused after the Android was stopped
    version guard       another Android version on existing data is refused before anything changes
    backup              marker file -> backup -> restore as new (offline twin) -> delete -> boot the copy
    integrated GPU      boots on Mesa, not SwiftShader; screenshot is not black
    renderer change     software -> integrated GPU rebuilds the pair and clears shader caches
    NVIDIA              through a relay this test starts on its own socket and stops again

Needs Docker, the binder module, internet access (to build two small images once) and a local
Android image (SF_LIVE_IMAGE, default redroid:13-noflag). The GPU scenarios skip themselves
on a machine without that hardware. The NVIDIA scenario also needs a built relay and runtime:

    SF_LIVE_VENUS_RELAY    install prefix of the patched relay (bin/, lib/, libexec/)
    SF_LIVE_VENUS_RUNTIME  folder with vulkan.virtio.so, gralloc.venus.so, gralloc.venus.x86.so

Everything created carries the test prefix (containers, volumes, the helper image) and is
removed at the end. A relay that was already running on the machine is never touched: the
test's own relay has its own socket inside the test's farm home and is stopped by the
process id it was started with.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from superfarm.config import Config, update_config
from superfarm.core.adb import Adb, AdbError
from superfarm.core.docker import Docker
from superfarm.core.instances import InstanceManager, NeedsForce, OverCapacity, mem_available_mb
from superfarm.core.store import Store
from superfarm.core.tasks import TaskManager
from superfarm.host import gpu as host_gpu
from superfarm.host import venus
from superfarm.paths import Paths
from superfarm.resources import resource_path
from superfarm.util import proc

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("SF_LIVE") != "1", reason="set SF_LIVE=1 to run live tests")]

PREFIX = "sfte2e"
PORT_BASE = 17600
IMAGE = os.environ.get("SF_LIVE_IMAGE", "redroid:13-noflag")
HELPER_BASE = os.environ.get("SF_LIVE_HELPER", "alpine:latest")
TOOLS_TAG = "sftest-e2e-tools:1"
VENUS_RELAY = os.environ.get("SF_LIVE_VENUS_RELAY", "")
VENUS_RUNTIME = os.environ.get("SF_LIVE_VENUS_RUNTIME", "")
# The default test image has a patched compositor that takes its frame rate from this property
# (set to 25 in its /vendor/build.prop) and ignores the standard boot argument.
FPS_PROPERTY = os.environ.get("SF_LIVE_FPS_PROPERTY", "ro.farm.redroid_fps" if IMAGE == "redroid:13-noflag" else "")
INTERNET_IP = "1.1.1.1"                 # a public address that answers ping; no name lookup involved


# ============================================================================ plumbing

def cleanup(docker: Docker) -> None:
    """Remove what this test can have created, and nothing else: selection is by the test
    prefix in the farm label *and* in the name."""
    rows = docker.ps(all=True, labels={"io.superfarm.farm": PREFIX})
    docker.rm(*[c["name"] for c in rows if c["name"].startswith(PREFIX + "-")])
    for volume in docker.volumes(PREFIX + "-data-"):
        docker.volume_rm(volume)


class Live:
    """One throwaway farm: its own home, the real modules, the test prefix."""

    def __init__(self, docker: Docker, config: dict | None = None):
        # A short path: the relay's unix socket lives under it, and socket paths are limited to 107 bytes.
        self.home = Path(tempfile.mkdtemp(prefix="sfte2e-"))
        self.docker = docker
        base = {
            "docker": {"name_prefix": PREFIX},
            "network": {"adb_port_base": PORT_BASE},
            "android": {"images": {"13": {"base": IMAGE, "tag": IMAGE, "fps_property": FPS_PROPERTY}}},
            "device": {"profile": "samsung-s23"},
            "performance": {"boot_timeout": 300},
        }
        self.paths = Paths(self.home).ensure()
        self.tasks = TaskManager(workers=2)
        self.mgr = InstanceManager(update_config(Config.model_validate(base), config or {}), self.paths, docker,
                                   Store(self.paths), self.tasks)
        self.mgr.images.tools_tag, self.mgr.images.tools_base = TOOLS_TAG, HELPER_BASE

    def reconfigure(self, patch: dict) -> None:
        self.mgr.reload_config(update_config(self.mgr.config, patch))

    def close(self) -> None:
        self.tasks.shutdown()
        for i in range(1, 10):
            try:
                Adb(f"127.0.0.1:{PORT_BASE + i}").disconnect()
            except Exception:  # noqa: BLE001 - best effort
                pass
        cleanup(self.docker)
        shutil.rmtree(self.home, ignore_errors=True)


@pytest.fixture(scope="module")
def docker():
    d = Docker()
    ok, why = d.available()
    if not ok:
        pytest.skip(f"Docker is not usable: {why}")
    for image in (IMAGE, HELPER_BASE):
        if not d.image_exists(image):
            pytest.skip(f"image {image} is not on this machine (set SF_LIVE_IMAGE / SF_LIVE_HELPER)")
    cleanup(d)
    yield d
    cleanup(d)
    if d.image_exists(TOOLS_TAG):
        d.run(["image", "rm", TOOLS_TAG], check=False)


@pytest.fixture
def live(docker):
    if 0 <= mem_available_mb() < 4000:
        pytest.skip("less than 4 GB of RAM available; not adding an Android to this machine now")
    made: list[Live] = []

    def make(config: dict | None = None) -> Live:
        farm = Live(docker, config)
        made.append(farm)
        return farm

    try:
        yield make
    finally:
        for farm in made:
            farm.close()


def report(scenario: str, **facts) -> None:
    """Measured facts, printed for whoever runs the test with -s (boot times, renderer strings)."""
    print(f"\nE2E {scenario}: " + json.dumps(facts, ensure_ascii=False))


def inspect(docker: Docker, name: str) -> dict:
    data = docker.inspect(name)
    assert data is not None, f"container {name} does not exist"
    return data


def as_root(farm: Live, id: int, argv: list[str], *, timeout: float = 30, check: bool = True) -> proc.Result:
    """Run a command as root inside the Android (ADB's shell is an unprivileged user)."""
    return farm.docker.exec(farm.mgr.names(id).android, argv, timeout=timeout, check=check)


def reaches_internet(farm: Live, id: int) -> bool:
    """One ICMP echo to a public address from inside the Android."""
    return as_root(farm, id, ["ping", "-c", "1", "-W", "4", INTERNET_IP], timeout=20, check=False).ok


def wait_for(what, timeout: float = 60, every: float = 2.0):
    deadline = time.monotonic() + timeout
    while True:
        value = what()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(every)


def renderer(adb: Adb) -> str:
    """The `GLES:` line of SurfaceFlinger: vendor, renderer, version of what composes the screen.
    The whole dump is read here, never piped into something that closes early on the device."""
    def read() -> str:
        try:
            dump = adb.shell("dumpsys SurfaceFlinger", timeout=30, check=False)
        except AdbError:
            return ""
        return next((line.strip()[len("GLES:"):].strip() for line in dump.splitlines() if line.strip().startswith("GLES:")), "")
    return wait_for(read, timeout=60) or ""


def brightest_pixel(adb: Adb) -> int:
    """0 for an all-black screenshot, up to 255. Waits for the launcher to draw its first frame."""
    from PIL import Image

    def look() -> int:
        try:
            image = Image.open(io.BytesIO(adb.screencap_png())).convert("RGB")
        except (AdbError, OSError):
            return 0
        return max(high for _, high in image.getextrema())
    return wait_for(look, timeout=90, every=3.0) or 0


def fatal_signals(adb: Adb) -> dict[str, int]:
    """Native crashes since boot, from the crash log: process name -> how many."""
    log = adb.shell("logcat -b crash -d", timeout=30, check=False)
    out: dict[str, int] = {}
    for line in log.splitlines():
        if "Fatal signal" in line:
            name = line.rsplit("(", 1)[-1].rstrip(") \r")
            out[name] = out.get(name, 0) + 1
    return out


def not_bluetooth(crashed: dict[str, int]) -> dict[str, int]:
    """The test image's Bluetooth HAL aborts in a loop at first boot in every rendering mode
    (there is no Bluetooth hardware; the farm disables the package right after that boot).
    It says nothing about graphics, so the GPU scenarios look at everything else."""
    return {name: n for name, n in crashed.items() if "bluetooth" not in name}


# Everything tar can see of a volume, as one number: file contents, numeric owners, modes,
# modification times, ACLs and extended attributes, in a fixed order. Access and change times
# and the process id GNU tar puts into its header names are left out, as they differ per run.
_FINGERPRINT = (
    "cd /data && tar --xattrs --xattrs-include='*' --acls --numeric-owner --sort=name --warning=no-file-ignored "
    "--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime -cf - . | sha256sum | cut -d' ' -f1; "
    "tar --xattrs --xattrs-include='*' --warning=no-file-ignored -cf - . | tar --xattrs -tvvf - | grep -c '^  x: '"
)


def fingerprint(farm: Live, id: int) -> tuple[str, int]:
    """(digest of a stopped Android's whole /data, number of extended attributes in it)."""
    volume = farm.mgr.volume_of(farm.mgr.store.get(id))
    digest, count = farm.mgr._helper_run(id, ["sh", "-c", _FINGERPRINT], gnu_tar=True, timeout=600,
                                         mounts=[f"{volume}:/data:ro"]).split()
    return digest, int(count)


def integrated_gpu():
    cards = [g for g in host_gpu.detect_gpus() if getattr(g, "integrated", False) and g.usable_modes]
    if not cards:
        pytest.skip("no integrated GPU with a Mesa driver on this machine")
    if not os.access(cards[0].node, os.R_OK | os.W_OK):
        pytest.skip(f"no access to {cards[0].node} for this user")
    return cards[0]


# ============================================================================ scenarios

def test_software_rendering_direct_network(live):
    """(a) The default path: CPU rendering, the host's own connection."""
    farm = live({"gpu": {"mode": "software"}, "network": {"mode": "direct"}})
    mgr, docker = farm.mgr, farm.docker
    names = mgr.names(1)
    spec = mgr.create(name="e2e software")[0]
    assert spec.id == 1 and mgr.status(1)["state"] == "missing"

    result = mgr.start(1)
    assert result["state"] == "booted" and result["recreated"] and result["gpu"] == "software"
    assert not [w for w in result["warnings"] if not w.startswith("over capacity")], result["warnings"]
    assert docker.running(names.net) and docker.running(names.android)
    # the pair is exactly what the modules planned: Android inside the sidecar's namespace, one plan
    android, net = inspect(docker, names.android), inspect(docker, names.net)
    assert android["HostConfig"]["NetworkMode"] == f"container:{net['Id']}"
    assert android["Config"]["Labels"]["io.superfarm.plan"] == net["Config"]["Labels"]["io.superfarm.plan"] != ""
    assert net["Config"]["Image"].startswith("superfarm/net:")
    assert net["HostConfig"]["PortBindings"]["5555/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": str(PORT_BASE + 1)}]
    assert "androidboot.redroid_gpu_mode=guest" in android["Config"]["Cmd"]

    adb = mgr.adb(1)
    assert adb.shell(["echo", "hello from", "the farm"]) == "hello from the farm"
    assert adb.getprop("sys.boot_completed") == "1"
    assert adb.getprop("ro.product.model") == "SM-S911B" and adb.getprop("ro.serialno") == spec.device.serial
    gles = renderer(adb)
    assert "SwiftShader" in gles, gles
    assert brightest_pixel(adb) > 16
    assert wait_for(lambda: reaches_internet(farm, 1), timeout=30), "the Android has no outbound connectivity"

    status = mgr.status(1)
    assert status["state"] == "booted" and status["gpu"]["mode"] == "software" and status["net"]["mode"] == "direct"
    assert status["net"]["state"] == "direct" and status["net"]["ready"] and status["data_android_version"] == "13"
    assert mgr.capacity()["running"] == 1
    report("software+direct", boot_seconds=result["boot_seconds"], renderer=gles, gpu_reason=result["gpu_reason"],
           fatal_signals=fatal_signals(adb))

    mgr.stop(1)
    assert not docker.running(names.android) and not docker.running(names.net)
    assert mgr.status(1)["state"] == "stopped"
    farm.reconfigure({"backup": {"auto_before_risky": False}})          # backups have their own scenario
    assert mgr.delete(1)["data"] == "removed"
    assert mgr.store.ids() == [] and docker.ps(all=True, labels={"io.superfarm.farm": PREFIX}) == []
    assert docker.volumes(PREFIX + "-data-") == []


def test_network_mode_none_has_no_way_out(live):
    """(d) `none`: ADB is still published, but nothing leaves the Android."""
    farm = live({"gpu": {"mode": "software"}, "backup": {"auto_before_risky": False}})
    mgr = farm.mgr
    mgr.create(template={"network": {"mode": "none"}})
    result = mgr.start(1)
    assert result["state"] == "booted"
    adb = mgr.adb(1)
    assert adb.shell(["echo", "still reachable"]) == "still reachable"
    assert not reaches_internet(farm, 1)
    assert adb.shell("ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; echo $?") != "0"
    status = mgr.status(1)
    assert status["net"]["mode"] == "none" and status["net"]["state"] == "isolated", status["net"]
    assert status["net"]["killswitch"]["active"] is True, status["net"]
    report("network none", boot_seconds=result["boot_seconds"], net_state=status["net"]["state"])
    mgr.delete(1)


def test_per_instance_overrides_reach_docker_and_android(live):
    """(e) cpus, memory, cpuset, screen and fps of one Android, as Docker and Android see them.

    The frame rate is passed as `androidboot.redroid_fps`, which stock images read, and under
    the image's own property when android.images.<version>.fps_property names one."""
    farm = live({"gpu": {"mode": "software"}, "network": {"mode": "none"}, "backup": {"auto_before_risky": False}})
    mgr = farm.mgr
    mgr.create(template={"fps": 15, "width": 360, "height": 640, "dpi": 120,
                         "resources": {"cpus": 1.5, "memory_mb": 1280, "memory_swap_mb": 1536, "cpuset": "0-1"}})
    result = mgr.start(1)
    assert result["state"] == "booted"
    android = inspect(farm.docker, mgr.names(1).android)
    host = android["HostConfig"]
    assert host["NanoCpus"] == 1_500_000_000
    assert host["Memory"] == 1280 * 1024 * 1024 and host["MemorySwap"] == 1536 * 1024 * 1024
    assert host["CpusetCpus"] == "0-1"
    cmd = android["Config"]["Cmd"]
    for arg in ("androidboot.redroid_fps=15", "androidboot.redroid_width=360", "androidboot.redroid_height=640",
                "androidboot.redroid_dpi=120", *([f"{FPS_PROPERTY}=15"] if FPS_PROPERTY else [])):
        assert arg in cmd, (arg, cmd)
    assert android["Config"]["Labels"]["io.superfarm.cpuset"] == "0-1"

    adb = mgr.adb(1)
    assert adb.size() == (360, 640)
    assert adb.getprop("ro.boot.redroid_fps") == "15"
    assert as_root(farm, 1, ["nproc"]).out == "2"                             # the two pinned cores are all it sees
    # what the compositor really runs at, not what it was told
    dump = adb.shell("dumpsys SurfaceFlinger", timeout=30, check=False)
    rate = next((line.split(":", 1)[1].strip() for line in dump.splitlines() if line.strip().startswith("refresh-rate")), "")
    assert rate.startswith("15."), rate
    report("overrides", boot_seconds=result["boot_seconds"], refresh_rate=rate, fps_property=FPS_PROPERTY)
    mgr.delete(1)


def test_capacity_warns_by_default_and_refuses_only_when_enforced(live):
    """(f) Over capacity with the real estimate of this machine: warn, refuse, force."""
    # A guard no machine can satisfy makes "over capacity" certain without loading the host.
    farm = live({"gpu": {"mode": "software"}, "network": {"mode": "none"}, "backup": {"auto_before_risky": False},
                 "performance": {"memory_guard_mb": 10_000_000}})
    mgr = farm.mgr
    mgr.create()
    capacity = mgr.capacity()
    assert capacity["over"] and capacity["enforced"] is False and capacity["running"] == 0
    estimate = capacity["estimate"]
    assert estimate["max_instances"] >= 1 and estimate["binding"] in ("ram", "cpu", "vram"), estimate
    assert capacity["headroom"] == estimate["max_instances"] and capacity["memory_available_mb"] > 0

    warned = mgr.start(1)                                    # default: the operator may run more than advised
    assert warned["state"] == "booted"
    assert any(w.startswith("over capacity:") and "performance.enforce_capacity is off" in w for w in warned["warnings"])
    assert mgr.status(1)["warnings"] == warned["warnings"]
    mgr.stop(1)

    farm.reconfigure({"performance": {"enforce_capacity": True}})
    with pytest.raises(OverCapacity) as refused:
        mgr.start(1)
    assert refused.value.capacity["over"] and refused.value.capacity["estimate"]["max_instances"] == estimate["max_instances"]
    assert not farm.docker.running(mgr.names(1).android)

    forced = mgr.start(1, force=True)
    assert forced["state"] == "booted" and forced["recreated"] is False
    assert any("because the start was forced" in w for w in forced["warnings"])
    report("capacity", estimate_max=estimate["max_instances"], binding=estimate["binding"], mode=estimate["mode"],
           first_boot_seconds=warned["boot_seconds"], reused_boot_seconds=forced["boot_seconds"])
    mgr.delete(1)


def test_enforced_capacity_never_takes_a_running_android_away(live):
    """(f) The defect this guards against: with enforce_capacity on and no force, a restart
    stopped the Android and then refused to start it. A refusal has to come before anything
    is stopped, for one Android and for a bulk restart or recreate alike."""
    farm = live({"gpu": {"mode": "software"}, "network": {"mode": "none"}, "backup": {"auto_before_risky": False}})
    mgr, docker = farm.mgr, farm.docker
    names = mgr.names(1)
    mgr.create()
    first = mgr.start(1)
    # From now on the machine counts as full, and the estimate is enforced.
    farm.reconfigure({"performance": {"enforce_capacity": True, "memory_guard_mb": 10_000_000}})
    assert mgr.capacity()["over"] and mgr.capacity()["enforced"]

    def started_at(name: str) -> str:
        return inspect(docker, name)["State"]["StartedAt"]

    def container_ids() -> dict[str, str]:
        return {n: inspect(docker, n)["Id"] for n in (names.android, names.net)}

    ids, began = container_ids(), started_at(names.android)

    # running: it is already counted, so restarting it is no capacity question and it comes back
    restarted = mgr.restart(1)
    assert restarted["state"] == "booted" and restarted["recreated"] is False and restarted["warnings"] == [], restarted
    assert docker.running(names.android) and container_ids() == ids and started_at(names.android) != began

    task = farm.tasks.wait(mgr.bulk("restart", "all"), timeout=600)
    assert task.state == "done" and task.results[1]["ok"], task.results
    assert docker.running(names.android) and mgr.status(1)["state"] == "booted"

    task = farm.tasks.wait(mgr.bulk("recreate", "all"), timeout=600)
    assert task.results[1]["ok"], task.results
    assert docker.running(names.android) and container_ids() != ids

    # stopped: starting it would add an Android, so it is refused, with nothing touched
    mgr.stop(1)
    ids = container_ids()
    stopped_state = {n: inspect(docker, n)["State"]["FinishedAt"] for n in ids}
    for action in (mgr.restart, mgr.recreate):
        with pytest.raises(OverCapacity):
            action(1)
    task = farm.tasks.wait(mgr.bulk("restart", "all"), timeout=600)
    assert not task.results[1]["ok"] and "OverCapacity" in task.results[1]["detail"], task.results
    assert container_ids() == ids and {n: inspect(docker, n)["State"]["FinishedAt"] for n in ids} == stopped_state
    assert not docker.running(names.android) and not docker.running(names.net)
    assert "OverCapacity" in mgr.status(1)["last_error"]

    task = farm.tasks.wait(mgr.bulk("restart", "all", force=True), timeout=600)
    assert task.results[1]["ok"] and "over capacity" in task.results[1]["detail"], task.results
    assert docker.running(names.android) and mgr.adb(1).shell(["echo", "back"]) == "back"
    report("capacity restart", first_boot_seconds=first["boot_seconds"], restart_seconds=restarted["boot_seconds"])
    mgr.delete(1)


def test_android_version_guard_on_existing_data(live):
    """(i) Two Androids of different versions need a second image, which this test does not
    fetch. What is tested live is the guard: once an Android has booted, another version is
    refused, at update and again at start, before any image is looked for or anything stops."""
    absent = {v: {"base": f"sftest-e2e-absent:{v}", "tag": f"sftest-e2e-absent:{v}"} for v in ("12", "14")}
    farm = live({"gpu": {"mode": "software"}, "network": {"mode": "none"}, "backup": {"auto_before_risky": False},
                 "android": {"images": absent}})
    mgr, docker = farm.mgr, farm.docker
    names = mgr.names(1)
    assert [row["version"] for row in mgr.images.status()] == ["12", "13", "14"]
    assert {row["version"]: row["features"]["nvidia"]["supported"] for row in mgr.images.status()} == {
        "12": False, "13": True, "14": False}
    mgr.create(template={"android_version": "13"})
    assert mgr.status(1)["data_android_version"] == ""
    first = mgr.start(1)
    assert mgr.status(1)["data_android_version"] == "13"
    marker = "/data/local/tmp/sfte2e-version.txt"
    mgr.adb(1).shell(["sh", "-c", f"echo kept > {marker}"])
    container = inspect(docker, names.android)["Id"]

    for other in ("12", "14"):
        with pytest.raises(NeedsForce, match="written by Android 13"):
            mgr.update(1, {"android_version": other})
    assert mgr.store.get(1).android_version == "13"

    # forced at update: saved, and the running Android is not touched by saving
    assert mgr.update(1, {"android_version": "12"}, force=True).android_version == "12"
    assert docker.running(names.android)
    # a downgrade is refused again when it would take effect, and the Android keeps running
    for action in (mgr.restart, mgr.recreate):
        with pytest.raises(NeedsForce, match="written by Android 13 and it is now set to Android 12"):
            action(1)
    assert docker.running(names.android) and inspect(docker, names.android)["Id"] == container
    assert mgr.adb(1).shell(["cat", marker]) == "kept"
    status = mgr.status(1)
    assert status["android_version"] == "12" and status["data_android_version"] == "13" and "NeedsForce" in status["last_error"]
    assert not any(docker.image_exists(entry["tag"]) for entry in absent.values())       # nothing was fetched

    # going back to the version that wrote the data needs no force, and the data is as it was
    assert mgr.update(1, {"android_version": "13"}).android_version == "13"
    again = mgr.restart(1)
    assert again["state"] == "booted" and again["recreated"] is False and again["warnings"] == [], again
    assert mgr.adb(1).shell(["cat", marker]) == "kept"
    report("version guard", first_boot_seconds=first["boot_seconds"], restart_seconds=again["boot_seconds"])
    mgr.delete(1)


def test_backup_delete_restore_round_trip(live):
    """(g) A marker file survives backup -> restore as a new Android, with owners, modes and
    extended attributes intact; a copy next to its original is created offline; deleting an
    Android leaves a safety backup behind."""
    farm = live({"gpu": {"mode": "software"}, "network": {"mode": "none"}})
    mgr, docker, backups = farm.mgr, farm.docker, farm.mgr.backups
    spec = mgr.create(name="e2e backup")[0]
    first = mgr.start(1)
    adb = mgr.adb(1)
    marker = "/data/local/tmp/sfte2e-marker.txt"
    adb.shell(["sh", "-c", f"echo round-trip-{spec.device.android_id} > {marker}"])
    samples = [marker, "/data/system/packages.xml", "/data/misc", "/data/user_de/0/com.android.systemui"]

    def owners(id: int) -> str:
        return as_root(farm, id, ["stat", "-c", "%n %u:%g %a", *samples]).out

    before = owners(1)
    assert len(before.splitlines()) == len(samples), before
    mgr.stop(1)
    original = fingerprint(farm, 1)
    assert original[1] > 100, original                       # Android's installer tags every app folder with xattrs

    folder = backups.create(1)
    manifest = backups.manifest(folder)
    assert manifest["consistent"] and manifest["android_version"] == "13" and manifest["reason"] == "manual"
    assert manifest["files"][manifest["data"]]["size"] > 1_000_000
    assert backups.verify(folder, deep=True)

    # a copy next to its original: same identity, so it is created with networking off
    mgr.update(1, {"network": {"mode": "direct"}})
    copy_id = backups.restore(folder)
    copy = mgr.store.get(copy_id)
    assert copy_id == 2 and copy.device == spec.device and copy.network.mode == "none"
    assert "Android 1 has the same identity" in copy.notes
    # the restored volume is the original, down to owners, modes, times and extended attributes
    assert fingerprint(farm, copy_id) == original

    # deleting the original takes a safety backup first and says where it is
    deleted = mgr.delete(1)
    assert deleted["data"] == "removed" and deleted["backup"] and deleted["warnings"] == []
    assert backups.verify(deleted["backup"]) and backups.manifest(deleted["backup"])["reason"] == "before-delete"
    assert not docker.volume_exists(mgr.names(1).volume)

    second = mgr.start(copy_id)
    assert second["state"] == "booted"
    copy_adb = mgr.adb(copy_id)
    assert copy_adb.shell(["cat", marker]) == f"round-trip-{spec.device.android_id}"
    assert copy_adb.setting_get("secure", "android_id") == spec.device.android_id
    assert owners(copy_id) == before                         # and Android itself sees the same owners and modes
    assert not reaches_internet(farm, copy_id)               # offline, as created
    size_mb = manifest["files"][manifest["data"]]["size"] // 1048576
    report("backup round trip", first_boot_seconds=first["boot_seconds"], restored_boot_seconds=second["boot_seconds"],
           archive_mb=size_mb, compression=manifest["compression"], extended_attributes=original[1])
    farm.reconfigure({"backup": {"auto_before_risky": False}})
    mgr.delete(copy_id)

    # backup -> delete -> restore: with the original and its copy both gone, the same backup
    # comes back as a new Android, and this time nothing turns its network off
    assert mgr.store.ids() == [] and docker.volumes(PREFIX + "-data-") == []
    again_id = backups.restore(folder)
    again = mgr.store.get(again_id)
    assert again.device == spec.device and again.network.mode is None and "same identity" not in again.notes
    assert fingerprint(farm, again_id) == original
    third = mgr.start(again_id)
    assert third["state"] == "booted"
    assert mgr.adb(again_id).shell(["cat", marker]) == f"round-trip-{spec.device.android_id}"
    assert owners(again_id) == before
    report("backup after delete", restored_boot_seconds=third["boot_seconds"], restored_as=again_id)
    mgr.delete(again_id)
    assert [b["reason"] for b in backups.list()] == ["before-delete", "manual"]


def test_integrated_gpu_renders_with_mesa(live):
    """(b) `gpu_mode: igpu`: the integrated GPU through Mesa, not SwiftShader on the CPU."""
    card = integrated_gpu()
    farm = live({"network": {"mode": "none"}, "backup": {"auto_before_risky": False}})
    mgr = farm.mgr
    mgr.create(template={"gpu_mode": "igpu"})
    result = mgr.start(1)
    assert result["state"] == "booted" and result["gpu"] == card.vendor and result["gpu_node"] == card.node
    android = inspect(farm.docker, mgr.names(1).android)
    assert "androidboot.redroid_gpu_mode=host" in android["Config"]["Cmd"]
    assert f"androidboot.redroid_gpu_node={card.node}" in android["Config"]["Cmd"]
    assert any(d["PathOnHost"] == card.node for d in android["HostConfig"]["Devices"] or [])
    adb = mgr.adb(1)
    gles = renderer(adb)
    assert gles and "SwiftShader" not in gles, gles
    assert "mesa" in gles.lower() or card.vendor in gles.lower(), gles
    assert brightest_pixel(adb) > 16
    crashed = fatal_signals(adb)
    # Known on Mesa's host mode: the graphics allocator service aborts once while Android
    # boots and is restarted. Anything else crashing would be news.
    assert set(not_bluetooth(crashed)) <= {"allocator@2.0-s"}, crashed
    report("integrated GPU", boot_seconds=result["boot_seconds"], renderer=gles, card=card.name,
           gpu_reason=result["gpu_reason"], fatal_signals=crashed)
    mgr.delete(1)


def test_changing_the_renderer_rebuilds_the_pair_and_clears_shader_caches(live):
    """(h) software -> integrated GPU on the same data: new containers, stale caches gone."""
    card = integrated_gpu()
    farm = live({"network": {"mode": "none"}, "backup": {"auto_before_risky": False}})
    mgr, docker = farm.mgr, farm.docker
    names = mgr.names(1)
    mgr.create(template={"gpu_mode": "software"})
    first = mgr.start(1)
    adb = mgr.adb(1)
    before = renderer(adb)
    assert "SwiftShader" in before, before
    # Real caches exist after a boot; a canary with a cache's name proves what the clearing
    # removes, and a neighbour proves what it leaves alone. (Android writes fresh caches as
    # soon as it draws again, so the real ones cannot be used to tell.)
    cache_dir = "/data/user_de/0/com.android.systemui/code_cache"
    assert wait_for(lambda: as_root(farm, 1, ["find", "/data/user_de", "-name", "*.shaders_cache"]).out, timeout=60)
    as_root(farm, 1, ["sh", "-c", f"mkdir -p {cache_dir} && echo stale > {cache_dir}/com.android.sfte2e.shaders_cache "
                                  f"&& echo keep > {cache_dir}/sfte2e-keep.txt"])
    ids = {n: inspect(docker, n)["Id"] for n in (names.android, names.net)}
    plan = docker.label(names.android, "io.superfarm.plan")
    assert mgr.store.state_get(1, "renderer") == "software"

    mgr.update(1, {"gpu_mode": "igpu"})
    second = mgr.restart(1)
    assert second["state"] == "booted" and second["recreated"] is True and second["gpu"] == card.vendor
    assert inspect(docker, names.android)["Id"] != ids[names.android]
    assert inspect(docker, names.net)["Id"] != ids[names.net]              # the pair, not the Android alone
    assert docker.label(names.android, "io.superfarm.plan") != plan
    assert docker.label(names.android, "io.superfarm.gpu") == card.vendor
    assert mgr.store.state_get(1, "renderer") == card.vendor
    assert as_root(farm, 1, ["cat", "/data/.superfarm_renderer"]).out == card.vendor
    assert not as_root(farm, 1, ["ls", f"{cache_dir}/com.android.sfte2e.shaders_cache"], check=False).ok
    assert as_root(farm, 1, ["cat", f"{cache_dir}/sfte2e-keep.txt"]).out == "keep"
    after = renderer(mgr.adb(1))
    assert after and "SwiftShader" not in after, after
    report("renderer change", software_boot_seconds=first["boot_seconds"], gpu_boot_seconds=second["boot_seconds"],
           before=before, after=after)
    mgr.delete(1)


def make_runtime(target: Path) -> Path:
    """A Venus runtime folder as native/venus/build.sh lays it out: the three libraries from
    SF_LIVE_VENUS_RUNTIME (copied, the source is only read) and this project's gpu_config.sh."""
    target.mkdir(parents=True)
    for name in venus.RUNTIME_FILES:
        source = Path(VENUS_RUNTIME) / name
        if name == "gpu_config.sh" and not source.is_file():
            source = resource_path("native", "venus", "gpu_config_venus.sh")
        shutil.copyfile(source, target / name)
        os.chmod(target / name, 0o755)
    return target


def test_nvidia_through_a_relay_of_its_own(live):
    """(c) NVIDIA rendering through the relay, without going near a relay that is already
    serving other Androids on this machine."""
    if not VENUS_RELAY or not VENUS_RUNTIME:
        pytest.skip("set SF_LIVE_VENUS_RELAY and SF_LIVE_VENUS_RUNTIME to a built relay and runtime")
    cards = [g for g in host_gpu.detect_gpus() if "nvidia" in g.usable_modes]
    if not cards:
        pytest.skip("no NVIDIA card with NVIDIA's driver on this machine")
    others = {p["pid"]: p["socket"] for p in venus.find_relays()}          # not ours: must be untouched afterwards

    farm = live({"network": {"mode": "none"}, "backup": {"auto_before_risky": False}})
    sockets = farm.home / "venus"
    farm.reconfigure({"gpu": {"nvidia": {"relay_dir": VENUS_RELAY, "runtime_dir": str(make_runtime(farm.home / "venus-runtime")),
                                         "socket_dir": str(sockets)}}})
    mgr = farm.mgr
    mgr.create(template={"gpu_mode": "nvidia"})
    own_pid = 0
    try:
        result = mgr.start(1)
        relays = [r for r in venus.relay_status(mgr.config, nodes=[result["gpu_node"]]) if r["running"]]
        assert len(relays) == 1 and relays[0]["socket"].startswith(str(sockets)) and relays[0]["pid"] not in others
        own_pid = relays[0]["pid"]
        assert result["state"] == "booted" and result["gpu"] == "nvidia" and result["gpu_node"] == cards[0].node

        android = inspect(farm.docker, mgr.names(1).android)
        assert "androidboot.redroid_gpu_mode=venus" in android["Config"]["Cmd"]
        binds = android["HostConfig"]["Binds"]
        assert f"{sockets / Path(cards[0].node).name}:/venus" in binds
        assert all(any(b.endswith(f":{target}:ro") for b in binds) for target in venus.RUNTIME_FILES.values())

        adb = mgr.adb(1)
        gles = renderer(adb)
        assert "venus" in gles.lower() or "nvidia" in gles.lower(), gles
        assert "SwiftShader" not in gles, gles
        assert brightest_pixel(adb) > 16
        crashed = fatal_signals(adb)
        assert not_bluetooth(crashed) == {}, crashed
        report("NVIDIA relay", boot_seconds=result["boot_seconds"], renderer=gles, card=cards[0].name,
               gpu_reason=result["gpu_reason"], fatal_signals=crashed)
        mgr.delete(1)
    finally:
        cleanup(farm.docker)
        for relay in venus.find_relays():
            # Only the relay serving the socket inside this test's own farm home, by its pid.
            if relay["socket"].startswith(str(sockets)) and relay["pid"] not in others:
                proc.kill_tree(relay["pid"])
                try:
                    os.waitpid(relay["pid"], 0)
                except ChildProcessError:
                    pass
    assert own_pid and not any(p["pid"] == own_pid for p in venus.find_relays())
    assert {p["pid"]: p["socket"] for p in venus.find_relays()} == others       # every other relay is still there

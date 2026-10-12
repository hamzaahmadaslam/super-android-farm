"""Control against a real Android container. Opt-in: SF_LIVE=1.

    SF_LIVE=1 SF_LIVE_IMAGE=<local redroid image> python -m pytest tests/live/test_control_live.py

The tests use one throwaway Android. If nothing answers on SF_LIVE_ADB_PORT (default
17401) they start a container named SF_LIVE_CONTAINER (default sftest-ctl-android) from
SF_LIVE_IMAGE with `--pull never`, and remove it afterwards; a container that was already
running is used and left alone. Nothing else on the host is touched.

The app that gets installed is built here: a manifest written by the test's own binary-XML
writer, zipped, and signed (APK Signature Scheme v2) with a key made on the spot. It has
no code, which is enough for the package manager to install, split, grant and remove it.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from superfarm.config import Config
from superfarm.control import apps as apps_mod
from superfarm.control.apkinfo import parse_apk
from superfarm.control.apps import Apps
from superfarm.control.device import Device
from superfarm.control.files import Files
from superfarm.control.library import AppLibrary
from superfarm.control.shell import Shell
from superfarm.core.adb import Adb
from superfarm.core.docker import Docker
from superfarm.core.tasks import TaskManager
from superfarm.paths import get_paths

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("SF_LIVE") != "1", reason="live test: set SF_LIVE=1")]

PORT = int(os.environ.get("SF_LIVE_ADB_PORT", "17401"))
CONTAINER = os.environ.get("SF_LIVE_CONTAINER", "sftest-ctl-android")
IMAGE = os.environ.get("SF_LIVE_IMAGE", "redroid/redroid:13.0.0-latest")
PKG = "org.superfarm.livetest"


def load_support():
    name = "sf_control_support"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "unit" / "test_control_support.py"
        spec = importlib.util.spec_from_file_location(name, path)
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


S = load_support()


class OneAndroid:
    """The instance-manager interface, for a farm that consists of one container."""

    def __init__(self, adb: Adb):
        self.config = Config()
        self._adb = adb

    def adb(self, instance_id: int) -> Adb:
        return self._adb

    def select(self, selector: str) -> list[int]:
        return [1]

    def resolved(self, instance_id: int):
        return SimpleNamespace(names=SimpleNamespace(android=CONTAINER))


@pytest.fixture(scope="module")
def android():
    if not shutil.which("adb") or not shutil.which("docker"):
        pytest.skip("needs adb and docker")
    adb = Adb.for_port(PORT)
    created = False
    if not (adb.connect(attempts=1) and adb.booted()):
        run = subprocess.run(["docker", "run", "-d", "--pull", "never", "--privileged", "--name", CONTAINER, "--memory", "1536m",
                              "--cpus", "2", "-p", f"127.0.0.1:{PORT}:5555", IMAGE, "androidboot.redroid_width=480",
                              "androidboot.redroid_height=854", "androidboot.redroid_dpi=160"], capture_output=True, text=True, timeout=120)
        if run.returncode != 0:
            pytest.skip(f"could not start {IMAGE} (set SF_LIVE_IMAGE to a local Android image): {run.stderr.strip()[-200:]}")
        created = True
        if not adb.wait_boot(timeout=180):
            subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, timeout=120)
            pytest.fail("the test Android did not boot within 180 s")
    yield adb
    if created:
        adb.disconnect()
        subprocess.run(["docker", "rm", "-f", "-v", CONTAINER], capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def farm(android, tmp_path_factory):
    paths = get_paths(tmp_path_factory.mktemp("home")).ensure()
    mgr, tasks = OneAndroid(android), TaskManager(workers=4)
    library = AppLibrary(paths, mgr.config)
    yield SimpleNamespace(adb=android, mgr=mgr, tasks=tasks, paths=paths, library=library,
                          apps=Apps(mgr, library, tasks, docker=Docker()), files=Files(mgr, tasks, paths),
                          device=Device(mgr, tasks), shell=Shell(mgr, tasks))
    android.shell(["pm", "uninstall", PKG], check=False)
    tasks.shutdown()


def ok(farm, task, timeout=180) -> str:
    farm.tasks.wait(task, timeout=timeout)
    assert task.state == "done" and task.results[1]["ok"], (task.state, task.error, task.results)
    return task.results[1]["detail"]


def failed(farm, task, timeout=180) -> str:
    farm.tasks.wait(task, timeout=timeout)
    assert task.state == "done" and not task.results[1]["ok"], task.results
    return task.results[1]["detail"]


@pytest.fixture(scope="module")
def bundles(farm, tmp_path_factory):
    """Two versions of a split app as .xapk bundles, signed with one throwaway key."""
    if not shutil.which("openssl"):
        pytest.skip("needs the openssl command to sign the test app")
    work = tmp_path_factory.mktemp("build")
    key, cert = S.new_signing_key(work)
    perms = ("android.permission.CAMERA", "android.permission.RECORD_AUDIO", "android.permission.INTERNET",
             "android.permission.SYSTEM_ALERT_WINDOW")

    def signed(manifest: bytes, **kw) -> bytes:
        return S.sign_apk(S.make_apk(None, manifest, **kw), key, cert)

    def bundle(version: int) -> Path:
        def part(name, **kw):
            return signed(S.manifest(PKG, version, split=name), **kw)
        entries = {
            "base.apk": signed(S.manifest(PKG, version, f"{version}.0", label="Live Test", permissions=perms, launcher=None, has_code=False)),
            "config.x86_64.apk": part("config.x86_64", abis=("x86_64",)), "config.arm64_v8a.apk": part("config.arm64_v8a", abis=("arm64-v8a",)),
            "config.x86.apk": part("config.x86", abis=("x86",)), "config.armeabi_v7a.apk": part("config.armeabi_v7a", abis=("armeabi-v7a",)),
            "config.mdpi.apk": part("config.mdpi"), "config.xxhdpi.apk": part("config.xxhdpi"),
            "config.en.apk": part("config.en"), "config.fr.apk": part("config.fr"), "config.ja.apk": part("config.ja"),
            f"Android/obb/{PKG}/main.{version}.{PKG}.obb": os.urandom(4096),
            "manifest.json": b'{"name": "Live Test"}',
        }
        return S.make_bundle(work / f"livetest-{version}.xapk", entries)

    return {1: bundle(1), 2: bundle(2)}


# ----------------------------------------------------------------------------- reading the device

def test_parses_a_real_system_apk(farm):
    path = farm.adb.shell("pm path com.android.settings").splitlines()[0].removeprefix("package:")
    local = farm.files.pull(1, path, allow_any=True)
    info = parse_apk(local, use_aapt=False)
    assert info.package == "com.android.settings" and info.label and info.permissions and info.launcher.startswith("com.android.settings/")
    assert info.version_code == apps_mod.installed_version(farm.adb, "com.android.settings")
    assert info.target_sdk >= info.min_sdk >= 1
    shutil.rmtree(local.parent)


def test_info(farm):
    i = farm.device.info(1)
    sdk = int(farm.adb.getprop("ro.build.version.sdk"))
    assert i["sdk"] == sdk and i["model"] and i["abis"] and i["width"] > 0 and i["height"] > 0 and i["density"] > 0
    assert len(i["android_id"]) == 16 and i["uptime_seconds"] is not None and 0 <= i["uptime_seconds"] < 7 * 86400
    assert i["storage"]["total_mb"] > i["storage"]["free_mb"] > 0 and i["memory"]["used_mb"] > 50
    assert i["foreground"]["package"]
    assert farm.device.screenshot(1)[:8] == b"\x89PNG\r\n\x1a\n"
    assert farm.device.logcat(1, 20).count("\n") >= 5


def test_inventory_lists_system_packages(farm):
    everything = farm.apps.installed(1, third_party=False)
    names = {r["package"] for r in everything}
    assert {"android", "com.android.settings", "com.android.shell"} <= names and all(r["version"] for r in everything)
    assert all(not r["system"] for r in farm.apps.installed(1))


# ----------------------------------------------------------------------------- shell

def test_shell_bounds_and_exit_codes(farm):
    assert farm.shell.exec(1, ["sh", "-c", "echo out; echo err >&2; exit 7"])["code"] == 7
    quoted = farm.shell.exec(1, ["echo", "a b", "$(id)", "; reboot", "x'y"])
    assert quoted["code"] == 0 and quoted["output"] == "a b $(id) ; reboot x'y\n"
    flood = farm.shell.exec(1, ["cat", "/dev/zero"], max_output_kb=64)
    assert flood["truncated"] and flood["code"] is None and len(flood["output"]) == 64 * 1024
    slow = farm.shell.exec(1, ["sleep", "60"], timeout=2)
    assert slow["timed_out"] and slow["seconds"] < 10
    assert farm.shell.exec(1, ["id", "-u"], root=True)["output"].strip() == "0"
    task = farm.shell.run("all", ["getprop", "ro.build.version.sdk"])
    assert ok(farm, task) == farm.adb.getprop("ro.build.version.sdk") and farm.shell.outputs(task.id)[1]["code"] == 0
    assert "exit 3" in failed(farm, farm.shell.run("all", ["sh", "-c", "exit 3"]))


# ----------------------------------------------------------------------------- files

def test_push_list_pull_delete(farm, tmp_path):
    local = tmp_path / "report (final) $x.bin"
    local.write_bytes(os.urandom(70_000))
    remote = "/sdcard/Download/sf live/it's a file $(id).bin"
    assert ok(farm, farm.files.push("all", local, remote)) == f"saved as {remote}"
    entry = next(e for e in farm.files.list(1, "/sdcard/Download/sf live") if e["name"] == "it's a file $(id).bin")
    assert entry["type"] == "file" and entry["size"] == 70_000
    assert any(e["name"] == "Download" and e["type"] == "dir" for e in farm.files.list(1, "/sdcard"))
    back = farm.files.pull(1, remote)
    assert back.read_bytes() == local.read_bytes()
    shutil.rmtree(back.parent)
    assert ok(farm, farm.files.delete("all", remote)) == "deleted" and ok(farm, farm.files.delete("all", remote)) == "was not there"
    farm.adb.shell(["rmdir", "/sdcard/Download/sf live"], check=False)
    with pytest.raises(FileNotFoundError):
        farm.files.pull(1, remote)


def test_to_gallery_lands_in_the_media_index(farm, tmp_path):
    from PIL import Image
    picture = tmp_path / "gallery test.jpg"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(picture)
    assert ok(farm, farm.files.to_gallery("all", picture)) == "/sdcard/Pictures/gallery_test.jpg (in the media index)"
    ok(farm, farm.files.delete("all", "/sdcard/Pictures/gallery_test.jpg"))


# ----------------------------------------------------------------------------- apps

def test_split_app_lifecycle(farm, bundles):
    lib, apps, adb = farm.library, farm.apps, farm.adb
    v1, v2 = lib.add("livetest-1.xapk", bundles[1]), lib.add("livetest-2.xapk", bundles[2])
    assert v1["id"] == f"{PKG}-1" and len(v1["files"]) == 10 and v1["obb"][0]["size"] == 4096 and not v1["warnings"]
    adb.shell(["pm", "uninstall", PKG], check=False)
    traits = apps_mod.read_traits(adb)

    detail = ok(farm, apps.install("all", v1["id"], grant=False))
    assert detail.startswith("installed 1.0") and "1 OBB" in detail
    assert apps_mod.installed_version(adb, PKG) == 1
    on_device = sorted(Path(p.removeprefix("package:")).name for p in adb.shell(["pm", "path", PKG]).splitlines())
    abi_token = traits.abis[0].replace("-", "_")
    assert "base.apk" in on_device and f"split_config.{abi_token}.apk" in on_device      # the device's own CPU split...
    assert len([n for n in on_device if any(t in n for t in ("x86", "arm"))]) == 1         # ...and no other
    assert len([n for n in on_device if "dpi" in n]) == 1 and "split_config.ja.apk" not in on_device
    assert adb.shell(["stat", "-c", "%s", f"/sdcard/Android/obb/{PKG}/main.1.{PKG}.obb"]) == "4096"

    row = next(r for r in apps.installed(1) if r["package"] == PKG)
    assert (row["version"], row["version_code"], row["label"], row["enabled"], row["system"]) == ("1.0", 1, "Live Test", True, False)
    assert apps.matrix("all")[PKG] == {1: "1.0"}

    # idempotent rollout
    assert ok(farm, apps.ensure("all", v1["id"])) == "already installed (1.0)"
    assert ok(farm, apps.ensure("all", v2["id"])).startswith("updated from versionCode 1: installed 2.0")
    assert ok(farm, apps.ensure("all", v1["id"])) == "kept newer version (versionCode 2 > 1)"
    assert "INSTALL_FAILED_VERSION_DOWNGRADE" in failed(farm, apps.install("all", v1["id"]))

    # permissions
    assert apps.permissions(1, PKG)["dangerous"] == ["android.permission.CAMERA", "android.permission.RECORD_AUDIO"]
    assert ok(farm, apps.grant("all", PKG)) == "granted 2 runtime permissions"
    assert "android.permission.CAMERA: granted=true" in adb.shell(["dumpsys", "package", PKG])
    assert ok(farm, apps.revoke("all", PKG, "CAMERA")) == "revoked android.permission.CAMERA"
    assert "android.permission.CAMERA: granted=false" in adb.shell(["dumpsys", "package", PKG])
    assert ok(farm, apps.grant("all", PKG, "SYSTEM_ALERT_WINDOW")).endswith("(app-op)")
    assert "does not request" in failed(farm, apps.grant("all", PKG, "READ_CONTACTS"))

    # state
    assert ok(farm, apps.disable("all", PKG)) == "disabled-user"
    assert not next(r for r in apps.installed(1) if r["package"] == PKG)["enabled"]
    assert ok(farm, apps.enable("all", PKG)) == "enabled"
    assert ok(farm, apps.stop("all", PKG)) == "stopped" and ok(farm, apps.clear("all", PKG)) == "data cleared"
    assert "nothing the launcher can start" in failed(farm, apps.launch("all", PKG))     # the test app has no activity

    # app data backup and restore (advanced): owners must survive the round trip
    docker = Docker()
    data = f"/data/data/{PKG}"
    owner = docker.exec(CONTAINER, ["stat", "-c", "%u:%g", data]).out
    docker.exec(CONTAINER, ["sh", "-c", f"mkdir -p {data}/files && echo saved-state > {data}/files/state.txt && chown -R {owner} {data}/files"])
    archive = Path(ok(farm, apps.backup_data("all", PKG)))
    assert archive.is_file() and apps.data_backups(PKG)[0]["file"] == archive.name
    assert apps.verify_app_data(archive, PKG)["version_code"] == 2
    docker.exec(CONTAINER, ["sh", "-c", f"echo changed > {data}/files/state.txt; echo junk > {data}/files/junk.txt"])
    assert ok(farm, apps.restore_data("all", PKG, archive)).startswith("restored ")
    assert docker.exec(CONTAINER, ["cat", f"{data}/files/state.txt"]).out == "saved-state"
    assert not docker.exec(CONTAINER, ["test", "-e", f"{data}/files/junk.txt"], check=False).ok
    assert docker.exec(CONTAINER, ["stat", "-c", "%u:%g", f"{data}/files/state.txt"]).out == owner

    # removal, twice
    assert ok(farm, apps.uninstall("all", PKG)) == "uninstalled"
    assert ok(farm, apps.uninstall("all", PKG)) == "was not installed"
    assert apps_mod.installed_version(adb, PKG) is None and PKG not in apps.matrix("all")
    assert "not installed" in failed(farm, apps.grant("all", PKG))
    assert "disable them instead" in failed(farm, apps.uninstall("all", "com.android.settings"))


def test_launch_and_stop_a_real_app(farm):
    ok(farm, farm.device.wake("all"))
    assert ok(farm, farm.apps.launch("all", "com.android.settings")).startswith("started com.android.settings/")
    deadline = time.monotonic() + 15
    while farm.adb.current_app()[0] != "com.android.settings" and time.monotonic() < deadline:
        time.sleep(0.5)
    assert farm.adb.current_app()[0] == "com.android.settings"
    assert farm.device.info(1)["foreground"]["package"] == "com.android.settings"
    ok(farm, farm.apps.stop("all", "com.android.settings"))
    time.sleep(1.5)
    assert farm.adb.current_app()[0] != "com.android.settings"
    assert "does not exist" in failed(farm, farm.apps.launch("all", "com.android.settings", activity=".NoSuchActivity"))


# ----------------------------------------------------------------------------- device settings

def test_device_settings_round_trip(farm):
    d, adb = farm.device, farm.adb
    before = d.info(1)
    try:
        assert ok(farm, d.set_timezone("all", "Asia/Tokyo")) == "time zone Asia/Tokyo" and d.info(1)["timezone"] == "Asia/Tokyo"
        assert adb.shell("date +%Z") == "JST"
        ok(farm, d.set_proxy("all", "10.11.12.13:3128"))
        assert d.info(1)["proxy"] == "10.11.12.13:3128"
        assert ok(farm, d.set_proxy("all", "")) == "proxy off" and d.info(1)["proxy"] == ""
        ok(farm, d.set_dark_mode("all", True))
        assert d.info(1)["dark_mode"]
        ok(farm, d.set_dark_mode("all", False))
        ok(farm, d.set_font_scale("all", 1.15))
        ok(farm, d.set_animations("all", False))
        ok(farm, d.set_battery("all", 42, True))
        i = d.info(1)
        assert i["font_scale"] == 1.15 and not i["animations"] and not i["dark_mode"]
        assert (i["battery"]["level"], i["battery"]["status"], i["battery"]["plugged"]) == (42, "charging", True)
    finally:
        ok(farm, d.set_battery("all"))
        ok(farm, d.set_font_scale("all", 1.0))
        ok(farm, d.set_animations("all", before["animations"]))
        ok(farm, d.set_timezone("all", before["timezone"] or "UTC"))


def test_mock_location(farm):
    if int(farm.adb.getprop("ro.build.version.sdk")) < 31:
        assert "Android 12 or newer" in failed(farm, farm.device.set_geo("all", 35.6762, 139.6503))
        return
    assert ok(farm, farm.device.set_geo("all", 35.6762, 139.6503, accuracy=7)).startswith("location 35.676200,139.650300")
    dump = farm.adb.shell("dumpsys location", timeout=30)
    assert "gps 35.676200,139.650300" in dump and "network 35.676200,139.650300" in dump and "mock" in dump
    assert ok(farm, farm.device.set_geo("all", None)) == "mock location removed"
    assert "gps provider [mock]" not in farm.adb.shell("dumpsys location", timeout=30)


def test_clipboard_round_trip(farm):
    sdk = int(farm.adb.getprop("ro.build.version.sdk"))
    if sdk < 31:
        assert "Android 12 or newer" in failed(farm, farm.device.set_clipboard("all", "x"))
        return
    for text in ("plain ascii", "ünïcödé ✓ 日本語 and 'quotes' $(not run)\nsecond line", "x" * 3000):
        assert ok(farm, farm.device.set_clipboard("all", text)) == f"clipboard set ({len(text)} characters)"
        assert farm.device.clipboard(1) == text


def test_open_url(farm):
    assert "nothing on the device opens this link" in failed(farm, farm.device.open_url("all", "sf-no-such-scheme://x"))
    assert ok(farm, farm.device.open_url("all", "https://example.com/?a=1&b=2")) == "opened"
    farm.adb.key("HOME")


def test_locale_change_through_soft_reboot(farm):
    original = farm.device.info(1)["locale"] or "en-US"
    target = "fr-FR" if not original.startswith("fr") else "de-DE"
    pid = farm.adb.shell("pidof system_server")
    try:
        assert ok(farm, farm.device.set_locale("all", target)).startswith(f"locale {target} active")
        assert farm.adb.shell("pidof system_server") != pid                       # the framework really restarted
        assert farm.device.info(1)["locale"] == target
        assert farm.adb.shell("am get-config").splitlines()[0].startswith(f"config: {target[:2]}-r{target[3:]}")
        assert farm.adb.state() == "device"                                      # ...and ADB never went away
    finally:
        ok(farm, farm.device.set_locale("all", original))
    assert ok(farm, farm.device.reboot("all")).startswith("framework restarted in")

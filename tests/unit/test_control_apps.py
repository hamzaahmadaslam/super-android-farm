"""Apps across the fleet: parsers for Android's output, and the Task semantics of rollouts."""
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from superfarm.config import Config
from superfarm.control import apps as apps_mod
from superfarm.control.apps import Apps, check_package, explain_install_failure, inspect_data_archive
from superfarm.control.library import AppLibrary
from superfarm.core.docker import Docker
from superfarm.core.tasks import TaskManager
from superfarm.paths import get_paths


def load_support():
    name = "sf_control_support"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_control_support.py"))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


S = load_support()
PKG = "org.example.game"
TRAITS = ("abilist=x86_64,arm64-v8a,x86,armeabi-v7a\nbridge=libnb.so\nsdk=33\nlocale=\nrolocale=en-US\n"
          "syslocales=de-DE,en-US\nPhysical density: 160\nOverride density: 320\n")

# Captured from an Android 13 container (trimmed); the format is the contract these parsers rely on.
DUMPSYS = textwrap.dedent("""\
    Packages:
      Package [com.android.settings] (104b8e8):
        userId=1000
        pkg=Package{5bfbb00 com.android.settings}
        codePath=/system/system_ext/priv-app/Settings
        versionCode=33 minSdk=33 targetSdk=33
        versionName=13
        flags=[ SYSTEM HAS_CODE ALLOW_CLEAR_USER_DATA ]
        lastUpdateTime=2024-05-27 15:38:55
        pkgFlags=[ SYSTEM HAS_CODE ALLOW_CLEAR_USER_DATA ]
        requested permissions:
          android.permission.CAMERA
        User 0: ceDataInode=11037127 installed=true hidden=false suspended=false stopped=false notLaunched=false enabled=0 instant=false
          firstInstallTime=2024-05-27 15:38:55
      Package [org.example.game] (9a69b7e):
        userId=10123
        codePath=/data/app/~~Zk3x==/org.example.game-Qw9==
        versionCode=10 minSdk=21 targetSdk=33
        versionName=1.0 beta = 2
        flags=[ HAS_CODE ALLOW_CLEAR_USER_DATA ]
        installerPackageName=com.android.shell
        lastUpdateTime=2026-01-02 03:04:05
        requested permissions:
          android.permission.CAMERA
          android.permission.INTERNET
          android.permission.READ_MEDIA_IMAGES: restricted=true
          android.permission.SYSTEM_ALERT_WINDOW
          android.permission.BODY_SENSORS_BACKGROUND
        install permissions:
          android.permission.INTERNET: granted=true
        User 0: ceDataInode=123 installed=true hidden=false suspended=false stopped=true notLaunched=false enabled=3 instant=false
          runtime permissions:
            android.permission.CAMERA: granted=false, flags=[ USER_SENSITIVE_WHEN_GRANTED ]
      Package [org.example.gone] (1a2b3c4):
        userId=10124
        versionCode=2 minSdk=21 targetSdk=33
        flags=[ HAS_CODE ]
        User 0: ceDataInode=0 installed=false hidden=false enabled=0
        User 10: ceDataInode=5 installed=true hidden=false enabled=0

    Hidden system packages:
      Package [org.example.game] (deadbee):
        versionCode=1 minSdk=21 targetSdk=33
        versionName=0.1-factory
        flags=[ SYSTEM HAS_CODE ]
    """)


@pytest.fixture
def env(tmp_path):
    paths = get_paths(tmp_path / "home").ensure()
    lib = AppLibrary(paths, Config())
    lib.add("game.xapk", S.make_bundle(tmp_path / "game.xapk", {
        "base.apk": S.make_apk(None, S.manifest(PKG, 10, "1.0", label="Game")),
        "config.arm64_v8a.apk": S.make_apk(None, S.manifest(PKG, 10, split="config.arm64_v8a"), abis=("arm64-v8a",)),
        "config.x86_64.apk": S.make_apk(None, S.manifest(PKG, 10, split="config.x86_64"), abis=("x86_64",)),
        "config.xhdpi.apk": S.make_apk(None, S.manifest(PKG, 10, split="config.xhdpi")),
        "config.de.apk": S.make_apk(None, S.manifest(PKG, 10, split="config.de")),
        "config.fr.apk": S.make_apk(None, S.manifest(PKG, 10, split="config.fr")),
        f"Android/obb/{PKG}/main.10.{PKG}.obb": b"obb"}))
    mgr = S.FakeManager(ids=(1, 2, 3))
    for dev in mgr.devices.values():
        dev.on(r"getprop ro\.product\.cpu\.abilist", TRAITS)
    tasks = TaskManager(workers=4)
    yield mgr, lib, tasks, Apps(mgr, lib, tasks, docker=Docker("true"))
    tasks.shutdown()


def done(tasks, task):
    tasks.wait(task, timeout=20)
    assert task.state == "done", (task.state, task.error, task.message)
    return task


# ----------------------------------------------------------------------------- parsers

def test_parse_dumpsys_packages():
    rows = {r["package"]: r for r in apps_mod.parse_dumpsys_packages(DUMPSYS)}
    assert set(rows) == {"com.android.settings", PKG, "org.example.gone"}       # the hidden factory copy is not a second entry
    game = rows[PKG]
    assert (game["version_code"], game["version_name"], game["uid"]) == (10, "1.0 beta = 2", 10123)
    assert not game["system"] and not game["enabled"] and game["installed"] and game["installer"] == "com.android.shell"
    assert rows["com.android.settings"]["system"] and rows["com.android.settings"]["enabled"]
    assert not rows["org.example.gone"]["installed"]                              # other users' installs do not count
    assert apps_mod.parse_dumpsys_packages("") == []


def test_parse_pm_list_handles_equals_signs_in_paths():
    rows = apps_mod.parse_pm_list(
        "package:/data/app/~~Zk3x==/org.example.game-Qw9==/base.apk=org.example.game versionCode:10 uid:10123\n"
        "package:com.android.music versionCode:33\npackage:plain.name\ngarbage line\n")
    assert rows == [
        {"package": PKG, "path": "/data/app/~~Zk3x==/org.example.game-Qw9==/base.apk", "version_code": 10, "uid": 10123},
        {"package": "com.android.music", "path": "", "version_code": 33, "uid": 0},
        {"package": "plain.name", "path": "", "version_code": 0, "uid": 0}]


def test_parse_requested_permissions():
    assert apps_mod.parse_requested_permissions(DUMPSYS, PKG) == [
        "android.permission.CAMERA", "android.permission.INTERNET", "android.permission.READ_MEDIA_IMAGES",
        "android.permission.SYSTEM_ALERT_WINDOW", "android.permission.BODY_SENSORS_BACKGROUND"]
    assert apps_mod.parse_requested_permissions(DUMPSYS, "com.android.settings") == ["android.permission.CAMERA"]
    assert apps_mod.parse_requested_permissions(DUMPSYS, "org.example.gone") == []
    assert apps_mod.parse_requested_permissions(DUMPSYS, "not.installed") is None


def test_parse_traits():
    t = apps_mod.parse_traits(TRAITS)
    assert t.abis == ["x86_64", "arm64-v8a", "x86", "armeabi-v7a"] and t.density == 320 and t.sdk == 33
    assert t.locales == ["de-DE", "en-US"] and t.native_bridge
    bare = apps_mod.parse_traits("abilist=x86_64\nbridge=0\nsdk=\nlocale=\nrolocale=fr-FR\nsyslocales=null\nPhysical density: 240\n")
    assert bare.locales == ["fr-FR"] and bare.density == 240 and not bare.native_bridge and bare.sdk == 0
    assert apps_mod.parse_traits("").locales == ["en-US"]                         # an unreadable device still gets a sane default


@pytest.mark.parametrize("bad", ["", "a b", "a;reboot", "$(id)", "a/b", "-rf", "pkg\n", "../x", "a..b", None, 5])
def test_package_names_are_validated(bad, env):
    _, _, _, apps = env
    with pytest.raises(ValueError):
        check_package(bad)
    for call in (apps.uninstall, apps.launch, apps.stop, apps.clear, apps.enable, apps.disable, apps.grant, apps.revoke):
        with pytest.raises(ValueError):
            call("all", bad)


def test_install_failures_are_explained():
    msg = explain_install_failure("fake:1: install failed: Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]")
    assert msg.startswith("INSTALL_FAILED_UPDATE_INCOMPATIBLE: ") and "uninstall it first" in msg
    assert explain_install_failure("something else") == "something else"


# ----------------------------------------------------------------------------- install / ensure

def test_install_picks_splits_per_device_and_pushes_obb(env):
    mgr, lib, tasks, apps = env
    mgr.devices[2].on(r"getprop ro\.product\.cpu\.abilist", TRAITS.replace("x86_64,arm64-v8a,x86,armeabi-v7a", "arm64-v8a").replace("de-DE,en-US", "fr-FR"))
    task = done(tasks, apps.install("1,2", f"{PKG}-10", grant=False))
    assert task.kind == "app-install" and task.targets == [1, 2] and task.title == "Install Game 1.0"
    assert mgr.devices[1].installs == [(["base.apk", "split_config.x86_64.apk", "split_config.xhdpi.apk", "split_config.de.apk"],
                                        {"replace": True, "grant": False, "downgrade": False})]
    assert mgr.devices[2].installs[0][0] == ["base.apk", "split_config.arm64_v8a.apk", "split_config.xhdpi.apk", "split_config.fr.apk"]
    assert mgr.devices[3].installs == []
    assert mgr.devices[1].pushed == [(str(lib.file(f"{PKG}-10", f"obb/main.10.{PKG}.obb")), f"/sdcard/Android/obb/{PKG}/main.10.{PKG}.obb")]
    assert mgr.devices[1].ran(r"^mkdir -p /sdcard/Android/obb/org\.example\.game$")
    assert task.results[1] == {"ok": True, "detail": "installed 1.0, 4 APKs, 1 OBB"}


def test_one_failing_device_never_stops_the_rest(env):
    mgr, _, tasks, apps = env
    mgr.devices[1].install_error = "Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]"
    mgr.devices[2].online = False
    task = done(tasks, apps.install("all", f"{PKG}-10"))
    assert task.output == {"ok": 1, "failed": 2} and task.results[3]["ok"]
    assert "data volume is full" in task.results[1]["detail"] and not task.results[1]["ok"]
    assert "does not answer on ADB" in task.results[2]["detail"]


def test_incompatible_device_fails_alone_with_the_reason(env):
    mgr, _, tasks, apps = env
    mgr.devices[3].on(r"getprop ro\.product\.cpu\.abilist", TRAITS.replace("x86_64,arm64-v8a,x86,armeabi-v7a", "x86").replace("libnb.so", "0"))
    task = done(tasks, apps.install("all", f"{PKG}-10"))
    assert task.output == {"ok": 2, "failed": 1}
    assert "has native code for arm64-v8a, x86_64 but the device runs x86" in task.results[3]["detail"] and mgr.devices[3].installs == []


def test_ensure_installs_only_where_missing_or_older(env):
    mgr, _, tasks, apps = env
    mgr.devices[1].on(r"pm list packages --show-versioncode", f"package:{PKG} versionCode:10\npackage:{PKG}.extra versionCode:99")
    mgr.devices[2].on(r"pm list packages --show-versioncode", f"package:{PKG} versionCode:9")
    mgr.devices[3].on(r"pm list packages --show-versioncode", f"package:{PKG}.extra versionCode:99")     # a different app with a longer name
    task = done(tasks, apps.ensure("all", f"{PKG}-10"))
    assert task.results[1]["detail"] == "already installed (1.0)" and mgr.devices[1].installs == []
    assert task.results[2]["detail"].startswith("updated from versionCode 9: installed 1.0") and len(mgr.devices[2].installs) == 1
    assert task.results[3]["detail"].startswith("installed 1.0") and len(mgr.devices[3].installs) == 1
    mgr.devices[2].on(r"pm list packages --show-versioncode", f"package:{PKG} versionCode:11")
    again = done(tasks, apps.ensure("2", f"{PKG}-10"))
    assert again.results[2] == {"ok": True, "detail": "kept newer version (versionCode 11 > 10)"} and len(mgr.devices[2].installs) == 1


def test_unknown_app_and_empty_selector_fail_before_any_task(env):
    _, _, tasks, apps = env
    with pytest.raises(KeyError):
        apps.install("all", "org.nope-1")
    with pytest.raises(ValueError, match="matches no Android"):
        apps.install("tag:none", f"{PKG}-10")
    assert tasks.list() == []


def test_selector_may_be_ids(env):
    mgr, _, tasks, apps = env
    assert done(tasks, apps.stop([3, 1], PKG)).targets == [1, 3]
    assert done(tasks, apps.stop(2, PKG)).targets == [2]
    with pytest.raises(ValueError):
        apps.stop([99999], PKG)


# ----------------------------------------------------------------------------- lifecycle

def test_uninstall_is_idempotent_and_explains_system_apps(env):
    mgr, _, tasks, apps = env
    mgr.devices[1].on(r"pm list packages --show-versioncode", f"package:{PKG} versionCode:10").on(r"^pm uninstall", "Success")
    mgr.devices[3].on(r"pm list packages --show-versioncode", f"package:{PKG} versionCode:10").on(
        r"^pm uninstall", (1, "Failure [DELETE_FAILED_INTERNAL_ERROR]", ""))
    task = done(tasks, apps.uninstall("all", PKG, keep_data=True))
    assert task.results[1] == {"ok": True, "detail": "uninstalled (data kept)"} and mgr.devices[1].argv() == ["pm", "uninstall", "-k", PKG]
    assert task.results[2] == {"ok": True, "detail": "was not installed"} and not mgr.devices[2].ran("pm uninstall")
    assert not task.results[3]["ok"] and "disable them instead" in task.results[3]["detail"]


def test_launch_resolves_the_launcher_activity_then_falls_back_to_monkey(env):
    mgr, _, tasks, apps = env
    mgr.devices[1].on(r"resolve-activity", f"priority=0 preferredOrder=0\n{PKG}/.ui.Main")
    mgr.devices[2].on(r"resolve-activity", "No activity found").on(r"^monkey", "Events injected: 1\n")
    mgr.devices[3].on(r"resolve-activity", "No activity found").on(r"^monkey", (252, "** No activities found to run, monkey aborted.", ""))
    task = done(tasks, apps.launch("all", PKG))
    assert task.results[1]["detail"] == f"started {PKG}/.ui.Main"
    assert mgr.devices[1].argv() == ["am", "start", "-n", f"{PKG}/.ui.Main", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER"]
    assert task.results[2]["ok"] and mgr.devices[2].argv() == ["monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "--pct-syskeys", "0", "1"]
    assert not task.results[3]["ok"] and "not installed or has nothing the launcher can start" in task.results[3]["detail"]


def test_launch_reports_am_errors_that_exit_zero(env):
    mgr, _, tasks, apps = env
    mgr.devices[1].on(r"^am start", (0, "Starting: Intent { cmp=x }", "Error type 3\nError: Activity class {x} does not exist."))
    task = done(tasks, apps.launch("1", PKG, activity=".Missing"))
    assert not task.results[1]["ok"] and "does not exist" in task.results[1]["detail"]
    assert mgr.devices[1].argv() == ["am", "start", "-n", f"{PKG}/.Missing"]
    with pytest.raises(ValueError):
        apps.launch("1", PKG, activity="x; reboot")


def test_stop_clear_enable_disable(env):
    mgr, _, tasks, apps = env
    dev = mgr.devices[1]
    dev.on(r"^pm clear", "Success").on(r"^pm enable", f"Package {PKG} new state: enabled").on(
        r"^pm disable-user", f"Package {PKG} new state: disabled-user")
    assert done(tasks, apps.stop("1", PKG)).results[1]["detail"] == "stopped" and dev.argv() == ["am", "force-stop", PKG]
    assert done(tasks, apps.clear("1", PKG)).results[1]["detail"] == "data cleared"
    assert done(tasks, apps.disable("1", PKG)).results[1]["detail"] == "disabled-user" and dev.argv() == ["pm", "disable-user", "--user", "0", PKG]
    assert done(tasks, apps.enable("1", PKG)).results[1]["detail"] == "enabled"
    failed = done(tasks, apps.clear("2", PKG))                                     # `pm clear` printed "Failed": not installed
    assert not failed.results[2]["ok"] and "not installed" in failed.results[2]["detail"]
    mgr.devices[3].on(r"^pm enable", (255, "", "java.lang.IllegalArgumentException: Unknown package: x\n\tat com.android.server"))
    assert "Unknown package" in done(tasks, apps.enable("3", PKG)).results[3]["detail"]


# ----------------------------------------------------------------------------- permissions

def test_grant_all_grants_only_runtime_permissions_this_android_knows(env):
    mgr, _, tasks, apps = env
    dev = mgr.devices[1].on(r"^dumpsys package", DUMPSYS)
    dev.props["ro.build.version.sdk"] = "32"            # READ_MEDIA_IMAGES and BODY_SENSORS_BACKGROUND arrive with API 33
    assert done(tasks, apps.grant("1", PKG)).results[1]["detail"] == "granted 1 runtime permission"
    assert dev.ran(r"^pm grant") == [f"pm grant {PKG} android.permission.CAMERA"]
    dev.props["ro.build.version.sdk"] = "33"
    dev.on(r"pm revoke .*BODY_SENSORS_BACKGROUND", (255, "", "java.lang.SecurityException: not changeable"))
    detail = done(tasks, apps.revoke("1", PKG)).results[1]["detail"]
    assert detail == "revoked 2 runtime permissions; 1 refused by Android (BODY_SENSORS_BACKGROUND)"
    assert len(dev.ran(r"^pm revoke")) == 3


def test_grant_single_special_and_unknown(env):
    mgr, _, tasks, apps = env
    dev = mgr.devices[1].on(r"^dumpsys package", DUMPSYS)
    assert done(tasks, apps.grant("1", PKG, "camera")).results[1]["detail"] == "granted android.permission.CAMERA"
    assert dev.argv() == ["pm", "grant", PKG, "android.permission.CAMERA"]
    assert done(tasks, apps.grant("1", PKG, "SYSTEM_ALERT_WINDOW")).results[1]["detail"] == "granted android.permission.SYSTEM_ALERT_WINDOW (app-op)"
    assert dev.argv() == ["appops", "set", PKG, "SYSTEM_ALERT_WINDOW", "allow"]
    revoked = done(tasks, apps.revoke("1", PKG, "android.permission.SYSTEM_ALERT_WINDOW"))
    assert revoked.results[1]["detail"].startswith("revoked ") and dev.argv()[-1] == "default"
    not_requested = done(tasks, apps.grant("1", PKG, "RECORD_AUDIO"))
    assert not not_requested.results[1]["ok"] and "does not request" in not_requested.results[1]["detail"]
    missing = done(tasks, apps.grant("2", PKG))                                    # `pm grant` would exit 0 for a missing package
    assert missing.results[2] == {"ok": False, "detail": f"AdbError: {PKG} is not installed"}
    with pytest.raises(ValueError):
        apps.grant("1", PKG, "CAMERA; reboot")
    assert apps.permissions(1, PKG)["dangerous"] == ["android.permission.CAMERA", "android.permission.READ_MEDIA_IMAGES",
                                                     "android.permission.BODY_SENSORS_BACKGROUND"]
    assert apps.permissions(1, PKG)["special"] == ["android.permission.SYSTEM_ALERT_WINDOW"]


# ----------------------------------------------------------------------------- inventory

def test_installed_and_matrix(env):
    mgr, _, _, apps = env
    mgr.devices[1].on(r"^dumpsys package packages", DUMPSYS)
    mgr.devices[2].on(r"^dumpsys package packages", DUMPSYS.replace("versionName=1.0 beta = 2", "versionName=0.9").replace("enabled=3", "enabled=0"))
    mgr.devices[3].online = False
    rows = apps.installed(1)
    assert [r["package"] for r in rows] == [PKG] and rows[0]["label"] == "Game" and rows[0]["version"] == "1.0 beta = 2"
    assert [r["package"] for r in apps.installed(1, third_party=False)] == ["com.android.settings", PKG]
    detail = apps.matrix_detail("all")
    assert detail["instances"] == [1, 2, 3] and list(detail["errors"]) == [3] and "does not answer" in detail["errors"][3]
    assert detail["packages"] == {PKG: {1: {"version": "1.0 beta = 2", "version_code": 10, "enabled": False},
                                        2: {"version": "0.9", "version_code": 10, "enabled": True}}}
    assert apps.matrix("all") == {PKG: {1: "1.0 beta = 2", 2: "0.9"}}


def test_installed_falls_back_to_pm_list(env):
    mgr, _, _, apps = env
    mgr.devices[1].on(r"^pm list packages --show-versioncode -U", f"package:{PKG} versionCode:10 uid:10123\npackage:android versionCode:33 uid:1000"
                      ).on(r"^pm list packages -3", f"package:{PKG}").on(r"^pm list packages -d", f"package:{PKG}")
    rows = apps.installed(1, third_party=False)
    assert [(r["package"], r["system"], r["enabled"], r["version"]) for r in rows] == [("android", True, True, "33"), (PKG, False, False, "10")]


# ----------------------------------------------------------------------------- app data (advanced)

def make_tar(path: Path, members: list[tuple]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, kind, extra in members:
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 10123
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "sym":
                info.type, info.linkname = tarfile.SYMTYPE, extra
                tar.addfile(info)
            elif kind == "hard":
                info.type, info.linkname = tarfile.LNKTYPE, extra
                tar.addfile(info)
            elif kind == "chr":
                info.type = tarfile.CHRTYPE
                tar.addfile(info)
            else:
                info.size = len(extra)
                tar.addfile(info, io.BytesIO(extra))
    return path


def test_data_archive_inspection(tmp_path):
    good = make_tar(tmp_path / "good.tar.gz", [(PKG, "dir", None), (f"{PKG}/files/a.db", "file", b"x"),
                                                (f"{PKG}/lib", "sym", "/data/app/x/lib/x86_64")])
    assert inspect_data_archive(good, PKG) == {"members": 3, "uid": 10123}
    cases = {
        "outside": [(PKG, "dir", None), ("other.pkg/x", "file", b"x")],
        "dotdot": [(f"{PKG}/../../system/x", "file", b"x")],
        "absolute": [("/data/system/x", "file", b"x")],
        "through a symbolic link": [(f"{PKG}/esc", "sym", "/system"), (f"{PKG}/esc/build.prop", "file", b"x")],
        "hard-links": [(f"{PKG}/h", "hard", "../../system/build.prop")],
        "device node": [(f"{PKG}/null", "chr", None)],
    }
    for word, members in cases.items():
        with pytest.raises(ValueError, match="outside|through a symbolic link|hard-links|device node"):
            inspect_data_archive(make_tar(tmp_path / f"{word.split()[0]}.tar.gz", members), PKG)
    (tmp_path / "junk.tar.gz").write_bytes(b"not a tar")
    with pytest.raises(ValueError, match="not a readable"):
        inspect_data_archive(tmp_path / "junk.tar.gz", PKG)
    with pytest.raises(ValueError, match="empty"):
        inspect_data_archive(make_tar(tmp_path / "empty.tar.gz", []), PKG)


FAKE_DOCKER = '''#!/usr/bin/env python3
"""Stands in for `docker`: `exec` runs the command on the host with /data/data mapped to a folder."""
import os, subprocess, sys
root = os.environ["FAKE_DATA_ROOT"]
args = sys.argv[1:]
if args[:2] == ["container", "inspect"]:
    print("true"); sys.exit(0)
if args[0] == "exec":
    args = [a for a in args[1:] if a != "-i"][1:]
    args = [a.replace("/data/data", root) for a in args]
    if args[0] == "restorecon":
        sys.exit(0)
    sys.exit(subprocess.run(args).returncode)
sys.exit(2)
'''


def test_backup_and_restore_app_data_round_trip(env, tmp_path, monkeypatch):
    mgr, lib, tasks, _ = env
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    data_root = tmp_path / "data"
    app = data_root / PKG
    (app / "files").mkdir(parents=True)
    (app / "files" / "save.db").write_bytes(b"progress" * 1000)
    (app / "shared_prefs").mkdir()
    (app / "shared_prefs" / "login.xml").write_text("<map/>")
    os.symlink("/nonexistent/lib", app / "lib")
    monkeypatch.setenv("FAKE_DATA_ROOT", str(data_root))
    apps = Apps(mgr, lib, tasks, docker=Docker(str(fake)))

    task = done(tasks, apps.backup_data("1", PKG))
    archive = Path(task.results[1]["detail"])
    assert archive.parent == lib.paths.backups / "apps" / PKG and archive.name.startswith(f"{PKG}_android1_") and archive.suffixes[-2:] == [".tar", ".gz"]
    assert mgr.devices[1].ran(f"^am force-stop {PKG}$")                           # stopped first: no half-written files
    assert [b["file"] for b in apps.data_backups(PKG)] == [archive.name] and apps.data_backups()[0]["package"] == PKG
    assert not [p for p in archive.parent.iterdir() if p.name.startswith(".")]    # no temp files left behind
    manifest = json.loads((archive.parent / (archive.name + ".json")).read_text())
    assert (manifest["kind"], manifest["package"], manifest["instance"], manifest["members"]) == ("app-data", PKG, 1, 6)
    assert manifest["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest() == apps.data_backups(PKG)[0]["sha256"]
    assert apps.verify_app_data(archive, PKG)["size"] == archive.stat().st_size

    (app / "files" / "save.db").write_bytes(b"lost")
    (app / "files" / "junk.tmp").write_text("created after the backup")
    restored = done(tasks, apps.restore_data("1", PKG, archive))
    assert restored.results[1]["ok"], restored.results
    assert (app / "files" / "save.db").read_bytes() == b"progress" * 1000 and not (app / "files" / "junk.tmp").exists()
    assert (app / "shared_prefs" / "login.xml").read_text() == "<map/>" and os.readlink(app / "lib") == "/nonexistent/lib"

    with pytest.raises(ValueError, match="holds data of"):
        apps.verify_app_data(archive, "org.other.app")
    tampered = archive.with_name("tampered.tar.gz")
    tampered.write_bytes(archive.read_bytes() + b"\x00")
    (archive.parent / "tampered.tar.gz.json").write_text((archive.parent / (archive.name + ".json")).read_text())
    with pytest.raises(ValueError, match="does not match its manifest"):
        apps.restore_app_data(1, PKG, tampered)
    assert (app / "files" / "save.db").read_bytes() == b"progress" * 1000         # refused before anything was wiped
    with pytest.raises(ValueError, match="only restored from"):
        apps.restore_app_data(1, PKG, tmp_path / "elsewhere.tar.gz")
    evil = make_tar(archive.parent / "evil.tar.gz", [(f"{PKG}/../../x", "file", b"x")])
    with pytest.raises(ValueError, match="outside"):
        apps.restore_app_data(1, PKG, evil)
    with pytest.raises(RuntimeError, match="no data"):
        apps.backup_app_data(1, "org.not.installed")
    with pytest.raises(RuntimeError, match="not installed"):
        apps.restore_app_data(1, "org.not.installed", make_tar(lib.paths.backups / "apps" / "x.tar.gz", [("org.not.installed", "dir", None)]))

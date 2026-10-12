"""Network, app library and app actions, files, device settings, shell; and the upload path."""
from __future__ import annotations

import io
import stat

import pytest
import test_control_support as S
from api_fakes import api
from net_fakes import OVPN

from superfarm.api import uploads as uploads_mod
from superfarm.net import gateway as gateway_mod

PKG = "org.example.app"
SECRET = "hunter2-very-secret"      # noqa: S105 - test value that must never come back


class Devices(dict):
    """One control-level fake device per ADB serial, created on first use."""

    def __call__(self, serial: str) -> S.FakeAdb:
        if serial not in self:
            self[serial] = S.FakeAdb(serial)
            # Shell.run starts the adb program itself (to bound its output). In a unit test
            # that must fail to start rather than reach whatever is connected to this host.
            self[serial].binary = "adb-is-not-used-in-unit-tests"
        return self[serial]

    def of(self, id: int) -> S.FakeAdb:
        return self(f"127.0.0.1:{17000 + id}")


@pytest.fixture
def a(tmp_path):
    with api(tmp_path) as made:
        made.devices = Devices()
        made.core.mgr.create(count=3)
        made.core.mgr.backends.adb = made.devices
        made.core.docker.containers.update({
            made.core.names(i).android: {"running": True, "image": "", "state": "running",
                                         "labels": made.core.names(i).labels("android")} for i in (1, 2, 3)})
        yield made


def multipart(name: str, data: bytes, **fields) -> dict:
    return {"files": {"file": (name, io.BytesIO(data), "application/octet-stream")}, "data": fields}


def uploads_left(a) -> list:
    folder = a.farm.paths.run / "uploads"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def settle(a) -> None:
    """Upload clean-up runs on a thread once the task is done; give it a moment."""
    import time
    deadline = time.monotonic() + 5
    while uploads_left(a) and time.monotonic() < deadline:
        time.sleep(0.02)


# ------------------------------------------------------------------------------ uploads

def test_app_upload_is_parsed_into_the_library_and_leaves_no_temp_file(a):
    r = a.client.post("/api/apps/library", **multipart("Example App (1).apk", S.make_apk()))
    assert r.status_code == 201, r.text
    app = r.json()["app"]
    assert app["package"] == PKG and app["version_name"] == "1.2.3"
    assert [x["id"] for x in a.client.get("/api/apps/library").json()["apps"]] == [app["id"]]
    assert uploads_left(a) == []


def test_raw_body_upload_takes_its_name_from_the_query(a):
    r = a.client.post("/api/apps/library", params={"filename": "plain.apk"}, content=S.make_apk(),
                      headers={"content-type": "application/octet-stream"})
    assert r.status_code == 201 and r.json()["app"]["original"] == "plain.apk"


def test_upload_cap_is_enforced_on_the_bytes_that_arrive(tmp_path):
    with api(tmp_path, {"server": {"max_upload_mb": 1}}) as a:
        big = b"\0" * (1024 * 1024 + 4096)
        r = a.client.post("/api/apps/library", **multipart("big.apk", big))
        assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
        assert "max_upload_mb" in r.json()["error"]["message"]

        def chunks():                       # no Content-Length to give it away early
            for _ in range(5):
                yield b"\0" * (256 * 1024 + 64)

        r = a.client.post("/api/apps/library", params={"filename": "big.apk"}, content=chunks())
        assert r.status_code == 413
        assert uploads_left(a) == [] and a.farm.library.list() == []


def test_upload_under_the_cap_passes_the_same_route(tmp_path):
    with api(tmp_path, {"server": {"max_upload_mb": 1}}) as a:
        assert a.client.post("/api/apps/library", **multipart("ok.apk", S.make_apk())).status_code == 201


def test_a_declared_length_over_the_cap_is_refused_before_reading(tmp_path, monkeypatch):
    with api(tmp_path, {"server": {"max_upload_mb": 1}}) as a:
        monkeypatch.setattr(uploads_mod.tempfile, "mkstemp", lambda **kw: pytest.fail("a temp file was created"))
        r = a.client.post("/api/apps/library", params={"filename": "big.apk"}, content=b"\0" * (2 * 1024 * 1024))
        assert r.status_code == 413


@pytest.mark.parametrize("name", ["../../etc/passwd.apk", "/etc/cron.d/evil.apk", "..\\..\\evil.apk", "a/b/c.apk",
                                  "\x00evil.apk", "..evil.apk"])
def test_upload_names_cannot_leave_the_library(a, name):
    r = a.client.post("/api/apps/library", **multipart(name, S.make_apk()))
    assert r.status_code == 201, r.text
    app = r.json()["app"]
    assert "/" not in app["original"] and "\\" not in app["original"] and not app["original"].startswith(".")
    stored = [p for p in a.farm.paths.home.rglob("*.apk")]
    assert stored and all(a.farm.paths.apps in p.parents for p in stored)
    assert not (a.farm.paths.home.parent / "etc").exists()


@pytest.mark.parametrize("kw,status", [
    ({"data": {"selector": "all"}}, 400),                                   # a form without a file part
    ({"content": b"", "params": {"filename": "x.apk"}}, 400),               # an empty file
    ({"content": b"--x\r\ngarbage", "headers": {"content-type": "multipart/form-data"}}, 400),   # no boundary
])
def test_malformed_uploads_are_400(a, kw, status):
    r = a.client.post("/api/apps/library", **kw)
    assert r.status_code == status and r.json()["error"]["code"] == "bad_request"
    assert uploads_left(a) == []


def test_two_files_in_one_upload_are_refused(a):
    files = [("file", ("a.apk", S.make_apk(), "application/octet-stream")),
             ("file", ("b.apk", S.make_apk(), "application/octet-stream"))]
    r = a.client.post("/api/apps/library", files=files)
    assert r.status_code == 400 and "one file" in r.json()["error"]["message"]
    assert uploads_left(a) == []


def test_not_an_app_is_400_and_nothing_is_kept(a):
    r = a.client.post("/api/apps/library", **multipart("notes.txt", b"hello"))
    assert r.status_code == 400 and "unsupported file type" in r.json()["error"]["message"]
    r = a.client.post("/api/apps/library", **multipart("fake.apk", b"this is not a zip"))
    assert r.status_code == 400
    assert uploads_left(a) == [] and a.farm.library.list() == []


def test_uploads_dir_is_private_and_swept_at_start(a):
    folder = uploads_mod.uploads_dir(a.farm)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    (folder / "up-stale").write_bytes(b"x")
    (folder / "up-stale.d").mkdir()
    (folder / "up-stale.d" / "f").write_bytes(b"x")
    assert uploads_mod.sweep(a.farm) == 2 and uploads_left(a) == []


def test_upload_requires_authentication_before_any_byte_is_stored(a, monkeypatch):
    monkeypatch.setattr(uploads_mod.tempfile, "mkstemp", lambda **kw: pytest.fail("a temp file was created"))
    assert a.anonymous.post("/api/apps/library", **multipart("x.apk", S.make_apk())).status_code == 401


# -------------------------------------------------------------------------- app library

def test_library_delete_and_unknown_ids(a):
    app_id = a.client.post("/api/apps/library", **multipart("x.apk", S.make_apk())).json()["app"]["id"]
    assert a.client.delete(f"/api/apps/library/{app_id}").json() == {"deleted": app_id}
    assert a.client.delete(f"/api/apps/library/{app_id}").status_code == 404
    for bad in ("..", "..%2F..%2Fsecrets", ".staging-x", "a b"):
        r = a.client.delete(f"/api/apps/library/{bad}")
        assert r.status_code in (400, 404), (bad, r.status_code)
    assert (a.farm.paths.secrets / "api-token").exists()


# -------------------------------------------------------------------------- app actions

def test_install_ensure_and_uninstall_run_as_tasks(a):
    app_id = a.client.post("/api/apps/library", **multipart("x.apk", S.make_apk())).json()["app"]["id"]
    for dev in (a.devices.of(i) for i in (1, 2, 3)):
        dev.on(r"getprop ro\.product\.cpu\.abilist", "abilist=x86_64\nsdk=33\n")
    task = a.wait(a.client.post("/api/apps/install", json={"selector": "1-2", "app_id": app_id}))
    assert task["kind"] == "app-install" and task["targets"] == [1, 2] and task["failed"] == 0
    assert a.devices.of(1).installs and not a.devices.of(3).installs
    a.devices.of(3).on(r"pm list packages --show-versioncode", f"package:{PKG} versionCode:7")
    task = a.wait(a.client.post("/api/apps/ensure", json={"selector": "3", "app_id": app_id}))
    assert "already installed" in task["results"]["3"]["detail"]
    a.devices.of(1).on(r"pm list packages --show-versioncode", f"package:{PKG} versionCode:7").on(r"^pm uninstall", "Success")
    task = a.wait(a.client.post("/api/apps/uninstall", json={"selector": "1", "package": PKG}))
    assert task["results"]["1"]["ok"]


@pytest.mark.parametrize("action", ["launch", "stop", "clear", "enable", "disable", "grant", "revoke"])
def test_app_actions_return_a_task(a, action):
    r = a.client.post(f"/api/apps/{action}", json={"selector": "1", "package": PKG})
    assert r.status_code == 202 and r.json()["task"]["targets"] == [1]
    a.wait(r)


@pytest.mark.parametrize("path,body,status", [
    ("/api/apps/install", {"selector": "all", "app_id": "nope-1"}, 404),
    ("/api/apps/install", {"selector": "all", "app_id": "../../x"}, 400),
    ("/api/apps/install", {"selector": "all"}, 422),
    ("/api/apps/launch", {"selector": "all", "package": "not a package; rm -rf /"}, 400),
    ("/api/apps/launch", {"selector": "9", "package": PKG}, 400),            # matches no Android
    ("/api/apps/launch", {"selector": "all", "package": PKG, "shell": "x"}, 422),
    ("/api/apps/uninstall", {"selector": "bogus", "package": PKG}, 400),
    ("/api/apps/explode", {"selector": "all", "package": PKG}, 404),
])
def test_app_actions_refuse_bad_requests(a, path, body, status):
    r = a.client.post(path, json=body)
    assert r.status_code == status, r.text
    assert a.farm.tasks.list() == []


def test_matrix_and_installed(a):
    for i in (1, 2):
        a.devices.of(i).on(r"^pm list packages --show-versioncode -U", f"package:{PKG} versionCode:10 uid:10123").on(
            r"^pm list packages -3", f"package:{PKG}").on(r"^dumpsys package packages", "")
    matrix = a.client.get("/api/apps/matrix", params={"selector": "1-2"}).json()["matrix"]
    assert set(matrix[PKG]) == {"1", "2"}
    detail = a.client.get("/api/apps/matrix", params={"selector": "1-2", "detail": "true"}).json()
    assert detail["instances"] == [1, 2] and detail["errors"] == {}
    apps = a.client.get("/api/instances/1/apps").json()["apps"]
    assert apps[0]["package"] == PKG
    assert a.client.get("/api/apps/matrix", params={"selector": "bogus"}).status_code == 400
    assert a.client.get("/api/instances/9/apps").status_code == 404


def test_an_android_that_does_not_answer_is_409(a):
    a.devices.of(1).online = False
    r = a.client.get("/api/instances/1/apps")
    assert r.status_code == 409 and r.json()["error"]["code"] == "device_unavailable"


# -------------------------------------------------------------------------------- files

def test_push_uploads_once_and_pushes_to_every_selected_android(a):
    r = a.client.post("/api/files/push", **multipart("Holiday Photo.jpg", b"jpeg-bytes", selector="1-2", remote="/sdcard/Pictures/"))
    task = a.wait(r)
    assert task["kind"] == "file-push" and task["targets"] == [1, 2] and task["failed"] == 0
    local, remote = a.devices.of(1).pushed[0]
    assert remote == "/sdcard/Pictures/Holiday_Photo.jpg" and "Holiday_Photo.jpg" in local
    assert not a.devices.of(3).pushed
    settle(a)
    assert uploads_left(a) == []


def test_push_defaults_to_the_download_folder_and_accepts_query_fields(a):
    r = a.client.post("/api/files/push", params={"selector": "1", "filename": "notes.txt"}, content=b"hello")
    a.wait(r)
    assert a.devices.of(1).pushed[0][1] == "/sdcard/Download/notes.txt"


@pytest.mark.parametrize("remote", ["/data/data/x", "/sdcard/../data/system/x", "relative/path", "/etc/hosts",
                                    "/sdcard/a\nb", "/system/bin/sh"])
def test_push_refuses_destinations_outside_the_shared_folders(a, remote):
    r = a.client.post("/api/files/push", **multipart("x.txt", b"data", selector="1", remote=remote))
    assert r.status_code == 400, r.text
    assert not a.devices.of(1).pushed and a.farm.tasks.list() == []
    assert uploads_left(a) == []


def test_push_and_gallery_need_a_selector(a):
    for path in ("/api/files/push", "/api/files/gallery"):
        r = a.client.post(path, **multipart("x.jpg", b"data"))
        assert r.status_code == 400 and "selector" in r.json()["error"]["message"]
        assert uploads_left(a) == []


def test_gallery_puts_a_picture_where_gallery_apps_look(a):
    task = a.wait(a.client.post("/api/files/gallery", **multipart("../../pic.JPG", b"jpeg", selector="2")))
    assert task["kind"] == "file-gallery" and task["results"]["2"]["ok"]
    assert a.devices.of(2).pushed[0][1].endswith("/pic.JPG") and "/.." not in a.devices.of(2).pushed[0][1]
    settle(a)
    assert uploads_left(a) == []


def test_list_and_pull(a):
    dev = a.devices.of(1)
    dev.on(r"^stat -L -c .* /sdcard/Download/report\.pdf$", "regular file|2048")
    r = a.client.get("/api/instances/1/files/pull", params={"path": "/sdcard/Download/report.pdf"})
    assert r.status_code == 200 and r.content == b"pulled:/sdcard/Download/report.pdf"
    assert 'filename="report.pdf"' in r.headers["content-disposition"]
    pulls = a.farm.paths.run / "pulls"
    assert not pulls.exists() or list(pulls.iterdir()) == []            # the temporary copy is gone
    dev.on(r"^stat -L -c .* /sdcard$", "directory|4096").on(
        r"^find -H /sdcard ", "directory|4096|1|770|/sdcard/DCIM\nregular file|3|2|660|/sdcard/a.txt\n")
    listing = a.client.get("/api/instances/1/files").json()
    assert listing["path"] == "/sdcard" and [e["name"] for e in listing["entries"]] == ["DCIM", "a.txt"]
    assert a.client.get("/api/instances/1/files", params={"path": "/sdcard/none"}).status_code == 404
    dev.on(r"^stat -L -c .* /sdcard/a\.txt$", "regular file|3")
    assert a.client.get("/api/instances/1/files", params={"path": "/sdcard/a.txt"}).status_code == 400


@pytest.mark.parametrize("path", ["/data/system/users/0/accounts.db", "/sdcard/../data/misc/x", "../../etc/passwd",
                                  "/proc/1/environ", "sdcard/x", "/sdcard/x\x00y"])
def test_pull_and_list_refuse_paths_outside_the_shared_folders(a, path):
    for route in ("/api/instances/1/files/pull", "/api/instances/1/files"):
        r = a.client.get(route, params={"path": path})
        assert r.status_code == 400, (route, path, r.text)
    assert not a.devices.of(1).pulled


def test_pull_of_a_missing_file_is_404_and_pull_needs_a_path(a):
    assert a.client.get("/api/instances/1/files/pull", params={"path": "/sdcard/none.txt"}).status_code == 404
    assert a.client.get("/api/instances/1/files/pull").status_code == 422


# ----------------------------------------------------------------------- device settings

@pytest.mark.parametrize("route,body", [
    ("timezone", {"timezone": "Europe/Berlin"}), ("geo", {"lat": 52.52, "lon": 13.4}), ("geo", {}),
    ("proxy", {"proxy": "10.0.0.1:3128"}), ("proxy", {"proxy": ""}), ("clipboard", {"text": "hello"}),
    ("animations", {"enabled": False}), ("dark-mode", {"enabled": True}), ("font-scale", {"scale": 1.3}),
    ("open-url", {"url": "https://example.org/"}), ("wake", {}), ("locale", {"locale": "de-DE", "restart": False}),
])
def test_device_settings_return_a_task_for_the_selection(a, route, body):
    r = a.client.post(f"/api/device/{route}", json={"selector": "1,3", **body})
    assert r.status_code == 202, r.text
    assert r.json()["task"]["targets"] == [1, 3]
    a.wait(r, 20)


@pytest.mark.parametrize("route,body,status", [
    ("timezone", {"timezone": "Mars/Olympus; reboot"}, 400), ("geo", {"lat": 200, "lon": 0}, 400),
    ("proxy", {"proxy": "not a proxy"}, 400), ("font-scale", {"scale": 9}, 422), ("open-url", {"url": "no scheme here"}, 400),
    ("locale", {"locale": "x"}, 422), ("locale", {"locale": "$(reboot)"}, 400), ("reboot", {"mode": "hard"}, 400),
    ("animations", {}, 422), ("clipboard", {"text": ""}, 422), ("timezone", {"timezone": "UTC", "extra": 1}, 422),
    ("selfdestruct", {}, 404),
])
def test_device_settings_refuse_bad_values(a, route, body, status):
    r = a.client.post(f"/api/device/{route}", json={"selector": "all", **body})
    assert r.status_code == status, r.text
    assert a.farm.tasks.list() == []


def test_reboot_soft_restarts_the_framework_and_container_goes_through_the_manager(a, monkeypatch):
    from superfarm.control import device as device_mod
    monkeypatch.setattr(device_mod, "soft_restart", lambda adb, timeout=120: 1.5)
    task = a.wait(a.client.post("/api/device/reboot", json={"selector": "1"}))
    assert task["kind"] == "device-reboot" and "restarted in 1.5s" in task["results"]["1"]["detail"]
    r = a.client.post("/api/device/reboot", json={"selector": "1", "mode": "container", "force": True})
    assert r.status_code == 202 and r.json()["task"]["kind"] == "instance.restart"
    a.farm.tasks.wait(a.farm.tasks.get(r.json()["task"]["id"]), 10)


def test_info_screenshot_and_logcat(a):
    dev = a.devices.of(1)
    dev.on(r"^logcat ", "--------- beginning of main\nl1 keep\nl2 drop\nl3 keep\n")
    info = a.client.get("/api/instances/1/info").json()
    assert info["foreground"] == {"package": "com.android.launcher3", "activity": "com.android.launcher3.Launcher"}
    shot = a.client.get("/api/instances/1/screenshot")
    assert shot.headers["content-type"] == "image/png" and shot.content.startswith(b"\x89PNG")
    assert a.client.get("/api/instances/1/logcat", params={"lines": 2, "filter": "keep"}).json()["text"] == "l1 keep\nl3 keep"
    assert a.client.get("/api/instances/1/logcat", params={"lines": 0}).status_code == 400
    assert a.client.get("/api/instances/1/logcat", params={"priority": "Z"}).status_code == 400
    assert a.client.get("/api/instances/1/logcat", params={"package": "bad pkg;"}).status_code == 400


# -------------------------------------------------------------------------------- shell

@pytest.fixture
def ran(monkeypatch):
    """Commands Shell.run would have started, answered without starting anything."""
    from superfarm.control import shell as shell_mod
    started: list[list[str]] = []

    def fake(argv, *, timeout, max_bytes):
        started.append(list(argv))
        return shell_mod.ExecResult(code=0, output="13\n", truncated=False, timed_out=False, seconds=0.01)

    monkeypatch.setattr(shell_mod, "run_bounded", fake)
    return started


def test_shell_runs_argv_and_keeps_the_full_output(a, ran):
    r = a.client.post("/api/shell", json={"selector": "1,2", "argv": ["getprop", "ro.build.version.release"]})
    task = a.wait(r)
    assert task["kind"] == "shell" and task["targets"] == [1, 2] and task["failed"] == 0
    assert sorted(argv[2] for argv in ran) == ["127.0.0.1:17001", "127.0.0.1:17002"]
    out = a.client.get(f"/api/shell/{task['id']}/output").json()
    assert out["task"]["id"] == task["id"]
    assert out["outputs"]["1"]["output"] == "13\n" and out["outputs"]["2"]["code"] == 0
    assert a.client.get("/api/shell/t999/output").status_code == 404


def test_shell_output_is_only_for_shell_tasks(a):
    other = a.farm.tasks.submit("test", "not a shell", lambda t: None)
    assert a.client.get(f"/api/shell/{other.id}/output").status_code == 404


def test_destructive_commands_are_refused_unless_forced(a, ran):
    r = a.client.post("/api/shell", json={"selector": "1", "argv": ["rm", "-rf", "/data"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "refused"
    assert a.farm.tasks.list() == [] and ran == []
    r = a.client.post("/api/shell", json={"selector": "1", "argv": ["rm", "-rf", "/data"], "force": True})
    assert r.status_code == 202
    a.wait(r)
    assert len(ran) == 1


@pytest.mark.parametrize("body", [{"argv": "getprop"}, {"argv": []}, {"argv": ["id"], "timeout": 0},
                                  {"argv": ["id"], "timeout": 99999}, {"command": "id"}, {"argv": [["id"]]}])
def test_shell_body_is_strict(a, body):
    assert a.client.post("/api/shell", json={"selector": "1", **body}).status_code == 422
    assert a.farm.tasks.list() == []


def test_shell_metacharacters_stay_one_argument(a, ran):
    a.wait(a.client.post("/api/shell", json={"selector": "1", "argv": ["echo", "a; reboot", "$(id)", "`id`"]}))
    assert ran[0][:4] == ["adb-is-not-used-in-unit-tests", "-s", "127.0.0.1:17001", "shell"]
    line = ran[0][4]
    assert "'a; reboot'" in line and "'$(id)'" in line and "'`id`'" in line


# ------------------------------------------------------------------------------ network

def test_vpn_profile_upload_list_delete(a):
    r = a.client.post("/api/net/profiles", **multipart("../../My Server (1).OVPN", OVPN.encode()))
    assert r.status_code == 201 and r.json() == {"name": "My_Server_1_.ovpn"}
    stored = a.farm.paths.vpn / "My_Server_1_.ovpn"
    assert stored.is_file() and stat.S_IMODE(stored.stat().st_mode) == 0o600
    rows = a.client.get("/api/net/profiles").json()["profiles"]
    assert rows[0]["name"] == "My_Server_1_.ovpn" and rows[0]["valid"] is True
    assert "BEGIN" not in str(rows) and "PRIVATE KEY" not in str(rows)            # never the profile's contents
    assert a.client.delete("/api/net/profiles/My_Server_1_.ovpn").status_code == 200
    assert a.client.delete("/api/net/profiles/My_Server_1_.ovpn").status_code == 404
    assert uploads_left(a) == []


def test_hostile_and_oversized_profiles_are_refused(a):
    r = a.client.post("/api/net/profiles", **multipart("notes.txt", OVPN.encode()))
    assert r.status_code == 400
    r = a.client.post("/api/net/profiles", **multipart("big.ovpn", b"#" * (1024 * 1024 + 10)))
    assert r.status_code == 413
    assert list(a.farm.paths.vpn.iterdir()) == [] and uploads_left(a) == []


@pytest.mark.parametrize("name", ["..", "..%2F..%2Fsecrets%2Fapi-token", "%2Fetc%2Fpasswd", "x/../../y", ".hidden"])
def test_profile_delete_cannot_reach_other_files(a, name):
    r = a.client.delete(f"/api/net/profiles/{name}")
    assert r.status_code in (400, 404), r.text
    assert (a.farm.paths.secrets / "api-token").is_file()


def test_credentials_are_write_only(a):
    r = a.client.put("/api/net/credentials/home", json={"username": "user-" + SECRET, "password": SECRET})
    assert r.status_code == 200 and r.json() == {"name": "home", "stored": True}
    stored = a.farm.paths.secrets / "vpn-home.auth"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600 and SECRET in stored.read_text()
    listed = a.client.get("/api/net/credentials")
    assert listed.json() == {"credentials": [{"name": "home"}]}
    assert SECRET not in r.text and SECRET not in listed.text
    # nothing the API offers reads them back
    for path in ("/api/net/credentials/home", "/api/net/profiles", "/api/config", "/api/system"):
        assert SECRET not in a.client.get(path).text
    assert a.client.get("/api/net/credentials/home").status_code == 405
    assert a.client.delete("/api/net/credentials/home").status_code == 200
    assert a.client.delete("/api/net/credentials/home").status_code == 404
    assert a.client.get("/api/net/credentials").json() == {"credentials": []}


@pytest.mark.parametrize("body", [{"username": "u"}, {"password": "p"}, {"username": "", "password": "p"},
                                  {"username": "u", "password": "p", "note": "x"}])
def test_rejected_credentials_are_not_echoed(a, body):
    body = {k: (v + SECRET if v else v) for k, v in body.items()}
    r = a.client.put("/api/net/credentials/home", json=body)
    assert r.status_code == 422 and SECRET not in r.text


def test_credentials_with_a_line_break_are_refused_without_echo(a):
    r = a.client.put("/api/net/credentials/home", json={"username": "u", "password": SECRET + "\nsecond"})
    assert r.status_code == 400 and SECRET not in r.text
    assert not (a.farm.paths.secrets / "vpn-home.auth").exists()


@pytest.mark.parametrize("name", ["..", "a b", "x" * 80, "..%2Fapi-token", "%2e%2e%2fapi-token"])
def test_credential_names_cannot_leave_the_secrets_folder(a, name):
    r = a.client.put(f"/api/net/credentials/{name}", json={"username": "u", "password": "p"})
    assert r.status_code in (400, 404), r.text
    assert sorted(p.name for p in a.farm.paths.secrets.iterdir()) == ["api-token"]
    assert a.client.delete(f"/api/net/credentials/{name}").status_code in (400, 404)
    assert (a.farm.paths.secrets / "api-token").is_file()


def test_gateways_are_saved_in_the_config_and_brought_up_on_request(a, monkeypatch):
    a.client.post("/api/net/profiles", **multipart("eu.ovpn", OVPN.encode()))
    calls = []
    monkeypatch.setattr(gateway_mod, "status", lambda docker, config, name: {"name": name, "state": "missing"})
    monkeypatch.setattr(gateway_mod, "ensure", lambda docker, config, paths, name: calls.append(("ensure", name)) or {"name": name, "action": "created"})
    monkeypatch.setattr(gateway_mod, "remove", lambda docker, config, name, force=False: calls.append(("remove", name, force)) or {"name": name, "removed": ["sfx-gw-eu"]})
    r = a.client.put("/api/net/gateways/eu", json={"profile": "eu.ovpn"})
    assert r.status_code == 200 and a.farm.config.network.gateways["eu"].profile == "eu.ovpn"
    listed = a.client.get("/api/net/gateways").json()["gateways"]
    assert listed == [{"name": "eu", "state": "missing", "config": {"provider": "openvpn", "profile": "eu.ovpn",
                                                                     "credentials": "default", "subnet": ""}}]
    assert a.client.post("/api/net/gateways/eu/up").json()["action"] == "created"
    assert a.client.post("/api/net/gateways/eu/down", params={"force": "true"}).status_code == 200
    assert calls == [("ensure", "eu"), ("remove", "eu", True)]
    assert a.client.delete("/api/net/gateways/eu").json() == {"deleted": "eu", "removed": ["sfx-gw-eu"]}
    assert a.farm.config.network.gateways == {}


def test_gateway_routes_validate(a, monkeypatch):
    monkeypatch.setattr(gateway_mod, "remove", lambda *args, **kw: pytest.fail("removed"))
    assert a.client.put("/api/net/gateways/eu", json={"profile": "missing.ovpn"}).status_code == 404
    assert a.client.put("/api/net/gateways/Bad Name", json={"profile": "x.ovpn"}).status_code == 400
    assert a.client.put("/api/net/gateways/eu", json={}).status_code == 422
    assert a.client.put("/api/net/gateways/eu", json={"profile": "x.ovpn", "subnet": "not-a-subnet"}).status_code == 400
    assert a.client.post("/api/net/gateways/eu/up").status_code == 404
    assert a.client.post("/api/net/gateways/eu/sideways").status_code in (404, 422)
    assert a.client.delete("/api/net/gateways/eu").status_code == 404
    assert a.farm.config.network.gateways == {}


def test_a_gateway_in_use_is_not_deleted(a, monkeypatch):
    monkeypatch.setattr(gateway_mod, "remove", lambda *args, **kw: pytest.fail("removed"))
    a.client.post("/api/net/profiles", **multipart("eu.ovpn", OVPN.encode()))
    a.client.put("/api/net/gateways/eu", json={"profile": "eu.ovpn"})
    a.client.patch("/api/instances/2", json={"patch": {"network": {"mode": "gateway", "gateway": "eu"}}})
    r = a.client.delete("/api/net/gateways/eu")
    assert r.status_code == 400 and "Android(s) 2" in r.json()["error"]["message"]
    assert "eu" in a.farm.config.network.gateways


def test_instance_net_status(a, monkeypatch):
    from superfarm.api.routes import net as net_routes
    monkeypatch.setattr(net_routes, "net_status", lambda docker, r: {"mode": r.net_mode, "state": "connected", "public_ip": None})
    monkeypatch.setattr(net_routes, "net_public_ip", lambda docker, r, url: "198.51.100.7")
    assert a.client.get("/api/instances/1/net").json() == {"mode": "direct", "state": "connected", "public_ip": None}
    assert a.client.get("/api/instances/1/net", params={"refresh": "true"}).json()["public_ip"] == "198.51.100.7"
    assert a.client.get("/api/instances/9/net").status_code == 404

"""Files to and from Androids: path confinement and the commands that reach the device."""
import importlib.util
import sys
from pathlib import Path

import pytest

from superfarm.config import Config
from superfarm.control import files as files_mod
from superfarm.control.files import Files, gallery_folder, parse_listing, validate_remote
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


@pytest.fixture
def env(tmp_path):
    mgr, tasks = S.FakeManager(ids=(1, 2)), TaskManager(workers=4)
    local = tmp_path / "holiday photo.jpg"
    local.write_bytes(b"\xff\xd8jpeg")
    yield mgr, tasks, Files(mgr, tasks, get_paths(tmp_path / "home").ensure()), local
    tasks.shutdown()


def done(tasks, task):
    tasks.wait(task, timeout=20)
    assert task.state == "done", (task.state, task.error)
    return task


@pytest.mark.parametrize("path,expected", [
    ("/sdcard/Download/a.txt", "/sdcard/Download/a.txt"), ("/sdcard", "/sdcard"), ("/sdcard//Pictures/./x.png", "/sdcard/Pictures/x.png"),
    ("/data/local/tmp/x", "/data/local/tmp/x"), ("/storage/emulated/0/DCIM/", "/storage/emulated/0/DCIM"),
    ("/sdcard/a/../b", "/sdcard/b"), ("//sdcard/x", "/sdcard/x"), ("/sdcard/with space & $(id) `x`.txt", "/sdcard/with space & $(id) `x`.txt"),
])
def test_allowed_remote_paths(path, expected):
    assert validate_remote(path) == expected


@pytest.mark.parametrize("path", [
    "/data/data/org.example/files/x", "/system/build.prop", "/sdcard/../data/x", "/sdcard/../../etc/hosts", "/sdcardx/y",
    "/data/local/tmpfoo", "/", "/data/local", "relative/path", "", "sdcard/x", "/sdcard/a\nb", "/sdcard/a\x00b", "/sdcard/" + "x" * 2000, None,
])
def test_refused_remote_paths(path):
    with pytest.raises(ValueError):
        validate_remote(path)


def test_allow_any_lifts_the_folder_rule_but_not_the_sanity_checks():
    assert validate_remote("/data/data/org.example/../x", allow_any=True) == "/data/data/x"
    for bad in ("relative", "/a\nb", ""):
        with pytest.raises(ValueError):
            validate_remote(bad, allow_any=True)


def test_gallery_folder():
    assert gallery_folder("a.JPG") == "/sdcard/Pictures" and gallery_folder("clip.mp4") == "/sdcard/Movies"
    with pytest.raises(ValueError, match="not a picture or video"):
        gallery_folder("notes.txt")


def test_parse_listing():
    rows = parse_listing("regular file|157|1791768667|660|/sdcard/Pictures/two.png\n"
                         "directory|4096|1791768493|2770|/sdcard/Pictures/.thumbnails\n"
                         "regular empty file|0|1791768000|660|/sdcard/Pictures/odd|name.txt\n"
                         "symbolic link|12|1791768001|777|/sdcard/Pictures/link\n"
                         "stat: '/x': No such file or directory\n", "/sdcard/Pictures")
    assert [(r["name"], r["type"], r["size"]) for r in rows] == [
        (".thumbnails", "dir", 4096), ("link", "link", 12), ("odd|name.txt", "file", 0), ("two.png", "file", 157)]
    assert rows[0]["path"] == "/sdcard/Pictures/.thumbnails" and rows[3]["modified"] == 1791768667 and rows[3]["mode"] == "660"


def test_push_to_file_and_to_folder(env):
    mgr, tasks, files, local = env
    task = done(tasks, files.push("all", local, "/sdcard/Download/renamed.jpg"))
    assert task.kind == "file-push" and task.results[1]["detail"] == "saved as /sdcard/Download/renamed.jpg"
    assert mgr.devices[2].pushed == [(str(local), "/sdcard/Download/renamed.jpg")]
    assert mgr.devices[2].ran(r"^mkdir -p /sdcard/Download$")
    task = done(tasks, files.push("1", local, "/sdcard/Download/"))              # trailing slash: keep the file's own (safe) name
    assert task.results[1]["detail"] == "saved as /sdcard/Download/holiday_photo.jpg"
    mgr.devices[1].on(r"^stat -L -c .* /sdcard/Docs$", "directory|4096")
    assert done(tasks, files.push("1", local, "/sdcard/Docs")).results[1]["detail"] == "saved as /sdcard/Docs/holiday_photo.jpg"


def test_push_refuses_bad_destinations_and_missing_or_huge_files(env, tmp_path):
    mgr, tasks, files, local = env
    with pytest.raises(ValueError, match="outside"):
        files.push("all", local, "/system/app/x.apk")
    with pytest.raises(FileNotFoundError):
        files.push("all", tmp_path / "nope.bin", "/sdcard/x")
    mgr.config = Config.model_validate({"control": {"max_transfer_mb": 1}})
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x00" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="larger than 1 MB"):
        files.push("all", big, "/sdcard/x")
    assert tasks.list() == []


def test_shell_metacharacters_in_names_reach_the_device_as_one_argument(env):
    mgr, tasks, files, local = env
    done(tasks, files.push("1", local, "/sdcard/a b; rm -rf $(x)/f.jpg"))
    assert mgr.devices[1].argv(-1) == ["mkdir", "-p", "/sdcard/a b; rm -rf $(x)"]
    assert mgr.devices[1].pushed[-1][1] == "/sdcard/a b; rm -rf $(x)/f.jpg"


def test_to_gallery_pushes_then_scans_and_reports_the_index(env):
    mgr, tasks, files, local = env
    mgr.devices[1].on(r"^content query", "Row: 0 _data=/storage/emulated/0/Pictures/holiday_photo.jpg\n")
    task = done(tasks, files.to_gallery("all", local))
    assert task.results[1]["detail"] == "/sdcard/Pictures/holiday_photo.jpg (in the media index)"
    assert task.results[2]["detail"] == "/sdcard/Pictures/holiday_photo.jpg (pushed; not in the media index yet)"
    dev = mgr.devices[1]
    assert dev.pushed == [(str(local), "/sdcard/Pictures/holiday_photo.jpg")]
    assert dev.ran(r"MEDIA_SCANNER_SCAN_FILE -d file:///storage/emulated/0/Pictures/holiday_photo\.jpg") and dev.ran(r"scan_volume")
    assert "content://media/external/images/media" in dev.calls[-1]
    video = local.with_name("clip.mp4")
    video.write_bytes(b"mp4")
    assert done(tasks, files.to_gallery("1", video, name="My Clip.mp4")).results[1]["detail"].startswith("/sdcard/Movies/My_Clip.mp4")
    with pytest.raises(ValueError):
        files.to_gallery("1", local, name="notes.txt")


def test_pull_checks_type_and_size_then_fetches_into_a_fresh_folder(env):
    mgr, tasks, files, _ = env
    dev = mgr.devices[1].on(r"^stat -L -c .* /sdcard/Download/report\.pdf$", "regular file|2048")
    dest = files.pull(1, "/sdcard/Download/report.pdf")
    assert dest.name == "report.pdf" and dest.read_bytes() == b"pulled:/sdcard/Download/report.pdf"
    assert dest.parent.parent == files.paths.run / "pulls" and dev.pulled == [("/sdcard/Download/report.pdf", str(dest))]
    with pytest.raises(FileNotFoundError):
        files.pull(1, "/sdcard/missing.txt")
    dev.on(r"^stat -L -c .* /sdcard/DCIM$", "directory|4096")
    with pytest.raises(IsADirectoryError):
        files.pull(1, "/sdcard/DCIM")
    dev.on(r"^stat -L -c .* /sdcard/huge\.bin$", f"regular file|{5 * 1024 ** 3}")
    with pytest.raises(ValueError, match="larger than"):
        files.pull(1, "/sdcard/huge.bin")
    with pytest.raises(ValueError, match="outside"):
        files.pull(1, "/data/system/users/0/accounts.db")
    assert len(dev.pulled) == 1 and len(list((files.paths.run / "pulls").iterdir())) == 1


def test_list_and_delete(env):
    mgr, tasks, files, _ = env
    dev = mgr.devices[1].on(r"^stat -L -c .* /sdcard$", "directory|4096").on(
        r"^find -H /sdcard ", "directory|4096|1|770|/sdcard/DCIM\nregular file|3|2|660|/sdcard/a.txt\n")
    assert [e["name"] for e in files.list(1)] == ["DCIM", "a.txt"]
    assert dev.argv() == ["find", "-H", "/sdcard", "-maxdepth", "1", "-mindepth", "1", "-exec", "stat", "-c", files_mod._STAT_FORMAT, "{}", "+"]
    with pytest.raises(FileNotFoundError):
        files.list(1, "/sdcard/none")
    dev.on(r"^stat -L -c .* /sdcard/a\.txt$", "regular file|3")
    with pytest.raises(NotADirectoryError):
        files.list(1, "/sdcard/a.txt")
    task = done(tasks, files.delete("all", "/sdcard/a.txt"))
    assert task.results[1]["detail"] == "deleted" and dev.argv() == ["rm", "-f", "--", "/sdcard/a.txt"]
    assert task.results[2]["detail"] == "was not there"
    dev.on(r"^stat -L -c .* /sdcard/DCIM$", "directory|4096")
    assert "is a folder" in done(tasks, files.delete("1", "/sdcard/DCIM")).results[1]["detail"]
    with pytest.raises(ValueError, match="refusing"):
        files.delete("all", "/sdcard/")

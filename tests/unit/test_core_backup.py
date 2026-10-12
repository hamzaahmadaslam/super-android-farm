"""Backups of one Android: folder layout + manifest, verification, restore rules, safety
backups, retention, the schedule.

The helper container is replaced by a small script that plays `docker run ... tar`, so the
real streaming code (a child process writing into a file this process opened) is exercised.
"""
import hashlib
import json
import os
import stat
import sys
import time
from datetime import datetime

import yaml

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core_fakes import PREFIX, Farm, options  # noqa: E402

from superfarm.core import backup as bk  # noqa: E402
from superfarm.core.backup import BackupError, Backups, select_for_pruning  # noqa: E402
from superfarm.core.instances import InstanceError, NeedsForce  # noqa: E402
from superfarm.core.tasks import Cancelled, Task  # noqa: E402

ANDROID, NET, VOLUME = f"{PREFIX}-android-1", f"{PREFIX}-net-1", f"{PREFIX}-data-1"
PAYLOAD = b"ARCHIVE-BYTES-" * 1000

FAKE_DOCKER = r"""#!/bin/sh
# Stands in for `docker run --rm ... <image> tar|find ...` in the backup tests.
echo "$*" >> "$FAKE_LOG"
case "$*" in
  *"-cf - ."*) [ -n "$FAKE_SLEEP" ] && sleep "$FAKE_SLEEP"
               cat "$FAKE_PAYLOAD"; echo "tar: ./file: file changed as we read it" >&2; exit "${FAKE_EXIT:-0}" ;;
  *"-xpf -"*)  cat > "$FAKE_SINK"; exit "${FAKE_EXTRACT_EXIT:-0}" ;;
  *"-tf -"*)   cat > /dev/null; exit "${FAKE_LIST_EXIT:-0}" ;;
  *find*)      exit 0 ;;
esac
exit 64
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    farm = Farm(tmp_path, {"backup": {"auto_before_risky": True}})
    script = tmp_path / "fake-docker"
    script.write_text(FAKE_DOCKER)
    script.chmod(0o755)
    (tmp_path / "payload").write_bytes(PAYLOAD)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("FAKE_PAYLOAD", str(tmp_path / "payload"))
    monkeypatch.setenv("FAKE_SINK", str(tmp_path / "sink"))
    for name in ("FAKE_EXIT", "FAKE_SLEEP", "FAKE_EXTRACT_EXIT", "FAKE_LIST_EXIT"):
        monkeypatch.delenv(name, raising=False)
    farm.docker.binary = str(script)
    farm.docker.images[farm.mgr.images.tools_tag] = {"id": "sha256:tools", "labels": {}}      # helper image already built
    farm.log = tmp_path / "log"
    farm.sink = tmp_path / "sink"
    farm.backups = farm.mgr.backups
    yield farm
    farm.close()


def helper_lines(farm) -> list[str]:
    return farm.log.read_text().splitlines() if farm.log.exists() else []


def started(farm, **template):
    farm.mgr.create(template=template or None)
    farm.mgr.start(1)
    return farm.store.get(1)


# ---------------------------------------------------------------------- arguments

def test_tar_keeps_everything_android_needs():
    create = bk.create_arguments("zstd", 3)
    for flag in ("--xattrs", "--xattrs-include=*", "--acls", "--numeric-owner", "--sparse"):
        assert flag in create and flag in bk.extract_arguments("zstd")
    assert create[:3] == ["tar", "-C", "/data"] and create[-3:] == ["-cf", "-", "."]
    assert create[create.index("-I") + 1] == "zstd -T4 -3"
    assert bk.create_arguments("gzip", 19)[bk.create_arguments("gzip", 19).index("-I") + 1] == "pigz -p 4 -9"
    assert "-I" not in bk.create_arguments("none", 3)
    assert bk.extract_arguments("zstd")[-3:] == ["--zstd", "-xpf", "-"]
    assert "--gzip" in bk.extract_arguments("gzip") and bk.extract_arguments("none")[-2:] == ["-xpf", "-"]
    with pytest.raises(BackupError):
        bk.create_arguments("rar", 1)
    with pytest.raises(BackupError):
        bk.extract_arguments("rar")


# ------------------------------------------------------------------------- create

def test_create_writes_a_folder_with_data_settings_and_manifest(env):
    spec = started(env)
    env.mgr.stop(1)
    task = Task(id="t", kind="backup", title="t")
    folder = env.backups.create(1, task=task)

    assert folder.parent == env.paths.backups / "android-1" and bk._HANDLE.match(f"android-1/{folder.name}")
    assert sorted(p.name for p in folder.iterdir()) == ["data.tar.zst", "instance.yaml", "manifest.json"]
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in folder.iterdir())
    data = env.backups.data_file(folder)
    assert data == folder / "data.tar.zst" and data.read_bytes() == PAYLOAD
    assert yaml.safe_load((folder / "instance.yaml").read_text()) == spec.model_dump(mode="json")

    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest == env.backups.manifest(folder)
    assert manifest["kind"] == "superfarm-android-backup" and manifest["format"] == 2 and manifest["id"] == 1
    assert manifest["superfarm_version"] == __import__("superfarm").__version__
    assert manifest["files"] == {
        "data.tar.zst": {"size": len(PAYLOAD), "sha256": hashlib.sha256(PAYLOAD).hexdigest()},
        "instance.yaml": {"size": (folder / "instance.yaml").stat().st_size,
                          "sha256": hashlib.sha256((folder / "instance.yaml").read_bytes()).hexdigest()}}
    assert manifest["compression"] == "zstd" and manifest["consistent"] is True and manifest["reason"] == "manual"
    assert manifest["android_version"] == "13" and manifest["image"] == "local/android:13"
    assert manifest["data_volume"] == VOLUME and manifest["android_id"] == spec.device.android_id
    line = helper_lines(env)[-1]
    assert "--network none" in line and f"-v {VOLUME}:/data:ro" in line          # read-only, offline
    assert "--xattrs --xattrs-include=* --acls --numeric-owner --sparse" in line
    assert f"backup android-1/{folder.name}" in task.message


def test_running_android_is_stopped_for_the_copy_and_started_again(env):
    started(env)
    env.docker.calls.clear()
    folder = env.backups.create(1)
    kinds = [(c[0], c[1]) for c in env.docker.calls if c[0] in ("stop", "start")]
    assert kinds[:2] == [("stop", ANDROID), ("stop", NET)]
    assert kinds[-2:] == [("start", NET), ("start", ANDROID)]
    assert env.docker.running(ANDROID)
    assert env.backups.manifest(folder)["consistent"] is True


def test_an_android_stopped_for_its_backup_comes_back_even_when_capacity_is_enforced(env):
    started(env)
    env.mgr.config.performance.enforce_capacity = True
    env.capacity_max = 0                                     # a fresh start would be refused now
    env.backups.create(1)
    assert env.docker.running(ANDROID)


def test_live_backup_is_marked_inconsistent_and_tolerates_changing_files(env, monkeypatch):
    started(env)
    env.mgr.config.backup.stop_for_backup = False
    monkeypatch.setenv("FAKE_EXIT", "1")               # tar: "file changed as we read it"
    env.docker.calls.clear()
    folder = env.backups.create(1)
    assert not [c for c in env.docker.calls if c[0] == "stop"]
    assert env.backups.manifest(folder)["consistent"] is False


def test_tar_warning_exit_is_an_error_for_a_stopped_android(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    monkeypatch.setenv("FAKE_EXIT", "1")
    with pytest.raises(BackupError, match="file changed"):
        env.backups.create(1)
    assert list((env.paths.backups / "android-1").iterdir()) == [] and env.backups.list() == []


def test_failed_tar_leaves_no_files_and_restarts_the_android(env, monkeypatch):
    started(env)
    monkeypatch.setenv("FAKE_EXIT", "2")
    with pytest.raises(BackupError, match="exit 2"):
        env.backups.create(1)
    assert list((env.paths.backups / "android-1").iterdir()) == []
    assert env.docker.running(ANDROID)


def test_backup_needs_existing_data_and_an_existing_android(env):
    env.mgr.create()
    with pytest.raises(BackupError, match="no data yet"):
        env.backups.create(1)
    with pytest.raises(KeyError):
        env.backups.create(5)


def test_backup_to_another_folder_and_other_compressions(env, tmp_path):
    started(env)
    env.mgr.stop(1)
    env.mgr.config.backup.compression = "gzip"
    folder = env.backups.create(1, dest=tmp_path / "usb" / "farm")
    assert folder.parent == tmp_path / "usb" / "farm" / "android-1" and (folder / "data.tar.gz").is_file()
    assert "-I pigz -p 4 -3" in helper_lines(env)[-1]
    assert env.backups.verify(folder) and env.backups.list() == []          # found by path, not in the default folder
    assert [b["dir"] for b in env.backups.list(tmp_path / "usb" / "farm")] == [str(folder)]
    env.mgr.config.backup.compression = "none"
    assert env.backups.data_file(env.backups.create(1)).name == "data.tar"


def test_cancelling_removes_the_helper_and_the_unfinished_folder(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    monkeypatch.setenv("FAKE_SLEEP", "30")
    task = Task(id="t", kind="backup", title="t")
    real_step, steps = task.step, []

    def step(message=None, **kw):                       # cancel once the copy is under way
        steps.append(message)
        real_step(message, **kw)
        if message and "copying" in message:
            task._cancel.set()

    task.step = step
    began = time.monotonic()
    with pytest.raises(Cancelled):
        env.backups.create(1, task=task)
    assert time.monotonic() - began < 10
    assert any(n.startswith(f"{PREFIX}-backup-1-") for n in env.docker.names_called("rm"))
    assert list((env.paths.backups / "android-1").iterdir()) == []


def test_an_android_being_backed_up_is_locked(env, monkeypatch):
    started(env)
    seen = []
    original = env.backups._write

    def write(*a, **k):
        seen.append(env.mgr.list_status()[0]["busy"])
        return original(*a, **k)

    env.backups._write = write
    env.backups.create(1)
    assert seen == ["backup"]


def test_two_backups_in_one_second_get_different_names(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    monkeypatch.setattr(bk.time, "gmtime", lambda *a: time.struct_time(time.localtime(1_700_000_000)))
    a, b = env.backups.create(1), env.backups.create(1)
    assert a != b and b.name == a.name + "-2" and len(env.backups.list()) == 2


def test_helper_mounts_only_what_it_is_given(env):
    started(env)
    env.mgr.stop(1)
    env.backups.create(1)
    line = helper_lines(env)[-1].split()
    assert options(line, "-v") == [f"{VOLUME}:/data:ro"]
    assert "--privileged" not in line and "--rm" in line


# --------------------------------------------------------------- names and paths

def test_a_backup_is_found_by_name_folder_or_any_file_in_it(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    name = f"android-1/{folder.name}"
    for handle in (name, folder, str(folder), folder / "data.tar.zst", folder / "manifest.json"):
        assert env.backups.locate(handle) == folder and env.backups.verify(handle)


@pytest.mark.parametrize("name", [
    "../outside/20240101-000000", "android-1/../../etc", "android-1", "20240101-000000", "android-1/20240101-000000/x",
    "android-x/20240101-000000", "farm/../android-1/20240101-000000", "android-1/2024", "", ".",
])
def test_a_relative_name_can_only_mean_a_backup_in_the_backup_folder(env, name):
    with pytest.raises(BackupError, match="bad backup name"):
        env.backups.locate(name)
    assert env.backups.verify(name) is False and env.backups.manifest(name) is None


def test_a_folder_without_a_manifest_is_not_a_backup(env, tmp_path):
    (env.paths.backups / "android-1" / "20240101-000000").mkdir(parents=True)
    with pytest.raises(BackupError, match="is not a backup"):
        env.backups.locate("android-1/20240101-000000")
    with pytest.raises(BackupError, match="is not a backup"):
        env.backups.locate(tmp_path)
    assert env.backups.list() == []


def test_a_manifest_cannot_point_at_files_outside_its_folder(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    meta = folder / "manifest.json"
    data = json.loads(meta.read_text())
    data["files"]["../../../etc/hostname"] = {"size": 1, "sha256": "x"}
    meta.write_text(json.dumps(data))
    assert env.backups.manifest(folder) is None and not env.backups.verify(folder)
    with pytest.raises(BackupError, match="not a backup of an Android"):
        env.backups.restore(folder)


# ------------------------------------------------------------------------- verify

def test_verify_detects_damage(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    data = folder / "data.tar.zst"
    assert env.backups.verify(folder) and env.backups.verify(folder, deep=True)
    assert "-tf -" in helper_lines(env)[-1] and "--zstd" in helper_lines(env)[-1]

    damaged = bytearray(PAYLOAD)
    damaged[100] ^= 0xFF
    data.write_bytes(bytes(damaged))
    assert not env.backups.verify(folder)                     # same size, one flipped bit
    data.write_bytes(PAYLOAD[:-1])
    assert not env.backups.verify(folder)                     # truncated
    data.write_bytes(PAYLOAD)
    assert env.backups.verify(folder)
    settings = (folder / "instance.yaml").read_text()
    (folder / "instance.yaml").write_text(settings.replace("id: 1", "id: 7"))
    assert not env.backups.verify(folder)                     # every file is covered, not only the data
    (folder / "instance.yaml").write_text(settings)
    (folder / "instance.yaml").unlink()
    assert not env.backups.verify(folder)                     # a listed file is gone
    (folder / "manifest.json").unlink()
    assert not env.backups.verify(folder)                     # nothing to check against
    assert not env.backups.verify("android-9/20000101-000000")


def test_deep_verify_fails_when_tar_cannot_read_the_archive(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    monkeypatch.setenv("FAKE_LIST_EXIT", "2")
    assert env.backups.verify(folder) and not env.backups.verify(folder, deep=True)


# ------------------------------------------------------------------------ restore

def test_restore_over_an_android_needs_force_and_says_what_else_is_possible(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    before = len(helper_lines(env))
    with pytest.raises(NeedsForce, match="replaces everything that Android holds now") as refused:
        env.backups.restore(folder, 1)
    assert "restore without an id" in str(refused.value) and refused.value.code == "needs_force"
    assert len(helper_lines(env)) == before and not env.sink.exists()        # refused before any work
    with pytest.raises(KeyError):
        env.backups.restore(folder, 9)                        # no such Android: not a force question


def test_restore_is_refused_over_a_running_android_even_when_forced(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    env.mgr.start(1)
    before = len(helper_lines(env))
    with pytest.raises(BackupError, match="never restored over a running Android"):
        env.backups.restore(folder, 1, force=True)
    assert len(helper_lines(env)) == before and not env.sink.exists()
    assert len(env.backups.list()) == 1                       # and no safety backup was taken for nothing


def test_restore_refuses_a_damaged_or_incomplete_backup_before_touching_data(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    before = len(helper_lines(env))
    (folder / "data.tar.zst").write_bytes(PAYLOAD[:-5] + b"XXXXX")
    with pytest.raises(BackupError, match="checksum"):
        env.backups.restore(folder, 1, force=True)
    with pytest.raises(BackupError, match="checksum"):
        env.backups.restore(folder)
    (folder / "manifest.json").unlink()
    with pytest.raises(BackupError, match="is not a backup"):
        env.backups.restore(folder, 1, force=True)
    with pytest.raises(BackupError, match="is not a backup"):
        env.backups.restore("android-1/19990101-000000", 1, force=True)
    assert len(helper_lines(env)) == before and env.store.ids() == [1]


def test_forced_restore_over_an_android_backs_up_its_current_data_first(env):
    spec = started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    env.store.state_set(1, "trim", {"hash": "x"})
    env.store.state_set(1, "data_android_version", "14")
    before = len(helper_lines(env))
    task = Task(id="t", kind="restore", title="t")
    assert env.backups.restore(folder, 1, task=task, force=True) == 1
    safety, wipe, extract = helper_lines(env)[before:]
    assert "-cf - ." in safety and f"-v {VOLUME}:/data:ro" in safety          # the copy comes before the wipe
    assert "find /data -mindepth 1 -delete" in wipe and f"-v {VOLUME}:/data" in wipe
    assert "--zstd -xpf -" in extract and "--xattrs --xattrs-include=* --acls --numeric-owner --sparse" in extract
    assert " -i " in extract and "--network none" in extract
    assert env.sink.read_bytes() == PAYLOAD
    rows = env.backups.list()
    assert [r["reason"] for r in rows] == ["before-restore", "manual"]
    assert env.store.state_get(1, "safety_backup") == rows[0]["dir"]
    assert env.store.get(1).device == spec.device
    # what the farm believed about the old data is forgotten; the markers in /data decide
    assert env.store.state_get(1, "trim") is None and env.store.state_get(1, "renderer") is None
    assert env.store.state_get(1, "data_android_version") == "13"            # the release that wrote the restored data


def test_restore_over_goes_ahead_without_a_safety_backup_only_because_it_is_forced(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    monkeypatch.setenv("FAKE_EXIT", "2")                      # the safety copy fails
    task = Task(id="t", kind="restore", title="t")
    steps = []
    real = task.step
    task.step = lambda message=None, **kw: (steps.append(message), real(message, **kw))[1]
    assert env.backups.restore(folder, 1, task=task, force=True) == 1
    assert any("safety backup (before-restore) failed and the operation went ahead because it was forced" in (m or "")
               for m in steps)
    assert env.sink.read_bytes() == PAYLOAD and env.store.state_get(1, "safety_backup") is None


def test_no_safety_backup_when_the_target_has_no_data_or_the_setting_is_off(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    env.mgr.create()                                          # Android 2: never started, no volume
    assert env.backups.restore(folder, 2, force=True) == 2
    env.mgr.config.backup.auto_before_risky = False
    assert env.backups.restore(folder, 1, force=True) == 1
    assert [r["reason"] for r in env.backups.list()] == ["manual"]


def test_restore_into_another_android_moves_the_identity_with_the_data(env):
    spec = started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    env.mgr.create()
    assert env.store.get(2).device.android_id != spec.device.android_id
    assert env.backups.restore(folder, 2, force=True) == 2
    assert env.store.get(2).device == spec.device
    assert f"{PREFIX}-data-2" in env.docker.volume_labels


def test_restore_as_a_new_android_next_to_the_original_comes_up_offline(env):
    spec = started(env, name="Shop", tags=["eu"], fps=30)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    new_id = env.backups.restore(folder)                      # no force needed: nothing existing is touched
    new = env.store.get(new_id)
    assert new_id == 2 and new.name == "Shop" and new.tags == ["eu"] and new.fps == 30
    assert new.device == spec.device                          # exactly the Android that was saved
    assert new.network.mode == "none"                         # ...so it must not go online beside its twin
    assert "Android 1 has the same identity" in new.notes and f"Restored from android-1/{folder.name}" in new.notes
    assert env.sink.read_bytes() == PAYLOAD
    assert env.docker.volume_labels[f"{PREFIX}-data-2"]["io.superfarm.instance"] == "2"
    assert env.store.state_get(2, "data_android_version") == "13"
    assert env.store.get(1) == spec and len(env.backups.list()) == 1          # the original: untouched, not backed up again


def test_the_operator_can_keep_the_network_of_a_restored_twin(env):
    started(env, network={"mode": "proxy", "proxy": "10.0.0.5:3128"})
    env.mgr.stop(1)
    folder = env.backups.create(1)
    new = env.store.get(env.backups.restore(folder, keep_network=True))
    assert new.network.mode == "proxy" and "same identity" not in new.notes


def test_restore_as_a_new_android_after_the_original_is_gone_keeps_its_network(env):
    started(env, network={"mode": "proxy", "proxy": "10.0.0.5:3128"})
    env.mgr.stop(1)
    folder = env.backups.create(1)
    env.mgr.delete(1)
    new = env.store.get(env.backups.restore(folder))
    assert new.network.mode == "proxy" and new.network.proxy == "10.0.0.5:3128"
    assert "same identity" not in new.notes and "Restored from" in new.notes


def edit_saved_settings(folder, change) -> dict:
    """Rewrite a backup's instance.yaml the way someone with file access could, keeping the
    manifest consistent so the checksum is not what stops the restore."""
    saved = yaml.safe_load((folder / "instance.yaml").read_text())
    change(saved)
    (folder / "instance.yaml").write_text(yaml.safe_dump(saved))
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"]["instance.yaml"] = {"size": (folder / "instance.yaml").stat().st_size,
                                          "sha256": hashlib.sha256((folder / "instance.yaml").read_bytes()).hexdigest()}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return saved


def test_restore_falls_back_to_farm_defaults_for_settings_this_farm_lacks(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)

    def change(saved):
        saved["android_version"] = "8"                       # an image this farm does not have
        saved["device"]["profile"] = "a-preset-from-the-future"

    saved = edit_saved_settings(folder, change)
    env.mgr.delete(1)
    new = env.store.get(env.backups.restore(folder))
    assert new.android_version is None and new.device.profile == "none"
    assert new.device.android_id == saved["device"]["android_id"]
    assert new.device.props == saved["device"]["props"]


def test_a_restored_android_is_pinned_to_the_release_that_wrote_its_data(env):
    started(env)                                              # follows the farm default, 13
    env.mgr.stop(1)
    folder = env.backups.create(1)
    assert env.store.get(1).android_version is None
    new = env.store.get(env.backups.restore(folder))
    assert new.android_version == "13"                        # a later change of the default cannot move it


def test_a_backup_cannot_make_the_farm_fetch_an_image(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    edit_saved_settings(folder, lambda saved: saved.update(image="registry.example/unknown:latest"))
    new = env.store.get(env.backups.restore(folder))
    assert new.image is None                                 # not on this machine: not taken over
    edit_saved_settings(folder, lambda saved: saved.update(image="local/android:13"))
    assert env.store.get(env.backups.restore(folder)).image == "local/android:13"
    assert env.docker.names_called("pull") == []


def test_a_hand_edited_volume_path_is_never_mounted(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    spec = env.store.get(1)
    spec.data_volume = "/etc"
    env.store.save(spec)
    before = len(helper_lines(env))
    with pytest.raises(Exception, match="not a Docker volume name"):
        env.backups.create(1)
    with pytest.raises(Exception, match="not a Docker volume name"):
        env.backups.restore(folder, 1, force=True)
    assert len(helper_lines(env)) == before


def test_failed_restore_into_a_new_android_leaves_nothing_behind(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    monkeypatch.setenv("FAKE_EXTRACT_EXIT", "2")
    with pytest.raises(BackupError, match="incomplete"):
        env.backups.restore(folder)
    assert env.store.ids() == [1] and f"{PREFIX}-data-2" not in env.docker.volume_labels


def test_restore_into_an_adopted_volume_never_creates_it(env):
    started(env)
    env.mgr.stop(1)
    folder = env.backups.create(1)
    env.docker.volume_labels["old_data"] = {}
    env.mgr.create(template={"data_volume": "old_data"})
    del env.docker.volume_labels["old_data"]
    with pytest.raises(BackupError, match="adopted volume"):
        env.backups.restore(folder, 2, force=True)


# ----------------------------------------------------------- safety before delete

def test_delete_backs_the_data_up_first_and_reports_where(env):
    started(env)
    result = env.mgr.delete(1)
    rows = env.backups.list()
    assert [r["reason"] for r in rows] == ["before-delete"] and rows[0]["consistent"]
    assert result == {"id": 1, "data": "removed", "volume": VOLUME, "backup": rows[0]["dir"], "warnings": []}
    assert env.store.ids() == [] and VOLUME not in env.docker.volume_labels and env.docker.containers == {}
    # the copy was made from the stopped Android, before anything was removed
    calls = [c[:2] for c in env.docker.calls if c[0] in ("stop", "rm", "volume_rm")]
    assert calls.index(("stop", ANDROID)) < calls.index(("rm", ANDROID)) < calls.index(("volume_rm", VOLUME))
    assert env.backups.verify(rows[0]["name"])


def test_delete_is_refused_when_the_safety_backup_fails_and_nothing_is_lost(env, monkeypatch):
    started(env)
    monkeypatch.setenv("FAKE_EXIT", "2")
    with pytest.raises(NeedsForce, match=r"safety backup \(before-delete\) could not be made, so nothing was changed"):
        env.mgr.delete(1)
    assert env.store.ids() == [1] and VOLUME in env.docker.volume_labels
    assert ANDROID in env.docker.containers and not env.docker.running(ANDROID)     # stopped, not removed
    assert env.backups.list() == []
    assert env.mgr.start(1)["state"] == "booted"              # and it still works


def test_forced_delete_goes_ahead_without_a_backup_and_says_so(env, monkeypatch):
    started(env)
    monkeypatch.setenv("FAKE_EXIT", "2")
    result = env.mgr.delete(1, force=True)
    assert result["data"] == "removed" and result["backup"] == ""
    assert "safety backup (before-delete) failed and the operation went ahead because it was forced" in result["warnings"][0]
    assert env.store.ids() == [] and VOLUME not in env.docker.volume_labels


def test_no_safety_backup_when_no_data_would_be_lost(env):
    started(env)
    before = len(helper_lines(env))
    assert env.mgr.delete(1, keep_data=True) == {"id": 1, "data": "kept", "volume": VOLUME, "backup": "", "warnings": []}
    env.mgr.create()                                          # never started: no volume
    assert env.mgr.delete(env.store.ids()[0])["data"] in ("none", "kept")
    env.docker.volume_labels["old_data"] = {}
    adopted = env.mgr.create(template={"data_volume": "old_data"})[0]
    result = env.mgr.delete(adopted.id)                       # an adopted volume is only detached
    assert result["data"] == "kept" and "left in place" in result["warnings"][0]
    assert len(helper_lines(env)) == before and env.backups.list() == []


def test_no_safety_backup_when_the_setting_is_off(env):
    started(env)
    env.mgr.config.backup.auto_before_risky = False
    assert env.mgr.delete(1)["backup"] == "" and env.backups.list() == []


def test_bulk_delete_reports_each_backup_and_each_refusal(env, monkeypatch):
    env.mgr.create(count=2)
    for i in (1, 2):
        env.mgr.start(i)
    monkeypatch.setenv("FAKE_EXIT", "2")
    task = env.tasks.wait(env.mgr.bulk("delete", "all"), timeout=30)
    assert all(not r["ok"] and "NeedsForce" in r["detail"] for r in task.results.values()) and env.store.ids() == [1, 2]
    monkeypatch.delenv("FAKE_EXIT")
    task = env.tasks.wait(env.mgr.bulk("delete", "all"), timeout=30)
    assert all(r["ok"] and "deleted with its data; backup: " in r["detail"] for r in task.results.values())
    assert env.store.ids() == [] and len(env.backups.list()) == 2


def test_safety_is_an_instance_error_so_callers_can_tell_refusals_from_faults(env):
    assert issubclass(NeedsForce, InstanceError) and not issubclass(NeedsForce, BackupError)


# ------------------------------------------------------- list / delete / retention

def make_backups(env, monkeypatch, count: int, ids=(1,)):
    stamps = iter(range(1_700_000_000, 1_700_000_000 + 1000 * 100, 1000))
    out = []
    for instance in ids:
        for _ in range(count):
            now = next(stamps)
            monkeypatch.setattr(bk.time, "time", lambda now=now: now)
            monkeypatch.setattr(bk.time, "gmtime", lambda *a, now=now: time.struct_time(time.localtime(now)))
            out.append(env.backups.create(instance))
    return out


def two_stopped(env):
    env.mgr.create(count=2)
    for i in (1, 2):
        env.mgr.start(i)
        env.mgr.stop(i)


def test_list_is_newest_first_and_ignores_strangers(env, monkeypatch):
    two_stopped(env)
    made = make_backups(env, monkeypatch, 2, ids=(1, 2))
    (env.paths.backups / "notes.json").write_text('{"kind": "something else"}')
    stranger = env.paths.backups / "android-1" / "20200101-000000"
    stranger.mkdir()
    (stranger / "manifest.json").write_text('{"kind": "something else"}')
    (env.paths.backups / "android-1" / "20200101-000001").mkdir()
    (env.paths.backups / "android-1" / "20200101-000001" / "manifest.json").write_text("{not json")
    rows = env.backups.list()
    assert [r["dir"] for r in rows] == [str(p) for p in reversed(made)]
    assert [r["name"] for r in rows] == [f"{p.parent.name}/{p.name}" for p in reversed(made)]
    assert [r["id"] for r in rows] == [2, 2, 1, 1] and {r["kind"] for r in rows} == {"android"}
    assert all(r["present"] and r["size"] == len(PAYLOAD) and r["consistent"] and r["file"].endswith("data.tar.zst")
               for r in rows)
    (made[0] / "data.tar.zst").unlink()
    assert [r["present"] for r in env.backups.list()] == [True, True, True, False]
    assert env.backups.list(env.paths.home / "nowhere") == []


def test_delete_removes_one_backup_and_only_inside_the_backup_folder(env, tmp_path):
    started(env)
    env.mgr.stop(1)
    inside_dir, outside_dir = env.backups.create(1), env.backups.create(1, dest=tmp_path / "usb")
    (inside_dir / "notes-by-the-operator.txt").write_text("keep me")
    env.backups.delete(f"android-1/{inside_dir.name}")
    assert sorted(p.name for p in inside_dir.iterdir()) == ["notes-by-the-operator.txt"]     # only its own files
    assert env.backups.list() == []
    with pytest.raises(BackupError, match="not a backup inside the backup directory"):
        env.backups.delete(outside_dir)
    assert env.backups.verify(outside_dir)
    with pytest.raises(BackupError):
        env.backups.delete("android-1/../../usb")
    second = env.backups.create(1)
    env.backups.delete(second)
    assert not second.exists()


def test_retention_keeps_the_newest_of_each_android():
    def entry(id, created, android_id="a", kind="android"):
        return {"kind": kind, "id": id, "android_id": android_id, "created": created, "dir": f"{kind}-{id}-{created}"}

    entries = [entry(1, t) for t in (10, 30, 20)] + [entry(2, t, "b") for t in (5, 6)] + [entry(None, t, "", "farm") for t in (1, 2, 3)]
    assert select_for_pruning(entries, 0) == []                               # 0 = keep everything
    assert [e["dir"] for e in select_for_pruning(entries, 2)] == ["android-1-10", "farm-None-1"]
    assert [e["dir"] for e in select_for_pruning(entries, 1)] == ["android-1-20", "android-1-10", "android-2-5",
                                                                  "farm-None-2", "farm-None-1"]
    assert select_for_pruning(entries, 3) == []
    # a new Android that took over a deleted one's number does not push out the old one's last backups
    reused = [entry(1, 100, "new"), entry(1, 90, "new"), entry(1, 50, "old")]
    assert select_for_pruning(reused, 1) == [reused[1]]


def test_prune_keeps_the_newest_per_android(env, monkeypatch):
    two_stopped(env)
    made = make_backups(env, monkeypatch, 3, ids=(1, 2))
    env.mgr.config.backup.keep = 2
    removed = env.backups.prune()
    assert sorted(removed) == sorted([made[0], made[3]])       # the oldest of each
    assert len(env.backups.list()) == 4 and not made[0].exists() and not made[3].exists()
    assert env.backups.prune() == []
    env.mgr.config.backup.keep = 0
    assert env.backups.prune() == [] and len(env.backups.list()) == 4


# ----------------------------------------------------------------- many / schedule

def test_create_many_is_one_task_with_a_result_per_android(env, monkeypatch):
    env.mgr.create(count=3)
    env.mgr.start(1)
    env.mgr.start(3)
    env.mgr.stop(3)
    task = env.tasks.wait(env.backups.create_many("all"), timeout=30)
    assert task.state == "done" and task.kind == "backup.manual" and task.targets == [1, 2, 3]
    assert task.results[2] == {"ok": True, "detail": "skipped: no data yet"}
    assert all(task.results[i]["ok"] and "/android-" in task.results[i]["detail"] for i in (1, 3))
    assert task.output == {"ok": 3, "failed": 0, "pruned": []} and len(env.backups.list()) == 2
    monkeypatch.setenv("FAKE_EXIT", "2")
    task = env.tasks.wait(env.backups.create_many("1,3"), timeout=30)
    assert task.state == "done" and task.output["failed"] == 2 and "BackupError" in task.results[1]["detail"]


def at(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


def test_tick_does_nothing_without_a_schedule(env):
    started(env)
    assert env.backups.tick(at("2024-03-01 04:00")) is None
    assert not (env.paths.backups / bk.SCHEDULE_STATE).exists()
    assert env.backups.schedule_status() == {"schedule": "", "selector": "all", "next_run": 0, "last_run": 0,
                                             "skipped": 0, "error": ""}


def test_tick_runs_the_schedule_when_due_and_applies_retention(env):
    two_stopped(env)
    cfg = env.mgr.config.backup
    cfg.schedule, cfg.schedule_selector, cfg.keep = "0 4 * * *", "1", 2
    assert env.backups.tick(at("2024-03-01 03:59")) is None                   # first sight: only notes when it is due
    assert env.backups.schedule_status()["next_run"] == at("2024-03-01 04:00")
    assert env.backups.tick(at("2024-03-01 03:59") + 30) is None              # not yet
    days = ["2024-03-01 04:00", "2024-03-02 04:00", "2024-03-03 04:01"]
    for day in days:
        task = env.backups.tick(at(day))
        assert task is not None and task.kind == "backup.scheduled" and task.targets == [1]
        env.tasks.wait(task, timeout=30)
        assert task.results[1]["ok"]
        assert env.backups.tick(at(day) + 30) is None                         # once per occurrence
    rows = env.backups.list()
    assert len(rows) == 2 and {r["reason"] for r in rows} == {"scheduled"} and {r["id"] for r in rows} == {1}
    assert len(task.output["pruned"]) == 1                                    # retention ran at the end of the third run
    status = env.backups.schedule_status()
    assert status["last_run"] == at(days[-1]) and status["next_run"] == at("2024-03-04 04:00") and status["skipped"] == 0


def test_tick_survives_a_daemon_restart_and_makes_up_one_missed_run(env):
    started(env)
    env.mgr.stop(1)
    env.mgr.config.backup.schedule = "0 4 * * *"
    assert env.backups.tick(at("2024-03-01 03:00")) is None
    later = Backups(env.mgr)                                  # a new daemon, three days on
    task = later.tick(at("2024-03-04 09:30"))
    assert task is not None
    env.tasks.wait(task, timeout=30)
    assert later.tick(at("2024-03-04 09:31")) is None         # one run for three missed nights, not three
    assert later.schedule_status()["next_run"] == at("2024-03-05 04:00") and len(env.backups.list()) == 1


def test_tick_skips_an_occurrence_while_the_previous_run_is_still_going(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    env.mgr.config.backup.schedule = "*/5 * * * *"
    monkeypatch.setenv("FAKE_SLEEP", "2")
    env.backups.tick(at("2024-03-01 04:01"))
    first = env.backups.tick(at("2024-03-01 04:05"))
    assert first is not None and first.active
    assert env.backups.tick(at("2024-03-01 04:10")) is None
    assert env.backups.schedule_status()["skipped"] == 1
    env.tasks.wait(first, timeout=30)
    assert env.backups.tick(at("2024-03-01 04:15")) is not None


def test_a_changed_schedule_starts_counting_afresh_and_a_bad_one_never_runs(env):
    started(env)
    env.mgr.stop(1)
    cfg = env.mgr.config.backup
    cfg.schedule = "0 4 * * *"
    env.backups.tick(at("2024-03-01 03:00"))
    cfg.schedule = "30 5 * * *"
    assert env.backups.tick(at("2024-03-01 04:30")) is None   # the old 04:00 slot is not owed
    assert env.backups.schedule_status()["next_run"] == at("2024-03-01 05:30")
    with pytest.raises(ValueError, match="five fields"):
        cfg.schedule = "every night"                          # the config refuses it ...
    cfg.__dict__["schedule"] = "every night"                  # ... and if one got in anyway, nothing runs
    assert env.backups.tick(at("2024-03-02 05:30")) is None
    assert "five fields" in env.backups.schedule_status()["error"]
    assert env.backups.list() == []

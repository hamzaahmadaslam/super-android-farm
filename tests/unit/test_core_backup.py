"""Backups: archive + manifest, verification, restore rules, pruning.

The helper container is replaced by a small script that plays `docker run ... tar`, so the
real streaming code (a child process writing into a file this process opened) is exercised.
"""
import hashlib
import json
import os
import stat
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core_fakes import PREFIX, Farm, options  # noqa: E402

from superfarm.core import backup as bk  # noqa: E402
from superfarm.core.backup import BackupError, Backups  # noqa: E402
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
    farm = Farm(tmp_path)
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
    farm.backups = Backups(farm.mgr)
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

def test_create_writes_an_archive_and_a_manifest(env):
    spec = started(env)
    env.mgr.stop(1)
    task = Task(id="t", kind="backup", title="t")
    archive = env.backups.create(1, task=task)

    assert archive.parent == env.paths.backups and archive.name.startswith("android-1-") and archive.name.endswith(".tar.zst")
    assert archive.read_bytes() == PAYLOAD
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert not list(env.paths.backups.glob("*.partial"))
    manifest = json.loads(env.backups.manifest_path(archive).read_text())
    assert stat.S_IMODE(env.backups.manifest_path(archive).stat().st_mode) == 0o600
    assert manifest["kind"] == "superfarm-android-backup" and manifest["id"] == 1
    assert manifest["sha256"] == hashlib.sha256(PAYLOAD).hexdigest() and manifest["size"] == len(PAYLOAD)
    assert manifest["compression"] == "zstd" and manifest["consistent"] is True
    assert manifest["android_version"] == "13" and manifest["data_volume"] == VOLUME
    assert manifest["spec"]["device"]["android_id"] == spec.device.android_id
    line = helper_lines(env)[-1]
    assert "--network none" in line and f"-v {VOLUME}:/data:ro" in line          # read-only, offline
    assert "--xattrs --xattrs-include=* --acls --numeric-owner --sparse" in line
    assert "backup android-1-" in task.message


def test_running_android_is_stopped_for_the_copy_and_started_again(env):
    started(env)
    env.docker.calls.clear()
    archive = env.backups.create(1)
    kinds = [(c[0], c[1]) for c in env.docker.calls if c[0] in ("stop", "start")]
    assert kinds[:2] == [("stop", ANDROID), ("stop", NET)]
    assert kinds[-2:] == [("start", NET), ("start", ANDROID)]
    assert env.docker.running(ANDROID)
    assert env.backups.manifest(archive)["consistent"] is True


def test_live_backup_is_marked_inconsistent_and_tolerates_changing_files(env, monkeypatch):
    started(env)
    env.mgr.config.backup.stop_for_backup = False
    monkeypatch.setenv("FAKE_EXIT", "1")               # tar: "file changed as we read it"
    env.docker.calls.clear()
    archive = env.backups.create(1)
    assert not [c for c in env.docker.calls if c[0] == "stop"]
    assert env.backups.manifest(archive)["consistent"] is False


def test_tar_warning_exit_is_an_error_for_a_stopped_android(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    monkeypatch.setenv("FAKE_EXIT", "1")
    with pytest.raises(BackupError, match="file changed"):
        env.backups.create(1)
    assert list(env.paths.backups.iterdir()) == []


def test_failed_tar_leaves_no_files_and_restarts_the_android(env, monkeypatch):
    started(env)
    monkeypatch.setenv("FAKE_EXIT", "2")
    with pytest.raises(BackupError, match="exit 2"):
        env.backups.create(1)
    assert list(env.paths.backups.iterdir()) == []
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
    archive = env.backups.create(1, dest=tmp_path / "usb" / "farm")
    assert archive.parent == tmp_path / "usb" / "farm" and archive.name.endswith(".tar.gz")
    assert "-I pigz -p 4 -3" in helper_lines(env)[-1]
    env.mgr.config.backup.compression = "none"
    assert env.backups.create(1).name.endswith(".tar")


def test_cancelling_removes_the_helper_and_the_partial_file(env, monkeypatch):
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
    assert list(env.paths.backups.iterdir()) == []


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


# ------------------------------------------------------------------------- verify

def test_verify_detects_damage(env):
    started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    assert env.backups.verify(archive) and env.backups.verify(archive.name)         # by path or bare name
    assert env.backups.verify(archive, deep=True)
    assert "-tf -" in helper_lines(env)[-1] and "--zstd" in helper_lines(env)[-1]

    data = bytearray(archive.read_bytes())
    data[100] ^= 0xFF
    archive.write_bytes(bytes(data))
    assert not env.backups.verify(archive)                    # same size, one flipped bit
    archive.write_bytes(PAYLOAD[:-1])
    assert not env.backups.verify(archive)                    # truncated
    archive.write_bytes(PAYLOAD)
    assert env.backups.verify(archive)
    env.backups.manifest_path(archive).unlink()
    assert not env.backups.verify(archive)                    # nothing to check against
    assert not env.backups.verify("android-9-20000101-000000.tar.zst")


def test_deep_verify_fails_when_tar_cannot_read_the_archive(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    monkeypatch.setenv("FAKE_LIST_EXIT", "2")
    assert env.backups.verify(archive) and not env.backups.verify(archive, deep=True)


# ------------------------------------------------------------------------ restore

def test_restore_is_refused_over_a_running_android(env):
    started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    env.mgr.start(1)
    before = len(helper_lines(env))
    with pytest.raises(BackupError, match="never restored over a running Android"):
        env.backups.restore(archive, 1)
    assert len(helper_lines(env)) == before and not env.sink.exists()


def test_restore_refuses_a_damaged_or_orphaned_archive_before_touching_data(env):
    started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    before = len(helper_lines(env))
    archive.write_bytes(PAYLOAD[:-5] + b"XXXXX")
    with pytest.raises(BackupError, match="checksum"):
        env.backups.restore(archive, 1)
    env.backups.manifest_path(archive).unlink()
    with pytest.raises(BackupError, match="no manifest"):
        env.backups.restore(archive, 1)
    with pytest.raises(BackupError, match="does not exist"):
        env.backups.restore("android-1-19990101-000000.tar.zst", 1)
    assert len(helper_lines(env)) == before and env.store.ids() == [1]


def test_restore_into_the_same_android_replaces_its_data(env):
    spec = started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    env.store.state_set(1, "trim", {"hash": "x"})
    before = len(helper_lines(env))
    assert env.backups.restore(archive, 1) == 1
    wipe, extract = helper_lines(env)[before:]
    assert "find /data -mindepth 1 -delete" in wipe and f"-v {VOLUME}:/data" in wipe
    assert "--zstd -xpf -" in extract and "--xattrs --xattrs-include=* --acls --numeric-owner --sparse" in extract
    assert " -i " in extract and "--network none" in extract
    assert env.sink.read_bytes() == PAYLOAD
    assert env.store.get(1).device == spec.device
    # what the farm believed about the old data is forgotten; the markers in /data decide
    assert env.store.state_get(1, "trim") is None and env.store.state_get(1, "renderer") is None


def test_restore_into_another_android_moves_the_identity_with_the_data(env):
    spec = started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    env.mgr.create()
    assert env.store.get(2).device.android_id != spec.device.android_id
    assert env.backups.restore(archive, 2) == 2
    assert env.store.get(2).device == spec.device
    assert f"{PREFIX}-data-2" in env.docker.volume_labels


def test_restore_as_a_new_android_next_to_the_original_comes_up_offline(env):
    spec = started(env, name="Shop", tags=["eu"], fps=30)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    new_id = env.backups.restore(archive)
    new = env.store.get(new_id)
    assert new_id == 2 and new.name == "Shop" and new.tags == ["eu"] and new.fps == 30
    assert new.device == spec.device                          # exactly the Android that was saved
    assert new.network.mode == "none"                         # ...so it must not go online beside its twin
    assert "Android 1 has the same identity" in new.notes
    assert env.sink.read_bytes() == PAYLOAD
    assert env.docker.volume_labels[f"{PREFIX}-data-2"]["io.superfarm.instance"] == "2"


def test_restore_as_a_new_android_after_the_original_is_gone_keeps_its_network(env):
    started(env, network={"mode": "proxy", "proxy": "10.0.0.5:3128"})
    env.mgr.stop(1)
    archive = env.backups.create(1)
    env.mgr.delete(1)
    new = env.store.get(env.backups.restore(archive))
    assert new.network.mode == "proxy" and new.network.proxy == "10.0.0.5:3128"
    assert "same identity" not in new.notes and "Restored from" in new.notes


def test_restore_falls_back_to_farm_defaults_for_settings_this_farm_lacks(env):
    started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    meta = env.backups.manifest_path(archive)
    data = json.loads(meta.read_text())
    data["spec"]["android_version"] = "8"                     # an image this farm does not have
    data["spec"]["device"]["profile"] = "a-preset-from-the-future"
    meta.write_text(json.dumps(data))
    env.mgr.delete(1)
    new = env.store.get(env.backups.restore(archive))
    assert new.android_version is None and new.device.profile == "none"
    assert new.device.android_id == data["spec"]["device"]["android_id"]
    assert new.device.props == data["spec"]["device"]["props"]


def test_a_manifest_cannot_make_the_farm_fetch_an_image(env):
    started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    meta = env.backups.manifest_path(archive)
    data = json.loads(meta.read_text())
    data["spec"]["image"] = "registry.example/unknown:latest"
    meta.write_text(json.dumps(data))
    new = env.store.get(env.backups.restore(archive))
    assert new.image is None                                 # not on this machine: not taken over
    data["spec"]["image"] = "local/android:13"
    meta.write_text(json.dumps(data))
    assert env.store.get(env.backups.restore(archive)).image == "local/android:13"
    assert env.docker.names_called("pull") == []


def test_a_hand_edited_volume_path_is_never_mounted(env):
    started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    spec = env.store.get(1)
    spec.data_volume = "/etc"
    env.store.save(spec)
    before = len(helper_lines(env))
    with pytest.raises(Exception, match="not a Docker volume name"):
        env.backups.create(1)
    with pytest.raises(Exception, match="not a Docker volume name"):
        env.backups.restore(archive, 1)
    assert len(helper_lines(env)) == before


def test_failed_restore_into_a_new_android_leaves_nothing_behind(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    monkeypatch.setenv("FAKE_EXTRACT_EXIT", "2")
    with pytest.raises(BackupError, match="incomplete"):
        env.backups.restore(archive)
    assert env.store.ids() == [1] and f"{PREFIX}-data-2" not in env.docker.volume_labels


def test_restore_into_an_adopted_volume_never_creates_it(env):
    started(env)
    env.mgr.stop(1)
    archive = env.backups.create(1)
    env.docker.volume_labels["old_data"] = {}
    env.mgr.create(template={"data_volume": "old_data"})
    del env.docker.volume_labels["old_data"]
    with pytest.raises(BackupError, match="adopted volume"):
        env.backups.restore(archive, 2)


# ------------------------------------------------------------------- list / prune

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


def test_list_is_newest_first_and_ignores_strangers(env, monkeypatch):
    env.mgr.create(count=2)
    for i in (1, 2):
        env.mgr.start(i)
        env.mgr.stop(i)
    made = make_backups(env, monkeypatch, 2, ids=(1, 2))
    (env.paths.backups / "android-notes.json").write_text('{"kind": "something else"}')
    (env.paths.backups / "android-broken.json").write_text("{not json")
    rows = env.backups.list()
    assert [r["name"] for r in rows] == [p.name for p in reversed(made)]
    assert [r["id"] for r in rows] == [2, 2, 1, 1]
    assert all(r["present"] and r["size"] == len(PAYLOAD) and r["consistent"] for r in rows)
    made[0].unlink()
    assert [r["present"] for r in env.backups.list()] == [True, True, True, False]
    assert env.backups.list(env.paths.home / "nowhere") == []


def test_prune_keeps_the_newest_per_android(env, monkeypatch):
    env.mgr.create(count=2)
    for i in (1, 2):
        env.mgr.start(i)
        env.mgr.stop(i)
    made = make_backups(env, monkeypatch, 3, ids=(1, 2))
    env.mgr.config.backup.keep = 2
    removed = env.backups.prune()
    assert sorted(p.name for p in removed) == sorted([made[0].name, made[3].name])       # the oldest of each
    assert len(env.backups.list()) == 4
    assert not made[0].exists() and not env.backups.manifest_path(made[0]).exists()
    assert env.backups.prune() == []
    env.mgr.config.backup.keep = 0
    assert env.backups.prune() == [] and len(env.backups.list()) == 4


def test_two_backups_in_one_second_get_different_names(env, monkeypatch):
    started(env)
    env.mgr.stop(1)
    monkeypatch.setattr(bk.time, "gmtime", lambda *a: time.struct_time(time.localtime(1_700_000_000)))
    a, b = env.backups.create(1), env.backups.create(1)
    assert a != b and a.exists() and b.exists() and len(env.backups.list()) == 2


def test_helper_mounts_only_what_it_is_given(env):
    started(env)
    env.mgr.stop(1)
    env.backups.create(1)
    line = helper_lines(env)[-1].split()
    assert options(line, "-v") == [f"{VOLUME}:/data:ro"]
    assert "--privileged" not in line and "--rm" in line

"""Farm backups: the settings of the whole farm, without Android data and without secrets."""
import io
import json
import os
import stat
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core_fakes import Farm  # noqa: E402

from superfarm.core import backup as bk  # noqa: E402
from superfarm.core.backup import BackupError  # noqa: E402
from superfarm.util.files import write_yaml  # noqa: E402


def populate(farm: Farm) -> None:
    """A farm with two Androids and a bit of everything a farm backup covers or must skip."""
    paths = farm.paths
    write_yaml(paths.config, {"farm_name": "Night shift"})
    farm.mgr.create(count=2, name="Shop")
    farm.store.state_set(1, "vpn_profile", "eu.ovpn")
    auto = paths.automation
    (auto / "datasets" / "login").mkdir(parents=True)
    (auto / "datasets" / "login" / "dataset.json").write_text('{"name": "login"}')
    (auto / "programs").mkdir()
    (auto / "programs" / "login.json").write_text('{"steps": []}')
    (auto / "flows").mkdir()
    (auto / "flows" / "open.yaml").write_text("steps: []\n")
    (auto / "profiles").mkdir()
    (auto / "profiles" / "com.example.app.json").write_text("{}")
    (auto / "schedules.json").write_text("[]")
    (auto / "runs" / "r1").mkdir(parents=True)                # run logs: not settings
    (auto / "runs" / "r1" / "log.txt").write_text("noise")
    values = auto / "values"
    (values / "instances").mkdir(parents=True)
    (values / "pools").mkdir()
    (values / "shared.json").write_text(json.dumps({"greeting": "hello", "account_password": "hunter2"}))
    (values / "shared.json.lock").write_text("")
    (values / "instances" / "1.json").write_text(json.dumps({"city": "Lisbon", "otp": "123456"}))
    (values / "pools" / "invites.txt").write_text("code-1\ncode-2\n")
    (values / "pools" / "pin_codes.txt").write_text("0000\n")
    (auto / "flows" / "link.yaml").symlink_to(paths.secrets / "api-token")      # must never be followed
    (paths.vpn / "eu.ovpn").write_text("remote vpn.example 1194\n<key>PRIVATE</key>\n")
    (paths.vpn / "us.conf").write_text("[Interface]\nPrivateKey = PRIVATE\n")
    (paths.secrets / "api-token").write_text("TOKEN-VALUE")
    (paths.secrets / "vpn-default.auth").write_text("user\npass\n")


@pytest.fixture
def farm(tmp_path):
    f = Farm(tmp_path)
    populate(f)
    yield f
    f.close()


def members(folder) -> dict[str, bytes]:
    with tarfile.open(folder / "farm.tar.gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


# ------------------------------------------------------------------------- create

def test_farm_backup_holds_settings_and_no_secrets(farm):
    folder = farm.mgr.backups.create_farm()
    assert folder.parent == farm.paths.backups / "farm" and bk._HANDLE.match(f"farm/{folder.name}")
    assert sorted(p.name for p in folder.iterdir()) == ["farm.tar.gz", "manifest.json"]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in folder.iterdir())
    files = members(folder)
    assert set(files) == {
        "config.yaml", "instances/1/instance.yaml", "instances/1/state.json", "instances/2/instance.yaml",
        "automation/datasets/login/dataset.json", "automation/programs/login.json", "automation/flows/open.yaml",
        "automation/profiles/com.example.app.json", "automation/schedules.json", "automation/values/shared.json",
        "automation/values/instances/1.json", "automation/values/pools/invites.txt", "vpn-profiles.json"}
    assert json.loads(files["vpn-profiles.json"]) == ["eu.ovpn", "us.conf"]              # names only
    assert json.loads(files["automation/values/shared.json"]) == {"greeting": "hello"}    # secret-named value dropped
    assert json.loads(files["automation/values/instances/1.json"]) == {"city": "Lisbon"}
    blob = b"".join(files.values())
    for secret in (b"hunter2", b"123456", b"PRIVATE", b"TOKEN-VALUE", b"0000"):
        assert secret not in blob

    manifest = farm.mgr.backups.manifest(folder)
    assert manifest["kind"] == "superfarm-farm-backup" and manifest["includes_secrets"] is False
    assert manifest["instances"] == [1, 2] and manifest["entries"] == len(files)
    assert manifest["superfarm_version"] and set(manifest["files"]) == {"farm.tar.gz"}
    assert farm.mgr.backups.verify(folder) and farm.mgr.backups.verify(f"farm/{folder.name}")
    (folder / "farm.tar.gz").write_bytes((folder / "farm.tar.gz").read_bytes()[:-1])
    assert not farm.mgr.backups.verify(folder)


def test_farm_backup_with_secrets_says_so_and_stays_owner_only(farm):
    folder = farm.mgr.backups.create_farm(include_secrets=True)
    files = members(folder)
    assert {"vpn/eu.ovpn", "vpn/us.conf", "secrets/api-token", "secrets/vpn-default.auth"} <= set(files)
    assert files["secrets/api-token"] == b"TOKEN-VALUE"
    assert b"hunter2" not in b"".join(files.values())        # a secret-named *value* is never stored anywhere
    assert farm.mgr.backups.manifest(folder)["includes_secrets"] is True
    assert stat.S_IMODE((folder / "farm.tar.gz").stat().st_mode) == 0o600
    farm.mgr.config.backup.include_secrets = True             # the config default is followed when not said
    assert farm.mgr.backups.manifest(farm.mgr.backups.create_farm())["includes_secrets"] is True


def test_farm_backups_are_listed_apart_from_android_backups_and_pruned(farm, tmp_path):
    backups = farm.mgr.backups
    made = [backups.create_farm() for _ in range(3)]
    assert backups.list() == []
    rows = backups.list(kind="farm")
    assert [r["dir"] for r in rows] == [str(p) for p in reversed(made)]
    assert all(r["kind"] == "farm" and r["id"] is None and r["file"].endswith("farm.tar.gz") and r["present"] for r in rows)
    assert len(backups.list(kind="all")) == 3
    farm.mgr.config.backup.keep = 1
    assert len(backups.prune()) == 2 and len(backups.list(kind="farm")) == 1
    elsewhere = backups.create_farm(dest=tmp_path / "usb")
    assert elsewhere.parent == tmp_path / "usb" / "farm" and backups.verify(elsewhere)


def test_a_farm_backup_is_not_restorable_as_an_android_and_the_other_way_round(farm):
    folder = farm.mgr.backups.create_farm()
    with pytest.raises(BackupError, match="not a backup of an Android"):
        farm.mgr.backups.restore(folder)
    android = farm.paths.backups / "android-1" / "20240101-000000"
    android.mkdir(parents=True)
    (android / "manifest.json").write_text(json.dumps({"kind": "superfarm-android-backup", "files": {}}))
    with pytest.raises(BackupError, match="is not a farm backup"):
        farm.mgr.backups.restore_farm(android)


# ------------------------------------------------------------------------ restore

def other_farm(tmp_path) -> Farm:
    return Farm(tmp_path / "other")


def test_restore_into_an_empty_farm_brings_everything_back(farm, tmp_path):
    folder = farm.mgr.backups.create_farm()
    new = other_farm(tmp_path)
    try:
        result = new.mgr.backups.restore_farm(folder)
        assert result["instances"] == [1, 2] and result["skipped"] == [] and result["includes_secrets"] is False
        assert "config.yaml" in result["restored"] and "automation/flows/open.yaml" in result["restored"]
        assert result["missing_vpn_profiles"] == ["eu.ovpn", "us.conf"]       # named, not carried: import them again
        assert new.store.ids() == [1, 2] and new.store.get(1) == farm.store.get(1)
        assert new.store.state_get(1, "vpn_profile") == "eu.ovpn"
        assert (new.paths.config).read_text() == farm.paths.config.read_text()
        assert json.loads((new.paths.automation / "values" / "shared.json").read_text()) == {"greeting": "hello"}
        assert stat.S_IMODE((new.paths.automation / "values" / "pools" / "invites.txt").stat().st_mode) == 0o600
        assert not (new.paths.secrets / "api-token").exists() and not (new.paths.vpn / "eu.ovpn").exists()
        assert new.docker.containers == {} and new.docker.volume_labels == {}     # settings only; nothing started
    finally:
        new.close()


def test_restore_merges_and_never_overwrites_unless_forced(farm, tmp_path):
    folder = farm.mgr.backups.create_farm()
    new = other_farm(tmp_path)
    try:
        mine = new.mgr.create(name="Mine")[0]                 # takes id 1 in the target farm
        write_yaml(new.paths.config, {"farm_name": "Day shift"})
        (new.paths.automation / "flows").mkdir(parents=True)
        (new.paths.automation / "flows" / "open.yaml").write_text("steps: [mine]\n")
        result = new.mgr.backups.restore_farm(folder)
        assert result["instances"] == [2]
        assert {"Android 1 (exists)", "config.yaml (exists)", "automation/flows/open.yaml (exists)"} <= set(result["skipped"])
        assert new.store.get(1) == mine and new.store.state(1) == {}          # neither its settings nor its state
        assert new.store.get(2).name == "Shop 2"
        assert "Day shift" in new.paths.config.read_text()
        assert (new.paths.automation / "flows" / "open.yaml").read_text() == "steps: [mine]\n"
        assert (new.paths.automation / "programs" / "login.json").is_file()   # what was missing came in

        forced = new.mgr.backups.restore_farm(folder, force=True)
        assert forced["instances"] == [1, 2] and forced["skipped"] == []
        assert new.store.get(1).name == "Shop 1" and "Night shift" in new.paths.config.read_text()
        assert (new.paths.automation / "flows" / "open.yaml").read_text() == "steps: []\n"
    finally:
        new.close()


def test_restore_with_secrets_writes_them_owner_only(farm, tmp_path):
    folder = farm.mgr.backups.create_farm(include_secrets=True)
    new = other_farm(tmp_path)
    try:
        result = new.mgr.backups.restore_farm(folder)
        assert result["includes_secrets"] and result["missing_vpn_profiles"] == []
        for path in (new.paths.secrets / "api-token", new.paths.secrets / "vpn-default.auth", new.paths.vpn / "eu.ovpn"):
            assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600
        assert (new.paths.secrets / "api-token").read_text() == "TOKEN-VALUE"
    finally:
        new.close()


def test_restore_refuses_a_damaged_farm_backup(farm, tmp_path):
    folder = farm.mgr.backups.create_farm()
    data = bytearray((folder / "farm.tar.gz").read_bytes())
    data[len(data) // 2] ^= 0xFF
    (folder / "farm.tar.gz").write_bytes(bytes(data))
    new = other_farm(tmp_path)
    try:
        with pytest.raises(BackupError, match="does not match its recorded checksums"):
            new.mgr.backups.restore_farm(folder)
        assert new.store.ids() == []
    finally:
        new.close()


def craft(folder, entries: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    """Replace a farm backup's archive with a hand-made one and fix its manifest, as someone
    with write access to the backup folder could."""
    with tarfile.open(folder / "farm.tar.gz", "w:gz") as tar:
        for info, content in entries:
            info.size = len(content) if info.isfile() else 0
            tar.addfile(info, io.BytesIO(content) if info.isfile() else None)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"]["farm.tar.gz"] = bk._file_entry(folder / "farm.tar.gz")
    (folder / "manifest.json").write_text(json.dumps(manifest))


def test_a_crafted_archive_cannot_write_outside_the_farm_or_plant_links(farm, tmp_path):
    folder = farm.mgr.backups.create_farm()
    link = tarfile.TarInfo("automation/flows/evil.yaml")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    device = tarfile.TarInfo("automation/dev")
    device.type = tarfile.CHRTYPE
    craft(folder, [
        (tarfile.TarInfo("../escape.txt"), b"x"),
        (tarfile.TarInfo("/abs/escape.txt"), b"x"),
        (tarfile.TarInfo("automation/../../escape.txt"), b"x"),
        (tarfile.TarInfo("logs/daemon.log"), b"x"),                     # not something a farm backup holds
        (tarfile.TarInfo("instances/1/../../../escape.txt"), b"x"),
        (tarfile.TarInfo("instances/7/authorized_keys"), b"x"),
        (tarfile.TarInfo("instances/abc/instance.yaml"), b"x"),
        (tarfile.TarInfo("instances/8/instance.yaml"), b"no_such_field: 1\n"),
        (tarfile.TarInfo("instances/8/state.json"), b"{}"),
        (link, b""), (device, b""),
        (tarfile.TarInfo("automation/flows/fine.yaml"), b"steps: []\n"),
    ])
    new = other_farm(tmp_path)
    try:
        result = new.mgr.backups.restore_farm(folder, force=True)
        assert result["restored"] == ["automation/flows/fine.yaml"] and result["instances"] == []
        assert len(result["skipped"]) == 10 and any("settings not valid here" in s for s in result["skipped"])
        assert not list(tmp_path.rglob("escape.txt")) and new.store.ids() == []
        assert not (new.paths.instances / "8").exists() and not (new.paths.automation / "flows" / "evil.yaml").exists()
    finally:
        new.close()

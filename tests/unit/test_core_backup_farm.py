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
    # Disaster recovery: a new home has a new key, so the operator vouches for the archive.
    folder = farm.mgr.backups.create_farm()
    new = other_farm(tmp_path)
    try:
        result = new.mgr.backups.restore_farm(folder, trust=True, restore_config=True)
        assert result["instances"] == [1, 2] and result["skipped"] == [] and result["includes_secrets"] is False
        assert result["own"] is False and result["adjusted"] == []
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
        result = new.mgr.backups.restore_farm(folder, trust=True, restore_config=True)
        assert result["instances"] == [2]
        assert {"Android 1 (exists)", "config.yaml (exists)", "automation/flows/open.yaml (exists)"} <= set(result["skipped"])
        assert new.store.get(1) == mine and new.store.state(1) == {}          # neither its settings nor its state
        assert new.store.get(2).name == "Shop 2"
        assert "Day shift" in new.paths.config.read_text()
        assert (new.paths.automation / "flows" / "open.yaml").read_text() == "steps: [mine]\n"
        assert (new.paths.automation / "programs" / "login.json").is_file()   # what was missing came in

        forced = new.mgr.backups.restore_farm(folder, force=True, trust=True, restore_config=True)
        assert forced["instances"] == [1, 2] and forced["skipped"] == []
        assert new.store.get(1).name == "Shop 1" and "Night shift" in new.paths.config.read_text()
        assert (new.paths.automation / "flows" / "open.yaml").read_text() == "steps: []\n"
    finally:
        new.close()


def test_restore_with_secrets_writes_them_owner_only(farm, tmp_path):
    folder = farm.mgr.backups.create_farm(include_secrets=True)
    new = other_farm(tmp_path)
    try:
        result = new.mgr.backups.restore_farm(folder, restore_secrets=True)
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


# ------------------------------------------------ whose backup is it: own, or someone else's

HOSTILE_SPEC = (b"name: Gift\nimage: registry.invalid/not-ours/android:latest\nautostart: true\n"
                b"data_volume: some-other-volume\nextra_boot_args: [androidboot.selinux=permissive]\n"
                b"extra_props: {ro.adb.secure: '0'}\nnetwork: {mode: none}\n")


def hostile(farm: Farm) -> str:
    """A farm backup as somebody else could hand it over: hand-made archive, matching checksum."""
    folder = farm.paths.backups / "farm" / "20250101-000000"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"kind": bk.FARM_KIND, "data": "farm.tar.gz", "files": {},
                                                      "includes_secrets": True}))
    craft(folder, [
        (tarfile.TarInfo("instances/7/instance.yaml"), HOSTILE_SPEC),
        (tarfile.TarInfo("instances/7/state.json"), b'{"data_android_version": "9"}'),
        (tarfile.TarInfo("instances/8/instance.yaml"), b"image: local/android:13\n"),
        (tarfile.TarInfo("config.yaml"), b"server: {host: 0.0.0.0}\ndocker: {binary: /tmp/not-docker}\n"),
        (tarfile.TarInfo("secrets/api-token"), b"THEIR-TOKEN"),
        (tarfile.TarInfo("secrets/farm-key"), b"k" * 64),
        (tarfile.TarInfo("vpn/theirs.ovpn"), b"remote vpn.invalid 1194\n"),
        (tarfile.TarInfo("automation/schedules.json"), b'[{"id": "abcdef", "enabled": true}]'),
        (tarfile.TarInfo("automation/flows/fine.yaml"), b"steps: [{home: }]\n"),
    ])
    return "farm/20250101-000000"


def test_a_farm_backup_made_elsewhere_cannot_decide_what_runs_here(tmp_path):
    new = Farm(tmp_path)                      # a farm that was never configured: no config.yaml to protect it
    try:
        result = new.mgr.backups.restore_farm(hostile(new))
        assert result["own"] is False
        assert result["restored"] == ["instances/7/instance.yaml", "instances/8/instance.yaml", "automation/flows/fine.yaml"]
        spec = new.store.get(7)
        assert spec.name == "Gift" and spec.network.mode == "none"        # harmless settings are kept
        assert (spec.image, spec.data_volume, spec.autostart, spec.extra_boot_args, spec.extra_props) == \
            (None, None, False, [], {})
        assert new.store.get(8).image == "local/android:13"               # an image this machine has is fine
        assert new.store.state(7) == {}
        # Everything that was not taken as it was is said, with the way to take it.
        [adjusted] = result["adjusted"]
        for word in ("Android 7", "registry.invalid/not-ours/android:latest", "some-other-volume",
                     "androidboot.selinux=permissive", "ro.adb.secure", "autostart switched off"):
            assert word in adjusted
        skipped = "\n".join(result["skipped"])
        for line in ("config.yaml (made by another farm: restore with `restore_config`",
                     "secrets/api-token (made by another farm: restore with `restore_secrets`",
                     "vpn/theirs.ovpn (made by another farm: restore with `restore_secrets`",
                     "automation/schedules.json (made by another farm: restore with `trust`",
                     "instances/7/state.json (made by another farm: restore with `trust`",
                     "secrets/farm-key (not a farm file)"):
            assert line in skipped
        assert not new.paths.config.exists() and not (new.paths.automation / "schedules.json").exists()
        assert not list(new.paths.secrets.iterdir()) and not list(new.paths.vpn.iterdir())
        assert not any(c[0] == "pull" for c in new.docker.calls)
        assert new.mgr.autostart() is None                                # and nothing starts by itself
    finally:
        new.close()


def test_another_spelling_of_a_name_is_still_that_file(tmp_path):
    new = Farm(tmp_path)
    try:
        folder = new.mgr.backups.create_farm()
        (folder / "manifest.json").write_text(json.dumps({**json.loads((folder / "manifest.json").read_text()), "seal": ""}))
        craft(folder, [(tarfile.TarInfo("./config.yaml"), b"farm_name: Theirs\n"),
                       (tarfile.TarInfo("automation/./schedules.json"), b"[]"),
                       (tarfile.TarInfo("automation//schedules.json"), b"[]"),
                       (tarfile.TarInfo("secrets//farm-key"), b"k" * 64),
                       (tarfile.TarInfo("secrets/./api-token"), b"THEIRS"),
                       (tarfile.TarInfo("instances/7/./state.json"), b"{}"),
                       (tarfile.TarInfo("./vpn-profiles.json"), b'["eu.ovpn"]')])
        key = (new.paths.secrets / "farm-key").read_text()
        result = new.mgr.backups.restore_farm(folder, force=True)
        assert result["restored"] == [] and len(result["skipped"]) == 6 and result["missing_vpn_profiles"] == ["eu.ovpn"]
        assert not new.paths.config.exists() and not (new.paths.automation / "schedules.json").exists()
        result = new.mgr.backups.restore_farm(folder, force=True, trust=True, restore_config=True, restore_secrets=True)
        assert (new.paths.secrets / "farm-key").read_text() == key        # not even when everything is asked for
        assert "secrets//farm-key (not a farm file)" in result["skipped"]
        assert sorted(result["restored"]) == ["automation/schedules.json", "automation/schedules.json", "config.yaml",
                                              "instances/7/state.json", "secrets/api-token"]
    finally:
        new.close()


def test_force_does_not_stand_in_for_the_explicit_choices(tmp_path):
    new = Farm(tmp_path)
    try:
        result = new.mgr.backups.restore_farm(hostile(new), force=True)
        assert not new.paths.config.exists() and not (new.paths.secrets / "api-token").exists()
        assert new.store.get(7).image is None and result["adjusted"]
    finally:
        new.close()


def test_each_explicit_choice_takes_exactly_its_part(tmp_path):
    new = Farm(tmp_path)
    try:
        name = hostile(new)
        result = new.mgr.backups.restore_farm(name, restore_config=True)
        assert "config.yaml" in result["restored"] and "0.0.0.0" in new.paths.config.read_text()
        assert new.store.get(7).image is None and not (new.paths.secrets / "api-token").exists()

        result = new.mgr.backups.restore_farm(name, restore_secrets=True)
        assert {"secrets/api-token", "vpn/theirs.ovpn"} <= set(result["restored"])
        assert not (new.paths.secrets / "farm-key").exists()               # the key of a farm is never taken over
        assert new.store.get(7).image is None

        result = new.mgr.backups.restore_farm(name, trust=True, force=True)
        spec = new.store.get(7)
        assert result["adjusted"] == [] and spec.image == "registry.invalid/not-ours/android:latest"
        assert spec.autostart and spec.extra_props == {"ro.adb.secure": "0"} and spec.data_volume == "some-other-volume"
        assert new.store.state(7) == {"data_android_version": "9"}
        assert "automation/schedules.json" in result["restored"]
    finally:
        new.close()


def test_a_config_the_daemon_could_not_load_is_never_restored(tmp_path):
    new = Farm(tmp_path)
    try:
        folder = new.mgr.backups.create_farm()
        for bad in (b"server: {port: not-a-number}\n", b"- a list\n", b"farm_name: [unclosed\n",
                    b"a: &a [x, x]\nfarm_name: *a\n"):
            craft(folder, [(tarfile.TarInfo("config.yaml"), bad),
                           (tarfile.TarInfo("instances/3/instance.yaml"), b"- a list\n"),
                           (tarfile.TarInfo("instances/4/instance.yaml"), b"a: &a [x]\ntags: *a\n")])
            result = new.mgr.backups.restore_farm(folder, restore_config=True, trust=True)
            assert result["restored"] == [] and not new.paths.config.exists() and new.store.ids() == []
            assert any(s.startswith("config.yaml (not a valid configuration") for s in result["skipped"])
            assert sum("settings not valid here" in s for s in result["skipped"]) == 2
    finally:
        new.close()


def test_the_farm_restores_its_own_backups_in_full(farm):
    farm.mgr.update(1, {"image": "registry.example/custom:1", "autostart": True, "extra_props": {"ro.x": "1"},
                        "extra_boot_args": ["androidboot.y=2"]})
    before = farm.store.get(1)
    folder = farm.mgr.backups.create_farm(include_secrets=True)
    manifest = farm.mgr.backups.manifest(folder)
    key = farm.paths.secrets / "farm-key"
    assert len(manifest["seal"]) == 64 and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert "secrets/farm-key" not in members(folder) and key.read_bytes() not in (folder / "farm.tar.gz").read_bytes()
    made_with = key.read_text()
    farm.mgr.backups.create_farm()
    assert key.read_text() == made_with                                      # one key, made once

    farm.mgr.delete(1, force=True)
    farm.paths.config.unlink()
    (farm.paths.secrets / "api-token").unlink()
    (farm.paths.automation / "schedules.json").unlink()
    result = farm.mgr.backups.restore_farm(folder)
    assert result["own"] is True and result["adjusted"] == [] and result["instances"] == [1]
    assert farm.store.get(1) == before and farm.store.state_get(1, "vpn_profile") == "eu.ovpn"
    assert {"config.yaml", "secrets/api-token", "automation/schedules.json"} <= set(result["restored"])
    assert all("(exists)" in s for s in result["skipped"])


def test_restoring_twice_and_backing_up_at_the_same_moment_do_no_harm(farm, tmp_path):
    # Pressed twice: the second restore finds everything in place and changes nothing.
    new = Farm(tmp_path / "second")
    try:
        name = hostile(new)
        first = new.mgr.backups.restore_farm(name)
        saved = (new.paths.instances / "7" / "instance.yaml").read_bytes()
        again = new.mgr.backups.restore_farm(name)
        assert again["restored"] == [] and again["instances"] == [] and again["adjusted"] == []
        assert {"Android 7 (exists)", "Android 8 (exists)", "automation/flows/fine.yaml (exists)"} <= set(again["skipped"])
        assert (new.paths.instances / "7" / "instance.yaml").read_bytes() == saved and first["instances"] == [7, 8]
    finally:
        new.close()
    # The first farm backups of a farm, started together: one key, and each is recognised as its own.
    key = farm.paths.secrets / "farm-key"
    assert not key.exists()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as pool:
        folders = list(pool.map(lambda _: farm.mgr.backups.create_farm(), range(4)))
    assert len({str(f) for f in folders}) == 4 and len(key.read_text()) == 64
    farm.mgr.delete(2, force=True)
    assert all(farm.mgr.backups.restore_farm(f)["own"] for f in folders)
    assert farm.store.ids() == [1, 2]


def test_the_seal_cannot_be_borrowed_for_another_archive(farm):
    backups = farm.mgr.backups
    genuine = backups.manifest(backups.create_farm())["seal"]
    folder = farm.paths.backups / "farm" / "20250101-000000"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"kind": bk.FARM_KIND, "data": "farm.tar.gz", "files": {},
                                                      "seal": genuine}))
    craft(folder, [(tarfile.TarInfo("instances/7/instance.yaml"), HOSTILE_SPEC),
                   (tarfile.TarInfo("config.yaml"), b"farm_name: Theirs\n")])
    farm.paths.config.unlink()
    result = backups.restore_farm(folder)
    assert result["own"] is False and not farm.paths.config.exists() and farm.store.get(7).image is None
    # No seal, an empty key file, a manifest that hides which file it describes: never "own".
    manifest = json.loads((folder / "manifest.json").read_text())
    (folder / "manifest.json").write_text(json.dumps({**manifest, "seal": ""}))
    (farm.paths.secrets / "farm-key").write_text("")
    assert backups.restore_farm(folder, force=True)["own"] is False
    (folder / "manifest.json").write_text(json.dumps({**manifest, "data": "farm.tar.gz", "files": {
        "other": manifest["files"]["farm.tar.gz"]}}))
    (folder / "other").write_bytes((folder / "farm.tar.gz").read_bytes())
    with pytest.raises(BackupError, match="does not say which file is its archive"):
        backups.restore_farm(folder)

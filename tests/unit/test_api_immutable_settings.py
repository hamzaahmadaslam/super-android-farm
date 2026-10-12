"""Settings that decide which program the daemon runs cannot be changed over HTTP."""
import pytest

from api_fakes import api


@pytest.mark.parametrize("patch", [
    {"docker": {"binary": "/tmp/other-program"}},
    {"gpu": {"nvidia": {"relay_dir": "/tmp/elsewhere"}}},
    {"gpu": {"nvidia": {"icd": "/tmp/other.json"}}},
])
def test_a_patch_cannot_change_which_program_the_daemon_runs(tmp_path, patch):
    with api(tmp_path) as a:
        before = a.farm.config.model_dump(mode="json")
        r = a.client.patch("/api/config", json={"patch": patch})
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "immutable_setting"
        assert "config.yaml" in r.json()["error"]["message"]
        assert a.farm.config.model_dump(mode="json") == before


def test_an_ordinary_setting_is_still_saved_and_the_current_value_may_be_repeated(tmp_path):
    with api(tmp_path) as a:
        same = a.farm.config.docker.binary
        r = a.client.patch("/api/config", json={"patch": {"docker": {"binary": same}, "android": {"fps": 25}}})
        assert r.status_code == 200, r.text
        assert a.farm.config.android.fps == 25


def test_restore_accepts_the_three_explicit_choices_for_a_foreign_farm_backup(tmp_path):
    with api(tmp_path) as a:
        # No such backup: the point is that the fields are accepted (404, not a 422 for unknown fields).
        r = a.client.post("/api/backups/restore", json={"archive": "farm/20200101-000000", "trust": True,
                                                        "restore_config": True, "restore_secrets": True})
        assert r.status_code == 404, r.text


def test_creating_the_same_dataset_twice_is_a_conflict_not_a_server_error(tmp_path):
    with api(tmp_path) as a:
        first = a.client.post("/api/automation/datasets", json={"name": "Open settings"})
        assert first.status_code in (200, 201), first.text
        second = a.client.post("/api/automation/datasets", json={"name": "Open settings"})
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "conflict"

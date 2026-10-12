"""A different Android version per instance: chosen at creation, guarded once data exists."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core_fakes import PREFIX, Farm  # noqa: E402

from superfarm.core.instances import NeedsForce, StartError, version_order  # noqa: E402

IMAGES = {"android": {"images": {
    "12": {"base": "example/android:12", "tag": "local/android:12"},
    "13": {"base": "example/android:13", "tag": "local/android:13"},
    "14": {"base": "example/android:14", "tag": "local/android:14"},
    "13-custom": {"base": "example/android:13c", "tag": "local/android:13c"},
}}}
ANDROID = f"{PREFIX}-android-1"


def make(tmp_path, auto_backup: bool) -> Farm:
    f = Farm(tmp_path, {**IMAGES, "backup": {"auto_before_risky": auto_backup}})
    for tag in ("local/android:12", "local/android:14", "local/android:13c"):
        f.docker.images[tag] = {"id": f"sha256:{tag}", "labels": {}}
    f.safety = []
    f.mgr.backups.safety = lambda spec, reason, *, force=False, task=None: (
        f.safety.append((spec.id, reason, force)) or (f"/backups/android-{spec.id}/x", ""))
    return f


@pytest.fixture
def farm(tmp_path):
    f = make(tmp_path, True)
    yield f
    f.close()


def image_of(farm) -> str:
    args = farm.docker.containers[ANDROID]["args"]
    return next(a for a in args if a.startswith("local/android:"))


def test_version_order():
    assert version_order("14", "13") == 1 and version_order("12", "13") == -1 and version_order("13", "13") == 0
    assert version_order("13-custom", "13") == 0 and version_order("9", "10") == -1      # numbers, not text
    assert version_order("custom", "13") is None and version_order("custom", "custom") == 0


def test_a_farm_mixes_versions_chosen_at_creation(farm):
    farm.mgr.create(template={"android_version": "12"})
    farm.mgr.create()                                         # the farm default
    farm.mgr.create(template={"android_version": "14"})
    assert [farm.mgr.resolved(i).android_version for i in (1, 2, 3)] == ["12", "13", "14"]
    for i in (1, 2, 3):
        farm.mgr.start(i)
    images = [next(a for a in farm.docker.containers[farm.names(i).android]["args"] if a.startswith("local/"))
              for i in (1, 2, 3)]
    assert images == ["local/android:12", "local/android:13", "local/android:14"]
    assert [s["android_version"] for s in farm.mgr.list_status()] == ["12", "13", "14"]
    with pytest.raises(ValueError, match="Android 15 has no image configured"):
        farm.mgr.create(template={"android_version": "15"})


def test_the_version_that_booted_the_data_is_recorded(farm):
    farm.mgr.create(template={"android_version": "12"})
    assert farm.mgr.status(1)["data_android_version"] == ""
    farm.mgr.start(1)
    assert farm.store.state_get(1, "data_android_version") == "12"
    assert farm.mgr.status(1)["data_android_version"] == "12"


def test_version_of_an_android_without_data_changes_freely(farm):
    farm.mgr.create(template={"android_version": "12"})
    assert farm.mgr.update(1, {"android_version": "14"}).android_version == "14"
    farm.mgr.start(1)
    assert image_of(farm) == "local/android:14" and farm.safety == []


def test_update_refuses_a_version_change_once_there_is_data(farm):
    farm.mgr.create(template={"android_version": "13"})
    farm.mgr.start(1)
    farm.mgr.stop(1)
    for wanted in ("14", "12"):
        with pytest.raises(NeedsForce, match=f"written by Android 13; its version is not changed to {wanted}") as refused:
            farm.mgr.update(1, {"android_version": wanted})
        assert refused.value.code == "needs_force"
    assert farm.store.get(1).android_version == "13"
    assert farm.mgr.update(1, {"android_version": "13", "fps": 30}).fps == 30       # same version: not a change
    assert farm.mgr.update(1, {"android_version": "14"}, force=True).android_version == "14"


def test_going_back_to_the_version_that_wrote_the_data_needs_no_force(farm):
    """The way out the refusal itself points to: a forced change that has not booted yet is undone freely."""
    farm.mgr.create(template={"android_version": "13"})
    farm.mgr.start(1)
    farm.mgr.stop(1)
    farm.mgr.update(1, {"android_version": "12"}, force=True)
    with pytest.raises(NeedsForce):
        farm.mgr.update(1, {"android_version": "14"})         # any other version is still a change of the data
    assert farm.mgr.update(1, {"android_version": "13"}).android_version == "13"
    result = farm.mgr.start(1)
    assert result["warnings"] == [] and farm.safety == [] and image_of(farm) == "local/android:13"


def test_data_without_a_recorded_version_still_counts_as_data(farm):
    """An adopted or restored volume: the farm never booted it, but it is not empty."""
    farm.mgr.create()
    farm.docker.volume_create(farm.names(1).volume, farm.names(1).labels("data"))
    with pytest.raises(NeedsForce, match="already has data"):
        farm.mgr.update(1, {"android_version": "14"})


def test_following_the_default_is_guarded_too(farm):
    farm.mgr.create()                                         # android_version None: follows the default (13)
    farm.mgr.start(1)
    farm.mgr.stop(1)
    farm.mgr.config.android.default_version = "12"            # the default moves under an Android with data
    with pytest.raises(NeedsForce, match="written by Android 13 and it is now set to Android 12"):
        farm.mgr.start(1)


def test_upgrade_starts_with_a_warning_and_a_backup_first(farm):
    farm.mgr.create(template={"android_version": "13"})
    farm.mgr.start(1)
    farm.mgr.stop(1)
    farm.mgr.update(1, {"android_version": "14"}, force=True)
    result = farm.mgr.start(1)                                # an upgrade needs no force
    assert result["state"] == "booted" and image_of(farm) == "local/android:14"
    assert any("written by Android 13 and now boots on Android 14" in w and "cannot go back" in w
               for w in result["warnings"])
    assert farm.safety == [(1, "before-version-change", False)] and result["backup"] == "/backups/android-1/x"
    assert farm.store.state_get(1, "data_android_version") == "14"
    farm.mgr.stop(1)
    again = farm.mgr.start(1)                                 # recorded: no second warning, no second backup
    assert again["warnings"] == [] and again["backup"] == "" and len(farm.safety) == 1


def test_downgrade_is_refused_at_start_unless_forced(farm):
    farm.mgr.create(template={"android_version": "13"})
    farm.mgr.start(1)
    farm.mgr.stop(1)
    farm.mgr.update(1, {"android_version": "12"}, force=True)
    with pytest.raises(NeedsForce, match="written by Android 13 and it is now set to Android 12"):
        farm.mgr.start(1)
    assert image_of(farm) == "local/android:13" and farm.safety == []      # nothing was rebuilt or copied
    assert farm.store.state_get(1, "data_android_version") == "13"
    assert "NeedsForce" in farm.mgr.status(1)["last_error"]
    with pytest.raises(NeedsForce):
        farm.mgr.recreate(1)
    result = farm.mgr.start(1, force=True)
    assert result["state"] == "booted" and image_of(farm) == "local/android:12"
    assert any(w.startswith("forced: booting data written by Android 13 on the older Android 12") for w in result["warnings"])
    assert farm.safety == [(1, "before-version-change", True)]


def test_a_refused_downgrade_leaves_a_running_android_running(farm):
    farm.mgr.create(template={"android_version": "13"})
    farm.mgr.start(1)
    farm.mgr.update(1, {"android_version": "12"}, force=True)
    for action in (farm.mgr.restart, farm.mgr.recreate):
        mark = len(farm.docker.calls)
        with pytest.raises(NeedsForce, match="it is now set to Android 12"):
            action(1)
        changed = [c for c in farm.docker.calls[mark:] if c[0] in ("stop", "rm", "start", "create")]
        assert changed == [] and farm.docker.running(ANDROID) and image_of(farm) == "local/android:13"
    assert farm.safety == []


def test_a_version_change_that_cannot_start_fails_before_the_stop_and_the_backup(farm):
    """The safety backup needs the Android stopped, so it comes last: after everything else
    that can refuse the start (here: the new version's image cannot be had)."""
    farm.mgr.create(template={"android_version": "13"})
    farm.mgr.start(1)
    farm.mgr.update(1, {"android_version": "14"}, force=True)
    del farm.docker.images["local/android:14"]
    farm.docker.pull_fails = True
    mark = len(farm.docker.calls)
    with pytest.raises(StartError, match="could not pull local/android:14"):
        farm.mgr.restart(1)
    assert [c for c in farm.docker.calls[mark:] if c[0] in ("stop", "rm", "start", "create")] == []
    assert farm.docker.running(ANDROID) and farm.safety == []
    assert farm.store.state_get(1, "data_android_version") == "13"


def test_a_version_change_on_a_running_android_stops_it_before_the_backup(farm):
    farm.mgr.create(template={"android_version": "13"})
    farm.mgr.start(1)
    farm.mgr.update(1, {"android_version": "14"}, force=True)
    stops_before = farm.docker.names_called("stop").count(ANDROID)
    seen = []
    farm.mgr.backups.safety = lambda spec, reason, *, force=False, task=None: (
        seen.append(farm.docker.running(ANDROID)) or ("", ""))
    farm.mgr.recreate(1)
    assert seen == [False] and farm.docker.names_called("stop").count(ANDROID) == stops_before + 1


def test_a_failed_safety_backup_refuses_the_version_change(farm):
    farm.mgr.create(template={"android_version": "13"})
    farm.mgr.start(1)
    farm.mgr.stop(1)
    farm.mgr.update(1, {"android_version": "14"}, force=True)

    def failing(spec, reason, *, force=False, task=None):
        if not force:
            raise NeedsForce("the safety backup could not be made, so nothing was changed: disk full")
        return "", "the safety backup failed and the operation went ahead because it was forced: disk full"

    farm.mgr.backups.safety = failing
    with pytest.raises(NeedsForce, match="disk full"):
        farm.mgr.start(1)
    assert image_of(farm) == "local/android:13"
    result = farm.mgr.start(1, force=True)
    assert image_of(farm) == "local/android:14" and any("went ahead because it was forced" in w for w in result["warnings"])


def test_the_same_release_under_another_name_is_a_note_not_a_version_change(farm):
    farm.mgr.create(template={"android_version": "13"})
    farm.mgr.start(1)
    farm.mgr.stop(1)
    farm.mgr.update(1, {"android_version": "13-custom"}, force=True)
    result = farm.mgr.start(1)
    assert any("last booted with Android image version '13'" in w for w in result["warnings"])
    assert farm.safety == [] and image_of(farm) == "local/android:13c"


def test_no_backup_when_auto_before_risky_is_off(tmp_path):
    farm = make(tmp_path, False)
    try:
        farm.mgr.create(template={"android_version": "13"})
        farm.mgr.start(1)
        farm.mgr.update(1, {"android_version": "14"}, force=True)
        result = farm.mgr.restart(1)
        assert result["state"] == "booted" and result["backup"] == "" and farm.safety == []
        assert any("now boots on Android 14" in w for w in result["warnings"])
    finally:
        farm.close()


def test_nvidia_on_a_version_without_the_runtime_is_refused_or_replaced(farm):
    farm.mgr.create(template={"android_version": "12", "gpu_mode": "nvidia"})
    farm.mgr.create(template={"android_version": "12"})                    # gpu mode auto
    farm.gpu.mode, farm.gpu.node = "nvidia", "/dev/dri/renderD129"
    with pytest.raises(StartError, match="gpu mode 'nvidia' is not available on Android 12: NVIDIA rendering is built "
                                         "for Android 13 only"):
        farm.mgr.start(1)
    assert farm.docker.containers == {}

    asked = []

    def choose(r, config, paths, *, counts=None):
        asked.append(r.gpu_mode)
        from core_fakes import FakeGpu
        return FakeGpu(mode="nvidia", node="/dev/dri/renderD129") if r.gpu_mode in ("auto", "igpu") else FakeGpu()

    farm.mgr.backends.gpu_choose = choose
    result = farm.mgr.start(2)
    assert asked == ["auto", "igpu", "software"] and result["gpu"] == "software"
    assert result["warnings"] == ["NVIDIA rendering is not available on Android 12: NVIDIA rendering is built for "
                                  "Android 13 only; using software instead"]
    farm.mgr.update(1, {"android_version": "13"})                          # on 13 the same request is fine
    farm.mgr.backends.gpu_choose = farm._choose
    assert farm.mgr.start(1)["gpu"] == "nvidia"

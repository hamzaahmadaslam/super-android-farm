"""Datasets, recordings and flow files on disk, and the path safety around them."""
from __future__ import annotations

import pytest

from superfarm.automation.store import (
    AutomationStore,
    Dataset,
    Recording,
    Step,
    check_package,
    check_slug,
    slugify,
)


def tap(rid: str = "", text: str = "", xy=(100, 200)) -> Step:
    element = {"id": rid, "text": text, "cls": "Button"} if (rid or text) else None
    return Step(kind="tap", device=list(xy), element=element)


def test_slugify_makes_a_safe_folder_name():
    assert slugify("Daily Sign-In #2!") == "daily-sign-in-2"
    assert slugify("  ../../etc/passwd ") == "etc-passwd"
    assert len(slugify("x" * 300)) <= 64
    for empty in ("", "   ", "!!!", "../.."):
        with pytest.raises(ValueError):
            slugify(empty)


@pytest.mark.parametrize("bad", ["", "..", "a/b", "A", "-a", "a b", "a\x00b", "x" * 65, None])
def test_check_slug_refuses_anything_but_a_plain_name(bad):
    with pytest.raises(ValueError):
        check_slug(bad)


def test_check_package():
    assert check_package("com.android.settings") == "com.android.settings"
    for bad in ("settings", "com..x", "com.x; reboot", "com.x/../y", "", "1com.x"):
        with pytest.raises(ValueError):
            check_package(bad)


def test_dataset_lifecycle(tmp_path):
    store = AutomationStore(tmp_path)
    dataset = store.create_dataset("Open Settings", "open the settings app", "com.android.settings")
    assert dataset.slug == "open-settings" and dataset.exists()
    assert dataset.name == "Open Settings" and dataset.package == "com.android.settings"
    with pytest.raises(FileExistsError):
        store.create_dataset("open settings")
    with pytest.raises(ValueError):
        store.create_dataset("x", package="not a package")
    assert [d.slug for d in store.datasets()] == ["open-settings"]

    dataset.update(description="changed")
    assert store.dataset("open-settings").description == "changed"
    with pytest.raises(KeyError):
        store.dataset("nope")
    assert store.delete_dataset("open-settings") and not store.delete_dataset("open-settings")
    assert store.datasets() == []


def test_recordings_round_trip(tmp_path):
    store = AutomationStore(tmp_path)
    dataset = store.create_dataset("search")
    recording = Recording(id=dataset.new_recording_id(), device="127.0.0.1:17501", instance=3, screen=[480, 854])
    recording.steps = [tap("search_bar"), Step(kind="type", text="hello"), tap("", "Submit"),
                       Step(kind="type", dynamic="term"), Step(kind="launch", package="com.example.app")]
    recording.steps[0].candidates = [{"id": "container"}]
    for index, step in enumerate(recording.steps):
        step.id = f"s{index:04d}"
    dataset.save_recording(recording)

    loaded = dataset.load_recording(recording.id)
    assert [s.kind for s in loaded.steps] == ["tap", "type", "tap", "type", "launch"]
    assert loaded.steps[0].element["id"] == "search_bar" and loaded.steps[0].candidates == [{"id": "container"}]
    assert loaded.steps[3].dynamic == "term" and loaded.steps[3].text is None
    assert loaded.taps == 2 and loaded.instance == 3 and loaded.screen == [480, 854]
    summary = dataset.recording_summaries()[0]
    assert summary["dynamic"] == ["term"] and summary["steps"] == 5
    assert summary["preview"][0] == "tap 'search_bar'" and "launch com.example.app" in summary["preview"]

    second = Recording(id=dataset.new_recording_id())
    second.steps = [tap("x")]
    dataset.save_recording(second)
    assert len(dataset.recording_ids()) == 2 and second.id.startswith("r002-")
    assert dataset.delete_recording(second.id) and len(dataset.recording_ids()) == 1
    assert dataset.delete_recording("r999-000000") is False
    assert dataset.summary()["recordings"] == 1


def test_a_recording_written_by_a_newer_version_still_loads(tmp_path):
    step = Step.from_dict({"kind": "tap", "device": [1, 2], "brand_new_field": {"x": 1}})
    assert step.kind == "tap" and step.device == [1, 2]
    recording = Recording.from_dict({"steps": [{"kind": "key", "key": "back"}, "junk"], "future": 1}, "r001")
    assert recording.id == "r001" and [s.kind for s in recording.steps] == ["key"]


@pytest.mark.parametrize("bad", ["..", "../x", "a/b", "", "r001/../../x", ".hidden"])
def test_recording_ids_cannot_leave_the_dataset(tmp_path, bad):
    dataset = AutomationStore(tmp_path).create_dataset("d")
    with pytest.raises(ValueError):
        dataset.recording_dir(bad)


def test_shot_paths_cannot_leave_the_recording(tmp_path):
    dataset = AutomationStore(tmp_path).create_dataset("d")
    assert dataset.shot_path("r001-1", "shots/s0000.png").name == "s0000.png"
    for bad in ("../../dataset.json", "/etc/passwd", "shots/../../../x"):
        with pytest.raises(ValueError):
            dataset.shot_path("r001-1", bad)


def test_a_dataset_cannot_be_opened_outside_its_root(tmp_path):
    (tmp_path / "outside").mkdir()
    for bad in ("..", "../outside", "a/b"):
        with pytest.raises(ValueError):
            Dataset(tmp_path / "datasets", bad)


def test_flow_files_are_named_by_slug_only(tmp_path):
    store = AutomationStore(tmp_path)
    store.write_flow("my-flow", "steps: []\n")
    assert store.flow_names() == ["my-flow"] and store.read_flow("my-flow") == "steps: []\n"
    (store.flows_dir / "Not A Slug.yaml").write_text("x")
    assert store.flow_names() == ["my-flow"]
    for bad in ("../x", "a/b", "", "UPPER"):
        with pytest.raises(ValueError):
            store.flow_path(bad)
    with pytest.raises(KeyError):
        store.read_flow("missing")
    assert store.delete_flow("my-flow") and not store.delete_flow("my-flow")


def test_run_folders_stay_under_the_runs_root(tmp_path):
    store = AutomationStore(tmp_path)
    folder = store.run_dir("../../t1", 4)
    assert folder.is_dir() and store.runs_dir.resolve() in folder.parents

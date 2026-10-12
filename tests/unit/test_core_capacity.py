"""Capacity is advice by default and a rule only when asked: capacity(), the warning path,
and performance.enforce_capacity with and without force."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core_fakes import Farm  # noqa: E402

from superfarm.core import instances as inst  # noqa: E402
from superfarm.core.instances import InstanceError, OverCapacity, _Booting  # noqa: E402


@pytest.fixture
def farm(tmp_path):
    f = Farm(tmp_path)
    yield f
    f.close()


def enforcing(tmp_path, **performance) -> Farm:
    return Farm(tmp_path, {"performance": {"enforce_capacity": True, **performance}})


# ------------------------------------------------------------------------- capacity()

def test_capacity_reports_estimate_running_and_headroom(farm):
    farm.mgr.create(count=3)
    farm.capacity_max = 5
    farm.mgr.start(1)
    farm.mgr.start(2)
    cap = farm.mgr.capacity()
    assert cap["running"] == 2 and cap["headroom"] == 3 and cap["over"] is False and cap["reasons"] == []
    assert cap["estimate"]["max_instances"] == 5 and cap["estimate"]["running_seen"] == 2
    assert cap["memory_available_mb"] == 16000 and cap["advice"] == ["add RAM"] and cap["enforced"] is False


def test_capacity_is_over_at_the_estimate_and_headroom_never_goes_negative(farm):
    farm.mgr.create(count=3)
    farm.capacity_max = 2
    for i in (1, 2, 3):
        farm.mgr.start(i)                                    # not enforced: the third one starts too
    cap = farm.mgr.capacity()
    assert cap["running"] == 3 and cap["headroom"] == 0 and cap["over"] is True
    assert "3 Android(s) are running" in cap["reasons"][0] and "about 2 (limited by ram)" in cap["reasons"][0]


def test_capacity_is_over_below_the_memory_guard(farm):
    farm.mem_mb = 900
    cap = farm.mgr.capacity()
    assert cap["over"] and cap["headroom"] == 20
    assert "900 MB of RAM is available" in cap["reasons"][0] and "performance.memory_guard_mb (1500 MB)" in cap["reasons"][0]
    farm.mgr.config.performance.memory_guard_mb = 0          # guard off
    assert farm.mgr.capacity()["over"] is False
    farm.mem_mb = -1                                         # unreadable meminfo: not a reason
    farm.mgr.config.performance.memory_guard_mb = 1500
    assert farm.mgr.capacity()["over"] is False


def test_a_failing_estimate_never_blocks(farm):
    def broken(config, running):
        raise OSError("no sysfs here")

    farm.mgr.backends.capacity_estimate = broken
    farm.mgr.create()
    cap = farm.mgr.capacity()
    assert cap["over"] is False and "no sysfs here" in cap["estimate"]["error"] and cap["advice"] == []
    assert farm.mgr.start(1)["state"] == "booted"


# ------------------------------------------------------------------------- warn (default)

def test_over_capacity_only_warns_by_default(farm):
    farm.mgr.create(count=2)
    farm.capacity_max = 1
    assert farm.mgr.start(1)["warnings"] == []
    result = farm.mgr.start(2)
    assert result["state"] == "booted"
    assert len(result["warnings"]) == 1 and result["warnings"][0].startswith("over capacity: 1 Android(s) are running")
    assert "performance.enforce_capacity is off" in result["warnings"][0]
    # the warning stays visible on the Android until its next start
    assert farm.mgr.status(2)["warnings"] == result["warnings"]
    assert farm.mgr.list_status()[0]["warnings"] == []


def test_low_memory_only_warns_by_default(farm):
    farm.mgr.create()
    farm.mem_mb = 900
    result = farm.mgr.start(1)
    assert result["state"] == "booted" and "below performance.memory_guard_mb" in result["warnings"][0]


def test_a_start_of_a_running_android_is_never_a_capacity_question(tmp_path):
    farm = enforcing(tmp_path)
    try:
        farm.mgr.create()
        farm.capacity_max = 1
        farm.mgr.start(1)
        again = farm.mgr.start(1)                            # already running: nothing is added to the machine
        assert again["already_running"] and again["warnings"] == []
        assert farm.mgr.recreate(1)["warnings"] == []        # rebuilt in place: it was already counted
    finally:
        farm.close()


# ------------------------------------------------------------------------- enforce

def test_enforced_capacity_refuses_before_touching_docker_and_carries_the_numbers(tmp_path):
    farm = enforcing(tmp_path)
    try:
        farm.mgr.create(count=2)
        farm.capacity_max = 1
        farm.mgr.start(1)
        created = list(farm.docker.names_called("create"))
        with pytest.raises(OverCapacity, match="not starting Android 2: over capacity") as refused:
            farm.mgr.start(2)
        assert isinstance(refused.value, InstanceError) and refused.value.code == "over_capacity"
        cap = refused.value.capacity
        assert cap["over"] and cap["running"] == 1 and cap["enforced"] and cap["estimate"]["max_instances"] == 1
        assert farm.docker.names_called("create") == created and f"{farm.names(2).android}" not in farm.docker.containers
        assert "OverCapacity" in farm.mgr.status(2)["last_error"]
    finally:
        farm.close()


def test_force_overrides_enforced_capacity_and_says_so(tmp_path):
    farm = enforcing(tmp_path)
    try:
        farm.mgr.create(count=2)
        farm.capacity_max = 1
        farm.mgr.start(1)
        result = farm.mgr.start(2, force=True)
        assert result["state"] == "booted" and "because the start was forced" in result["warnings"][0]
        farm.mgr.stop(2)
        with pytest.raises(OverCapacity):
            farm.mgr.restart(2)                               # it was stopped: this restart adds an Android
        assert farm.mgr.restart(2, force=True)["state"] == "booted"
    finally:
        farm.close()


def test_restarting_a_running_android_is_never_refused_halfway(tmp_path):
    """A restart that stopped an Android and then refused to start it would take it away."""
    farm = enforcing(tmp_path)
    try:
        farm.mgr.create()
        farm.mgr.start(1)
        farm.mem_mb = 900                                     # memory got tight while it was running
        result = farm.mgr.restart(1)
        assert result["state"] == "booted" and result["warnings"] == []
        farm.mgr.stop(1)
        with pytest.raises(OverCapacity):
            farm.mgr.start(1)
    finally:
        farm.close()


def test_enforced_memory_guard_refuses_and_force_overrides(tmp_path):
    farm = enforcing(tmp_path)
    try:
        farm.mgr.create()
        farm.mem_mb = 900
        with pytest.raises(OverCapacity, match="memory_guard_mb"):
            farm.mgr.start(1)
        assert farm.docker.containers == {}
        assert farm.mgr.start(1, force=True)["state"] == "booted"
    finally:
        farm.close()


def test_force_does_not_rebuild_containers(farm):
    """`force` is about refusals only; rebuilding is recreate()."""
    farm.mgr.create()
    farm.mgr.start(1)
    farm.mgr.stop(1)
    assert farm.mgr.start(1, force=True)["recreated"] is False
    assert farm.mgr.recreate(1)["recreated"] is True


def test_a_refused_start_releases_its_place(tmp_path):
    farm = enforcing(tmp_path)
    try:
        farm.mgr.create(count=2)
        farm.capacity_max = 0
        with pytest.raises(OverCapacity):
            farm.mgr.start(1)
        assert farm.mgr._booting.reserve(2, 100) == (set(), 0)     # Android 1 is not counted as booting
    finally:
        farm.close()


# ------------------------------------------------------------------------- simultaneous starts

def test_androids_that_are_booting_count_as_running_and_hold_memory():
    booting = _Booting()
    assert booting.reserve(1, 1100) == (set(), 0)
    assert booting.reserve(2, 1100) == ({1}, 1100)
    assert booting.reserve(1, 1100) == ({2}, 1100)            # the same Android again is not counted twice
    booting.release(1)
    assert booting.reserve(3, 500) == ({2}, 1100)


def test_memory_promised_to_booting_androids_is_subtracted(tmp_path):
    farm = enforcing(tmp_path)
    try:
        farm.mgr.create(count=2)
        farm.mem_mb = 2500
        farm.mgr._booting.reserve(1, 1100)                    # Android 1 is in the middle of booting
        with pytest.raises(OverCapacity, match="1100 MB of it promised to Androids that are booting") as refused:
            farm.mgr.start(2)
        assert refused.value.capacity["running"] == 1         # and it counts as running although no container exists yet
    finally:
        farm.close()


# ------------------------------------------------------------------------- bulk

def test_bulk_start_reports_each_refusal_and_force_reaches_every_android(tmp_path):
    farm = enforcing(tmp_path, start_concurrency=1)
    try:
        farm.mgr.create(count=3)
        farm.capacity_max = 1
        task = farm.tasks.wait(farm.mgr.bulk("start", "all"), timeout=20)
        assert task.state == "done" and task.results[1]["ok"]
        assert all(not task.results[i]["ok"] and "OverCapacity" in task.results[i]["detail"] for i in (2, 3))
        task = farm.tasks.wait(farm.mgr.bulk("start", "all", force=True), timeout=20)
        assert all(r["ok"] for r in task.results.values()) and len(farm.mgr.select("running")) == 3
        assert "warning(s): over capacity" in task.results[3]["detail"]
    finally:
        farm.close()


# ------------------------------------------------------------------------- real estimate

def test_default_estimate_uses_the_host_module_and_caches_the_card_list(monkeypatch):
    from superfarm.config import Config
    from superfarm.host import capacity, gpu

    calls = []
    monkeypatch.setattr(inst, "_gpu_list", None)
    monkeypatch.setattr(gpu, "detect_gpus", lambda: calls.append("detect") or [])
    monkeypatch.setattr(gpu, "nvidia_vram", lambda gpus: gpus)
    first = inst._capacity_estimate(Config(), 2)
    second = inst._capacity_estimate(Config(), 3)
    assert calls == ["detect"]                                # one probe for a whole bulk start
    assert first["max_instances"] == capacity.estimate(Config(), [], running=2)["max_instances"]
    assert {"limits", "binding", "advice", "max_instances"} <= set(second)

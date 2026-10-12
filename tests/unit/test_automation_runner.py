"""Fleet runs, schedules and the Automation facade, with scripted devices."""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from automation_fakes import PACKAGE, FakeDevice, FakeManager, button, el, field, screen  # noqa: E402

from superfarm.automation import Automation  # noqa: E402
from superfarm.automation.flows import FlowError  # noqa: E402
from superfarm.automation.program import Program, ProgramStep  # noqa: E402
from superfarm.automation.recorder import RecordingError  # noqa: E402
from superfarm.automation.schedule import Schedules, cron_next, parse_cron  # noqa: E402
from superfarm.config import Config  # noqa: E402
from superfarm.core.tasks import TaskManager  # noqa: E402
from superfarm.paths import Paths  # noqa: E402

HOME = screen(button("search", "Search", (20, 20, 220, 80)), el(id="title", text="Home", bounds=(0, 100, 480, 160)))
FORM = screen(field("query", "", (20, 20, 460, 80), focused=True), button("go", "Go", (20, 100, 220, 160)))
OTHER = screen(el(id="surprise", text="Something else", bounds=(0, 100, 480, 160)))

FLOW = """
package: com.example.app
variables: {query: earbuds}
steps:
  - tap: {id: search}
  - type: {text: "{query} from {instance_id}", into: {id: query}}
"""


class Farm:
    """An Automation wired to scripted devices instead of ADB."""

    def __init__(self, tmp_path, ids, *, max_parallel=8, start="home", **settings):
        self.config = Config.model_validate({"automation": {
            "max_parallel": max_parallel, "settle_seconds": 0, "human_jitter": False, "step_timeout": 1,
            "run_timeout": 30, "read_timeout": 5, **settings}})
        self.tasks = TaskManager()
        self.devices: dict[int, FakeDevice] = {}
        self.start = {i: start for i in ids} if isinstance(start, str) else start
        self.running_now = 0
        self.peak = 0
        self.gate = threading.Lock()
        self.auto = Automation(self.config, Paths(tmp_path).ensure(), FakeManager(ids), self.tasks)
        self.auto.runner._device_factory = self.device

    def device(self, instance_id: int) -> FakeDevice:
        farm = self

        class Counting(FakeDevice):
            def nodes(self, **kw):
                with farm.gate:
                    farm.running_now += 1
                    farm.peak = max(farm.peak, farm.running_now)
                try:
                    time.sleep(0.02)
                    return super().nodes(**kw)
                finally:
                    with farm.gate:
                        farm.running_now -= 1

        dev = Counting({"home": HOME, "form": FORM, "other": OTHER}, self.start[instance_id],
                       taps={("home", "search"): "form"})
        dev.serial = f"fake:{instance_id}"
        self.devices[instance_id] = dev
        return dev

    def wait(self, task, seconds=20):
        self.tasks.wait(task, timeout=seconds)
        assert not task.active, self.auto.run_status(task.id)
        return self.auto.run_status(task.id)

    def until(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("condition not reached")


@pytest.fixture
def farm_factory(tmp_path):
    made: list[Farm] = []

    def make(ids, **kw) -> Farm:
        made.append(Farm(tmp_path, ids, **kw))
        return made[-1]

    yield make
    for farm in made:
        farm.tasks.shutdown()


# ------------------------------------------------------------------- fleet
def test_a_flow_runs_on_every_selected_device_with_its_own_values(farm_factory):
    farm = farm_factory([1, 2, 3])
    farm.auto.flows.save("search", FLOW)
    task = farm.auto.run("flow", "search", "all")
    status = farm.wait(task)
    assert task.kind == "automation" and task.state == "done" and task.targets == [1, 2, 3]
    assert status["counts"] == {"done": 3} and task.output["ok"] == 3
    for i in (1, 2, 3):
        assert farm.devices[i].did("text") == [("text", f"earbuds from {i}")]
        assert task.results[i]["ok"] and status["devices"][str(i)]["steps"] == 2
    assert farm.auto.runs()[0]["id"] == task.id and farm.auto.runs(active_only=True) == []


def test_run_variables_override_flow_defaults(farm_factory):
    farm = farm_factory([1])
    farm.auto.flows.save("search", FLOW)
    farm.wait(farm.auto.run("flow", "search", "1", variables={"query": "phones"}))
    assert farm.devices[1].did("text") == [("text", "phones from 1")]


def test_max_parallel_limits_how_many_devices_work_at_once(farm_factory):
    farm = farm_factory(list(range(1, 9)), max_parallel=2)
    farm.auto.flows.save("search", FLOW)
    status = farm.wait(farm.auto.run("flow", "search", "all"))
    assert status["counts"] == {"done": 8} and farm.peak <= 2


def test_one_device_failing_or_needing_a_person_does_not_hold_up_the_others(farm_factory):
    farm = farm_factory([1, 2, 3, 4], max_parallel=1, start={1: "other", 2: "home", 3: "home", 4: "other"})
    farm.auto.flows.save("search", FLOW)
    task = farm.auto.run("flow", "search", "all")
    # 1 and 4 are on an unknown screen; with ONE slot, 2 and 3 can only finish if a
    # device waiting for a person gives its slot away
    farm.until(lambda: farm.auto.run_status(task.id)["counts"].get("done") == 2
               and farm.auto.run_status(task.id)["counts"].get("needs_attention") == 2)
    status = farm.auto.run_status(task.id)
    assert status["waiting"] == [1, 4] and task.active
    assert status["devices"]["1"]["waiting"]["what"] == "tap id='search'"
    assert "waiting for you" in task.message

    farm.devices[1].screen = "home"                                  # a person fixed device 1
    assert farm.auto.resolve(task.id, 1, "resume")
    assert farm.auto.resolve(task.id, 4, "abort")
    status = farm.wait(task)
    assert status["counts"] == {"done": 3, "failed": 1}
    assert task.results[4] == {"ok": False, "detail": "failed: stopped by the operator at tap id='search'"}
    assert task.results[1]["ok"] and not farm.auto.resolve(task.id, 1, "resume")
    assert task.output == {"ok": 3, "failed": 1, "devices": {"1": "done", "2": "done", "3": "done", "4": "failed"}}


def test_cancel_one_device_or_the_whole_run(farm_factory):
    farm = farm_factory([1, 2, 3], start="other")
    farm.auto.flows.save("search", FLOW)
    task = farm.auto.run("flow", "search", "all")
    farm.until(lambda: farm.auto.run_status(task.id)["counts"].get("needs_attention") == 3)
    assert farm.auto.cancel_run(task.id, 2)
    farm.until(lambda: farm.auto.run_status(task.id)["devices"]["2"]["state"] == "cancelled")
    assert farm.auto.run_status(task.id)["counts"] == {"needs_attention": 2, "cancelled": 1}
    assert not farm.auto.cancel_run(task.id, 2) and not farm.auto.cancel_run(task.id, 99)
    assert farm.auto.cancel_run(task.id)
    status = farm.wait(task)
    assert status["counts"] == {"cancelled": 3} and task.state == "cancelled"
    assert not any(d.acted for d in farm.devices.values())
    assert not farm.auto.cancel_run("t-unknown") and farm.auto.run_status("t-unknown") is None


def test_queued_devices_can_be_cancelled_before_they_start(farm_factory):
    farm = farm_factory([1, 2, 3], max_parallel=1, start="other", stop_on_unknown_screen=True)
    farm.auto.flows.save("slow", "steps: [{wait_for: {id: never}, timeout: 5}]")
    task = farm.auto.run("flow", "slow", "all")
    farm.until(lambda: farm.auto.run_status(task.id)["counts"].get("running") == 1)
    assert farm.auto.run_status(task.id)["counts"] == {"running": 1, "queued": 2}
    farm.auto.cancel_run(task.id)
    status = farm.wait(task)
    assert status["counts"] == {"cancelled": 3}
    assert sum(1 for r in task.results.values() if "before it started" in r["detail"]) == 2


def test_a_device_cannot_be_in_two_runs_at_once(farm_factory):
    farm = farm_factory([1, 2], start={1: "other", 2: "home"})
    farm.auto.flows.save("search", FLOW)
    first = farm.auto.run("flow", "search", "1")
    farm.until(lambda: farm.auto.run_status(first.id)["counts"].get("needs_attention") == 1)
    second = farm.auto.run("flow", "search", "all")
    status = farm.wait(second)
    assert status["devices"]["2"]["state"] == "done"
    assert status["devices"]["1"]["state"] == "failed" and first.id in status["devices"]["1"]["message"]
    with pytest.raises(RecordingError, match="busy in automation run"):
        farm.auto.start_recording("whatever", 1)
    farm.auto.cancel_run(first.id)
    farm.wait(first)


def test_problems_are_raised_before_any_device_is_touched(farm_factory):
    farm = farm_factory([1, 2])
    farm.auto.flows.save("search", FLOW)
    farm.auto.flows.save("needs-role", "package: com.example.app\nsteps: [{tap: '@send'}]")
    for call, error in (
            (lambda: farm.auto.run("macro", "search", "all"), ValueError),
            (lambda: farm.auto.run("flow", "missing", "all"), FlowError),
            (lambda: farm.auto.run("flow", "needs-role", "all"), FlowError),
            (lambda: farm.auto.run("flow", "search", "none"), ValueError),
            (lambda: farm.auto.run("flow", "search", "all", variables={"bad name": "x"}), ValueError),
            (lambda: farm.auto.run("program", "missing", "all"), KeyError),
            (lambda: farm.auto.run("program", "../x", "all"), ValueError)):
        with pytest.raises(error):
            call()
    assert farm.devices == {} and farm.tasks.list() == []


def test_a_trained_program_runs_across_the_fleet_and_in_practice_mode(farm_factory):
    farm = farm_factory([1, 2])
    Program(slug="demo", name="Demo", package=PACKAGE, screen=[480, 854], steps=[
        ProgramStep(id="p000", kind="tap", selector={"id": "search"}, device=[120, 50]),
        ProgramStep(id="p001", kind="type", dynamic="term", selector={"id": "query"})]).save(farm.auto.store.programs_dir)
    farm.auto.values.set_shared("term", "shared term")
    farm.auto.values.set_instance(2, "term", "term of two")

    practice = farm.auto.run("program", "demo", "all", dry_run=True)
    status = farm.wait(practice)
    assert status["dry_run"] and status["counts"] == {"done": 2} and not any(d.acted for d in farm.devices.values())
    assert "practice" in practice.title

    status = farm.wait(farm.auto.run("program", "demo", "all"))
    assert status["counts"] == {"done": 2}
    assert farm.devices[1].did("text") == [("text", "shared term")]
    assert farm.devices[2].did("text") == [("text", "term of two")]
    assert farm.auto.program("demo")["outline"][0]["label"] == "tap search"
    assert [p["slug"] for p in farm.auto.programs()] == ["demo"]
    assert farm.auto.delete_program("demo") and farm.auto.programs() == []


def test_a_device_that_cannot_be_reached_is_one_failed_result(farm_factory):
    farm = farm_factory([1, 2])
    farm.auto.flows.save("search", FLOW)
    real = farm.device

    def factory(instance_id):
        if instance_id == 1:
            from superfarm.automation.ui import UiError
            raise UiError("Android 1 does not answer over ADB (fake:1); start it first")
        return real(instance_id)

    farm.auto.runner._device_factory = factory
    task = farm.auto.run("flow", "search", "all")
    status = farm.wait(task)
    assert status["devices"]["1"]["state"] == "failed" and "start it first" in task.results[1]["detail"]
    assert status["devices"]["2"]["state"] == "done"


def test_secret_run_variables_stay_out_of_status_and_disk(farm_factory, tmp_path):
    farm = farm_factory([1])
    farm.auto.flows.save("login", "variables: {password: null}\nsteps: [{tap: {id: search}}, {type: '{password}'}]")
    task = farm.auto.run("flow", "login", "1", variables={"password": "hunter2"})
    status = farm.wait(task)
    assert farm.devices[1].did("text") == [("text", "hunter2")]
    assert "hunter2" not in str(status) and "hunter2" not in str(task.to_dict())
    assert not any("hunter2" in p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file())


# ------------------------------------------------------------- the facade
def test_record_train_and_run_through_the_facade(farm_factory):
    farm = farm_factory([1, 2, 3])
    auto = farm.auto
    assert auto.create_dataset("Open Search", "open the search form", PACKAGE)["slug"] == "open-search"
    for instance in (1, 2, 3):
        auto.start_recording("open-search", instance)
        step = auto.record_tap(instance, 0.25, 0.058)
        assert step["element"]["id"] == "search"
        auto.record_text(instance, "hello")
        assert auto.recording_status(instance)["steps"] == 2
        assert "saved" in auto.stop_recording(instance)["summary"]
    listing = auto.dataset("open-search")
    assert listing["recordings"] == 3 and len(listing["recordings_list"]) == 3
    rid = listing["recordings_list"][0]["id"]
    assert auto.recording("open-search", rid)["describe"] == ["tap 'Search'", "type 'hello'"]
    assert auto.recording_shot("open-search", rid, "shots/s0000.png").is_file()
    with pytest.raises(ValueError):
        auto.recording_shot("open-search", rid, "../../dataset.json")

    training = auto.train("open-search")
    farm.tasks.wait(training, timeout=20)
    assert training.state == "done" and training.output["steps"] == 2 and training.kind == "automation-train"
    assert auto.datasets()[0]["trained"]
    assert auto.program_shot("open-search", "shots/p000.png").is_file()

    for dev in farm.devices.values():
        dev.screen, dev.sent = "home", []
    status = farm.wait(auto.run("program", "open-search", "1-3"))
    assert status["counts"] == {"done": 3}
    assert all(d.did("text") == [("text", "hello")] for d in farm.devices.values())

    assert auto.llm_status() == {"provider": "none", "ok": True,
                                 "detail": "no model configured; automation is deterministic"}
    assert auto.delete_recording("open-search", rid) and auto.delete_dataset("open-search")
    with pytest.raises(ValueError, match="no recordings yet"):
        auto.create_dataset("Empty")
        auto.train("empty")


def test_devices_being_recorded_are_not_run_on(farm_factory):
    farm = farm_factory([1, 2])
    farm.auto.create_dataset("d")
    farm.auto.flows.save("search", FLOW)
    farm.auto.start_recording("d", 1)
    with pytest.raises(ValueError, match="being recorded right now: 1"):
        farm.auto.run("flow", "search", "all")
    assert farm.wait(farm.auto.run("flow", "search", "2"))["counts"] == {"done": 1}
    assert farm.auto.cancel_recording(1)


def test_screen_reading_and_teaching_through_the_facade(farm_factory):
    farm = farm_factory([1])
    seen = farm.auto.read_screen(1)
    assert seen["package"] == PACKAGE and seen["size"] == [480, 854] and seen["instance"] == 1
    assert any(e["selector"] == {"id": "search", "text": "Search"} for e in seen["elements"])
    assert farm.auto.element_at(1, 120, 50)["id"] == "search"
    taught = farm.auto.teach_role(1, PACKAGE, "search_button", 120, 50, with_text=False)
    assert taught == {"role": "search_button", "selector": {"id": "search"}}
    farm.auto.flows.save("by-role", f"package: {PACKAGE}\nsteps: [{{tap: '@search_button'}}]")
    assert farm.wait(farm.auto.run("flow", "by-role", "1"))["counts"] == {"done": 1}
    with pytest.raises(ValueError, match="nothing with an id"):
        farm.auto.teach_role(1, PACKAGE, "nothing", 470, 840)


def test_runs_survive_a_config_reload(farm_factory):
    farm = farm_factory([1], start="other")
    farm.auto.flows.save("search", FLOW)
    task = farm.auto.run("flow", "search", "1")
    farm.until(lambda: farm.auto.run_status(task.id)["counts"].get("needs_attention") == 1)
    farm.auto.reload_config(farm.config)
    assert farm.auto.run_status(task.id)["waiting"] == [1]
    farm.devices[1].screen = "home"
    assert farm.auto.resolve(task.id, 1, "resume")
    assert farm.wait(task)["counts"] == {"done": 1}


# ---------------------------------------------------------------- schedules
def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


@pytest.mark.parametrize("expression, after, expected", [
    ("* * * * *", "2026-03-10 12:00", "2026-03-10 12:01"),
    ("*/15 * * * *", "2026-03-10 12:07", "2026-03-10 12:15"),
    ("*/15 * * * *", "2026-03-10 12:45", "2026-03-10 13:00"),
    ("30 3 * * *", "2026-03-10 03:30", "2026-03-11 03:30"),              # strictly after
    ("30 3 * * *", "2026-03-10 03:29", "2026-03-10 03:30"),
    ("0 9-17 * * 1-5", "2026-03-13 17:00", "2026-03-16 09:00"),          # Friday evening -> Monday
    ("0 0 1 * *", "2026-12-15 08:00", "2027-01-01 00:00"),
    ("0 0 31 * *", "2026-04-01 00:00", "2026-05-31 00:00"),              # April has no 31st
    ("0 12 29 2 *", "2026-03-01 00:00", "2028-02-29 12:00"),             # the next leap day
    ("0 0 * * 0", "2026-03-10 00:00", "2026-03-15 00:00"),               # Sunday as 0
    ("0 0 * * 7", "2026-03-10 00:00", "2026-03-15 00:00"),               # ... and as 7
    ("0 0 13 * 5", "2026-03-10 00:00", "2026-03-13 00:00"),              # 13th OR a Friday (here: both)
    ("0 0 20 * 1", "2026-03-10 00:00", "2026-03-16 00:00"),              # the Monday comes before the 20th
    ("5,35 8 * 6 *", "2026-06-30 08:35", "2027-06-01 08:05"),
    ("10-20/5 * * * *", "2026-03-10 12:16", "2026-03-10 12:20"),
])
def test_cron_next(expression, after, expected):
    assert cron_next(parse_cron(expression), at(after)) == at(expected)


@pytest.mark.parametrize("bad", ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                                 "* * * * 8", "*/0 * * * *", "a * * * *", "5-1 * * * *", "0 0 31 2 *", "1,,2 * * * *"])
def test_bad_cron_expressions_are_refused(bad):
    with pytest.raises(ValueError):
        parse_cron(bad)


class Starter:
    def __init__(self):
        self.started: list[str] = []
        self.active: set[str] = set()
        self.fail = False

    def start(self, schedule):
        if self.fail:
            raise ValueError("the selector matches no instance")
        task_id = f"t{len(self.started) + 1}"
        self.started.append(schedule.name)
        self.active.add(task_id)
        return type("T", (), {"id": task_id})()


@pytest.fixture
def schedules(tmp_path):
    starter = Starter()
    return Schedules(tmp_path / "schedules.json", starter.start, is_active=lambda t: t in starter.active,
                     forbidden_words=["password"]), starter


def test_an_interval_schedule_fires_when_due_and_never_overlaps_itself(schedules):
    book, starter = schedules
    created = book.add(name="hourly", kind="flow", target="search", selector="all", every=3600, now=1000.0)
    assert created.next_run == 4600.0 and book.tick(now=4599.0) == []
    assert book.tick(now=4600.0) == [created.id] and starter.started == ["hourly"]
    row = book.list()[0]
    assert row["last_task"] == "t1" and row["runs"] == 1 and row["next_run"] == 8200.0 and row["last_result"] == "started"

    # still running when it comes due again: skipped, not queued, not started twice
    assert book.tick(now=8200.0) == [] and starter.started == ["hourly"]
    row = book.list()[0]
    assert row["skipped"] == 1 and "previous run was still going" in row["last_result"] and row["next_run"] == 11800.0
    starter.active.clear()
    assert book.tick(now=11800.0) == [created.id] and len(starter.started) == 2


def test_missed_occurrences_are_not_replayed(schedules):
    book, starter = schedules
    created = book.add(name="often", kind="flow", target="search", selector="all", every=60, now=0.0)
    starter.active.clear()
    assert book.tick(now=86_400.0) == [created.id]                  # a day late: one run, not 1440
    assert len(starter.started) == 1 and book.list()[0]["next_run"] == 86_460.0
    assert book.tick(now=86_401.0) == []


def test_a_cron_schedule_and_disabled_or_broken_ones(schedules):
    book, starter = schedules
    start = at("2026-03-10 12:00").timestamp()
    nightly = book.add(name="nightly", kind="program", target="demo", selector="1-5", cron="30 3 * * *", now=start)
    paused = book.add(name="paused", kind="flow", target="x", selector="all", every=60, enabled=False, now=start)
    assert nightly.next_run == at("2026-03-11 03:30").timestamp()
    assert book.tick(now=at("2026-03-11 03:29").timestamp()) == []
    starter.fail = True
    assert book.tick(now=at("2026-03-11 03:30").timestamp()) == []
    row = next(r for r in book.list() if r["id"] == nightly.id)
    assert "could not start: the selector matches no instance" in row["last_result"]
    assert row["next_run"] == at("2026-03-12 03:30").timestamp()    # one broken schedule keeps its rhythm
    starter.fail = False
    assert book.tick(now=at("2026-03-12 03:31").timestamp()) == [nightly.id]
    assert next(r for r in book.list() if r["id"] == paused.id)["runs"] == 0


def test_schedules_are_validated_and_persisted(schedules, tmp_path):
    book, starter = schedules
    good = {"name": "n", "kind": "flow", "target": "t", "selector": "all"}
    for bad in ({**good, "every": 5}, {**good}, {**good, "every": 60, "cron": "* * * * *"},
                {**good, "cron": "nonsense"}, {**good, "every": 60, "kind": "macro"},
                {**good, "every": 60, "name": " "}, {**good, "every": 60, "selector": ""},
                {**good, "every": 60, "variables": {"password": "hunter2"}},
                {**good, "every": 60, "variables": {"bad name": "x"}}):
        with pytest.raises(ValueError):
            book.add(**bad)
    assert book.list() == []
    created = book.add(**good, every=60, variables={"query": 5}, now=0.0)
    again = Schedules(tmp_path / "schedules.json", starter.start, is_active=lambda t: False)
    assert again.get(created.id).variables == {"query": "5"}

    changed = again.update(created.id, {"cron": "0 * * * *", "enabled": False}, now=0.0)
    assert changed.cron == "0 * * * *" and changed.every == 0 and not changed.enabled
    back = again.update(created.id, {"every": 120}, now=10.0)
    assert back.every == 120 and back.cron == "" and back.next_run == 130.0
    with pytest.raises(ValueError, match="cannot change"):
        again.update(created.id, {"runs": 99})
    with pytest.raises(KeyError):
        again.update("0123456789ab", {"enabled": True})
    assert again.remove(created.id) and not again.remove(created.id) and not again.remove("../x")


def test_the_facade_tick_starts_due_schedules_as_runs(farm_factory):
    farm = farm_factory([1, 2])
    farm.auto.flows.save("search", FLOW)
    created = farm.auto.schedules.add(name="soon", kind="flow", target="search", selector="all", every=60,
                                      now=time.time() - 120)
    assert farm.auto.tick() == [created.id]
    row = farm.auto.schedules.list()[0]
    task = farm.tasks.get(row["last_task"])
    assert farm.wait(task)["counts"] == {"done": 2}
    assert farm.auto.tick() == []

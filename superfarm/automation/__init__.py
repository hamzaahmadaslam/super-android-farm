"""Automation: record, learn, replay and script any app across the fleet.

    record     the panel sends taps for one device; each is resolved against the live
               UI tree *before* it is performed                       (recorder.py)
    train      several recordings of one goal become one program by consensus;
               a model may describe the steps but never changes them  (trainer.py)
    run        a trained program or a hand-written YAML flow on any selection of
               devices, each on its own, with pop-up rules, a never-tap list,
               deadlines, and a hand-off to a person when stuck  (runtime.py, flows.py)
    teach      per-app profiles: named selectors and pop-up rules      (profiles.py)
    values     shared values, per-instance values, consumable pools     (values.py)
    schedule   recurring runs                                         (schedule.py)

Nothing in this package knows about any particular app. `Automation` is the one
object the API, the CLI and the daemon talk to.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config
from ..core.tasks import Task, TaskManager
from ..paths import Paths
from . import llm as llm_mod
from .flows import FlowError, FlowLibrary
from .memory import FixMemory
from .profiles import Profiles, screen_elements
from .program import Program
from .recorder import Recorder, RecordingError
from .runner import Runner
from .schedule import Schedule, Schedules
from .selectors import Selector, find, find_scored, match, score
from .store import AutomationStore, check_slug
from .trainer import train as train_dataset
from .ui import Device, UiError, element_at, foreground_package, read_nodes
from .values import Values

__all__ = ["Automation", "FlowError", "RecordingError", "Selector", "UiError", "element_at", "find", "find_scored",
           "match", "read_nodes", "score"]


class Automation:
    def __init__(self, config: Config, paths: Paths, mgr: Any, tasks: TaskManager):
        self.paths, self.mgr, self.tasks = paths, mgr, tasks
        self.store = AutomationStore(paths.automation)
        self._build(config)

    def _build(self, config: Config) -> None:
        self.config = config
        auto = config.automation
        self.values = Values(self.store.values_dir, auto.forbidden_field_words)
        self.profiles = Profiles(self.store.profiles_dir)
        self.flows = FlowLibrary(self.store, auto.forbidden_field_words)
        self.memory = FixMemory(self.store.memory_dir, enabled=auto.learn_fixes)
        previous = getattr(self, "runner", None)
        self.runner = Runner(config, self.paths, self.mgr, self.tasks, store=self.store, values=self.values,
                             profiles=self.profiles, library=self.flows, memory=self.memory)
        if previous is not None:
            # Runs started before a config change keep going and stay visible.
            self.runner.adopt(previous)
        recorder = getattr(self, "recorder", None)
        self.recorder = Recorder(self.store, auto, self.runner.device, self.values)
        if recorder is not None:
            self.recorder.adopt(recorder)
        self.schedules = Schedules(self.store.schedules_file, self._start_scheduled, is_active=self._task_active,
                                   forbidden_words=auto.forbidden_field_words)

    def reload_config(self, config: Config) -> None:
        self._build(config)

    # ------------------------------------------------------------ hierarchy
    def device(self, id: int) -> Device:
        return self.runner.device(id)

    def read_screen(self, id: int) -> dict:
        """One fresh, bounded read of a device: its elements and what is in front."""
        device = self.device(id)
        try:
            nodes = device.nodes()
            package, activity = device.current()
            size = list(device.size())
        finally:
            device.close()
        return {"instance": int(id), "package": package or foreground_package(nodes), "activity": activity,
                "size": size, "nodes": nodes, "elements": screen_elements(nodes)}

    def element_at(self, id: int, x: int, y: int) -> dict | None:
        device = self.device(id)
        try:
            return element_at(device.nodes(), int(x), int(y))
        finally:
            device.close()

    # ------------------------------------------------------------- datasets
    def datasets(self) -> list[dict]:
        return [d.summary(self.store.programs_dir) for d in self.store.datasets()]

    def create_dataset(self, name: str, description: str = "", package: str = "") -> dict:
        return self.store.create_dataset(name, description, package).summary(self.store.programs_dir)

    def dataset(self, slug: str) -> dict:
        dataset = self.store.dataset(slug)
        return {**dataset.summary(self.store.programs_dir), "recordings_list": dataset.recording_summaries()}

    def update_dataset(self, slug: str, **changes: Any) -> dict:
        dataset = self.store.dataset(slug)
        dataset.update(**changes)
        return dataset.summary(self.store.programs_dir)

    def delete_dataset(self, slug: str) -> bool:
        return self.store.delete_dataset(slug)

    def recording(self, slug: str, rid: str) -> dict:
        recording = self.store.dataset(slug).load_recording(rid)
        if recording is None:
            raise KeyError(f"no recording {rid!r} in {slug!r}")
        return {**recording.to_dict(), "describe": [s.describe() for s in recording.steps]}

    def delete_recording(self, slug: str, rid: str) -> bool:
        return self.store.dataset(slug).delete_recording(rid)

    def recording_shot(self, slug: str, rid: str, relative: str) -> Path:
        """Path of one step's picture, for the panel to serve."""
        path = self.store.dataset(slug).shot_path(rid, relative)
        if not path.is_file():
            raise KeyError("no such picture")
        return path

    # ------------------------------------------------------------ recording
    def start_recording(self, dataset: str, id: int) -> dict:
        busy = self.runner.busy()
        if int(id) in busy:
            raise RecordingError(f"Android {id} is busy in automation run {busy[int(id)]}; wait for it or cancel it")
        return self.recorder.start(dataset, id)

    def record_tap(self, id: int, x: float, y: float, *, normalized: bool = True) -> dict:
        return self.recorder.session(id).tap(x, y, normalized=normalized)

    def record_long_press(self, id: int, x: float, y: float, *, ms: int = 700, normalized: bool = True) -> dict:
        return self.recorder.session(id).long_press(x, y, ms=ms, normalized=normalized)

    def record_swipe(self, id: int, x1: float, y1: float, x2: float, y2: float, *, ms: int = 300,
                     normalized: bool = True) -> dict:
        return self.recorder.session(id).swipe(x1, y1, x2, y2, ms=ms, normalized=normalized)

    def record_text(self, id: int, value: str | None = None, *, name: str | None = None) -> dict:
        return self.recorder.session(id).text(value, name=name)

    def record_key(self, id: int, name: str) -> dict:
        return self.recorder.session(id).key(name)

    def record_launch(self, id: int, package: str) -> dict:
        return self.recorder.session(id).launch(package)

    def record_wait(self, id: int, seconds: float) -> dict:
        return self.recorder.session(id).wait(seconds)

    def record_assert_visible(self, id: int, x: float, y: float, *, normalized: bool = True) -> dict:
        return self.recorder.session(id).assert_visible(x, y, normalized=normalized)

    def record_undo(self, id: int) -> bool:
        return self.recorder.session(id).undo()

    def recording_status(self, id: int) -> dict:
        return self.recorder.session(id).status()

    def stop_recording(self, id: int) -> dict:
        return self.recorder.stop(id)

    def cancel_recording(self, id: int) -> bool:
        return self.recorder.cancel(id)

    # ------------------------------------------------------------- training
    def train(self, dataset: str) -> Task:
        """Build the dataset's program. A Task, because a model may take a while."""
        target = self.store.dataset(dataset)
        if not target.recording_ids():
            raise ValueError(f"the dataset {target.name!r} has no recordings yet")

        def work(task: Task) -> dict:
            task.step("building the consensus")
            try:
                client = llm_mod.make_client(self.config.automation.llm, self.paths)
            except ValueError as e:
                client = None
                task.step(f"training without a model: {e}")
            program = train_dataset(target, self.store.programs_dir, client=client,
                                    forbidden_words=self.config.automation.forbidden_field_words, log=task.step)
            return program.summary()

        return self.tasks.submit("automation-train", f"train {target.name}", work)

    def programs(self) -> list[dict]:
        return [p.summary() for p in Program.list(self.store.programs_dir)]

    def program(self, slug: str) -> dict:
        program = Program.load(self.store.programs_dir, check_slug(slug))
        if program is None:
            raise KeyError(f"no trained program {slug!r}")
        return {**program.summary(), "outline": program.outline()}

    def program_shot(self, slug: str, relative: str) -> Path:
        program = Program.load(self.store.programs_dir, check_slug(slug))
        path = program.shot_path(relative) if program else None
        if path is None or not path.is_file():
            raise KeyError("no such picture")
        return path

    def delete_program(self, slug: str) -> bool:
        return Program.delete(self.store.programs_dir, slug)

    # ------------------------------------------------------------- profiles
    def teach_role(self, id: int, package: str, role: str, x: int, y: int, *, with_text: bool = True) -> dict:
        """Name the element under (x, y) on a device's live screen as a role of an app."""
        device = self.device(id)
        try:
            nodes = device.nodes()
        finally:
            device.close()
        # Resolved exactly as a recorded tap would be: the element a tap there lands
        # on, with the label inside it - so a role names what a person would point at,
        # not an anonymous wrapper that happens to be the smallest box.
        node = element_at(nodes, int(x), int(y))
        if node is None or not (node["id"] or node["text"] or node["desc"]):
            raise ValueError("there is nothing with an id, text or description at that point")
        return {"role": role, "selector": self.profiles.teach(package, role, node, with_text=with_text)}

    # ------------------------------------------------------------------ run
    def run(self, kind: str, name: str, selector: str, *, variables: dict | None = None,
            dry_run: bool = False) -> Task:
        """Run a trained program or a flow on the selected instances. kind = program | flow."""
        recording = self.recorder.busy()
        if recording and not dry_run:
            chosen = {int(i) for i in self.mgr.select(selector)} & recording
            if chosen:
                raise ValueError("these devices are being recorded right now: "
                                 + ", ".join(str(i) for i in sorted(chosen)) + ". Stop the recording first.")
        return self.runner.run(kind, name, selector, variables=variables, dry_run=dry_run)

    def run_status(self, task_id: str) -> dict | None:
        return self.runner.status(task_id)

    def runs(self, active_only: bool = False) -> list[dict]:
        return self.runner.runs(active_only)

    def cancel_run(self, task_id: str, id: int | None = None) -> bool:
        return self.runner.cancel(task_id, id)

    def resolve(self, task_id: str, id: int, action: str = "resume") -> bool:
        """Answer a device that needs attention: resume | skip | abort."""
        return self.runner.resolve(task_id, id, action)

    # ------------------------------------------------------------------ llm
    def llm_status(self) -> dict:
        settings = self.config.automation.llm
        if settings.provider == "none":
            return {"provider": "none", "ok": True, "detail": "no model configured; automation is deterministic"}
        try:
            ok, detail = llm_mod.LlmClient(settings, self.paths).health()
        except ValueError as e:
            ok, detail = False, str(e)
        return {"provider": settings.provider, "ok": ok, "detail": detail}

    # ------------------------------------------------------------ schedules
    def _start_scheduled(self, schedule: Schedule) -> Task:
        return self.run(schedule.kind, schedule.target, schedule.selector, variables=schedule.variables,
                        dry_run=schedule.dry_run)

    def _task_active(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        return bool(task and task.active)

    def tick(self) -> list[str]:
        """Called by the daemon about once a minute: start the schedules that are due."""
        return self.schedules.tick()

"""Running one program or flow across the fleet.

One Task per run (so the panel and CLI show progress and can cancel it), one thread
per device, and `automation.max_parallel` devices working at the same time while the
rest wait as `queued`. Devices are independent in every way that matters:

  * each has its own state (queued / running / done / failed / needs_attention /
    cancelled), step index and message;
  * one that fails, stalls or needs a person never holds up the others;
  * a device waiting for a person gives its slot to a queued device, and takes one
    again when it is resumed;
  * each can be cancelled or resolved (resume / skip / abort) on its own.

Everything goes over ADB to one serial per thread. Nothing here touches a desktop
window or a mouse, which is what lets forty devices run at once.
"""
from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import Config
from ..core.tasks import Task, TaskManager
from ..paths import Paths
from . import llm as llm_mod
from .flows import Flow, FlowLibrary, FlowRun
from .memory import FixMemory
from .profiles import Profiles
from .program import Program
from .runtime import ProgramRun, Session
from .store import AutomationStore, check_slug
from .ui import Device, UiError, make_reader
from .values import Resolver, Values, check_name

KIND = "automation"          # Task.kind of every run started here
KEEP_RUNS = 50


class _Entry:
    """One device's part in a run."""

    def __init__(self, instance_id: int):
        self.id = instance_id
        self.state = "queued"
        self.message = ""
        self.session: Session | None = None
        self.cancel = threading.Event()
        self.holding = False                 # does this device hold a parallel slot right now?
        self.finished = False

    def snapshot(self) -> dict:
        if self.session is not None:
            data = self.session.snapshot()
            # A device waiting for a person is reported as that, even though its
            # thread is alive; a finished session keeps its own final state.
            return {"id": self.id, **data}
        return {"id": self.id, "state": self.state, "message": self.message, "step": 0, "steps": 0,
                "step_label": "", "error": self.message if self.state == "failed" else "", "waiting": None}


class FleetRun:
    def __init__(self, kind: str, name: str, ids: list[int], dry_run: bool):
        self.id = ""
        self.kind, self.name, self.dry_run = kind, name, dry_run
        self.entries: dict[int, _Entry] = {i: _Entry(i) for i in ids}
        self.created = time.time()
        self.task: Task | None = None

    def status(self) -> dict:
        devices = {str(i): e.snapshot() for i, e in self.entries.items()}
        counts: dict[str, int] = {}
        for snap in devices.values():
            counts[snap["state"]] = counts.get(snap["state"], 0) + 1
        return {"id": self.id, "kind": self.kind, "name": self.name, "dry_run": self.dry_run,
                "created": self.created, "state": self.task.state if self.task else "queued",
                "counts": counts, "devices": devices,
                "waiting": [int(i) for i, d in devices.items() if d.get("waiting")]}


class Runner:
    def __init__(self, config: Config, paths: Paths, mgr: Any, tasks: TaskManager, *, store: AutomationStore,
                 values: Values, profiles: Profiles, library: FlowLibrary, memory: FixMemory,
                 device_factory: Callable[[int], Any] | None = None,
                 model_factory: Callable[[], llm_mod.LlmClient | None] | None = None,
                 log: Callable[[str], None] | None = None):
        self.config, self.paths, self.mgr, self.tasks = config, paths, mgr, tasks
        self.store, self.values, self.profiles, self.library, self.memory = store, values, profiles, library, memory
        self._device_factory = device_factory or self.default_device
        self._model_factory = model_factory or (lambda: llm_mod.make_client(self.config.automation.llm, self.paths))
        self._log = log
        self._runs: collections.OrderedDict[str, FleetRun] = collections.OrderedDict()
        self._lock = threading.Lock()

    def adopt(self, previous: Runner) -> None:
        """Take over the runs of the runner this one replaces (after a config change),
        so they stay visible and steerable."""
        with self._lock, previous._lock:
            self._runs = previous._runs
        if previous._device_factory != previous.default_device:
            self._device_factory = previous._device_factory      # an injected factory stays injected

    def device(self, instance_id: int) -> Any:
        """The device object for an instance (the seam tests replace)."""
        return self._device_factory(instance_id)

    # -- devices -----------------------------------------------------------
    def default_device(self, instance_id: int) -> Device:
        """The automation view of one instance, with the configured hierarchy reader."""
        adb = self.mgr.adb(instance_id)
        if adb.state() != "device" and not adb.connect(attempts=2, delay=0.5):
            raise UiError(f"Android {instance_id} does not answer over ADB ({adb.serial}); start it first")
        auto = self.config.automation
        return Device(adb, reader=make_reader(adb, auto.hierarchy_reader), read_timeout=auto.read_timeout,
                      instance_id=instance_id)

    def busy(self) -> dict[int, str]:
        """Devices an unfinished run is working on -> that run's id."""
        out: dict[int, str] = {}
        with self._lock:
            for run in self._runs.values():
                if run.task is not None and run.task.active:
                    for entry in run.entries.values():
                        if not entry.finished:
                            out[entry.id] = run.id
        return out

    # -- starting ----------------------------------------------------------
    def run(self, kind: str, name: str, selector: str, *, variables: dict | None = None,
            dry_run: bool = False) -> Task:
        """Start a program or a flow on every instance the selector names.

        Everything that can be wrong before a device is touched - an unknown name, a
        flow that does not parse, an untaught role, a selector matching nothing - is
        raised here, at once, rather than reported per device later.
        """
        if kind not in ("program", "flow"):
            raise ValueError("kind must be `program` or `flow`")
        clean: dict[str, str] = {}
        for key, value in (variables or {}).items():
            clean[check_name(str(key))] = str(value)
        program: Program | None = None
        flow: Flow | None = None
        if kind == "program":
            program = Program.load(self.store.programs_dir, check_slug(name))
            if program is None:
                raise KeyError(f"no trained program {name!r}; train its dataset first")
            if not program.steps:
                raise ValueError(f"the program {name!r} has no steps; record the goal again and retrain")
        else:
            flow = self.library.check(name, self.profiles)
        ids = [int(i) for i in self.mgr.select(selector)]
        if not ids:
            raise ValueError(f"the selector {selector!r} matches no instance")

        fleet = FleetRun(kind, name, ids, dry_run)
        taken = self.busy()
        for instance_id, entry in fleet.entries.items():
            if instance_id in taken:
                entry.state, entry.finished = "failed", True
                entry.message = f"already busy in automation run {taken[instance_id]}; cancel that run or wait for it"
        title = f"{'practice ' if dry_run else ''}{kind} {name} on {len(ids)} device(s)"

        def work(task: Task) -> dict:
            return self._work(task, fleet, program, flow, clean)

        with self._lock:
            task = self.tasks.submit(KIND, title, work, targets=ids)
            fleet.id, fleet.task = task.id, task
            self._runs[task.id] = fleet
            while len(self._runs) > KEEP_RUNS:
                oldest = next(iter(self._runs))
                if self._runs[oldest].task is not None and self._runs[oldest].task.active:  # type: ignore[union-attr]
                    break
                self._runs.popitem(last=False)
        return task

    # -- the task body -----------------------------------------------------
    def _work(self, task: Task, fleet: FleetRun, program: Program | None, flow: Flow | None,
              variables: dict[str, str]) -> dict:
        slots = threading.Semaphore(self.config.automation.max_parallel)
        model = None
        if program is not None and not fleet.dry_run:
            try:
                model = self._model_factory()
            except ValueError as e:
                task.step(f"running without a model: {e}")
        todo = [e for e in fleet.entries.values() if not e.finished]
        for entry in fleet.entries.values():
            if entry.finished:
                task.set_result(entry.id, False, entry.message)
        total = len(fleet.entries)
        task.step(f"0/{total}", current=total - len(todo), total=total)
        threads = [threading.Thread(target=self._device, name=f"sf-auto-{task.id}-{e.id}", daemon=True,
                                    args=(task, fleet, e, slots, program, flow, variables, model)) for e in todo]
        for thread in threads:
            thread.start()
        while any(t.is_alive() for t in threads):
            if task.cancelled:
                for entry in todo:
                    entry.cancel.set()
            finished = sum(1 for e in fleet.entries.values() if e.finished)
            waiting = sum(1 for e in todo if e.session is not None and e.session.waiting)
            task.step(f"{finished}/{total}" + (f", {waiting} waiting for you" if waiting else ""), current=finished)
            time.sleep(0.2)
        states = {e.id: e.snapshot()["state"] for e in fleet.entries.values()}
        ok = sum(1 for s in states.values() if s == "done")
        task.step(f"{ok} of {total} finished", current=total)
        return {"ok": ok, "failed": total - ok, "devices": {str(k): v for k, v in states.items()}}

    def _take_slot(self, entry: _Entry, slots: threading.Semaphore) -> bool:
        while not slots.acquire(timeout=0.2):
            if entry.cancel.is_set():
                return False
        entry.holding = True
        return True

    def _device(self, task: Task, fleet: FleetRun, entry: _Entry, slots: threading.Semaphore,
                program: Program | None, flow: Flow | None, variables: dict[str, str],
                model: llm_mod.LlmClient | None) -> None:
        def attention(waiting: bool) -> None:
            # A device waiting for a person does no work: lend its slot to a queued one.
            if waiting and entry.holding:
                entry.holding = False
                slots.release()
            elif not waiting and not entry.holding:
                self._take_slot(entry, slots)

        try:
            if not self._take_slot(entry, slots) or entry.cancel.is_set():
                entry.state, entry.message = "cancelled", "cancelled before it started"
                return
            entry.state = "running"
            device = self.device(entry.id)
            resolver = Resolver(self.values, instance_id=entry.id, serial=getattr(device, "serial", ""),
                                variables=variables, run_id=task.id, consume=not fleet.dry_run)
            common: dict[str, Any] = {"resolver": resolver, "label": f"#{entry.id}", "dry_run": fleet.dry_run,
                                      "cancel": entry.cancel, "log": self._log, "on_attention": attention}
            session: Session
            if program is not None:
                profile = self.profiles.get(program.package)
                session = ProgramRun(program, device, self.config.automation, memory=self.memory, model=model,
                                     overlays=profile.resolved_overlays() if profile else (),
                                     never_tap=profile.resolved_never_tap() if profile else (), **common)
            else:
                assert flow is not None
                artifacts: Path | None = self.store.run_dir(task.id, entry.id)
                session = FlowRun(flow, device, self.config.automation, library=self.library,
                                  profiles=self.profiles, artifacts=artifacts, **common)
            entry.session = session
            session.run()  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001 - one device's failure is that device's result
            plain = isinstance(e, (UiError, ValueError, KeyError))       # written for people already
            problem = str(e).strip("'\"") if plain else f"{type(e).__name__}: {e}"
            if entry.session is None:
                entry.state, entry.message = "failed", problem
            else:
                entry.session.state, entry.session.error = "failed", problem
        finally:
            if entry.holding:
                entry.holding = False
                slots.release()
            entry.finished = True
            snap = entry.snapshot()
            detail = snap.get("error") or snap.get("message") or snap["state"]
            task.set_result(entry.id, snap["state"] == "done", f"{snap['state']}: {detail}" if detail != snap["state"] else detail)

    # -- watching and steering ---------------------------------------------
    def status(self, task_id: str) -> dict | None:
        with self._lock:
            run = self._runs.get(task_id)
        return run.status() if run else None

    def runs(self, active_only: bool = False) -> list[dict]:
        with self._lock:
            runs = list(self._runs.values())
        out = [r.status() for r in reversed(runs) if not active_only or (r.task is not None and r.task.active)]
        return out

    def cancel(self, task_id: str, instance_id: int | None = None) -> bool:
        """Cancel a whole run, or one device of it (the others carry on)."""
        with self._lock:
            run = self._runs.get(task_id)
        if run is None:
            return False
        if instance_id is None:
            for entry in run.entries.values():
                entry.cancel.set()
            return self.tasks.cancel(task_id) or True
        entry = run.entries.get(int(instance_id))
        if entry is None or entry.finished:
            return False
        entry.cancel.set()
        return True

    def resolve(self, task_id: str, instance_id: int, action: str = "resume") -> bool:
        """Answer a device that needs attention: resume | skip | abort."""
        with self._lock:
            run = self._runs.get(task_id)
        entry = run.entries.get(int(instance_id)) if run else None
        if entry is None or entry.session is None:
            return False
        return entry.session.resolve_attention(action)

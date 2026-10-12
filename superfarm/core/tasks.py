"""Background work with progress: one Task per bulk operation.

Anything that touches more than one Android, or takes more than a moment, runs as a
Task so the panel and CLI can show progress, per-device outcomes and cancel it. The
central rule of fleet operations lives in run_on_instances(): every device is
independent, and one that fails or stalls never holds up the others.

One operator request per Android at a time. Work that changes what an Android is doing
(start, stop, restart, recreate, delete, a manual backup, a restore over it) is submitted
`exclusive`. While such a task has not finished with an Android, another exclusive request
that names it is refused as a whole with TaskBusy, which names the task; it is neither
queued nor joined to the first. A double click therefore gives one task and one refusal
pointing at it, never the same work twice. An Android the task has already finished with
is free again while the task goes on with the others. busy_targets() is the one place
that answers "who is busy with what".
"""
from __future__ import annotations

import itertools
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal
from collections.abc import Callable

TaskState = Literal["queued", "running", "done", "failed", "cancelled"]


class Cancelled(Exception):
    pass


class TaskBusy(RuntimeError):
    """Refused: an active task is still working on some of the same Androids. `task` is
    that task and `ids` are the Androids both name."""

    def __init__(self, task: Task, ids: list[int]):
        ids = sorted(ids)
        if len(ids) == 1:
            which, it = f"Android {ids[0]} is", "it"
        else:
            which, it = f"Androids {', '.join(str(i) for i in ids[:10])}{', ...' if len(ids) > 10 else ''} are", "them"
        super().__init__(f"{which} busy: task {task.id} ({task.title}) is still working on {it}. "
                         "Wait for it to finish, or cancel it")
        self.task, self.ids = task, ids


@dataclass
class Task:
    id: str
    kind: str
    title: str
    targets: list[int] = field(default_factory=list)
    state: TaskState = "queued"
    current: int = 0
    total: int = 0
    message: str = ""
    error: str = ""
    results: dict[int, dict] = field(default_factory=dict)   # instance id -> {"ok": bool, "detail": str}
    output: Any = None
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    exclusive: bool = False        # holds its targets against other exclusive tasks (see the module text)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------ used by the work function
    def step(self, message: str | None = None, *, current: int | None = None, total: int | None = None) -> None:
        with self._lock:
            if message is not None:
                self.message = message
            if current is not None:
                self.current = current
            if total is not None:
                self.total = total

    def advance(self, message: str | None = None) -> None:
        with self._lock:
            self.current += 1
            if message is not None:
                self.message = message

    def set_result(self, instance_id: int, ok: bool, detail: str = "") -> None:
        with self._lock:
            self.results[int(instance_id)] = {"ok": bool(ok), "detail": str(detail)[:500]}

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def sleep(self, seconds: float) -> None:
        """Sleep that wakes at once when the task is cancelled."""
        if self._cancel.wait(seconds):
            raise Cancelled()

    # ------------------------------------------------------------- for display
    def to_dict(self) -> dict:
        with self._lock:
            failed = sum(1 for r in self.results.values() if not r["ok"])
            return {"id": self.id, "kind": self.kind, "title": self.title, "targets": list(self.targets),
                    "state": self.state, "current": self.current, "total": self.total, "message": self.message,
                    "error": self.error, "results": {str(k): v for k, v in self.results.items()},
                    "failed": failed, "output": self.output, "created": self.created,
                    "started": self.started, "finished": self.finished}

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")


class TaskManager:
    def __init__(self, workers: int = 8, keep: int = 200):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sf-task")
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._keep = keep

    def submit(self, kind: str, title: str, fn: Callable[[Task], Any], *, targets: list[int] | None = None,
               exclusive: bool = False) -> Task:
        """Queue work. With `exclusive`, TaskBusy is raised (and nothing is queued) when an
        active exclusive task is still working on one of `targets`; checking and queueing
        are one step, so of two requests at the same moment exactly one gets through."""
        with self._lock:
            if exclusive:
                busy = self._busy()
                held = [i for i in targets or [] if i in busy]
                if held:
                    raise TaskBusy(busy[held[0]], [i for i in held if busy[i] is busy[held[0]]])
            task = Task(id=f"t{next(self._ids)}", kind=kind, title=title, targets=list(targets or []),
                        exclusive=exclusive)
            self._tasks[task.id] = task
            self._order.append(task.id)
            for old in self._order[:-self._keep]:
                if not self._tasks[old].active:
                    self._tasks.pop(old, None)
            self._order = [i for i in self._order if i in self._tasks]
        self._pool.submit(self._run, task, fn)
        return task

    def _run(self, task: Task, fn: Callable[[Task], Any]) -> None:
        task.state, task.started = "running", time.time()
        try:
            task.output = fn(task)
            with task._lock:        # with cancel(): a cancel that was accepted is never followed by "done"
                task.state = "cancelled" if task.cancelled else "done"
        except Cancelled:
            task.state = "cancelled"
        except Exception as e:  # noqa: BLE001 - a task must never take the pool thread down
            task.state, task.error = "failed", f"{type(e).__name__}: {e}"
            task.message = traceback.format_exc(limit=3)[-600:]
        finally:
            task.finished = time.time()

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list(self, limit: int = 50, active_only: bool = False) -> list[Task]:
        with self._lock:
            tasks = [self._tasks[i] for i in reversed(self._order)]
        if active_only:
            tasks = [t for t in tasks if t.active]
        return tasks[:limit]

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        with task._lock:
            if not task.active:
                return False
            task._cancel.set()
            return True

    def busy_targets(self) -> dict[int, Task]:
        """Androids an active exclusive task has not finished with yet, each with that task
        (to refuse conflicting operations and to say who holds what)."""
        with self._lock:
            return self._busy()

    def _busy(self) -> dict[int, Task]:
        out: dict[int, Task] = {}
        for task_id in self._order:
            task = self._tasks[task_id]
            if task.exclusive and task.active:
                with task._lock:
                    done = set(task.results)
                out.update((i, task) for i in task.targets if i not in done)
        return out

    def wait(self, task: Task, timeout: float | None = None) -> Task:
        deadline = None if timeout is None else time.monotonic() + timeout
        while task.active and (deadline is None or time.monotonic() < deadline):
            time.sleep(0.05)
        return task

    def shutdown(self) -> None:
        for t in self.list(limit=10_000, active_only=True):
            t._cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)


def run_on_instances(manager: TaskManager, kind: str, title: str, ids: list[int],
                     fn: Callable[[int, Task], str | None], *, concurrency: int = 4,
                     stagger: float = 0.0, exclusive: bool = False) -> Task:
    """Run fn(instance_id, task) for every id, `concurrency` at a time.

    fn returns a short detail string on success and raises on failure. Each instance's
    outcome is recorded on its own; the task as a whole only fails if fn could not be
    scheduled at all. Cancelling stops instances that have not started yet. `exclusive`
    as for TaskManager.submit().
    """
    ids = [int(i) for i in ids]

    def work(task: Task) -> dict:
        task.step(f"0/{len(ids)}", current=0, total=len(ids))

        def one(instance_id: int) -> None:
            if task.cancelled:
                task.set_result(instance_id, False, "cancelled before it started")
                return
            try:
                detail = fn(instance_id, task)
                task.set_result(instance_id, True, detail or "ok")
            except Cancelled:
                task.set_result(instance_id, False, "cancelled")
            except Exception as e:  # noqa: BLE001 - one device's failure is a result, not a crash
                task.set_result(instance_id, False, f"{type(e).__name__}: {e}")
            finally:
                task.advance()
                task.step(f"{task.current}/{len(ids)}")

        with ThreadPoolExecutor(max_workers=max(1, concurrency), thread_name_prefix=f"sf-{kind}") as pool:
            for n, instance_id in enumerate(ids):
                if n and stagger:
                    time.sleep(stagger)
                pool.submit(one, instance_id)
        ok = sum(1 for r in task.results.values() if r["ok"])
        task.step(f"{ok} of {len(ids)} succeeded")
        return {"ok": ok, "failed": len(ids) - ok}

    return manager.submit(kind, title, work, targets=ids, exclusive=exclusive)

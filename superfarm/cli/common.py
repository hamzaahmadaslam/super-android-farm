"""What every command shares: the farm it works on, the daemon client, task following.

A command is a function `run(ctx) -> int` (the exit code). `Ctx` builds what the command
asks for and nothing more, so `sf version` never reads a config file and `sf doctor`
never opens a socket.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from functools import cached_property
from typing import Any

import yaml
from pydantic import ValidationError

from ..config import Config, load_config
from ..core.models import InstanceSpec, check_id, parse_selector
from ..paths import Paths, get_paths
from .client import Api, ApiError, CliError
from .render import Output, duration

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130
ACTIVE = ("queued", "running")


def explain_validation(error: ValidationError) -> str:
    """Pydantic errors as `key: problem` lines (its default text carries URLs and input dumps)."""
    lines = []
    for item in error.errors():
        where = ".".join(str(p) for p in item.get("loc", ()))
        lines.append(f"{where}: {item.get('msg', 'invalid')}" if where else str(item.get("msg", "invalid")))
    return "; ".join(lines) or "invalid"


class Ctx:
    def __init__(self, args: Any, out: Output):
        self.args = args
        self.out = out

    def sleep(self, seconds: float) -> None:
        """Every wait of the CLI goes through here (poll intervals, watch refresh)."""
        time.sleep(seconds)

    @cached_property
    def paths(self) -> Paths:
        home = getattr(self.args, "home", None)
        if not home:
            # Under sudo `~` is root's home; follow the invoking user's farm instead of
            # silently working on an empty one in /root.
            from ..host.setup import sudo_home
            home = sudo_home()
        return get_paths(home)

    @cached_property
    def config(self) -> Config:
        try:
            return load_config(self.paths)
        except ValidationError as e:
            raise CliError(f"{self.paths.config} is not valid: {explain_validation(e)}") from None
        except (OSError, ValueError, yaml.YAMLError) as e:
            raise CliError(f"cannot read {self.paths.config}: {' '.join(str(e).split())}") from None

    @cached_property
    def api(self) -> Api:
        return Api.from_farm(self.config, self.paths)

    @property
    def wait(self) -> bool:
        """Follow tasks to the end? Explicit flag first; otherwise only for a person at a terminal."""
        chosen = getattr(self.args, "wait", None)
        return self.out.tty if chosen is None else bool(chosen)

    # ------------------------------------------------------------ selectors
    def instances(self) -> list[dict]:
        return (self.api.get("/api/instances") or {}).get("instances") or []

    def select(self, selector: str, *, rows: list[dict] | None = None) -> list[int]:
        """Resolve a selector on this side, with the same parser the daemon uses. Needed
        where the API works on one Android at a time (edit, delete, status)."""
        rows = self.instances() if rows is None else rows
        known = {int(r["id"]): InstanceSpec(id=int(r["id"]), tags=list(r.get("tags") or [])) for r in rows}
        running = {int(r["id"]) for r in rows
                   if ((r.get("containers") or {}).get("android") or {}).get("state") == "running"}
        try:
            ids = parse_selector(selector, known, running)
        except ValueError as e:
            raise CliError(str(e), EXIT_USAGE) from None
        if not ids:
            raise CliError(f"selector {selector!r} matches no Android (see: sf ls)")
        return ids

    # ------------------------------------------------------------ results
    def finish(self, response: Any, *, done: str = "") -> int:
        """Handle what a mutating call returned: follow its task, or show the result."""
        task = response.get("task") if isinstance(response, dict) else None
        if isinstance(task, dict) and "id" in task:
            if self.wait:
                return follow(self, task)
            if self.out.json_mode:
                self.out.json(response)
            else:
                self.out.print(str(task["id"]))
                self.out.note(f"task {task['id']} started: {task.get('title', '')}  (follow it with: sf tasks {task['id']} --wait)")
            return EXIT_OK
        if self.out.json_mode:
            self.out.json(response)
        elif done:
            self.out.note(done)
        return EXIT_OK


def one_id(value: str) -> int:
    try:
        return check_id(value)
    except ValueError as e:
        raise CliError(str(e), EXIT_USAGE) from None


def confirm(ctx: Ctx, question: str) -> bool:
    """Ask before something that cannot be undone. Without a terminal there is nobody to
    ask, and guessing "yes" is how data gets lost: refuse and name the flag instead."""
    if getattr(ctx.args, "yes", False):
        return True
    import sys
    if not sys.stdin.isatty():
        raise CliError("refusing to continue without confirmation: repeat with --yes", EXIT_USAGE)
    ctx.out.clear_status()
    ctx.out.stderr.write(f"{question} [y/N] ")
    ctx.out.stderr.flush()
    try:
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        ctx.out.stderr.write("\n")
        return False
    return answer.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------- task following

def progress_line(task: dict, now: float | None = None) -> str:
    current, total = int(task.get("current") or 0), int(task.get("total") or 0)
    started = task.get("started") or task.get("created") or 0
    elapsed = duration((now if now is not None else time.time()) - started) if started else ""
    head = str(task.get("title") or task.get("kind") or "task")
    if total > 0:
        filled = min(10, max(0, round(10 * current / total)))
        head = f"[{'#' * filled}{'.' * (10 - filled)}] {current}/{total}  {head}"
    else:
        message = " ".join(str(task.get("message") or "").split())
        if message:
            head = f"{head}: {message}"
    return f"{head}  {elapsed}".rstrip()


class Interrupted(CliError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_INTERRUPTED)


def wait_for(ctx: Ctx, task: dict) -> dict:
    """Poll a task until it has ended, showing one updating progress line.

    Ctrl-C asks the daemon to cancel the task (work already running on a device finishes)
    and ends the command with exit code 130."""
    out, task_id = ctx.out, str(task["id"])
    delay = 0.2
    try:
        while task.get("state") in ACTIVE:
            out.status(progress_line(task))
            ctx.sleep(delay)
            delay = min(1.0, delay * 1.5)
            task = ctx.api.get(f"/api/tasks/{task_id}")["task"]
    except KeyboardInterrupt:
        out.clear_status()
        try:
            ctx.api.post(f"/api/tasks/{task_id}/cancel")
        except CliError as e:
            raise Interrupted(f"interrupted, and task {task_id} could not be cancelled: {e}") from None
        raise Interrupted(f"interrupted: asked the daemon to cancel task {task_id}") from None
    out.clear_status()
    return task


def follow(ctx: Ctx, task: dict) -> int:
    """Follow a task to its end, then report per-device results. Exit code 1 when the task
    failed, was cancelled, or any device failed."""
    return report(ctx, wait_for(ctx, task))


def task_failed(task: dict) -> bool:
    results = task.get("results") or {}
    return task.get("state") != "done" or any(not (r or {}).get("ok") for r in results.values())


def results_table(ctx: Ctx, results: dict) -> int:
    """Per-device outcomes (`{id: {ok, detail}}`) as a table. Returns how many failed."""
    def order(key: Any) -> tuple[int, str]:
        return (int(key), "") if str(key).isdigit() else (10**9, str(key))
    rows = [(key, "ok" if (results[key] or {}).get("ok") else "failed", (results[key] or {}).get("detail", ""))
            for key in sorted(results, key=order)]
    ctx.out.table(("ANDROID", "RESULT", "DETAIL"), rows, right=(0,), state_columns=(1,))
    failed = sum(1 for r in rows if r[1] != "ok")
    ctx.out.print(f"{len(rows) - failed} ok, {failed} failed")
    return failed


def report_results(ctx: Ctx, response: dict) -> int:
    """For routes that answer at once with `{"ok", "results": {id: {ok, detail}}}`."""
    results = (response or {}).get("results") or {}
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_FAILED if any(not (r or {}).get("ok") for r in results.values()) else EXIT_OK
    if not results:
        ctx.out.note("no Android was affected")
        return EXIT_OK
    return EXIT_FAILED if results_table(ctx, results) else EXIT_OK


def report(ctx: Ctx, task: dict) -> int:
    out = ctx.out
    code = EXIT_FAILED if task_failed(task) else EXIT_OK
    if out.json_mode:
        out.json({"task": task})        # the shape of GET /api/tasks/{id}
        return code
    results = task.get("results") or {}
    if results:
        results_table(ctx, results)
    elif task.get("state") == "done":
        output = task.get("output")
        shown = output if isinstance(output, (str, int, float)) and not isinstance(output, bool) else task.get("message")
        if shown not in (None, ""):
            out.print(str(shown))
    state = task.get("state")
    if state == "failed":
        out.error(f"task {task.get('id')} failed: {task.get('error') or 'unknown error'}")
    elif state == "cancelled":
        out.error(f"task {task.get('id')} was cancelled")
    return code


def explain_capacity(error: ApiError) -> list[str]:
    """Lines for an over-capacity refusal: what the estimate is, what binds, how to override.

    The API sends `{"error": {"code": "over_capacity", "message", "capacity": {...}}}` where
    capacity is InstanceManager.capacity()."""
    body = error.payload.get("error") if isinstance(error.payload, dict) else None
    capacity = body.get("capacity") if isinstance(body, dict) else None
    lines = [f"over capacity: {error.message}"]
    if isinstance(capacity, dict):
        estimate = capacity.get("estimate") if isinstance(capacity.get("estimate"), dict) else {}
        parts = []
        if "running" in capacity:
            parts.append(f"{capacity['running']} running")
        if "max_instances" in estimate:
            limit = f"about {estimate['max_instances']} estimated to fit"
            if estimate.get("binding"):
                limit += f" (limited by {str(estimate['binding']).upper()})"
            parts.append(limit)
        if "memory_available_mb" in capacity:
            parts.append(f"{capacity['memory_available_mb']} MB RAM available")
        if parts:
            lines.append("    " + ", ".join(parts))
        for reason in capacity.get("reasons") or []:
            if str(reason) not in error.message:        # the API's message usually quotes it already
                lines.append(f"    {reason}")
        advice = capacity.get("advice") or []
        if advice:
            lines.append("    What would raise the limit:")
            lines += [f"    - {line}" for line in advice[:3]]
    lines.append("The estimate is advisory. To start anyway, repeat the command with --force.")
    return lines


def each(ctx: Ctx, ids: list[int], call: Callable[[int], Any], *, verb: str,
         detail: Callable[[Any], str] = lambda response: "ok") -> int:
    """Run one API call per Android, for the routes that take a single id. Like a task:
    every Android has its own outcome and one refusal never stops the rest."""
    results: list[tuple[int, Any, ApiError | None]] = []
    for n, instance_id in enumerate(ids, 1):
        ctx.out.status(f"{verb} {n}/{len(ids)}: Android {instance_id}")
        try:
            results.append((instance_id, call(instance_id), None))
        except ApiError as e:
            results.append((instance_id, None, e))
    ctx.out.clear_status()
    failed = sum(1 for _, _, error in results if error is not None)
    if ctx.out.json_mode:
        ctx.out.json([{"id": i, "ok": error is None,
                       "response": response if error is None else error.payload} for i, response, error in results])
    else:
        ctx.out.table(("ANDROID", "RESULT", "DETAIL"),
                      [(i, "ok" if error is None else "failed", detail(response) if error is None else error.message)
                       for i, response, error in results], right=(0,), state_columns=(1,))
        ctx.out.print(f"{len(results) - failed} ok, {failed} failed")
    return EXIT_FAILED if failed else EXIT_OK

"""Automation from the terminal: datasets, training, programs, flows, runs, values, schedules.

Recording itself happens in the panel (it needs the live picture to click on); everything
that follows a recording can be driven from here.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any

from ..client import CliError
from ..common import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Ctx, one_id
from ..render import duration, when

API = "/api/automation"


def _listing(ctx: Ctx, path: str, key: str, headers: tuple[str, ...], row: Any, *, empty: str,
             query: dict[str, Any] | None = None, **table: Any) -> int:
    response = ctx.api.get(path, **(query or {}))
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    ctx.out.table(headers, [row(item) for item in response.get(key) or []], empty=empty, **table)
    return EXIT_OK


# ------------------------------------------------------------------ datasets / programs

def datasets(ctx: Ctx) -> int:
    return _listing(ctx, f"{API}/datasets", "datasets", ("DATASET", "NAME", "APP", "RECORDINGS", "STEPS", "TRAINED"),
                    lambda d: (d.get("slug"), d.get("name"), d.get("package") or "-", d.get("recordings", 0),
                               d.get("steps", 0), bool(d.get("trained"))),
                    right=(3, 4), empty="No datasets yet. Record one in the panel (Automation > Record).")


def train(ctx: Ctx) -> int:
    code = ctx.finish(ctx.api.post(f"{API}/datasets/{ctx.args.dataset}/train"))
    if code == EXIT_OK and ctx.wait:
        ctx.out.note(f"run it with: sf auto run program {ctx.args.dataset} <selector>")
    return code


def programs(ctx: Ctx) -> int:
    return _listing(ctx, f"{API}/programs", "programs", ("PROGRAM", "NAME", "APP", "STEPS", "FROM", "VALUES NEEDED"),
                    lambda p: (p.get("slug"), p.get("name"), p.get("package") or "-", p.get("steps", 0),
                               f"{p.get('from_recordings', 0)} recordings", p.get("dynamic") or "-"),
                    right=(3,), empty="No trained programs. Train one with: sf auto train <dataset>")


# ------------------------------------------------------------------ flows

def flows_ls(ctx: Ctx) -> int:
    return _listing(ctx, f"{API}/flows", "flows", ("FLOW", "APP", "STEPS", "PROBLEM", "WHAT IT DOES"),
                    lambda f: (f.get("name"), f.get("package") or "-", f.get("steps", "-"), f.get("error") or "",
                               f.get("description") or ""),
                    right=(2,), empty="No flows.")


def flows_show(ctx: Ctx) -> int:
    response = ctx.api.get(f"{API}/flows/{ctx.args.name}")
    if ctx.out.json_mode:
        ctx.out.json(response)
    else:
        ctx.out.stdout.write(str(response.get("text", "")))
        if response.get("error"):
            ctx.out.error(f"this flow does not validate: {response['error']}")
            return EXIT_FAILED
    return EXIT_OK


def _read_text(name: str) -> str:
    if name == "-":
        return sys.stdin.read()
    try:
        return Path(name).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise CliError(f"cannot read {name}: {getattr(e, 'strerror', None) or e}", EXIT_USAGE) from None


def flows_validate(ctx: Ctx) -> int:
    response = ctx.api.post(f"{API}/flows/validate", {"text": _read_text(ctx.args.file)})
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    flow = response.get("flow") or {}
    ctx.out.print(f"valid: {flow.get('name', '?')}, {flow.get('steps', 0)} steps")
    for line in flow.get("outline") or []:
        ctx.out.print(f"  {line}")
    return EXIT_OK


def flows_save(ctx: Ctx) -> int:
    response = ctx.api.put(f"{API}/flows/{ctx.args.name}", {"text": _read_text(ctx.args.file)})
    return ctx.finish(response, done=f"saved flow {ctx.args.name}. Run it with: sf auto run flow {ctx.args.name} <selector>")


def flows_rm(ctx: Ctx) -> int:
    return ctx.finish(ctx.api.delete(f"{API}/flows/{ctx.args.name}"), done=f"deleted flow {ctx.args.name}")


# ------------------------------------------------------------------ runs

def _secret(ctx: Ctx, name: str) -> str:
    """A run value that must not appear in the process list or in shell history."""
    if sys.stdin.isatty():
        value = getpass.getpass(f"value for {name} (not shown): ", stream=ctx.out.stderr)
    else:
        value = sys.stdin.readline().rstrip("\r\n")
    if not value:
        raise CliError(f"empty value for {name}", EXIT_USAGE)
    return value


def run_variables(ctx: Ctx) -> dict[str, str]:
    variables: dict[str, str] = {}
    for item in getattr(ctx.args, "var", None) or []:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise CliError(f"--var expects name=value, got {item!r}", EXIT_USAGE)
        variables[name.strip()] = value
    for name in getattr(ctx.args, "secret", None) or []:
        variables[name] = _secret(ctx, name)
    return variables


def run(ctx: Ctx) -> int:
    body = {"kind": ctx.args.kind, "name": ctx.args.name, "selector": ctx.args.selector,
            "variables": run_variables(ctx), "dry_run": ctx.args.dry_run}
    response = ctx.api.post(f"{API}/runs", body)
    task = (response or {}).get("task") or {}
    if ctx.wait and not ctx.out.json_mode:
        ctx.out.note(f"run {task.get('id')} started. A device that meets an unknown screen waits for you:\n"
                     f"    sf auto runs {task.get('id')}        (from another terminal) shows who is waiting\n"
                     f"    sf auto resolve {task.get('id')} <id>  lets it continue")
    return ctx.finish(response)


def runs(ctx: Ctx) -> int:
    task_id = getattr(ctx.args, "task", None)
    if not task_id:
        return _listing(ctx, f"{API}/runs", "runs",
                        ("RUN", "KIND", "NAME", "STATE", "DEVICES", "WAITING", "STARTED"),
                        lambda r: (r.get("id"), r.get("kind") + (" (dry run)" if r.get("dry_run") else ""), r.get("name"),
                                   r.get("state"),
                                   ", ".join(f"{n} {s}" for s, n in sorted((r.get("counts") or {}).items())) or "-",
                                   r.get("waiting") or "-", when(r.get("created"))),
                        query={"active": True} if getattr(ctx.args, "active", False) else None,
                        state_columns=(3,), empty="No runs.")
    response = ctx.api.get(f"{API}/runs/{task_id}")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    status = response.get("run") or {}
    ctx.out.print(f"run {status.get('id')}: {status.get('kind')} {status.get('name')} - {status.get('state')}")
    devices = status.get("devices") or {}
    rows = []
    for key in sorted(devices, key=lambda k: int(k) if str(k).isdigit() else 0):
        d = devices[key] or {}
        waiting = d.get("waiting")
        waiting_text = (waiting.get("reason") or waiting.get("message") or "yes") if isinstance(waiting, dict) \
            else (str(waiting) if waiting else "")
        rows.append((key, d.get("state"), f"{d.get('step', 0)}/{d.get('steps', 0)}", d.get("step_label") or "",
                     duration(d.get("elapsed")) if d.get("elapsed") is not None else "-",
                     waiting_text or d.get("error") or d.get("message") or ""))
    ctx.out.table(("ANDROID", "STATE", "STEP", "DOING", "TIME", "DETAIL"), rows, right=(0,), state_columns=(1,))
    if status.get("waiting"):
        ctx.out.note(f"waiting for you: {', '.join(str(i) for i in status['waiting'])}. Look at the device, then: "
                     f"sf auto resolve {status.get('id')} <id> [resume|skip|abort]")
    return EXIT_OK


def resolve(ctx: Ctx) -> int:
    instance_id = one_id(ctx.args.id)
    response = ctx.api.post(f"{API}/runs/{ctx.args.task}/resolve", {"id": instance_id, "action": ctx.args.action})
    return ctx.finish(response, done=f"Android {instance_id}: {ctx.args.action}")


# ------------------------------------------------------------------ values

def _values_path(args: argparse.Namespace, name: str = "") -> str:
    instance = getattr(args, "instance", None)
    base = f"{API}/values/instances/{one_id(instance)}" if instance is not None else f"{API}/values/shared"
    return f"{base}/{name}" if name else base


def values_ls(ctx: Ctx) -> int:
    instance = getattr(ctx.args, "instance", None)
    if instance is not None:
        response = ctx.api.get(f"{API}/values/instances/{one_id(instance)}")
        shared, pools = response, []
    else:
        response = ctx.api.get(f"{API}/values")
        shared, pools = response.get("shared") or {}, response.get("pools") or []
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    rows = [(name, value) for name, value in sorted((shared.get("values") or {}).items())]
    # Secret-named values are write-only: the API lists their names, never their content.
    rows += [(name, "(hidden)") for name in shared.get("hidden") or []]
    ctx.out.table(("VALUE", "CONTENT"), rows, empty="No values stored." if instance is None
                  else f"No values stored for Android {instance}.")
    if pools:
        ctx.out.print("\nPools (one line is used up per run):")
        ctx.out.table(("POOL", "REMAINING", "USED"), [(p.get("name"), p.get("remaining"), p.get("used")) for p in pools],
                      right=(1, 2))
    return EXIT_OK


def values_set(ctx: Ctx) -> int:
    value = ctx.args.value
    if value is None:
        value = _secret(ctx, ctx.args.name)
    response = ctx.api.put(_values_path(ctx.args, ctx.args.name), {"value": value})
    return ctx.finish(response, done=f"stored {ctx.args.name}")


def values_rm(ctx: Ctx) -> int:
    return ctx.finish(ctx.api.delete(_values_path(ctx.args, ctx.args.name)), done=f"removed {ctx.args.name}")


def values_pool_add(ctx: Ctx) -> int:
    lines = [line.rstrip("\r") for line in _read_text(ctx.args.file).split("\n")]
    lines = [line for line in lines if line.strip()]
    if not lines:
        raise CliError(f"{ctx.args.file}: no lines to add", EXIT_USAGE)
    response = ctx.api.post(f"{API}/values/pools/{ctx.args.name}", {"lines": lines})
    if ctx.out.json_mode:
        ctx.out.json(response)
    else:
        ctx.out.note(f"pool {ctx.args.name}: {response.get('added', 0)} added, {response.get('remaining', 0)} remaining")
    return EXIT_OK


def values_pool_rm(ctx: Ctx) -> int:
    return ctx.finish(ctx.api.delete(f"{API}/values/pools/{ctx.args.name}"), done=f"deleted pool {ctx.args.name}")


# ------------------------------------------------------------------ schedules

def schedule_ls(ctx: Ctx) -> int:
    return _listing(ctx, f"{API}/schedules", "schedules",
                    ("ID", "NAME", "RUNS", "ON", "WHEN", "ENABLED", "NEXT", "LAST RESULT"),
                    lambda s: (s.get("id"), s.get("name"), f"{s.get('kind')} {s.get('target')}", s.get("selector"),
                               s.get("cron") or f"every {duration(s.get('every'))}", bool(s.get("enabled")),
                               when(s.get("next_run")), s.get("last_result") or "-"),
                    empty="No schedules. Add one with: sf auto schedule add --help")


def schedule_add(ctx: Ctx) -> int:
    args = ctx.args
    if bool(args.every) == bool(args.cron):
        raise CliError("give exactly one of --every SECONDS or --cron 'M H DOM MON DOW'", EXIT_USAGE)
    body = {"name": args.name, "kind": args.kind, "target": args.target, "selector": args.selector,
            "every": args.every or 0, "cron": args.cron or "", "variables": run_variables(ctx), "dry_run": args.dry_run}
    response = ctx.api.post(f"{API}/schedules", body)
    if ctx.out.json_mode:
        ctx.out.json(response)
    else:
        created = response.get("schedule") or {}
        ctx.out.print(str(created.get("id")))
        ctx.out.note(f"scheduled; next run {when(created.get('next_run'))}")
    return EXIT_OK


def schedule_rm(ctx: Ctx) -> int:
    return ctx.finish(ctx.api.delete(f"{API}/schedules/{ctx.args.id}"), done=f"deleted schedule {ctx.args.id}")


def schedule_enable(enabled: bool):
    def call(ctx: Ctx) -> int:
        response = ctx.api.patch(f"{API}/schedules/{ctx.args.id}", {"enabled": enabled})
        return ctx.finish(response, done=f"schedule {ctx.args.id} {'enabled' if enabled else 'disabled'}")
    return call


# ------------------------------------------------------------------ parser

def register(b: Any, sub: argparse._SubParsersAction) -> None:
    auto = b.group(sub, "auto", "automation: train recordings into programs and run programs or flows",
                   description="Automation. Record a task a few times in the panel, train the recordings into a "
                               "program (sf auto train), then run it on any selection of Androids (sf auto run). "
                               "Flows are hand-written YAML scripts run the same way.")
    b.add(auto, "datasets", datasets, "list datasets (groups of recordings of one task)", "sf auto datasets")
    p = b.add(auto, "train", train, "turn a dataset's recordings into one program", "sf auto train open-settings")
    p.add_argument("dataset", help="dataset id as shown by `sf auto datasets`")
    b.add(auto, "programs", programs, "list trained programs", "sf auto programs")

    flows = b.group(auto, "flows", "hand-written YAML flows (bare: list them)", default=flows_ls)
    b.add(flows, "ls", flows_ls, "list flows, including the shipped examples", "sf auto flows ls")
    p = b.add(flows, "show", flows_show, "print a flow's YAML", "sf auto flows show examples/open-app")
    p.add_argument("name")
    p = b.add(flows, "validate", flows_validate, "check a YAML file without saving it", "sf auto flows validate login.yaml")
    p.add_argument("file", help="YAML file, or - for stdin")
    p = b.add(flows, "save", flows_save, "save a YAML file as a flow (validated first)", "sf auto flows save login login.yaml")
    p.add_argument("name", help="flow name")
    p.add_argument("file", help="YAML file, or - for stdin")
    p = b.add(flows, "rm", flows_rm, "delete a flow", "sf auto flows rm login")
    p.add_argument("name")

    p = b.add(auto, "run", run, "run a program or a flow on Androids",
              "sf auto run flow examples/open-app 1-10 --var package=com.android.settings\n"
              "sf auto run program sign-in tag:eu --var user=demo --secret password",
              description="Run a trained program or a flow on every selected Android, each on its own. A device "
                          "that meets a screen no rule covers stops and waits (see `sf auto runs <task>` and "
                          "`sf auto resolve`); the others carry on.")
    p.add_argument("kind", choices=("program", "flow"))
    p.add_argument("name", help="program or flow name")
    p.add_argument("selector", help="which Androids")
    _run_options(p)

    p = b.add(auto, "runs", runs, "list runs, or show one run device by device", "sf auto runs\nsf auto runs t12")
    p.add_argument("task", nargs="?", help="task id of a run")
    p.add_argument("--active", action="store_true", help="only runs still going")
    p = b.add(auto, "resolve", resolve, "let a device that waits for attention continue, skip the step or stop",
              "sf auto resolve t12 4 resume")
    p.add_argument("task", help="task id of the run")
    p.add_argument("id", help="Android id")
    p.add_argument("action", nargs="?", choices=("resume", "skip", "abort"), default="resume",
                   help="resume: you fixed the screen, carry on (default). skip: skip this step. abort: stop this device")

    vals = b.group(auto, "values", "values that runs type in: shared, per Android, and pools (bare: list them)",
                   default=values_ls)
    p = b.add(vals, "ls", values_ls, "list stored values and pools", "sf auto values ls\nsf auto values ls --instance 3")
    p.add_argument("--instance", metavar="ID", help="the values of one Android instead of the shared ones")
    p = b.add(vals, "set", values_set, "store a value",
              "sf auto values set city Berlin\nsf auto values set invite-code        (asked for, not shown)",
              description="Store a value under a name; flows and programs use it as {name}. Leave the value out "
                          "to be asked for it without echo (or to pipe it in), so it never appears in the "
                          "process list or in shell history. Names that look like secrets (password, pin, "
                          "otp, ...) are refused by the farm: pass those per run with `sf auto run --secret`.")
    p.add_argument("name")
    p.add_argument("value", nargs="?", help="the value; omit to type it hidden or read it from stdin")
    p.add_argument("--instance", metavar="ID", help="store it for one Android only")
    p = b.add(vals, "rm", values_rm, "remove a value", "sf auto values rm city")
    p.add_argument("name")
    p.add_argument("--instance", metavar="ID", help="remove the value of one Android")
    p = b.add(vals, "pool-add", values_pool_add, "add lines to a pool (each line is handed out once)",
              "sf auto values pool-add codes codes.txt")
    p.add_argument("name", help="pool name")
    p.add_argument("file", help="text file with one value per line, or - for stdin")
    p = b.add(vals, "pool-rm", values_pool_rm, "delete a pool", "sf auto values pool-rm codes")
    p.add_argument("name")

    sched = b.group(auto, "schedule", "recurring runs (bare: list them)", default=schedule_ls)
    b.add(sched, "ls", schedule_ls, "list schedules", "sf auto schedule ls")
    p = b.add(sched, "add", schedule_add, "run a program or flow on a timetable",
              "sf auto schedule add nightly flow examples/open-app all --cron '0 3 * * *' --var package=com.android.settings")
    p.add_argument("name", help="a name for the schedule")
    p.add_argument("kind", choices=("program", "flow"))
    p.add_argument("target", help="program or flow name")
    p.add_argument("selector", help="which Androids")
    p.add_argument("--every", type=int, metavar="SECONDS", help="run every so many seconds")
    p.add_argument("--cron", metavar="LINE", help="cron line: minute hour day-of-month month day-of-week")
    _run_options(p, secrets=False)
    p = b.add(sched, "rm", schedule_rm, "delete a schedule", "sf auto schedule rm s3")
    p.add_argument("id")
    p = b.add(sched, "enable", schedule_enable(True), "switch a schedule on", "sf auto schedule enable s3")
    p.add_argument("id")
    p = b.add(sched, "disable", schedule_enable(False), "switch a schedule off without deleting it",
              "sf auto schedule disable s3")
    p.add_argument("id")


def _run_options(p: argparse.ArgumentParser, *, secrets: bool = True) -> None:
    p.add_argument("--var", action="append", metavar="NAME=VALUE", help="a value for this run (repeatable)")
    if secrets:
        p.add_argument("--secret", action="append", metavar="NAME",
                       help="a value for this run that is asked for without echo, or read from stdin (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="find every element but do not tap or type")

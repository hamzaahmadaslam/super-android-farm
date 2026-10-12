"""Commands that work without the daemon: they call the modules directly.

These are the ones needed before a daemon can run at all (doctor, host setup, config) or
to run and reach it (serve, service, token). None of them opens a connection.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pwd
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .. import __version__
from ..config import Config, save_config
from ..resources import resource_path
from ..util import proc
from ..util.files import atomic_write
from . import configdocs, values
from .client import CliError
from .common import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Ctx, explain_validation

USER_UNIT = "superfarm.service"
SIGN_SCRIPT = resource_path("scripts", "host", "sign-module.sh")


# ------------------------------------------------------------------ doctor

def doctor(ctx: Ctx) -> int:
    from ..host import doctor as checks_mod
    checks = checks_mod.run_checks(ctx.config, ctx.paths)
    summary = checks_mod.summarize(checks)
    code = EXIT_FAILED if summary["counts"]["fail"] else EXIT_OK
    if ctx.out.json_mode:
        ctx.out.json({"checks": [dataclasses.asdict(c) for c in checks], "summary": summary})
        return code
    ctx.out.table(("STATUS", "CHECK", "DETAIL"), [(c.status, c.title, c.detail) for c in checks], state_columns=(0,))
    todo = [c for c in checks if c.status in ("fail", "warn")]
    for c in todo:
        # The table may have cut the detail; a problem is always shown in full, with its fix.
        ctx.out.print(f"\n[{c.status}] {c.title}: {c.detail}")
        for line in c.fix.splitlines():
            ctx.out.print(f"    {line}")
    counts = summary["counts"]
    ctx.out.print(f"\n{counts['ok']} ok, {counts['warn']} warnings, {counts['fail']} failed, {counts['skip']} skipped"
                  + ("" if summary["ready"] else "  - this machine is not ready to run Androids yet"))
    return code


# ------------------------------------------------------------------ host

def _show_steps(ctx: Ctx, steps: list[dict]) -> int:
    from ..host import setup
    if ctx.out.json_mode:
        ctx.out.json(steps)
    else:
        ctx.out.print(setup.format_steps(steps))
    return EXIT_FAILED if any(s["status"] == "failed" for s in steps) else EXIT_OK


def host_plan(ctx: Ctx) -> int:
    from ..host import setup
    try:
        steps = setup.apply(ctx.config, ctx.paths, dry_run=True, boot=True if ctx.args.boot else None)
    except ValueError as e:
        raise CliError(str(e)) from None
    code = _show_steps(ctx, steps)
    ctx.out.note("\nNothing was changed. Carry this out with: sf host apply")
    return code


def sudo_argv(ctx: Ctx, *, boot: bool) -> list[str]:
    """The command `sf host apply` hands over to when it is not root.

    sudo starts with a clean environment and root's home, so the farm is named explicitly:
    --home is the invoking user's farm home, --user the account the farm runs as. That is
    the same module the boot unit runs, so there is one root code path, not two."""
    user = os.environ.get("SUPERFARM_USER") or pwd.getpwuid(os.getuid()).pw_name
    argv = ["sudo", "--", sys.executable, "-m", "superfarm.host.setup", "apply",
            "--home", str(ctx.paths.home.expanduser().resolve()), "--user", user]
    if os.environ.get("SUPERFARM_GROUP"):
        argv += ["--group", os.environ["SUPERFARM_GROUP"]]
    if boot:
        argv.append("--boot")
    return argv


def host_apply(ctx: Ctx) -> int:
    from ..host import setup
    if ctx.args.dry_run:
        return host_plan(ctx)
    if os.geteuid() != 0:
        argv = sudo_argv(ctx, boot=ctx.args.boot)
        if not proc.which("sudo"):
            raise CliError("host setup needs root and sudo was not found. Run this as root:\n    "
                           + " ".join(shlex.quote(a) for a in argv[2:]))
        ctx.out.note("host setup needs root; continuing under sudo:\n    " + " ".join(shlex.quote(a) for a in argv))
        ctx.out.stdout.flush()
        os.execvp(argv[0], argv)    # noqa: S606 - fixed argv, no shell; replaces this process
    try:
        steps = setup.apply(ctx.config, ctx.paths, boot=True if ctx.args.boot else None)
    except (PermissionError, ValueError) as e:
        raise CliError(str(e)) from None
    return _show_steps(ctx, steps)


def _gpus() -> list:
    from ..host import gpu
    return gpu.nvidia_vram(gpu.detect_gpus())


def host_capacity(ctx: Ctx) -> int:
    from ..host import capacity
    estimate = capacity.estimate(ctx.config, _gpus(), workload=ctx.args.workload, paths=ctx.paths)
    if ctx.out.json_mode:
        ctx.out.json(estimate)
        return EXIT_OK
    limits = estimate["limits"]
    ctx.out.print(f"About {estimate['max_instances']} Androids fit on this machine in {estimate['mode']} mode "
                  f"({estimate['workload']} workload); {estimate['binding'].upper()} is what runs out first.")
    rows = []
    for name in ("ram", "cpu", "vram"):
        if name not in limits:
            continue
        limit = limits[name]
        basis = {"ram": f"{limit.get('per_android_mb')} MB per Android, {limit.get('total_mb')} MB total, "
                        f"{limit.get('host_reserve_mb')} MB kept for the host",
                 "cpu": f"{limit.get('percent_per_android')}% of one thread per Android, {limit.get('threads')} threads",
                 "vram": f"{limit.get('per_android_mb')} MB per Android, {limit.get('total_mb')} MB total"}[name]
        rows.append((name.upper(), limit["max"], basis))
    ctx.out.print()
    ctx.out.table(("LIMIT", "MAX", "BASIS"), rows, right=(1,))
    more = limits["ram"].get("more_now")
    if more is not None:
        ctx.out.print(f"\nWith the memory free right now, about {more} more would start.")
    if estimate.get("advice"):
        ctx.out.print("\nAdvice:")
        for line in estimate["advice"]:
            ctx.out.print(f"  - {line}")
    ctx.out.note("\nThis is an estimate from measured per-Android figures, not a limit: the farm lets you run more.")
    return EXIT_OK


def gpu_rows(gpus: list[dict]) -> list[tuple]:
    return [(g.get("node"), g.get("vendor"), "discrete" if g.get("discrete") else "integrated", g.get("name"),
             g.get("driver"), f"{g['vram_mb']} MB" if g.get("vram_mb") else "-",
             ",".join(g.get("usable_modes") or []) or "-", g.get("note") or "") for g in gpus]


GPU_HEADERS = ("NODE", "VENDOR", "KIND", "NAME", "DRIVER", "VRAM", "MODES", "NOTE")


def host_gpus(ctx: Ctx) -> int:
    from ..host import gpu
    found = _gpus()
    summary = gpu.summary(ctx.config, ctx.paths, found)
    if ctx.out.json_mode:
        ctx.out.json(summary)
        return EXIT_OK
    ctx.out.table(GPU_HEADERS, gpu_rows(summary["gpus"]),
                  empty="No GPU with a render node was found: Androids will draw on the CPU (software mode).")
    ctx.out.print(f"\nRendering modes usable now: {', '.join(summary['available_modes'])}")
    if summary.get("nvidia_blocker"):
        ctx.out.print(f"NVIDIA is not usable yet: {summary['nvidia_blocker']}")
    return EXIT_OK


def host_sign_module(ctx: Ctx) -> int:
    """Hand over to scripts/host/sign-module.sh. Signing reads a root-only key, so anything
    but a status report goes through sudo."""
    script = resource_path("scripts", "host", "sign-module.sh")
    if not script.is_file():
        raise CliError(f"{script} is missing from this installation")
    passed = list(ctx.args.rest)
    if not passed:
        passed = ["--status"]
    argv = ["bash", str(script), *passed]
    if os.geteuid() != 0 and "--status" not in passed and "--help" not in passed and "-h" not in passed:
        if not proc.which("sudo"):
            raise CliError("signing needs root and sudo was not found. Run this as root:\n    "
                           + " ".join(shlex.quote(a) for a in argv))
        argv = ["sudo", "--", *argv]
    ctx.out.stdout.flush()
    os.execvp(argv[0], argv)        # noqa: S606 - fixed program, arguments passed as a list
    return EXIT_OK                  # pragma: no cover - execvp does not return


# ------------------------------------------------------------------ config

def _plain(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).rstrip("\n")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _dig(data: Any, path: list[str], key: str) -> Any:
    for part in path:
        if not isinstance(data, dict) or part not in data:
            raise CliError(f"{key} is not set", EXIT_FAILED)
        data = data[part]
    return data


def _locate(key: str) -> tuple[list[str], Any, bool]:
    try:
        return values.locate(Config, key)
    except values.KeyValueError as e:
        raise CliError(f"{e}  (all settings: sf config docs)", EXIT_USAGE) from None


def config_get(ctx: Ctx) -> int:
    path, _, _ = _locate(ctx.args.key)
    value = _dig(ctx.config.model_dump(mode="json"), path, ctx.args.key)
    if ctx.out.json_mode:
        ctx.out.json(value)
    else:
        ctx.out.print(_plain(value))
    return EXIT_OK


def _save(ctx: Ctx, data: dict, done: str) -> int:
    try:
        config = Config.model_validate(data)
    except ValidationError as e:
        raise CliError(f"not saved: {explain_validation(e)}", EXIT_USAGE) from None
    ctx.paths.home.mkdir(parents=True, exist_ok=True)
    save_config(config, ctx.paths)
    if ctx.out.json_mode:
        ctx.out.json(config.model_dump(mode="json"))
    ctx.out.note(f"{done}\nSaved to {ctx.paths.config}. A running daemon reads it at its next start "
                 "(sf service stop && sf service start).")
    return EXIT_OK


def config_set(ctx: Ctx) -> int:
    key = ctx.args.key
    path, annotation, optional = _locate(key)
    try:
        value = values.coerce(annotation, ctx.args.value, optional=optional, key=key)
    except values.KeyValueError as e:
        raise CliError(str(e), EXIT_USAGE) from None
    data = values.merge(ctx.config.model_dump(mode="json"), values.nest(path, value))
    return _save(ctx, data, f"{key} = {json.dumps(value, ensure_ascii=False)}")


def config_unset(ctx: Ctx) -> int:
    key = ctx.args.key
    path, _, _ = _locate(key)
    data = ctx.config.model_dump(mode="json")
    defaults: Any = Config().model_dump(mode="json")
    node = data
    for part in path[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise CliError(f"{key} is not set")
        node = node[part]
        defaults = defaults.get(part) if isinstance(defaults, dict) else None
    last = path[-1]
    if not isinstance(node, dict) or last not in node:
        raise CliError(f"{key} is not set")
    if isinstance(defaults, dict) and last in defaults:
        node[last] = defaults[last]
        done = f"{key} is back to its default ({json.dumps(defaults[last], ensure_ascii=False)})"
    else:
        # An entry the defaults do not have (a gateway, an extra property): remove it.
        del node[last]
        done = f"{key} removed"
    return _save(ctx, data, done)


def config_show(ctx: Ctx) -> int:
    data = ctx.config.model_dump(mode="json")
    if ctx.args.changed:
        from ..util.files import read_yaml
        data = read_yaml(ctx.paths.config, {}) or {}
    if ctx.out.json_mode:
        ctx.out.json(data)
    else:
        ctx.out.print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip("\n") if data else "{}")
    return EXIT_OK


def config_path(ctx: Ctx) -> int:
    if ctx.out.json_mode:
        ctx.out.json({"home": str(ctx.paths.home), "config": str(ctx.paths.config),
                      "exists": ctx.paths.config.is_file()})
    else:
        ctx.out.print(str(ctx.paths.config))
    return EXIT_OK


def config_docs(ctx: Ctx) -> int:
    text = configdocs.render()
    if ctx.args.check:
        target = Path(ctx.args.check)
        try:
            current = target.read_text(encoding="utf-8")
        except OSError as e:
            raise CliError(f"cannot read {target}: {e.strerror or e}") from None
        if current != text:
            raise CliError(f"{target} is out of date: regenerate it with  sf config docs -o {target}")
        ctx.out.note(f"{target} is up to date")
        return EXIT_OK
    if ctx.args.output:
        target = Path(ctx.args.output)
        try:
            atomic_write(target, text)
        except OSError as e:
            raise CliError(f"cannot write {target}: {e.strerror or e}") from None
        ctx.out.note(f"wrote {target}")
        return EXIT_OK
    ctx.out.stdout.write(text)
    return EXIT_OK


# ------------------------------------------------------------------ token / serve / version

def token(ctx: Ctx) -> int:
    """Print the API token (creating it on first use). The token is the only thing this
    command writes to stdout, so `$(sf token)` works in scripts."""
    from ..api import auth
    ctx.paths.secrets.mkdir(parents=True, exist_ok=True)
    os.chmod(ctx.paths.secrets, 0o700)
    try:
        if ctx.args.rotate:
            value = auth.rotate_token(ctx.paths)
            ctx.out.note("New token written. The old one stops working at once; a running daemon signs every "
                         "browser out and closes open live views within a few seconds.")
        else:
            value = auth.ensure_token(ctx.paths)
    except OSError as e:
        raise CliError(f"cannot write {auth.token_path(ctx.paths)}: {e.strerror or e}") from None
    ctx.out.note("This token is equivalent to root on this machine: treat it like a root password.")
    if ctx.out.json_mode:
        ctx.out.json({"token": value, "path": str(auth.token_path(ctx.paths))})
    else:
        ctx.out.print(value)
    return EXIT_OK


def serve(ctx: Ctx) -> int:
    from ..api.server import serve as run_server
    result = run_server(home=str(ctx.paths.home), host=ctx.args.host, port=ctx.args.port)
    return int(result or 0)


def version(ctx: Ctx) -> int:
    if ctx.out.json_mode:
        ctx.out.json({"version": __version__, "python": ".".join(str(n) for n in sys.version_info[:3])})
    else:
        ctx.out.print(f"superfarm {__version__}")
    return EXIT_OK


# ------------------------------------------------------------------ service (systemd --user)
# The unit, its template and the reasons for a *user* service live in superfarm.host.service;
# these commands only call it and the user's own `systemctl`.

def _service_error(fn: Any, *args: Any, **kwargs: Any) -> Any:
    from ..host.service import ServiceError
    try:
        return fn(*args, **kwargs)
    except (ServiceError, ValueError) as e:
        raise CliError(str(e)) from None


def _systemctl(*args: str) -> None:
    from ..host import service
    try:
        result = proc.run(["systemctl", "--user", *args, service.UNIT], timeout=60, check=False)
    except proc.CommandError as e:
        raise CliError(f"systemctl is not usable ({e}); without systemd run the daemon with `sf serve` under "
                       "your own supervisor") from None
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise CliError(f"systemctl --user {' '.join(args)} {service.UNIT} failed: "
                       f"{detail[-1] if detail else f'exit {result.code}'}")


def _installed() -> None:
    from ..host import service
    if not (service.unit_dir() / service.UNIT).is_file():
        raise CliError("the service is not installed yet: run  sf service install")


def service_install(ctx: Ctx) -> int:
    from ..host import service
    result = _service_error(service.install, ctx.paths)
    if ctx.out.json_mode:
        ctx.out.json(result)
        return EXIT_OK
    ctx.out.print(result["unit"])
    ctx.out.note(("installed" if result["changed"] else "already up to date")
                 + (" and enabled: it starts with your session" if result["enabled"] else ""))
    for line in result["notes"]:
        ctx.out.note(f"note: {line}")
    ctx.out.note("start it now with:  sf service start")
    return EXIT_OK


def service_start(ctx: Ctx) -> int:
    _installed()
    _systemctl("start")
    ctx.out.note("started (sf service status shows it; sf service logs -f follows its output)")
    return EXIT_OK


def service_stop(ctx: Ctx) -> int:
    _installed()
    _systemctl("stop")
    ctx.out.note("stopped (Androids keep running; only the daemon stopped)")
    return EXIT_OK


def service_status(ctx: Ctx) -> int:
    from ..host import service
    status = _service_error(service.status)
    # Same convention as `systemctl is-active`: 0 only when it runs.
    code = EXIT_OK if status["active"] == "active" else 3
    if ctx.out.json_mode:
        ctx.out.json(status)
        return code
    if not status["installed"]:
        ctx.out.print("not installed (install it with: sf service install)")
        return code
    linger = {True: "yes", False: "no (the service stops at your last logout)", None: "unknown"}[status["linger"]]
    ctx.out.pairs([("service", service.UNIT), ("state", status["active"]), ("at login", status["enabled"]),
                   ("runs without a login", linger),
                   ("desktop session", ", ".join(status["display"]) or "none known to the user manager "
                                                                       "(device windows will not open)"),
                   ("unit", status["unit"])])
    if not status["manager"]:
        ctx.out.note("no systemd user session answers for this login (try from a normal login of this user)")
    return code


def service_logs(ctx: Ctx) -> int:
    from ..host import service
    if not proc.which("journalctl"):
        raise CliError("journalctl was not found; the daemon also writes its log under <home>/logs")
    argv = ["journalctl", "--user", "--unit", service.UNIT, "--no-pager", "--lines", str(ctx.args.lines)]
    if ctx.args.follow:
        argv.append("--follow")
    ctx.out.stdout.flush()
    os.execvp(argv[0], argv)        # noqa: S606 - fixed argv, no shell
    return EXIT_OK                  # pragma: no cover

"""Backups: create, list, verify, restore, prune, and the farm backup."""
from __future__ import annotations

import argparse
from typing import Any

from ..client import CliError
from ..common import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Ctx, confirm, one_id, report, wait_for
from ..render import size, when


def create(ctx: Ctx) -> int:
    return ctx.finish(ctx.api.post("/api/backups", {"selector": ctx.args.selector}))


def list_(ctx: Ctx) -> int:
    response = ctx.api.get("/api/backups")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    wanted = ctx.args.kind
    ctx.out.table(("BACKUP", "OF", "ANDROID", "CREATED", "SIZE", "WHY", "COMPLETE"),
                  [(b.get("name"), "farm settings" if b.get("kind") == "farm" else (b.get("instance_name") or b.get("id")),
                    b.get("android_version") or "-", when(b.get("created")), size(b.get("size")),
                    b.get("reason") or "-", bool(b.get("present")))
                   for b in response.get("backups") or [] if wanted in ("all", b.get("kind"))],
                  empty="No backups yet. Make one with: sf backup create <selector>")
    schedule = response.get("schedule") or {}
    if schedule.get("schedule"):
        ctx.out.note(f"scheduled backups ({schedule['schedule']}): "
                     + (f"NOT running - {schedule['error']}" if schedule.get("error")
                        else f"next {when(schedule.get('next_run'))}"))
    return EXIT_OK


def verify(ctx: Ctx) -> int:
    response = ctx.api.request("POST", "/api/backups/verify", body={"archive": ctx.args.archive, "deep": ctx.args.deep},
                               timeout=3600)
    good = bool((response or {}).get("ok"))
    if ctx.out.json_mode:
        ctx.out.json(response)
    elif good:
        ctx.out.print(f"{ctx.args.archive}: intact (every file matches its recorded checksum)")
    else:
        ctx.out.error(f"{ctx.args.archive}: DAMAGED or incomplete - do not rely on this backup")
    return EXIT_OK if good else EXIT_FAILED


# Flags of `sf backup restore` that are fields of POST /api/backups/restore. Only for farm backups.
_FARM_CHOICES = ("trust", "restore_config", "restore_secrets")


def _restore_farm(ctx: Ctx, body: dict[str, Any]) -> int:
    """Restore the farm's settings and say everything that was not taken as it was saved."""
    response = ctx.api.post("/api/backups/restore", body)
    task = response.get("task") if isinstance(response, dict) else None
    if not ctx.wait or not isinstance(task, dict) or "id" not in task:
        return ctx.finish(response)
    final = wait_for(ctx, task)
    code = report(ctx, final)
    output = final.get("output")
    if ctx.out.json_mode or code != EXIT_OK or not isinstance(output, dict):
        return code
    out = ctx.out
    ids = ", ".join(str(i) for i in output.get("instances") or []) or "none"
    out.print(f"restored {len(output.get('restored') or [])} file(s); Androids: {ids}")
    for line in output.get("adjusted") or []:
        out.print(f"changed: {line}")
    for line in output.get("skipped") or []:
        out.print(f"not restored: {line}")
    if output.get("missing_vpn_profiles"):
        out.note("VPN profiles the Androids refer to but this farm does not have (import them with sf vpn import): "
                 + ", ".join(str(n) for n in output["missing_vpn_profiles"]))
    held_back = [line for line in output.get("skipped") or [] if "made by another farm" in str(line)]
    if not output.get("own") and (held_back or output.get("adjusted")):
        out.note("This backup was not made by this farm, so only what cannot do harm was taken from it. If it is "
                 "yours (the farm before a reinstall), repeat with --trust for the Androids exactly as saved, "
                 "--restore-config for config.yaml, --restore-secrets for VPN profiles, logins and tokens.")
    return code


def restore(ctx: Ctx) -> int:
    args = ctx.args
    body: dict[str, Any] = {"archive": args.archive}
    chosen = {name: True for name in _FARM_CHOICES if getattr(args, name)}
    if args.archive.startswith("farm/"):
        if args.over is not None or args.keep_network:
            raise CliError("a farm backup restores settings, not one Android: leave --over and --keep-network out",
                           EXIT_USAGE)
        return _restore_farm(ctx, {**body, **chosen})
    if chosen:
        raise CliError("--trust, --restore-config and --restore-secrets are for farm backups (farm/<time>)", EXIT_USAGE)
    if args.over is not None:
        target = one_id(args.over)
        if not confirm(ctx, f"Replace everything Android {target} holds now with backup {args.archive}? "
                            "(Its current data is backed up first when backup.auto_before_risky is on.)"):
            ctx.out.note("nothing restored")
            return EXIT_OK
        # Answering yes (or --yes) is the consent the API asks for with `force`.
        body.update(id=target, force=True)
    if args.keep_network:
        body["keep_network"] = True
    code = ctx.finish(ctx.api.post("/api/backups/restore", body))
    if args.over is None and ctx.wait and code == EXIT_OK:
        ctx.out.note("Restored as a new Android (see: sf ls). If the original still exists, the copy has its "
                     "network switched off so the two never appear online together; change that with "
                     "sf set <id> network.mode=default once only one of them is in use.")
    return code


def prune(ctx: Ctx) -> int:
    response = ctx.api.post("/api/backups/prune")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    removed = (response or {}).get("removed") or []
    for name in removed:
        ctx.out.print(str(name))
    ctx.out.note(f"removed {len(removed)} old backup(s); backup.keep newest of each Android are kept")
    return EXIT_OK


def farm(ctx: Ctx) -> int:
    body = {"include_secrets": True} if ctx.args.include_secrets else {}
    response = ctx.api.request("POST", "/api/backups/farm", body=body, timeout=600)
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    ctx.out.print(str((response or {}).get("archive")))
    ctx.out.note("farm backup written: settings, Android definitions and automation, "
                 + ("WITH VPN logins and tokens - keep it private." if (response or {}).get("includes_secrets")
                    else "without secrets.") + f"\n{(response or {}).get('dir', '')}")
    return EXIT_OK


def register(b: Any, sub: argparse._SubParsersAction) -> None:
    backup = b.group(sub, "backup", "back up and restore Androids and the farm's settings")
    p = b.add(backup, "create", create, "back up the data of Androids", "sf backup create 1-5",
              description="Back up /data of every selected Android (tar + manifest with checksums). A running "
                          "Android is stopped for the copy and started again when backup.stop_for_backup is on.")
    p.add_argument("selector", help="which Androids")
    p = b.add(backup, "list", list_, "list the backups", "sf backup list", aliases=("ls",))
    p.add_argument("--kind", choices=("android", "farm", "all"), default="all", help="which backups (default: all)")
    p = b.add(backup, "verify", verify, "check a backup against its recorded checksums",
              "sf backup verify android-3/20250101-040000")
    p.add_argument("archive", help="backup name as shown by `sf backup list`")
    p.add_argument("--deep", action="store_true", help="also read the whole archive to check it can be unpacked")
    p = b.add(backup, "restore", restore, "restore a backup as a new Android, or over an existing one",
              "sf backup restore android-3/20250101-040000\nsf backup restore android-3/20250101-040000 --over 3\n"
              "sf backup restore farm/20250101-050000",
              description="Without --over the backup becomes a NEW Android and nothing existing is touched. "
                          "With --over ID that Android's data is replaced: it must be stopped, and you are asked first. "
                          "A farm backup (farm/<time>) merges settings into this farm and never overwrites what "
                          "exists. One this farm made itself is restored in full; from any other, only what cannot "
                          "decide what runs on this machine is taken unless you say so with the flags below.")
    p.add_argument("archive", help="backup name as shown by `sf backup list`")
    p.add_argument("--over", metavar="ID", help="replace the data of this existing Android (asks first)")
    p.add_argument("--keep-network", action="store_true",
                   help="restoring as new: keep the network on even though the original Android still exists")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--trust", action="store_true",
                   help="farm backup from elsewhere: keep the Androids exactly as saved (custom image, data volume, "
                        "boot arguments, autostart) and the schedules. Only for a backup you made yourself: "
                        "it decides what runs on this machine with full privileges")
    p.add_argument("--restore-config", action="store_true",
                   help="farm backup from elsewhere: also take its config.yaml (never implied by --trust)")
    p.add_argument("--restore-secrets", action="store_true",
                   help="farm backup from elsewhere: also take its VPN profiles, logins and tokens")
    b.add(backup, "prune", prune, "delete old backups, keeping the newest backup.keep of each Android", "sf backup prune")
    p = b.add(backup, "farm", farm, "back up the farm itself: settings, Android definitions, automation",
              "sf backup farm")
    p.add_argument("--include-secrets", action="store_true",
                   help="also store VPN logins and tokens (the archive is then owner-only; keep it private)")

"""VPN profiles, VPN credentials, per-Android network status, shared VPN gateways."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any

from ..client import ApiError, CliError
from ..common import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Ctx, confirm
from ..render import when

# ------------------------------------------------------------------ vpn profiles


def vpn_profiles(ctx: Ctx) -> int:
    response = ctx.api.get("/api/net/profiles")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    ctx.out.table(("PROFILE", "KIND", "SERVERS", "LOGIN", "VALID", "PROBLEM"),
                  [(p.get("name"), p.get("provider"), len(p.get("servers") or []),
                    "needed" if p.get("needs_credentials") else "-", bool(p.get("valid")), p.get("error") or "")
                   for p in response.get("profiles") or []],
                  right=(2,), empty="No VPN profiles yet. Import one with: sf vpn import FILE.ovpn")
    return EXIT_OK


def vpn_import(ctx: Ctx) -> int:
    imported = []
    for name in ctx.args.files:
        file = Path(name).expanduser()
        if not file.is_file():
            raise CliError(f"{name}: no such file", EXIT_USAGE)
        imported.append(ctx.api.upload("/api/net/profiles", file))
    if ctx.out.json_mode:
        ctx.out.json(imported if len(imported) != 1 else imported[0])
        return EXIT_OK
    for entry in imported:
        ctx.out.print(str(entry.get("name")))
    ctx.out.note("imported (script and plugin directives are stripped). If the profile asks for a login, store "
                 "it with: sf vpn credentials default")
    return EXIT_OK


def vpn_rm(ctx: Ctx) -> int:
    response = ctx.api.delete(f"/api/net/profiles/{ctx.args.name}")
    return ctx.finish(response, done=f"removed profile {ctx.args.name}")


# ------------------------------------------------------------------ credentials

def read_secret(ctx: Ctx, prompt: str, *, from_stdin: bool) -> str:
    """A secret from the terminal (not echoed) or from stdin. Never from the command line:
    arguments are visible to every user of the machine in the process list and end up in
    shell history."""
    if from_stdin:
        value = sys.stdin.readline().rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            raise CliError("no terminal to ask for the password: pipe it in and pass --password-stdin", EXIT_USAGE)
        try:
            value = getpass.getpass(prompt, stream=ctx.out.stderr)
        except EOFError:
            value = ""
    if not value:
        raise CliError("empty password: nothing stored", EXIT_USAGE)
    return value


def vpn_credentials(ctx: Ctx) -> int:
    args = ctx.args
    name = args.name
    if not name:
        response = ctx.api.get("/api/net/credentials")
        if ctx.out.json_mode:
            ctx.out.json(response)
        else:
            # Names only: the API has no way to read a stored username or password back.
            ctx.out.table(("CREDENTIALS",), [(c.get("name"),) for c in response.get("credentials") or []],
                          empty="No VPN credentials stored. Store some with: sf vpn credentials default")
        return EXIT_OK
    if args.delete:
        return ctx.finish(ctx.api.delete(f"/api/net/credentials/{name}"), done=f"deleted credentials {name}")
    username = args.username
    if not username:
        if args.password_stdin:
            raise CliError("--password-stdin needs --username (stdin carries the password only)", EXIT_USAGE)
        if not sys.stdin.isatty():
            raise CliError("no terminal to ask for the username: pass --username and --password-stdin", EXIT_USAGE)
        ctx.out.stderr.write(f"VPN username for {name!r}: ")
        ctx.out.stderr.flush()
        username = sys.stdin.readline().strip()
    if not username:
        raise CliError("empty username: nothing stored", EXIT_USAGE)
    password = read_secret(ctx, f"VPN password for {name!r} (not shown): ", from_stdin=args.password_stdin)
    response = ctx.api.put(f"/api/net/credentials/{name}", {"username": username, "password": password})
    return ctx.finish(response, done=f"stored credentials {name!r} (owner-only file; they cannot be read back)")


# ------------------------------------------------------------------ status

def vpn_status(ctx: Ctx) -> int:
    ids = ctx.select(ctx.args.selector)
    rows: list[tuple] = []
    collected: list[dict] = []
    failed = 0
    for n, instance_id in enumerate(ids, 1):
        ctx.out.status(f"reading network status {n}/{len(ids)}")
        try:
            status = ctx.api.get(f"/api/instances/{instance_id}/net", refresh=ctx.args.refresh or None)
        except ApiError as e:
            failed += 1
            collected.append({"id": instance_id, "error": e.payload})
            rows.append((instance_id, "-", "-", "error", "-", "-", "-", e.message))
            continue
        collected.append({"id": instance_id, **status})
        kill = status.get("killswitch") or {}
        if status.get("state") in ("missing", "stopped"):
            guard = "-"                 # nothing runs, so there is nothing to guard yet
        else:
            guard = "off" if not kill.get("enabled") else "active" if kill.get("active") else "NOT ACTIVE"
        since = status.get("since")
        rows.append((instance_id, status.get("mode"), status.get("profile") or "-", status.get("state"),
                     status.get("public_ip") or "-", guard, when(since) if since else "-", status.get("detail") or ""))
    ctx.out.clear_status()
    if ctx.out.json_mode:
        ctx.out.json(collected)
    else:
        ctx.out.table(("ID", "MODE", "PROFILE", "STATE", "PUBLIC IP", "KILL SWITCH", "SINCE", "DETAIL"), rows,
                      right=(0,), state_columns=(3,))
    return EXIT_FAILED if failed else EXIT_OK


# ------------------------------------------------------------------ gateways

def gateway_ls(ctx: Ctx) -> int:
    response = ctx.api.get("/api/net/gateways")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    ctx.out.table(("GATEWAY", "STATE", "PROFILE", "SUBNET", "MEMBERS", "DETAIL"),
                  [(g.get("name"), g.get("state"), (g.get("config") or {}).get("profile"),
                    g.get("subnet") or (g.get("config") or {}).get("subnet") or "-", len(g.get("members") or []),
                    g.get("detail") or "") for g in response.get("gateways") or []],
                  right=(4,), state_columns=(1,),
                  empty="No shared VPN gateways. Add one with: sf gateway add NAME --profile FILE.ovpn")
    return EXIT_OK


def gateway_add(ctx: Ctx) -> int:
    args = ctx.args
    body: dict[str, Any] = {"provider": args.provider, "profile": args.profile, "credentials": args.credentials,
                            "subnet": args.subnet or ""}
    response = ctx.api.put(f"/api/net/gateways/{args.name}", body)
    return ctx.finish(response, done=f"gateway {args.name} saved. Bring it up with: sf gateway up {args.name}\n"
                                     f"and put Androids behind it with: sf set <selector> network.mode=gateway "
                                     f"network.gateway={args.name}")


def gateway_rm(ctx: Ctx) -> int:
    name = ctx.args.name
    if not confirm(ctx, f"Remove gateway {name} (its container and network)?"):
        ctx.out.note("nothing removed")
        return EXIT_OK
    response = ctx.api.delete(f"/api/net/gateways/{name}", force=ctx.args.force or None)
    return ctx.finish(response, done=f"removed gateway {name}")


def gateway_action(action: str):
    def run(ctx: Ctx) -> int:
        name = ctx.args.name
        query = {"force": True} if getattr(ctx.args, "force", False) else {}
        response = ctx.api.request("POST", f"/api/net/gateways/{name}/{action}", body={}, query=query, timeout=180)
        if ctx.out.json_mode:
            ctx.out.json(response)
            return EXIT_OK
        if action == "up":
            state = (response or {}).get("state", "-")
            ctx.out.print(f"gateway {name}: {state}" + (f" - {response['detail']}" if (response or {}).get("detail") else ""))
            return EXIT_OK if (response or {}).get("ready", True) else EXIT_FAILED
        ctx.out.note(f"gateway {name} is down")
        return EXIT_OK
    return run


# ------------------------------------------------------------------ parser

def register(b: Any, sub: argparse._SubParsersAction) -> None:
    vpn = b.group(sub, "vpn", "VPN profiles, logins and tunnel status")
    b.add(vpn, "profiles", vpn_profiles, "list the imported VPN profiles", "sf vpn profiles")
    p = b.add(vpn, "import", vpn_import, "import OpenVPN (.ovpn) or WireGuard (.conf) profiles",
              "sf vpn import ~/vpn/nl-ams.ovpn ~/vpn/de-fra.ovpn")
    p.add_argument("files", nargs="+", metavar="FILE")
    p = b.add(vpn, "rm", vpn_rm, "remove a VPN profile", "sf vpn rm nl-ams.ovpn")
    p.add_argument("name", help="profile name as shown by `sf vpn profiles`")
    p = b.add(vpn, "credentials", vpn_credentials, "store the username and password a VPN profile logs in with",
              "sf vpn credentials default\nprintf '%s' \"$VPN_PASSWORD\" | sf vpn credentials default --username myaccount --password-stdin",
              description="Store a VPN login under a name (network.vpn.credentials says which name profiles use; "
                          "`default` unless changed). The password is asked for without echo, or read from "
                          "stdin with --password-stdin; it is never taken from the command line. Stored "
                          "logins cannot be read back, only replaced or deleted. Without a name: list the "
                          "names that exist.")
    p.add_argument("name", nargs="?", help="credentials name, e.g. default")
    p.add_argument("--username", help="VPN account name (asked for when omitted)")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin (one line)")
    p.add_argument("--delete", action="store_true", help="delete the stored login")
    p = b.add(vpn, "status", vpn_status, "tunnel state, public address and kill switch of Androids",
              "sf vpn status running\nsf vpn status 3 --refresh")
    p.add_argument("selector", nargs="?", default="running", help="which Androids (default: running)")
    p.add_argument("--refresh", action="store_true",
                   help="ask the public address again from inside each Android's network (a few seconds each)")

    gw = b.group(sub, "gateway", "shared VPN gateways: one tunnel for many Androids")
    b.add(gw, "ls", gateway_ls, "list gateways and their state", "sf gateway ls")
    p = b.add(gw, "add", gateway_add, "define a gateway (or change an existing one)",
              "sf gateway add eu --profile nl-ams.ovpn")
    p.add_argument("name", help="gateway name, e.g. eu")
    p.add_argument("--profile", required=True, help="VPN profile the gateway connects with")
    p.add_argument("--provider", choices=("openvpn", "wireguard"), default="openvpn")
    p.add_argument("--credentials", default="default", help="credentials name (default: default)")
    p.add_argument("--subnet", help="private /24 for its Androids (default: next free one from docker.subnet_pool)")
    p = b.add(gw, "rm", gateway_rm, "remove a gateway (asks first)", "sf gateway rm eu")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="remove it even while Androids are attached")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p = b.add(gw, "up", gateway_action("up"), "start a gateway and wait for its tunnel", "sf gateway up eu")
    p.add_argument("name")
    p = b.add(gw, "down", gateway_action("down"), "stop a gateway", "sf gateway down eu")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="stop it even while Androids are attached")


"""AI assistants: the MCP server (`sf mcp`) and the operator's approvals (`sf approvals`)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from ...ai.policy import Policy, decide
from ...ai.tools import all_tools
from ...mcp import INSTALL_HINT, clientconfig
from ..client import ApiError, CliError
from ..common import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Ctx
from ..render import duration

STATES = ("pending", "approved", "denied", "expired")


# ------------------------------------------------------------------------------- mcp

def serve(ctx: Ctx) -> int:
    """`sf mcp`: stdout is the protocol's wire in stdio mode, so nothing is printed there."""
    settings = ctx.config.ai.mcp
    if not settings.enabled:
        raise CliError("MCP access is switched off for this farm (ai.mcp.enabled). "
                       "Turn it on with:  sf config set ai.mcp.enabled true")
    try:
        from ...mcp import server
    except ImportError as e:
        if (e.name or "").split(".")[0] in ("mcp", "mcp_types"):
            raise CliError(INSTALL_HINT) from None
        raise
    if ctx.args.http:
        port = ctx.args.port or settings.http_port
        ctx.out.note(f"MCP over Streamable HTTP on http://127.0.0.1:{port}/mcp (this machine only). Clients send the "
                     "farm's API token as a bearer header (sf token). Ctrl-C stops it.")
        server.run_http(ctx.api, ctx.paths, port=port, elicitation=settings.elicitation,
                        attempts_per_minute=ctx.config.server.login_attempts_per_minute)
    else:
        if ctx.args.port:
            raise CliError("--port only applies together with --http", EXIT_USAGE)
        server.run_stdio(ctx.api, elicitation=settings.elicitation)
    return EXIT_OK


def config(ctx: Ctx) -> int:
    snippet = clientconfig.http(ctx.config, ctx.args.port) if ctx.args.http else clientconfig.stdio(ctx.paths)
    ctx.out.print(json.dumps(snippet, indent=2))
    if ctx.args.http:
        ctx.out.note("Start the endpoint with:  sf mcp --http   Put the token printed by `sf token` in place of the "
                     "placeholder. The key names for HTTP servers differ between MCP clients; check yours.")
    else:
        ctx.out.note("Add this to your MCP client's configuration. The client starts `sf mcp` itself; the farm "
                     "daemon must be running (sf serve or sf service start).")
    return EXIT_OK


def tool_rows(policy: Policy) -> list[dict]:
    rows = []
    for tool in all_tools():
        decision = decide(tool.name, tool.klass, policy)
        rows.append({"name": tool.name, "class": tool.klass, "decision": decision.action, "reason": decision.reason,
                     "title": tool.title, "routes": list(tool.routes)})
    return rows


def markdown() -> str:
    """The tool reference of docs/MCP.md, generated from the registry so it cannot drift."""
    heads = {"read": "Read-only tools", "change": "Tools that change the farm", "destructive": "Destructive tools"}
    lines: list[str] = []
    for klass, head in heads.items():
        lines += [f"### {head}", ""]
        for tool in (t for t in all_tools() if t.klass == klass):
            lines += [f"#### `{tool.name}`", "", tool.description, ""]
            properties = tool.input_schema.get("properties") or {}
            required = set(tool.input_schema.get("required") or [])
            if properties:
                lines += ["| Argument | Type | Required | Meaning |", "| --- | --- | --- | --- |"]
                for name, spec in properties.items():
                    kind = spec.get("type", "")
                    if "enum" in spec:
                        kind = "one of " + ", ".join(f"`{v}`" for v in spec["enum"])
                    elif kind == "array":
                        kind = f"list of {(spec.get('items') or {}).get('type', 'values')}"
                    meaning = str(spec.get("description", "")).replace("|", "\\|")
                    lines.append(f"| `{name}` | {kind} | {'yes' if name in required else 'no'} | {meaning} |")
                lines.append("")
            else:
                lines += ["No arguments.", ""]
            lines += ["API: " + ", ".join(f"`{r}`" for r in tool.routes), ""]
    return "\n".join(lines).rstrip() + "\n"


def tools(ctx: Ctx) -> int:
    if ctx.args.markdown:
        ctx.out.print(markdown().rstrip("\n"))
        return EXIT_OK
    policy = Policy.from_config(ctx.config)
    rows = tool_rows(policy)
    if ctx.out.json_mode:
        ctx.out.json({"policy": policy.mode, "tools": rows})
        return EXIT_OK
    words = {"allow": "runs", "ask": "asks you first", "deny": "not available"}
    ctx.out.table(("TOOL", "CLASS", "NOW", "WHAT IT DOES"),
                  [(r["name"], r["class"], words[r["decision"]], r["title"]) for r in rows], state_columns=())
    ctx.out.note(f"policy: {policy.mode} (ai.policy). Change single tools with ai.auto_approve and ai.deny.")
    return EXIT_OK


# ------------------------------------------------------------------------- approvals

def _age(stamp: Any, now: float) -> str:
    try:
        return duration(max(0.0, now - float(stamp)))
    except (TypeError, ValueError):
        return ""


def list_(ctx: Ctx) -> int:
    response = ctx.api.get("/api/approvals", state=getattr(ctx.args, "state", None), limit=getattr(ctx.args, "limit", 30))
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    now = float(response.get("now") or time.time())
    ctx.out.table(("ID", "STATE", "AGE", "ASKED BY", "REQUEST"),
                  [(a.get("id"), a.get("state"), _age(a.get("created"), now), a.get("caller"), a.get("summary"))
                   for a in response.get("approvals") or []], state_columns=(1,),
                  empty="No approval requests. They appear here when an AI assistant asks to change the farm.")
    if response.get("pending"):
        ctx.out.note(f"{response['pending']} waiting for you: sf approvals approve <id> | sf approvals deny <id> | sf approvals watch")
    return EXIT_OK


def _decided(ctx: Ctx, response: dict, word: str) -> int:
    if ctx.out.json_mode:
        ctx.out.json(response)
    else:
        item = response["approval"]
        ctx.out.note(f"{word}: {item.get('summary')}" + ("  (remembered for this assistant session)" if item.get("remembered") else ""))
    return EXIT_OK


def approve(ctx: Ctx) -> int:
    body = {"via": "terminal", "remember": bool(ctx.args.remember)}
    return _decided(ctx, ctx.api.post(f"/api/approvals/{ctx.args.id}/approve", body), "approved")


def deny(ctx: Ctx) -> int:
    body = {"via": "terminal", "reason": ctx.args.reason or ""}
    return _decided(ctx, ctx.api.post(f"/api/approvals/{ctx.args.id}/deny", body), "denied")


def _ask(ctx: Ctx, prompt: str) -> str:
    ctx.out.clear_status()
    ctx.out.stderr.write(prompt)
    ctx.out.stderr.flush()
    return sys.stdin.readline().strip()


def watch(ctx: Ctx) -> int:
    """Answer requests as they arrive. Needs a person: without a terminal there is nobody to ask."""
    if not sys.stdin.isatty():
        raise CliError("sf approvals watch asks questions and needs a terminal. In a script use: "
                       "sf approvals ls --state pending, then approve or deny by id.", EXIT_USAGE)
    skipped: set[str] = set()
    err = ctx.out.stderr
    while True:
        pending = [a for a in (ctx.api.get("/api/approvals", state="pending").get("approvals") or [])
                   if a.get("id") not in skipped]
        if not pending:
            ctx.out.status("waiting for approval requests from AI assistants (Ctrl-C to stop)")
            ctx.sleep(1.0)
            continue
        item = pending[-1]                              # the oldest first
        ctx.out.clear_status()
        err.write(f"\n{item.get('caller')} asks ({item.get('klass')}):\n    {item.get('summary')}\n")
        choices = "[y]es / [n]o / [s]kip" + (" / yes and [r]emember for this session" if item.get("can_remember") else "")
        answer = _ask(ctx, f"Approve? {choices}: ").lower()
        try:
            if answer in ("y", "yes", "r", "remember"):
                remember = answer in ("r", "remember") and bool(item.get("can_remember"))
                ctx.api.post(f"/api/approvals/{item['id']}/approve", {"via": "terminal", "remember": remember})
                err.write("approved\n")
            elif answer in ("n", "no"):
                reason = _ask(ctx, "Reason for the assistant (optional): ")
                ctx.api.post(f"/api/approvals/{item['id']}/deny", {"via": "terminal", "reason": reason[:500]})
                err.write("denied\n")
            else:
                skipped.add(item["id"])
                err.write("skipped; it stays in the list until it expires (sf approvals ls)\n")
        except ApiError as e:
            if e.status not in (404, 409):
                raise
            err.write(f"no longer open: {e.message}\n")          # answered elsewhere, or expired, meanwhile
    return EXIT_FAILED          # pragma: no cover - the loop ends with Ctrl-C


def register(b: Any, sub: argparse._SubParsersAction) -> None:
    mcp = b.group(sub, "mcp", "let MCP-enabled AI assistants see and operate the farm", default=serve,
                  description="Run the farm's MCP server. Alone, `sf mcp` speaks MCP over stdin/stdout: this is what a "
                              "desktop assistant or IDE agent launches (see `sf mcp config`). With --http it serves "
                              "Streamable HTTP on this machine only. Actions that change the farm follow ai.policy "
                              "and wait for your Yes or No (sf approvals). Details: docs/MCP.md.")
    parser = sub.choices["mcp"]
    parser.add_argument("--http", action="store_true",
                        help="serve Streamable HTTP on 127.0.0.1 instead of stdin/stdout (bearer token required)")
    parser.add_argument("--port", type=int, default=None, help="port for --http (default: ai.mcp.http_port, 7711)")
    p = b.add(mcp, "config", config, "print the configuration snippet for an MCP client",
              "sf mcp config\nsf mcp config --http",
              description="Print the `mcpServers` entry an MCP client needs to start this farm's server, with the "
                          "absolute paths of this install. It never contains the API token.")
    p.add_argument("--http", action="store_true", help="the snippet for the HTTP endpoint instead of stdio")
    p.add_argument("--port", type=int, default=None, help="port of the HTTP endpoint (default: ai.mcp.http_port)")
    p = b.add(mcp, "tools", tools, "list the tools an assistant gets, with their class and what the policy does",
              "sf mcp tools\nsf mcp tools --markdown")
    p.add_argument("--markdown", action="store_true", help="print the tool reference as Markdown (docs/MCP.md is built from it)")

    approvals = b.group(sub, "approvals", "answer AI assistants that ask to change the farm", default=list_,
                        description="An AI assistant that wants to change the farm waits for your Yes or No. Alone, "
                                    "`sf approvals` lists the requests.")
    p = b.add(approvals, "ls", list_, "list approval requests, newest first",
              "sf approvals ls\nsf approvals ls --state pending")
    p.add_argument("--state", choices=STATES, default=None, help="only requests in this state")
    p.add_argument("-n", "--limit", type=int, default=30, help="how many (default: 30)")
    p = b.add(approvals, "approve", approve, "say yes to a request: the action runs", "sf approvals approve 3f9a1c2e")
    p.add_argument("id", help="request id as shown by `sf approvals ls`")
    p.add_argument("--remember", action="store_true",
                   help="also approve the same tool on the same Androids for the rest of that assistant session "
                        "(not for destructive actions)")
    p = b.add(approvals, "deny", deny, "say no to a request: nothing runs and the assistant is told",
              "sf approvals deny 3f9a1c2e --reason 'not during business hours'")
    p.add_argument("id", help="request id as shown by `sf approvals ls`")
    p.add_argument("--reason", default="", help="shown to the assistant")
    b.add(approvals, "watch", watch, "answer requests in the terminal as they arrive", "sf approvals watch",
          description="Wait for approval requests and ask yes or no for each, until Ctrl-C.")

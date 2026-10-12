"""AI assistants: the operator's approvals inbox, and what assistants are allowed to do.

    GET    /api/approvals?state=&limit=     the requests, newest first
    POST   /api/approvals                   an assistant asks for a changing action; the daemon
                                            applies the policy and answers allow / ask (+ the
                                            request) or refuses
    GET    /api/approvals/{id}              one request
    GET    /api/approvals/{id}/wait         long poll: returns when it is decided or after `timeout`
    POST   /api/approvals/{id}/approve      the operator's Yes   {remember?}
    POST   /api/approvals/{id}/deny         the operator's No    {reason?}
    POST   /api/approvals/{id}/cancel       the assistant stopped waiting
    DELETE /api/approvals/sessions/{session}  an assistant's session ended
    GET    /api/mcp/info                    policy, tools with their class, client configuration

The line an operator approves is written here, from the tool's validated arguments and the
Androids the selector resolves to right now. The caller sends a tool name and arguments,
never a description: an assistant cannot word a deletion as something harmless.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from ...ai import approvals as approvals_mod
from ...ai.policy import ALLOW, ASK, DENY, READ, Policy, decide
from ...ai.safety import one_line, redact
from ...ai.tools import CALLER_HEADER, all_tools, caller_label, get_tool
from ...mcp import clientconfig, sdk_version
from ..audit import note
from ..auth import Identity, require
from ..common import Body, bounded, farm_of, instance_id
from ..errors import ApiError

router = APIRouter(dependencies=[Depends(require)])

MAX_WAIT = 30.0               # seconds one long poll may stay open
POLL = 0.2


class CallerMiddleware:
    """Puts the caller an AI bridge names in `X-SF-Caller` on the request's audit line, so
    the log shows which assistant an action came from. The request is authenticated like any
    other; the name is a label supplied by the caller, as a User-Agent is."""

    def __init__(self, app):
        self.app = app
        self._name = CALLER_HEADER.lower().encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            raw = next((v for k, v in scope.get("headers", []) if k == self._name), None)
            if raw:
                scope.setdefault("state", {}).setdefault("audit", {})["caller"] = caller_label(raw.decode("latin-1"))
        await self.app(scope, receive, send)


class Ask(Body):
    tool: str = Field(..., min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict, description="The tool's arguments, as the assistant sent them.")
    caller: str = Field(..., min_length=1, max_length=80, description="Who asks, e.g. `mcp:<client name>`.")
    session: str = Field(..., min_length=1, max_length=80, description="Opaque id of the assistant's session; `approve and remember` is tied to it.")


class Approve(Body):
    remember: bool = Field(False, description="Also approve the same tool on the same Androids for the rest of this assistant session. Ignored for destructive tools.")
    via: Literal["panel", "terminal", "assistant"] | None = Field(None, description="Where the answer was given, for the record.")


class Deny(Body):
    reason: str = Field("", max_length=500, description="Shown to the assistant.")
    via: Literal["panel", "terminal", "assistant"] | None = None


def _store(request: Request) -> approvals_mod.Approvals:
    return farm_of(request).approvals


def _by(identity: Identity, via: str | None) -> str:
    return f"{via or ('panel' if identity.method == 'session' else 'api')} ({identity.client})"


def _targets(farm, tool, args: dict) -> list[int]:
    if tool.target == "selector":
        ids = farm.instances.select(args["selector"])
        if not ids:
            raise ValueError("the selector matches no Android (see list_androids)")
        return ids
    if tool.target == "id" and args.get("id") is not None:
        return [instance_id(farm, int(args["id"]))]
    return []


@router.get("/api/approvals")
def approvals(request: Request, state: str | None = None, limit: int = 100) -> dict:
    store = _store(request)
    limit = bounded(limit, 1, 500, "limit")
    return {"approvals": [a.to_dict() for a in store.list(state, limit)], "pending": len(store.list("pending", 500)),
            "now": time.time()}


@router.post("/api/approvals", status_code=201)
def ask(body: Ask, request: Request):
    """Called by an AI bridge before every changing or destructive tool call. `targets` in the
    answer are the Androids the selector resolved to here; the bridge acts on exactly those."""
    farm = farm_of(request)
    try:
        tool = get_tool(body.tool)
    except KeyError as e:
        raise ValueError(str(e.args[0])) from None
    if tool.klass == READ:
        raise ValueError(f"{tool.name} only reads; it needs no approval")
    args = tool.validate(body.arguments)
    policy = Policy.from_config(farm.config)
    decision = decide(tool.name, tool.klass, policy)
    caller = one_line(body.caller, 80)
    note(request, name=caller, action=tool.name)
    if decision.action == DENY:
        raise ApiError(403, "ai_denied", f"{tool.name} is not available to AI assistants: {decision.reason}")
    ids = _targets(farm, tool, args)
    note(request, ids=ids)
    store = farm.approvals
    session = approvals_mod.check_session(body.session)
    if decision.action == ASK and store.remembered(session, tool.name, ids):
        decision = type(decision)(ALLOW, "approved earlier in this session (approve and remember)")
    if decision.action == ALLOW:
        return JSONResponse({"decision": ALLOW, "reason": decision.reason, "approval": None, "targets": ids})
    item = store.create(tool=tool.name, klass=tool.klass, summary=tool.summary(args, ids),
                        arguments=redact(args, policy.forbidden_words), targets=ids, caller=caller, session=session,
                        can_remember=tool.can_remember)
    return {"decision": ASK, "reason": decision.reason, "approval": item.to_dict(), "targets": ids}


@router.delete("/api/approvals/sessions/{session}")
def session_end(session: str, request: Request) -> dict:
    return {"ok": True, "forgotten": _store(request).end_session(approvals_mod.check_session(session))}


@router.get("/api/approvals/{id}")
def approval(id: str, request: Request) -> dict:
    return {"approval": _store(request).get(id).to_dict()}


@router.get("/api/approvals/{id}/wait")
async def approval_wait(id: str, request: Request, timeout: float = 20.0) -> dict:
    """Returns as soon as the request is no longer pending, or after `timeout` seconds with
    its state as it is. Waiting here holds no worker thread, so many assistants can wait."""
    store = await run_in_threadpool(_store, request)
    deadline = time.monotonic() + min(MAX_WAIT, max(0.0, timeout))
    while True:
        item = store.get(id)                     # memory only: safe on the event loop
        if item.state != approvals_mod.PENDING or time.monotonic() >= deadline:
            return {"approval": item.to_dict()}
        await asyncio.sleep(POLL)


@router.post("/api/approvals/{id}/approve")
def approve(id: str, request: Request, body: Approve | None = None, identity: Identity = Depends(require)) -> dict:
    body = body or Approve()
    item = _store(request).approve(id, by=_by(identity, body.via), remember=body.remember)
    note(request, name=item.caller, action=f"approve {item.tool}", ids=item.targets)
    return {"approval": item.to_dict()}


@router.post("/api/approvals/{id}/deny")
def deny(id: str, request: Request, body: Deny | None = None, identity: Identity = Depends(require)) -> dict:
    body = body or Deny()
    item = _store(request).deny(id, by=_by(identity, body.via), reason=one_line(body.reason, 500))
    note(request, name=item.caller, action=f"deny {item.tool}", ids=item.targets)
    return {"approval": item.to_dict()}


@router.post("/api/approvals/{id}/cancel")
def cancel(id: str, request: Request) -> dict:
    item = _store(request).cancel(id)
    note(request, name=item.caller, action=f"cancel {item.tool}")
    return {"approval": item.to_dict()}


@router.get("/api/mcp/info")
def mcp_info(request: Request) -> dict:
    """What AI assistants may do on this farm, and how to connect one."""
    farm = farm_of(request)
    policy = Policy.from_config(farm.config)
    tools = []
    for tool in all_tools():
        decision = decide(tool.name, tool.klass, policy)
        tools.append({"name": tool.name, "title": tool.title, "class": tool.klass, "decision": decision.action,
                      "reason": decision.reason, "description": tool.description})
    mcp = farm.config.ai.mcp
    return {**policy.to_dict(), "enabled": mcp.enabled, "http_port": mcp.http_port, "elicitation": mcp.elicitation,
            "sdk_version": sdk_version(), "pending": len(farm.approvals.list("pending", 500)), "tools": tools,
            "client_config": clientconfig.both(farm.config, farm.paths)}

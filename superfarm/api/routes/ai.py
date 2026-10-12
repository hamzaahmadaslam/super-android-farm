"""The in-farm AI: model profiles, the Assistant, and the Pilot.

    GET    /api/ai/models                              the configured profiles (never a key)
    POST   /api/ai/models/{name}/test                  really test one profile -> probe result

    GET    /api/ai/conversations                       Assistant conversations, newest first
    POST   /api/ai/conversations {model?}              start one
    GET    /api/ai/conversations/{id}?after=N          one, with its events after number N
    POST   /api/ai/conversations/{id}/messages {text}  the operator's message -> task
    POST   /api/ai/conversations/{id}/cancel           stop answering
    DELETE /api/ai/conversations/{id}

    POST   /api/ai/pilot {goal, package, selector, ...}             start a session -> task
    GET    /api/ai/pilot/sessions[?active=]                         sessions, newest first
    GET    /api/ai/pilot/sessions/{id}                              one, with per-device state
    POST   /api/ai/pilot/sessions/{id}/cancel {instance?}           stop it (or one device of it)
    POST   /api/ai/pilot/sessions/{id}/devices/{instance}/resolve {action}   answer needs-attention
    POST   /api/ai/pilot/sessions/{id}/confirm {instance, recording?}        accept an unverified run
    POST   /api/ai/pilot/{dataset}/train                            train + agreement summary -> task
    GET    /api/ai/pilot/{dataset}/summary                          the summary of a trained dataset

Progress is read by polling: a conversation (or a session) is cheap to fetch, and the
Task each of them runs as also appears on the events socket like any other task.
The Assistant's tool calls are made over this same API with the daemon's own token, so
they are authenticated, validated, approved and audit-logged like anybody else's.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from ...ai import models as models_mod
from ...cli.client import Api, connect_host, read_token, tls_context
from ..audit import note
from ..auth import require
from ..common import Body, accepted, farm_of, instance_id
from ..errors import ApiError

router = APIRouter(prefix="/api/ai", dependencies=[Depends(require)])


def _model_error(error: models_mod.ModelError) -> ApiError:
    """A model that is not configured is the caller's to fix (400); one that does not
    answer is somebody else's server (502)."""
    if error.kind == "config":
        return ApiError(400, "model_not_configured", str(error))
    return ApiError(502, f"model_{error.kind}", str(error))


def daemon_api(request: Request) -> Api:
    """An HTTP client for this very daemon, on the address it is really listening on
    (`sf serve --port` can differ from the config file)."""
    farm = farm_of(request)
    server = request.scope.get("server") or (farm.config.server.host, farm.config.server.port)
    host = connect_host(str(server[0]))
    if ":" in host:
        host = f"[{host}]"
    return Api(f"{request.url.scheme}://{host}:{int(server[1])}", read_token(farm.paths),
               context=tls_context(farm.config), timeout=120)


# ------------------------------------------------------------------------------- models

@router.get("/models")
def model_list(request: Request) -> dict:
    farm = farm_of(request)
    return {"models": models_mod.describe(farm.config, farm.paths), "default": farm.config.ai.default_model}


@router.post("/models/{name}/test")
def model_test(name: str, request: Request) -> dict:
    farm = farm_of(request)
    note(request, name=name)
    try:
        client = models_mod.make(farm.config, farm.paths, name)
    except models_mod.ModelError as e:
        raise _model_error(e) from None
    return {"probe": models_mod.probe(client)}


# ---------------------------------------------------------------------------- assistant

class ConversationCreate(Body):
    model: str | None = Field(None, max_length=32)


class Message(Body):
    text: str = Field(..., min_length=1, max_length=8000)


def _assistant(request: Request):
    return farm_of(request).assistant


@router.get("/conversations")
def conversations(request: Request) -> dict:
    return {"conversations": _assistant(request).list()}


@router.post("/conversations", status_code=201)
def conversation_create(request: Request, body: ConversationCreate | None = None) -> dict:
    try:
        created = _assistant(request).create((body or ConversationCreate()).model)
    except models_mod.ModelError as e:
        raise _model_error(e) from None
    note(request, name=created["id"])
    return {"conversation": created}


@router.get("/conversations/{id}")
def conversation(id: str, request: Request, after: int = 0) -> dict:
    return {"conversation": _assistant(request).get(id, after)}


@router.post("/conversations/{id}/messages")
def conversation_message(id: str, body: Message, request: Request):
    try:
        task = _assistant(request).post(id, body.text, daemon_api(request))
    except models_mod.ModelError as e:
        raise _model_error(e) from None
    return accepted(request, task, name=id)


@router.post("/conversations/{id}/cancel")
def conversation_cancel(id: str, request: Request) -> dict:
    note(request, name=id)
    return {"ok": True, "cancelled": _assistant(request).cancel(id)}


@router.delete("/conversations/{id}")
def conversation_delete(id: str, request: Request) -> dict:
    farm = farm_of(request)
    farm.assistant.delete(id)
    # "Approve and remember" belongs to the conversation: it ends with it.
    farm.approvals.end_session(id)
    note(request, name=id)
    return {"ok": True}


# -------------------------------------------------------------------------------- pilot

class PilotStart(Body):
    goal: str = Field(..., min_length=3, max_length=600)
    package: str = Field(..., min_length=3, max_length=200)
    selector: str = Field(..., min_length=1, max_length=500)
    model: str | None = Field(None, max_length=32)
    success: str | dict[str, Any] | None = Field(None, description="text:..., id:..., desc:..., regex:..., package:..., activity:..., or a selector mapping")
    max_steps: int | None = Field(None, ge=1, le=200)
    dataset: str | None = Field(None, max_length=64)
    runs: int = Field(1, ge=1, le=10)
    values: dict[str, str] | None = Field(None, description="Named values the Pilot may type (kept in memory for this session only)")
    allowed_packages: list[str] | None = Field(None, max_length=20)


class PilotCancel(Body):
    instance: int | None = None


class PilotResolve(Body):
    action: Literal["resume", "skip", "abort"] = "resume"


class PilotConfirm(Body):
    instance: int
    recording: str | None = Field(None, max_length=60)


def _pilot(request: Request):
    return farm_of(request).pilot


@router.post("/pilot")
def pilot_start(body: PilotStart, request: Request):
    try:
        task = _pilot(request).start(body.goal, body.package, body.selector, model=body.model, success=body.success,
                                     max_steps=body.max_steps, dataset=body.dataset, runs=body.runs,
                                     values=body.values, allowed_packages=body.allowed_packages)
    except models_mod.ModelError as e:
        raise _model_error(e) from None
    return accepted(request, task, selector=body.selector, name=body.package)


@router.get("/pilot/sessions")
def pilot_sessions(request: Request, active: bool = False) -> dict:
    return {"sessions": _pilot(request).sessions(active)}


@router.get("/pilot/sessions/{id}")
def pilot_session(id: str, request: Request) -> dict:
    return {"session": _pilot(request).status(id)}


@router.post("/pilot/sessions/{id}/cancel")
def pilot_cancel(id: str, request: Request, body: PilotCancel | None = None) -> dict:
    instance = (body or PilotCancel()).instance
    note(request, task=id, ids=[instance] if instance is not None else None)
    return {"ok": True, "cancelled": _pilot(request).cancel(id, instance)}


@router.post("/pilot/sessions/{id}/devices/{instance}/resolve")
def pilot_resolve(id: str, instance: str, request: Request, body: PilotResolve | None = None) -> dict:
    device = instance_id(farm_of(request), instance)
    action = (body or PilotResolve()).action
    note(request, task=id, ids=[device], action=action)
    if not _pilot(request).resolve(id, device, action):
        raise ApiError(409, "conflict", f"Android {device} is not waiting for an answer in pilot session {id}")
    return {"ok": True}


@router.post("/pilot/sessions/{id}/confirm")
def pilot_confirm(id: str, body: PilotConfirm, request: Request) -> dict:
    note(request, task=id, ids=[body.instance])
    return {"confirmed": _pilot(request).confirm(id, body.instance, body.recording)}


@router.post("/pilot/{dataset}/train")
def pilot_train(dataset: str, request: Request):
    return accepted(request, _pilot(request).train(dataset), name=dataset)


@router.get("/pilot/{dataset}/summary")
def pilot_summary(dataset: str, request: Request) -> dict:
    # A dataset that has not been trained yet has no summary. That is a normal state the
    # session page asks about on every load, so it is an empty answer and not a 404.
    try:
        return {"summary": _pilot(request).summary(dataset)}
    except (KeyError, FileNotFoundError):
        return {"summary": None}

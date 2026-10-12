"""Androids: create, change, start and stop, clone, delete, logs.

Repeated and simultaneous requests (a double click, two tabs):

    start, stop, restart, recreate, delete, bulk
        one request per Android at a time: while a task has not finished with an Android,
        another of these that names it is refused with 409 `busy`; `detail` holds the task's
        id and the Androids in question (see core.tasks)
    create
        makes new Androids each time it is called. A client that may send the same create
        twice (a retry, a double click) adds an `Idempotency-Key` header and gets the first
        answer again instead of a second set (see common.Idempotency)
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import Field
from starlette.responses import JSONResponse

from ...core import instances as instances_mod
from ...core.tasks import TaskBusy
from ..audit import note
from ..auth import require
from ..common import IDEMPOTENCY_HEADER, Body, accepted, bounded, farm_of, instance_id

router = APIRouter(dependencies=[Depends(require)])

Action = Literal["start", "stop", "restart", "recreate"]
BOOTS = ("start", "restart", "recreate")


class Create(Body):
    count: int = Field(1, ge=1, le=500)
    name: str = Field("", max_length=60)
    template: dict[str, Any] = Field(default_factory=dict, description="InstanceSpec fields shared by the new Androids (android_version, gpu_mode, network, tags, ...).")


class Update(Body):
    patch: dict[str, Any] = Field(..., description="InstanceSpec fields to change; applied at the next (re)start.")
    force: bool = Field(False, description="Allow a change that can lose data, such as another Android version on an existing /data.")


class Force(Body):
    force: bool = Field(False, description="Start even when the farm is over its capacity estimate and performance.enforce_capacity is on.")


class Clone(Body):
    with_data: bool = False


class Bulk(Body):
    action: Literal["start", "stop", "restart", "recreate", "delete"]
    selector: str = Field(..., min_length=1, max_length=500)
    force: bool = False


def _refuse_over_capacity(farm, ids: list[int], force: bool) -> None:
    """The capacity estimate is advice unless performance.enforce_capacity is on. When it is,
    the instance manager refuses each start that goes over, inside the task. The same rule is
    applied here first, so the caller gets a 409 with the numbers and can answer it with
    "run anyway" (force) instead of finding the refusal in a finished task."""
    if force or not ids or not farm.config.performance.enforce_capacity:
        return
    mgr = farm.instances
    starting = len(set(ids) - set(mgr.select("running")))
    if not starting:
        return
    capacity = mgr.capacity()
    reasons = list(capacity.get("reasons") or [])
    limit = (capacity.get("estimate") or {}).get("max_instances")
    if not capacity.get("over") and isinstance(limit, int) and starting > capacity.get("headroom", 0):
        reasons.append(f"{starting} Androids were asked to start and about {capacity['headroom']} more fit "
                       f"({capacity['running']} running, estimate {limit})")
    if reasons:
        raise instances_mod.OverCapacity(
            f"not starting: over capacity ({'; '.join(reasons)}). Stop some Androids, or start anyway with force; "
            "set performance.enforce_capacity to false to only be warned", capacity)


@router.get("/api/instances")
def list_instances(request: Request) -> dict:
    return {"instances": farm_of(request).instances.list_status()}


@router.post("/api/instances", status_code=201)
def create(body: Create, request: Request, response: Response) -> dict:
    """Add Androids. With an `Idempotency-Key` header (any 1-128 printable characters, e.g. a
    UUID made when the form was opened) a repeat of the same request within ten minutes
    returns the first answer, marked `Idempotency-Replayed: true`, and creates nothing."""
    def make() -> dict:
        specs = farm_of(request).instances.create(count=body.count, name=body.name, template=body.template)
        return {"instances": [s.model_dump(mode="json") for s in specs]}

    answer, replayed = request.app.state.idempotency.run(request.headers.get(IDEMPOTENCY_HEADER), body, make)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    note(request, ids=[s["id"] for s in answer["instances"]], count=len(answer["instances"]))
    return answer


@router.post("/api/instances/bulk")
def bulk(body: Bulk, request: Request):
    farm = farm_of(request)
    mgr = farm.instances
    if body.action in BOOTS:
        _refuse_over_capacity(farm, mgr.select(body.selector), body.force)
    task = mgr.bulk(body.action, body.selector, force=body.force)
    return accepted(request, task, selector=body.selector, action=body.action, force=body.force or None)


@router.get("/api/instances/{id}")
def status(id: str, request: Request) -> dict:
    farm = farm_of(request)
    n = instance_id(farm, id)
    return {**farm.instances.status(n), "spec": farm.store.get(n).model_dump(mode="json")}


@router.patch("/api/instances/{id}")
def update(id: str, body: Update, request: Request) -> dict:
    farm = farm_of(request)
    n = instance_id(farm, id)
    spec = farm.instances.update(n, body.patch, force=body.force)
    note(request, ids=[n], keys=sorted(body.patch)[:50], force=body.force or None)
    return {"instance": spec.model_dump(mode="json"),
            "note": "saved; a running Android picks the change up at its next restart"}


@router.delete("/api/instances/{id}")
def delete(id: str, request: Request, keep_data: bool = False, force: bool = False) -> JSONResponse:
    farm = farm_of(request)
    n = instance_id(farm, id)
    note(request, ids=[n], force=force or None)
    holder = farm.tasks.busy_targets().get(n)
    if holder is not None:              # the same rule as for the actions that run as tasks
        raise TaskBusy(holder, [n])
    result = farm.instances.delete(n, keep_data=keep_data, force=force)
    return JSONResponse(jsonable_encoder({"deleted": n, **(result or {})}))


@router.post("/api/instances/{id}/clone", status_code=201)
def clone(id: str, body: Clone, request: Request) -> dict:
    farm = farm_of(request)
    n = instance_id(farm, id)
    spec = farm.instances.clone(n, with_data=body.with_data)
    note(request, ids=[n, spec.id])
    return {"instance": spec.model_dump(mode="json")}


@router.get("/api/instances/{id}/logs")
def logs(id: str, request: Request, tail: int = 200) -> dict:
    farm = farm_of(request)
    n = instance_id(farm, id)
    tail = bounded(tail, 1, 5000, "tail")
    names = farm.instances.names(n)
    return {"id": n, "android": farm.docker.logs(names.android, tail=tail), "net": farm.docker.logs(names.net, tail=tail)}


@router.post("/api/instances/{id}/{action}")
def lifecycle(id: str, action: Action, request: Request, body: Force | None = None):
    farm = farm_of(request)
    n = instance_id(farm, id)
    force = bool(body and body.force)
    if action in BOOTS:
        _refuse_over_capacity(farm, [n], force)
    task = farm.instances.bulk(action, str(n), force=force)
    return accepted(request, task, action=action, force=force or None)

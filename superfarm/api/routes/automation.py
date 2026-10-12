"""Automation: the `Automation` facade, one route per public method.

    datasets / recordings / record   capture what a person does on one device
    train / programs                 turn recordings into a program
    flows                            hand-written YAML flows (bundled examples are read-only)
    profiles                         per-app roles, pop-up rules and never-tap lists
    values                           shared values, per-Android values, consumable pools
    runs / schedules                 run a program or flow on a selection, now or on a clock
    screen / llm                     read a device's UI tree; the optional model's health
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.responses import FileResponse

from ...automation.flows import parse_flow_text
from ...automation.profiles import AppProfile
from ..audit import note
from ..auth import require
from ..common import Body, accepted, bounded, farm_of, instance_id

router = APIRouter(prefix="/api/automation", dependencies=[Depends(require)])


def _auto(request: Request):
    return farm_of(request).automation


def _found(ok: bool, what: str) -> dict:
    if not ok:
        raise KeyError(f"no such {what}")
    return {"ok": True}


def _device(request: Request, id: str) -> int:
    return instance_id(farm_of(request), id)


# ----------------------------------------------------------------------------- datasets

class DatasetCreate(Body):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=4000)
    package: str = Field("", max_length=200)


class DatasetUpdate(Body):
    name: str | None = Field(None, max_length=120)
    description: str | None = Field(None, max_length=4000)
    package: str | None = Field(None, max_length=200)


@router.get("/datasets")
def datasets(request: Request) -> dict:
    return {"datasets": _auto(request).datasets()}


@router.post("/datasets", status_code=201)
def dataset_create(body: DatasetCreate, request: Request) -> dict:
    created = _auto(request).create_dataset(body.name, body.description, body.package)
    note(request, name=created.get("slug") or body.name)
    return {"dataset": created}


@router.get("/datasets/{slug}")
def dataset(slug: str, request: Request) -> dict:
    return {"dataset": _auto(request).dataset(slug)}


@router.patch("/datasets/{slug}")
def dataset_update(slug: str, body: DatasetUpdate, request: Request) -> dict:
    return {"dataset": _auto(request).update_dataset(slug, **body.model_dump(exclude_unset=True))}


@router.delete("/datasets/{slug}")
def dataset_delete(slug: str, request: Request) -> dict:
    return _found(_auto(request).delete_dataset(slug), "dataset")


@router.post("/datasets/{slug}/train")
def dataset_train(slug: str, request: Request):
    return accepted(request, _auto(request).train(slug), name=slug)


@router.get("/datasets/{slug}/recordings/{rid}")
def recording(slug: str, rid: str, request: Request) -> dict:
    return {"recording": _auto(request).recording(slug, rid)}


@router.delete("/datasets/{slug}/recordings/{rid}")
def recording_delete(slug: str, rid: str, request: Request) -> dict:
    return _found(_auto(request).delete_recording(slug, rid), "recording")


@router.get("/datasets/{slug}/recordings/{rid}/shots/{relative:path}")
def recording_shot(slug: str, rid: str, relative: str, request: Request) -> FileResponse:
    return FileResponse(_auto(request).recording_shot(slug, rid, relative))


# ---------------------------------------------------------------------------- recording

class RecordStart(Body):
    dataset: str = Field(..., min_length=1, max_length=64)
    id: int


class Point(Body):
    x: float
    y: float
    normalized: bool = True


class PointHold(Point):
    ms: int = Field(700, ge=1, le=30000)


class RecordSwipe(Body):
    x1: float
    y1: float
    x2: float
    y2: float
    ms: int = Field(300, ge=1, le=30000)
    normalized: bool = True


class RecordText(Body):
    value: str | None = Field(None, max_length=10000, description="Text typed now and stored as written.")
    name: str | None = Field(None, max_length=64, description="Record a placeholder `{name}` instead; the value comes from the run's values.")


class RecordKey(Body):
    name: str = Field(..., min_length=1, max_length=40)


class RecordLaunch(Body):
    package: str = Field(..., min_length=1, max_length=200)


class RecordWait(Body):
    seconds: float = Field(..., gt=0, le=600)


@router.get("/record")
def record_active(request: Request) -> dict:
    return {"recordings": _auto(request).recorder.active()}


@router.post("/record/start")
def record_start(body: RecordStart, request: Request) -> dict:
    n = instance_id(farm_of(request), body.id)
    note(request, ids=[n], name=body.dataset)
    return _auto(request).start_recording(body.dataset, n)


@router.get("/record/{id}")
def record_status(id: str, request: Request) -> dict:
    return _auto(request).recording_status(_device(request, id))


@router.post("/record/{id}/tap")
def record_tap(id: str, body: Point, request: Request) -> dict:
    return _auto(request).record_tap(_device(request, id), body.x, body.y, normalized=body.normalized)


@router.post("/record/{id}/long-press")
def record_long_press(id: str, body: PointHold, request: Request) -> dict:
    return _auto(request).record_long_press(_device(request, id), body.x, body.y, ms=body.ms, normalized=body.normalized)


@router.post("/record/{id}/swipe")
def record_swipe(id: str, body: RecordSwipe, request: Request) -> dict:
    return _auto(request).record_swipe(_device(request, id), body.x1, body.y1, body.x2, body.y2, ms=body.ms,
                                       normalized=body.normalized)


@router.post("/record/{id}/text")
def record_text(id: str, body: RecordText, request: Request) -> dict:
    return _auto(request).record_text(_device(request, id), body.value, name=body.name)


@router.post("/record/{id}/key")
def record_key(id: str, body: RecordKey, request: Request) -> dict:
    return _auto(request).record_key(_device(request, id), body.name)


@router.post("/record/{id}/launch")
def record_launch(id: str, body: RecordLaunch, request: Request) -> dict:
    return _auto(request).record_launch(_device(request, id), body.package)


@router.post("/record/{id}/wait")
def record_wait(id: str, body: RecordWait, request: Request) -> dict:
    return _auto(request).record_wait(_device(request, id), body.seconds)


@router.post("/record/{id}/assert-visible")
def record_assert_visible(id: str, body: Point, request: Request) -> dict:
    return _auto(request).record_assert_visible(_device(request, id), body.x, body.y, normalized=body.normalized)


@router.post("/record/{id}/undo")
def record_undo(id: str, request: Request) -> dict:
    return {"undone": _auto(request).record_undo(_device(request, id))}


@router.post("/record/{id}/stop")
def record_stop(id: str, request: Request) -> dict:
    return _auto(request).stop_recording(_device(request, id))


@router.post("/record/{id}/cancel")
def record_cancel(id: str, request: Request) -> dict:
    return {"cancelled": _auto(request).cancel_recording(_device(request, id))}


# ----------------------------------------------------------------------------- programs

@router.get("/programs")
def programs(request: Request) -> dict:
    return {"programs": _auto(request).programs()}


@router.get("/programs/{slug}")
def program(slug: str, request: Request) -> dict:
    return {"program": _auto(request).program(slug)}


@router.delete("/programs/{slug}")
def program_delete(slug: str, request: Request) -> dict:
    return _found(_auto(request).delete_program(slug), "program")


@router.get("/programs/{slug}/shots/{relative:path}")
def program_shot(slug: str, relative: str, request: Request) -> FileResponse:
    return FileResponse(_auto(request).program_shot(slug, relative))


# -------------------------------------------------------------------------------- flows

class FlowText(Body):
    text: str = Field(..., min_length=1, max_length=512 * 1024)


class FlowValidate(Body):
    text: str | None = Field(None, max_length=512 * 1024, description="YAML to check without saving it.")
    name: str | None = Field(None, max_length=80, description="A saved flow to check in full: flows it calls, and roles it uses.")


@router.get("/flows")
def flows(request: Request) -> dict:
    return {"flows": _auto(request).flows.list()}


@router.post("/flows/validate")
def flow_validate(body: FlowValidate, request: Request) -> dict:
    """400 with the reason when the flow is not valid; the summary when it is."""
    auto = _auto(request)
    if body.text is not None:
        flow = parse_flow_text(body.text, name=body.name or "", forbidden_words=auto.flows.forbidden_words)
    elif body.name:
        flow = auto.flows.check(body.name, auto.profiles)
    else:
        raise ValueError("give `text` (YAML to check) or `name` (a saved flow)")
    return {"ok": True, "flow": flow.summary()}


@router.get("/flows/{name:path}")
def flow_get(name: str, request: Request) -> dict:
    library = _auto(request).flows
    text = library.text(name)
    try:
        summary, error = library.get(name).summary(), ""
    except ValueError as e:          # a flow edited by hand into something invalid can still be opened and fixed
        summary, error = None, str(e)
    return {"name": name, "text": text, "flow": summary, "error": error, "builtin": name.startswith("examples/")}


@router.put("/flows/{name:path}")
def flow_put(name: str, body: FlowText, request: Request) -> dict:
    flow = _auto(request).flows.save(name, body.text)
    note(request, name=name)
    return {"flow": flow.summary()}


@router.delete("/flows/{name:path}")
def flow_delete(name: str, request: Request) -> dict:
    note(request, name=name)
    return _found(_auto(request).flows.delete(name), "flow")


# ----------------------------------------------------------------------------- profiles

class ProfileBody(Body):
    roles: dict[str, Any] = Field(default_factory=dict)
    overlays: list[dict[str, Any]] = Field(default_factory=list)
    never_tap: list[Any] = Field(default_factory=list)
    notes: str = Field("", max_length=4000)


class SelectorValue(Body):
    selector: dict[str, Any] | str


class OverlayBody(Body):
    rule: dict[str, Any]


class Teach(Body):
    id: int
    role: str = Field(..., min_length=1, max_length=60)
    x: int
    y: int
    with_text: bool = True


def _profile(profile: AppProfile) -> dict:
    return {"profile": profile.model_dump(mode="json", exclude_none=True)}


@router.get("/profiles")
def profiles(request: Request) -> dict:
    return {"profiles": _auto(request).profiles.list()}


@router.get("/profiles/{package}")
def profile_get(package: str, request: Request) -> dict:
    return _profile(_auto(request).profiles.load(package))


@router.put("/profiles/{package}")
def profile_put(package: str, body: ProfileBody, request: Request) -> dict:
    profile = AppProfile.model_validate({**body.model_dump(), "package": package})
    note(request, package=package)
    return _profile(_auto(request).profiles.save(profile))


@router.delete("/profiles/{package}")
def profile_delete(package: str, request: Request) -> dict:
    note(request, package=package)
    return _found(_auto(request).profiles.delete(package), "profile")


@router.put("/profiles/{package}/roles/{role}")
def profile_role_put(package: str, role: str, body: SelectorValue, request: Request) -> dict:
    return _profile(_auto(request).profiles.set_role(package, role, body.selector))


@router.delete("/profiles/{package}/roles/{role}")
def profile_role_delete(package: str, role: str, request: Request) -> dict:
    return _profile(_auto(request).profiles.remove_role(package, role))


@router.post("/profiles/{package}/overlays")
def profile_overlay_add(package: str, body: OverlayBody, request: Request) -> dict:
    return _profile(_auto(request).profiles.add_overlay(package, body.rule))


@router.delete("/profiles/{package}/overlays/{name}")
def profile_overlay_delete(package: str, name: str, request: Request) -> dict:
    return _profile(_auto(request).profiles.remove_overlay(package, name))


@router.post("/profiles/{package}/never-tap")
def profile_never_tap_add(package: str, body: SelectorValue, request: Request) -> dict:
    return _profile(_auto(request).profiles.add_never_tap(package, body.selector))


@router.delete("/profiles/{package}/never-tap/{index}")
def profile_never_tap_delete(package: str, index: int, request: Request) -> dict:
    return _profile(_auto(request).profiles.remove_never_tap(package, index))


@router.post("/profiles/{package}/teach")
def profile_teach(package: str, body: Teach, request: Request) -> dict:
    n = instance_id(farm_of(request), body.id)
    note(request, ids=[n], package=package)
    return _auto(request).teach_role(n, package, body.role, body.x, body.y, with_text=body.with_text)


# ------------------------------------------------------------------------------- values

class Value(Body):
    value: str = Field(..., max_length=100_000)


class PoolLines(Body):
    lines: list[str] = Field(..., min_length=1, max_length=20_000)


def _visible(values, data: dict[str, str]) -> dict:
    """Values by name. A name that looks like a secret cannot be stored through this API;
    should one be in the file anyway (edited by hand), its value is not sent out."""
    shown = {k: v for k, v in data.items() if not values.is_secret(k)}
    return {"values": shown, "hidden": sorted(k for k in data if k not in shown)}


@router.get("/values")
def values(request: Request) -> dict:
    store = _auto(request).values
    return {"shared": _visible(store, store.shared()), "pools": store.pools()}


@router.put("/values/shared/{name}")
def value_shared_put(name: str, body: Value, request: Request) -> dict:
    _auto(request).values.set_shared(name, body.value)
    note(request, name=name)
    return {"name": name, "stored": True}


@router.delete("/values/shared/{name}")
def value_shared_delete(name: str, request: Request) -> dict:
    note(request, name=name)
    return _found(_auto(request).values.remove_shared(name), "value")


@router.get("/values/instances/{id}")
def values_instance(id: str, request: Request) -> dict:
    store = _auto(request).values
    return _visible(store, store.instance(_device(request, id)))


@router.put("/values/instances/{id}/{name}")
def value_instance_put(id: str, name: str, body: Value, request: Request) -> dict:
    _auto(request).values.set_instance(_device(request, id), name, body.value)
    note(request, name=name)
    return {"name": name, "stored": True}


@router.delete("/values/instances/{id}/{name}")
def value_instance_delete(id: str, name: str, request: Request) -> dict:
    note(request, name=name)
    return _found(_auto(request).values.remove_instance(_device(request, id), name), "value")


@router.get("/values/pools")
def pools(request: Request) -> dict:
    return {"pools": _auto(request).values.pools()}


@router.get("/values/pools/{name}")
def pool(name: str, request: Request, limit: int = 50) -> dict:
    store = _auto(request).values
    limit = bounded(limit, 1, 1000, "limit")
    return {"name": name, "remaining": store.pool_count(name), "used": store.pool_used(name, limit)}


@router.post("/values/pools/{name}")
def pool_add(name: str, body: PoolLines, request: Request) -> dict:
    store = _auto(request).values
    added = store.pool_add(name, body.lines)
    note(request, name=name, count=added)
    return {"name": name, "added": added, "remaining": store.pool_count(name)}


@router.delete("/values/pools/{name}")
def pool_delete(name: str, request: Request) -> dict:
    note(request, name=name)
    return _found(_auto(request).values.pool_delete(name), "pool")


# --------------------------------------------------------------------------------- runs

class Run(Body):
    kind: Literal["program", "flow"]
    name: str = Field(..., min_length=1, max_length=80)
    selector: str = Field(..., min_length=1, max_length=500)
    variables: dict[str, str] = Field(default_factory=dict, description="Values for this run only; kept in memory, never written to disk.")
    dry_run: bool = False


class RunCancel(Body):
    id: int | None = Field(None, description="One device of the run; the others carry on. Leave out to cancel the whole run.")


class Resolve(Body):
    id: int
    action: Literal["resume", "skip", "abort"] = "resume"


@router.post("/runs")
def run_start(body: Run, request: Request):
    task = _auto(request).run(body.kind, body.name, body.selector, variables=body.variables, dry_run=body.dry_run)
    return accepted(request, task, selector=body.selector, name=body.name, action=body.kind)


@router.get("/runs")
def runs(request: Request, active: bool = False) -> dict:
    return {"runs": _auto(request).runs(active)}


@router.get("/runs/{task_id}")
def run_status(task_id: str, request: Request) -> dict:
    status = _auto(request).run_status(task_id)
    if status is None:
        raise KeyError(f"no automation run {task_id[:40]!r}")
    return {"run": status}


@router.post("/runs/{task_id}/cancel")
def run_cancel(task_id: str, request: Request, body: RunCancel | None = None) -> dict:
    note(request, task=task_id)
    return _found(_auto(request).cancel_run(task_id, body.id if body else None), "run (or it has finished)")


@router.post("/runs/{task_id}/resolve")
def run_resolve(task_id: str, body: Resolve, request: Request) -> dict:
    note(request, task=task_id, ids=[body.id], action=body.action)
    return _found(_auto(request).resolve(task_id, body.id, body.action), "device waiting for attention in that run")


# ---------------------------------------------------------------------------- schedules

class ScheduleCreate(Body):
    name: str = Field(..., min_length=1, max_length=80)
    kind: Literal["program", "flow"]
    target: str = Field(..., min_length=1, max_length=80)
    selector: str = Field(..., min_length=1, max_length=500)
    every: int = Field(0, ge=0, description="Seconds between runs. Use this or `cron`.")
    cron: str = Field("", max_length=120, description="`minute hour day-of-month month day-of-week`.")
    variables: dict[str, str] = Field(default_factory=dict)
    dry_run: bool = False
    enabled: bool = True


class ScheduleUpdate(Body):
    name: str | None = Field(None, min_length=1, max_length=80)
    kind: Literal["program", "flow"] | None = None
    target: str | None = Field(None, min_length=1, max_length=80)
    selector: str | None = Field(None, min_length=1, max_length=500)
    every: int | None = Field(None, ge=0)
    cron: str | None = Field(None, max_length=120)
    variables: dict[str, str] | None = None
    dry_run: bool | None = None
    enabled: bool | None = None


@router.get("/schedules")
def schedules(request: Request) -> dict:
    return {"schedules": _auto(request).schedules.list()}


@router.post("/schedules", status_code=201)
def schedule_create(body: ScheduleCreate, request: Request) -> dict:
    created = _auto(request).schedules.add(**body.model_dump())
    note(request, name=body.name, selector=body.selector)
    return {"schedule": asdict(created)}


@router.patch("/schedules/{schedule_id}")
def schedule_update(schedule_id: str, body: ScheduleUpdate, request: Request) -> dict:
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    return {"schedule": asdict(_auto(request).schedules.update(schedule_id, patch))}


@router.delete("/schedules/{schedule_id}")
def schedule_delete(schedule_id: str, request: Request) -> dict:
    return _found(_auto(request).schedules.remove(schedule_id), "schedule")


# ------------------------------------------------------------------------- screen / llm

@router.get("/screen/{id}")
def screen(id: str, request: Request) -> dict:
    return _auto(request).read_screen(_device(request, id))


@router.get("/screen/{id}/element")
def screen_element(id: str, request: Request, x: int, y: int) -> dict:
    return {"element": _auto(request).element_at(_device(request, id), x, y)}


@router.get("/llm")
def llm(request: Request) -> dict:
    return _auto(request).llm_status()

"""Health, login, system information, settings, images and tasks."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from ... import __version__
from ...config import Config
from ...core import device_profile
from ...core.metrics import farm_metrics
from ...farm import config_effects
from ...host import doctor as doctor_mod
from ...host import gpu as gpu_mod
from ...host import venus
from ..audit import note
from ..auth import CSRF_COOKIE, SESSION_COOKIE, Identity, client_of, require
from ..common import Body, accepted, bounded, farm_of
from ..errors import ApiError

public = APIRouter()
router = APIRouter(dependencies=[Depends(require)])

METRICS_TTL = 3.0      # one snapshot costs a `docker stats` (about two seconds); viewers share it
_metrics_lock = threading.Lock()


# ------------------------------------------------------------------------ health / auth

@public.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


class Login(Body):
    token: str = Field(..., min_length=1, max_length=512)


@public.post("/api/auth/login")
def login(body: Login, request: Request, response: Response) -> dict:
    auth = request.app.state.auth
    farm = farm_of(request)
    if not farm.config.server.auth_required:
        return {"ok": True, "auth_required": False, "csrf": ""}
    session = auth.login(client_of(request), body.token)
    secure = request.url.scheme == "https"
    lifetime = farm.config.server.session_hours * 3600
    response.set_cookie(SESSION_COOKIE, session.id, max_age=lifetime, path="/", httponly=True, samesite="strict",
                        secure=secure)
    # Readable by the page on purpose: it is the value the page echoes in X-SF-CSRF.
    response.set_cookie(CSRF_COOKIE, session.csrf, max_age=lifetime, path="/", httponly=False, samesite="strict",
                        secure=secure)
    return {"ok": True, "auth_required": True, "csrf": session.csrf, "expires": session.expires}


@public.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    # No login needed to log out, and no CSRF check: the worst a forged call does is sign the browser out.
    request.app.state.auth.sessions.drop(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/auth/me")
def me(request: Request, identity: Identity = Depends(require)) -> dict:
    out: dict[str, Any] = {"authenticated": True, "method": identity.method,
                           "auth_required": farm_of(request).config.server.auth_required}
    if identity.session is not None:
        out.update(csrf=identity.session.csrf, expires=identity.session.expires)
    return out


# ------------------------------------------------------------------------------- system

@router.get("/api/system")
def system(request: Request) -> dict:
    return farm_of(request).system_info()


@router.get("/api/system/capacity")
def capacity(request: Request) -> dict:
    return farm_of(request).capacity()


@router.get("/api/system/doctor")
def doctor(request: Request, fresh: bool = False) -> dict:
    checks = farm_of(request).doctor(fresh=fresh)
    return {"checks": [asdict(c) for c in checks], "summary": doctor_mod.summarize(checks)}


@router.get("/api/system/metrics")
def metrics(request: Request) -> dict:
    farm, state = farm_of(request), request.app.state
    with _metrics_lock:
        cached = getattr(state, "metrics", None)
        if cached and time.monotonic() - cached[0] < METRICS_TTL:
            return cached[1]
        snapshot = farm_metrics(farm.instances)
        state.metrics = (time.monotonic(), snapshot)
        return snapshot


@router.get("/api/system/gpus")
def gpus(request: Request) -> dict:
    """The cards, every rendering choice selectable on this machine (for the per-Android and
    bulk pickers), and the state of the NVIDIA runtime and relay."""
    farm = farm_of(request)
    cards = farm.gpus()
    runtime = venus.runtime_status(farm.config, farm.paths)
    return {
        "gpus": [asdict(g) for g in cards],
        "options": gpu_mod.options(farm.config, farm.paths, gpus=cards, runtime=runtime),
        "relay": {"runtime": runtime,
                  "relays": venus.relay_status(farm.config, nodes=[g.node for g in cards if g.vendor == "nvidia"])},
    }


@router.get("/api/device-profiles")
def device_profiles() -> dict:
    return {"profiles": device_profile.profiles()}


# ------------------------------------------------------------------------------- config

@router.get("/api/config")
def config_get(request: Request) -> dict:
    return {"config": farm_of(request).config.model_dump(mode="json")}


@router.get("/api/config/schema")
def config_schema() -> dict:
    return Config.model_json_schema()


class ConfigPatch(Body):
    patch: dict[str, Any] = Field(..., description="Nested settings to change, e.g. {\"android\": {\"fps\": 25}}.")


@router.patch("/api/config")
def config_patch(body: ConfigPatch, request: Request) -> dict:
    farm = farm_of(request)
    old = farm.config
    new = farm.set_config(body.patch)
    effects = config_effects(old, new)
    note(request, keys=effects["changed"][:50])
    request.app.state.reconfigure()
    return {"config": new.model_dump(mode="json"), **effects}


# ------------------------------------------------------------------------------- images

def _version(request: Request, version: str) -> str:
    if version not in farm_of(request).config.android.images:
        raise KeyError(f"Android {version[:32]!r} has no image configured (android.images)")
    return version


@router.get("/api/images")
def images(request: Request) -> dict:
    return {"images": farm_of(request).images.status()}


@router.post("/api/images/{version}/ensure")
def image_ensure(version: str, request: Request):
    farm = farm_of(request)
    version = _version(request, version)
    task = farm.tasks.submit("image-ensure", f"Prepare the Android {version} image",
                             lambda t: farm.images.ensure(version, task=t))
    return accepted(request, task, name=version)


@router.post("/api/images/{version}/build")
def image_build(version: str, request: Request):
    farm = farm_of(request)
    version = _version(request, version)
    task = farm.tasks.submit("image-build", f"Build the Android {version} image",
                             lambda t: farm.images.build(version, task=t))
    return accepted(request, task, name=version)


# -------------------------------------------------------------------------------- tasks

@router.get("/api/tasks")
def tasks(request: Request, active: bool = False, limit: int = 50) -> dict:
    limit = bounded(limit, 1, 500, "limit")
    return {"tasks": [t.to_dict() for t in farm_of(request).tasks.list(limit=limit, active_only=active)]}


@router.get("/api/tasks/{task_id}")
def task(task_id: str, request: Request) -> dict:
    found = farm_of(request).tasks.get(task_id)
    if found is None:
        raise KeyError(f"no task {task_id[:40]!r} (finished tasks are kept for a while, then forgotten)")
    return {"task": found.to_dict()}


@router.post("/api/tasks/{task_id}/cancel")
def task_cancel(task_id: str, request: Request) -> dict:
    farm = farm_of(request)
    found = farm.tasks.get(task_id)
    if found is None:
        raise KeyError(f"no task {task_id[:40]!r}")
    if not farm.tasks.cancel(task_id):
        raise ApiError(409, "conflict", f"task {found.id} has already finished ({found.state})")
    note(request, task=found.id)
    return {"task": found.to_dict()}

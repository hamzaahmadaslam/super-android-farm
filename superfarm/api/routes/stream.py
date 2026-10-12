"""Seeing and touching devices: single frames, the MJPEG stream, broadcast input, native windows."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

from ...stream import NoDesktopError, ScrcpyError
from ..audit import note
from ..auth import require
from ..common import Targets, farm_of, instance_id, resolve_targets

router = APIRouter(dependencies=[Depends(require)])

BOUNDARY = "sfframe"
_NO_STORE = {"Cache-Control": "no-store"}


# ------------------------------------------------------------------------------- screen

@router.get("/api/stream/capabilities")
def capabilities(request: Request) -> dict:
    """Which ways of watching a device this host offers, so the panel can pick one."""
    farm = farm_of(request)
    return {"h264": farm.h264.capability(), "windows": farm.windows.available(), "screen": farm.screen.stats()}


@router.get("/api/instances/{id}/frame.jpg")
def frame(id: str, request: Request, width: int | None = None, max_age: float = 1.0) -> Response:
    farm = farm_of(request)
    n = instance_id(farm, id)
    data = farm.screen.frame(n, width=width, max_age=min(30.0, max(0.0, max_age)))
    return Response(data, media_type="image/jpeg", headers=_NO_STORE)


@router.get("/api/instances/{id}/screen")
def screen_status(id: str, request: Request) -> dict:
    farm = farm_of(request)
    return farm.screen.status(instance_id(farm, id))


class _Mjpeg(StreamingResponse):
    """A stream whose source is closed the moment the response ends, however it ends.

    The source is ScreenHub.stream(), which counts as a viewer until its generator is closed.
    Left to garbage collection that would still happen, but only eventually; a viewer slot
    (and the device's capture loop) must be given back as soon as the browser goes away."""

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            await self.body_iterator.aclose()      # type: ignore[attr-defined]


@router.get("/api/instances/{id}/stream.mjpg")
async def mjpeg(id: str, request: Request, fps: float | None = None, width: int | None = None):
    farm = farm_of(request)
    n = await run_in_threadpool(instance_id, farm, id)
    hub = await run_in_threadpool(lambda: farm.screen)
    source = hub.stream(n, fps, width)
    try:
        # The first frame is taken before the response starts: a full house (ViewerLimit)
        # can then still be answered with a proper error instead of a stream that just ends.
        first = await source.__anext__()
    except StopAsyncIteration:
        first = None
    except BaseException:
        await source.aclose()
        raise

    async def parts():
        try:
            if first is not None:
                yield _part(first)
                async for jpeg in source:
                    yield _part(jpeg)
        finally:
            await source.aclose()

    return _Mjpeg(parts(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}", headers=_NO_STORE)


def _part(jpeg: bytes) -> bytes:
    return (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n").encode() + jpeg + b"\r\n"


# -------------------------------------------------------------------------------- input

class Tap(Targets):
    x: float
    y: float
    normalized: bool = Field(True, description="Coordinates are 0..1 of the screen, so one gesture fits devices of any resolution.")


class Swipe(Targets):
    x1: float
    y1: float
    x2: float
    y2: float
    ms: int = Field(300, ge=1, le=30000)
    normalized: bool = True


class LongPress(Tap):
    ms: int = Field(700, ge=1, le=30000)


class Text(Targets):
    value: str = Field(..., min_length=1, max_length=10000)


class Key(Targets):
    name: str | int = Field(..., description="A key name (HOME, BACK, ENTER, ...) or an Android key code.")


def _results(results: dict[int, dict]) -> dict:
    return {"ok": all(r.get("ok") for r in results.values()), "results": {str(k): v for k, v in results.items()}}


@router.post("/api/input/tap")
def input_tap(body: Tap, request: Request) -> dict:
    farm = farm_of(request)
    ids = resolve_targets(request, farm, body)
    return _results(farm.input.tap(ids, body.x, body.y, normalized=body.normalized))


@router.post("/api/input/swipe")
def input_swipe(body: Swipe, request: Request) -> dict:
    farm = farm_of(request)
    ids = resolve_targets(request, farm, body)
    return _results(farm.input.swipe(ids, body.x1, body.y1, body.x2, body.y2, body.ms, normalized=body.normalized))


@router.post("/api/input/long-press")
def input_long_press(body: LongPress, request: Request) -> dict:
    farm = farm_of(request)
    ids = resolve_targets(request, farm, body)
    return _results(farm.input.long_press(ids, body.x, body.y, body.ms, normalized=body.normalized))


@router.post("/api/input/text")
def input_text(body: Text, request: Request) -> dict:
    farm = farm_of(request)
    ids = resolve_targets(request, farm, body)
    return _results(farm.input.text(ids, body.value))


@router.post("/api/input/key")
def input_key(body: Key, request: Request) -> dict:
    farm = farm_of(request)
    ids = resolve_targets(request, farm, body)
    return _results(farm.input.key(ids, body.name))


# ------------------------------------------------------------------------------ windows

@router.get("/api/windows")
def windows(request: Request) -> dict:
    w = farm_of(request).windows
    return {"running": w.running(), "available": w.available()}


@router.post("/api/windows/close-all")
def windows_close_all(request: Request) -> dict:
    farm_of(request).windows.close_all()
    return {"ok": True, "running": []}


@router.post("/api/windows/{action}")
def windows_action(action: Literal["open", "close", "grid"], body: Targets, request: Request) -> dict:
    farm = farm_of(request)
    ids = resolve_targets(request, farm, body)
    note(request, action=action)
    w = farm.windows
    if action == "grid":
        return w.grid(ids)
    results: dict[str, dict] = {}
    if action == "close":
        for i in ids:
            w.close(i)
            results[str(i)] = {"ok": True}
        return {"ok": True, "results": results, "running": w.running()}
    for i in ids:
        try:
            results[str(i)] = {"ok": True, "detail": "", **w.open(i)}
        except NoDesktopError:
            raise                       # no desktop session at all: the same answer for every device
        except ScrcpyError as e:
            results[str(i)] = {"ok": False, "detail": str(e)[:300]}
    return {"ok": all(r["ok"] for r in results.values()), "results": results, "running": w.running()}

"""Virtual camera and microphone, the media library, and listening to one device."""
from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field, model_validator
from starlette.concurrency import run_in_threadpool

from ...media import feeds
from ...util.files import inside
from ..audit import note
from ..auth import require
from ..common import Body, Targets, confined, farm_of, instance_id, resolve_targets
from ..uploads import receive_upload

router = APIRouter(dependencies=[Depends(require)])

MEDIA_TYPES = feeds.VIDEO_EXT | feeds.AUDIO_EXT


class Play(Targets):
    source: str = Field(..., min_length=1, max_length=200, description="A file name from the media library, or `rtmp` for the live ingest.")
    sound: str = Field("source", max_length=200, description="source | none | a library file whose sound plays next to the picture.")


class Mic(Targets):
    mode: Literal["mute", "unmute", "solo"]


class OptionalTargets(Body):
    """ids or a selector; neither means every Android."""
    ids: list[int] | None = Field(None, max_length=500)
    selector: str | None = Field(None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _at_most_one(self) -> OptionalTargets:
        if self.ids is not None and self.selector is not None:
            raise ValueError("give `ids` or `selector`, not both")
        return self


def _optional_ids(request: Request, farm, body: OptionalTargets | None) -> list[int] | None:
    if body is None or (body.ids is None and body.selector is None):
        return None
    return resolve_targets(request, farm, Targets(ids=body.ids, selector=body.selector))


@router.get("/api/camera")
def camera(request: Request, android: bool = False) -> dict:
    cam = farm_of(request).camera
    return {"available": cam.available(), "status": cam.status(android=android)}


@router.get("/api/camera/sources")
def camera_sources(request: Request) -> dict:
    return {"sources": farm_of(request).camera.sources()}


@router.post("/api/camera/play")
def camera_play(body: Play, request: Request) -> dict:
    farm = farm_of(request)
    ids = resolve_targets(request, farm, body)
    note(request, name=body.source)
    return farm.camera.play(ids, body.source, sound=body.sound)


@router.post("/api/camera/stop")
def camera_stop(request: Request, body: OptionalTargets | None = None) -> dict:
    farm = farm_of(request)
    return farm.camera.stop(_optional_ids(request, farm, body))


@router.post("/api/camera/mic")
def camera_mic(body: Mic, request: Request) -> dict:
    farm = farm_of(request)
    # An empty list with `unmute` means every Android (Camera.mic's own rule).
    ids = resolve_targets(request, farm, body, allow_empty=body.mode == "unmute")
    note(request, action=body.mode)
    return farm.camera.mic(ids, body.mode)


@router.post("/api/camera/attach")
def camera_attach(body: Targets, request: Request) -> dict:
    farm = farm_of(request)
    return farm.camera.attach(resolve_targets(request, farm, body))


@router.post("/api/camera/install-hal")
def camera_install_hal(body: Targets, request: Request) -> dict:
    farm = farm_of(request)
    return farm.camera.install_hal(resolve_targets(request, farm, body))


# ------------------------------------------------------------------------ media library

@router.post("/api/media", status_code=201)
async def media_upload(request: Request) -> dict:
    """Add a video or sound file to the media library (the camera's sources)."""
    farm = farm_of(request)
    upload = await receive_upload(request, farm, default_name="")
    try:
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in MEDIA_TYPES:
            raise ValueError("unsupported media type; the library takes " + " ".join(sorted(MEDIA_TYPES)))
        farm.paths.media.mkdir(parents=True, exist_ok=True)
        target = inside(farm.paths.media, farm.paths.media / upload.filename)
        # Same file system (both under the farm home), so this is one atomic rename.
        await run_in_threadpool(os.replace, upload.path, target)
        os.chmod(target, 0o640)
    finally:
        upload.discard()
    note(request, name=upload.filename)
    return {"name": upload.filename, "size": upload.size}


@router.delete("/api/media/{name}")
def media_delete(name: str, request: Request) -> dict:
    farm = farm_of(request)
    path = confined(farm.paths.media, name, "media file")
    # Only what the library lists (Camera.sources): a media file directly in the folder.
    if (path.parent != farm.paths.media.resolve() or path.name.startswith(".")
            or path.suffix.lower() not in MEDIA_TYPES):
        raise KeyError(f"no such media file: {path.name}")
    path.unlink()
    note(request, name=path.name)
    return {"deleted": path.name}


# ------------------------------------------------------------------------------- listen

@router.get("/api/listen")
def listen_status(request: Request) -> dict:
    return farm_of(request).listener.status()


@router.post("/api/listen/{id}")
def listen_start(id: str, request: Request) -> dict:
    farm = farm_of(request)
    n = instance_id(farm, id)
    note(request, ids=[n])
    return farm.listener.start(n)


@router.delete("/api/listen")
def listen_stop(request: Request) -> dict:
    farm_of(request).listener.stop()
    return {"listening": None}

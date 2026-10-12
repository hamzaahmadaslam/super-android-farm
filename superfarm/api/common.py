"""Small helpers shared by the route modules.

Routes are plain `def` functions wherever they call into the farm: FastAPI runs those on a
worker thread, so a slow `docker` or `adb` call never stalls the event loop. The few routes
that must be `async` (uploads, streams) hand blocking work to `run_in_threadpool` themselves.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import JSONResponse

from ..core.models import check_id
from ..core.tasks import Task
from ..farm import Farm
from ..util.files import inside
from .audit import note
from .errors import ApiError

MAX_IDS = 500
_PLAIN_ID = re.compile(r"^[0-9]{1,4}$")
IDEMPOTENCY_HEADER = "idempotency-key"
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")       # printable ASCII, no spaces


class Body(BaseModel):
    """Base of every request body: unknown fields are an error, not silently ignored."""
    model_config = ConfigDict(extra="forbid")


class SelectorBody(Body):
    selector: str = Field(..., min_length=1, max_length=500, description="Which Androids: all | running | stopped | tag:x | 5 | 1-10 | comma-separated")


class Targets(Body):
    """Routes that act on devices directly take either explicit ids or a selector."""
    ids: list[int] | None = Field(None, max_length=MAX_IDS)
    selector: str | None = Field(None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _one(self) -> Targets:
        if (self.ids is None) == (self.selector is None):
            raise ValueError("give exactly one of `ids` or `selector`")
        return self


class _Remembered:
    def __init__(self, fingerprint: str, expires: float):
        self.fingerprint, self.expires = fingerprint, expires
        self.lock = threading.Lock()
        self.done, self.answer = False, None


class Idempotency:
    """The answers to requests that carried an `Idempotency-Key` header, so that the same
    request sent again (a retry after a timeout, a double click) gets the first answer
    instead of doing the work a second time.

        same key, same body      the first answer again; `run` reports it as replayed. Sent
                                 at the same moment, the second waits for the first.
        same key, another body   422 idempotency_key_reused
        the first one failed     nothing is remembered: the next request with the key runs

    Kept in memory only (a daemon restart forgets them), for `ttl` seconds, at most `limit`
    keys (the oldest go first). One per app."""

    def __init__(self, ttl: float = 600.0, limit: int = 256):
        self.ttl, self.limit = ttl, limit
        self.clock: Callable[[], float] = time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, _Remembered] = {}

    def run(self, key: str | None, body: BaseModel, fn: Callable[[], Any]) -> tuple[Any, bool]:
        """fn() once per key. Returns (answer, replayed). No key: fn() every time."""
        if key is None:
            return fn(), False
        if not _IDEMPOTENCY_KEY.match(key):
            raise ApiError(400, "bad_request", "Idempotency-Key must be 1 to 128 printable ASCII characters without spaces")
        fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        now = self.clock()
        with self._lock:
            for old in [k for k, e in self._entries.items() if e.expires <= now]:
                del self._entries[old]
            entry = self._entries.get(key)
            if entry is None:
                while len(self._entries) >= self.limit:
                    del self._entries[next(iter(self._entries))]
                entry = self._entries[key] = _Remembered(fingerprint, now + self.ttl)
        if entry.fingerprint != fingerprint:
            raise ApiError(422, "idempotency_key_reused", "this Idempotency-Key was first sent with a different request; "
                                                          "use a new key for a new request")
        with entry.lock:
            if entry.done:
                return entry.answer, True
            try:
                entry.answer = fn()
            except BaseException:
                with self._lock:
                    if self._entries.get(key) is entry:
                        del self._entries[key]
                raise
            entry.done = True
            return entry.answer, False


def farm_of(request: Request) -> Farm:
    return request.app.state.farm


def instance_id(farm: Farm, raw: str | int) -> int:
    """A path parameter as an existing Android's id: 400 when malformed, 404 when unknown."""
    # Stricter than check_id on purpose: int() also takes " 1", "+1", "1_0" and digits of
    # other scripts, and a URL should name an Android in exactly one way.
    if isinstance(raw, str) and not _PLAIN_ID.match(raw):
        raise ValueError(f"bad instance id {raw[:20]!r}")
    n = check_id(raw)
    if not farm.store.exists(n):
        raise KeyError(f"there is no Android {n}")
    return n


def resolve_targets(request: Request, farm: Farm, body: Targets, *, allow_empty: bool = False) -> list[int]:
    if body.ids is not None:
        ids = sorted({check_id(i) for i in body.ids})
        unknown = [i for i in ids if not farm.store.exists(i)]
        if unknown:
            raise KeyError(f"there is no Android {unknown[0]}")
        note(request, ids=ids)
    else:
        ids = farm.instances.select(body.selector or "")
        note(request, selector=body.selector, ids=ids)
    if not ids and not allow_empty:
        raise ValueError("no Android matches")
    return ids


def accepted(request: Request, task: Task, **facts: Any) -> JSONResponse:
    """Long work: 202 with the task, which the caller follows under /api/tasks/{id}."""
    note(request, task=task.id, ids=task.targets, **facts)
    # A quick task may already carry its output (a Path, a dataclass): encode, don't assume.
    return JSONResponse(jsonable_encoder({"task": task.to_dict()}), status_code=202)


def confined(root: Path, name: str, what: str = "file") -> Path:
    """A client-supplied name as a path that provably stays inside root, and exists."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError(f"bad {what} name")
    candidate = Path(name)
    path = inside(root, candidate if candidate.is_absolute() else root / candidate)
    if not path.is_file():
        raise KeyError(f"no such {what}: {candidate.name}")
    return path


def cleanup_after(farm: Farm, task: Task, upload: Any) -> None:
    """Delete an upload's temporary file once the task that reads it has finished."""
    def wait() -> None:
        farm.tasks.wait(task)
        upload.discard()

    threading.Thread(target=wait, name=f"sf-cleanup-{task.id}", daemon=True).start()


def bounded(value: int, low: int, high: int, what: str) -> int:
    if not low <= int(value) <= high:
        raise ApiError(400, "bad_request", f"{what} must be between {low} and {high}")
    return int(value)

"""One error shape for the whole API, and the mapping from the modules' exceptions to it.

    {"error": {"code": "not_found", "message": "there is no Android 7", "detail": ...}}

    400 bad_request      a value the caller sent is wrong (ValueError and friends)
    401 unauthorized     no or wrong credentials          403 forbidden / csrf
    404 not_found        no such Android, app, profile, task, file (KeyError)
    409 conflict         the farm's current state forbids it. `code` says which: busy (when
                         a task holds the Android, `detail` is {"task": id, "ids": [...]}),
                         over_capacity (with `capacity`), needs_force, refused,
                         device_unavailable, screen_offline, ...
    413 too_large        upload over server.max_upload_mb
    422 validation_error the request body does not match the route's schema
        idempotency_key_reused   the Idempotency-Key was first sent with a different request
    429 rate_limited     too many failed logins
    500 internal         a bug; the daemon log has the traceback, the response does not
    502 command_failed   Docker, ADB or a device did not do what was asked
    503 unavailable      a component cannot run on this machine (the message says why)

Routes raise the modules' own exceptions (or let them pass); nothing below a route knows
about HTTP. Messages are the modules' own and are written for the operator.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

log = logging.getLogger("superfarm.api")

_STATUS_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                 405: "method_not_allowed", 409: "conflict", 413: "too_large", 422: "validation_error",
                 429: "rate_limited", 500: "internal", 502: "command_failed", 503: "unavailable", 504: "timeout"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, detail: Any = None,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail, self.headers = status, code, message, detail, headers


def envelope(code: str, message: str, detail: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        error["detail"] = detail
    return {"error": error}


def error_response(status: int, code: str, message: str, detail: Any = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(envelope(code, message, detail), status_code=status, headers=headers)


def _text(exc: BaseException) -> str:
    # str(KeyError("x")) is "'x'": take the message itself.
    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc) or type(exc).__name__


def validation_detail(errors: list[dict]) -> list[dict]:
    """Where and why, never the offending value: a rejected body may hold a password."""
    out = []
    for err in errors:
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        out.append({"field": ".".join(loc), "message": str(err.get("msg", "invalid"))})
    return out


def _summary(detail: list[dict]) -> str:
    first = detail[0] if detail else {"field": "", "message": "invalid request"}
    text = f"{first['field']}: {first['message']}" if first["field"] else first["message"]
    return text + (f" (and {len(detail) - 1} more)" if len(detail) > 1 else "")


def install(app: FastAPI) -> None:
    """Register the handlers. Starlette picks the most specific class, so subclasses listed
    here win over ValueError / RuntimeError further down."""
    from ..control import LibraryError, Refused
    from ..core import instances as instances_mod
    from ..core.adb import AdbError
    from ..core.backup import BackupError
    from ..core.images import ImageError
    from ..core.tasks import TaskBusy
    from ..farm import Unavailable
    from ..stream import H264Error, NoDesktopError, ScreenError, ViewerLimit
    from ..util.proc import CommandError

    def simple(status: int, code: str):
        async def handler(_request: Request, exc: Exception) -> JSONResponse:
            return error_response(status, code, _text(exc))
        return handler

    @app.exception_handler(ApiError)
    async def _api(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.detail, exc.headers)

    @app.exception_handler(HTTPException)
    async def _http(_request: Request, exc: HTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        return error_response(exc.status_code, code, str(exc.detail), headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = validation_detail(list(exc.errors()))
        return error_response(422, "validation_error", _summary(detail), detail)

    @app.exception_handler(ValidationError)
    async def _model_validation(_request: Request, exc: ValidationError) -> JSONResponse:
        # A module validated a spec or a config and said no. Same care with values as above.
        detail = validation_detail(exc.errors(include_input=False, include_url=False, include_context=False))
        return error_response(400, "bad_request", _summary(detail), detail)

    @app.exception_handler(Unavailable)
    async def _unavailable(_request: Request, exc: Unavailable) -> JSONResponse:
        return error_response(503, "unavailable", str(exc), {"component": exc.component, "reason": exc.reason})

    @app.exception_handler(instances_mod.OverCapacity)
    async def _over(_request: Request, exc: Exception) -> JSONResponse:
        # The panel answers this one with "run anyway": the same call again with force.
        body = envelope("over_capacity", _text(exc), _text(exc))
        body["error"]["capacity"] = getattr(exc, "capacity", None)
        return JSONResponse(jsonable_encoder(body), status_code=409)

    @app.exception_handler(TaskBusy)
    async def _busy(_request: Request, exc: TaskBusy) -> JSONResponse:
        # A second request for an Android a task is still working on (a double click).
        return error_response(409, "busy", _text(exc), {"task": exc.task.id, "ids": exc.ids})

    @app.exception_handler(ScreenError)
    async def _screen(_request: Request, exc: ScreenError) -> JSONResponse:
        # offline / booting: the Android is not showing anything yet. error: the capture failed.
        status = 502 if exc.state == "error" else 409
        return error_response(status, f"screen_{exc.state}", _text(exc), {"state": exc.state})

    @app.exception_handler(CommandError)
    async def _command(_request: Request, exc: CommandError) -> JSONResponse:
        if exc.timed_out:
            return error_response(504, "timeout", _text(exc))
        return error_response(502, "command_failed", _text(exc))

    for cls, status, code in (
        (instances_mod.InstanceBusy, 409, "busy"),
        (instances_mod.NeedsForce, 409, "needs_force"),
        (instances_mod.InstanceError, 409, "conflict"),
        (BackupError, 409, "conflict"),
        (ImageError, 400, "bad_request"),
        (Refused, 409, "refused"),
        (LibraryError, 400, "bad_request"),
        (AdbError, 409, "device_unavailable"),
        (ViewerLimit, 503, "viewer_limit"),
        (H264Error, 409, "h264_unavailable"),
        (NoDesktopError, 503, "unavailable"),
        (KeyError, 404, "not_found"),
        (FileNotFoundError, 404, "not_found"),
        (FileExistsError, 409, "conflict"),          # a second create with a name that is taken
        (IsADirectoryError, 400, "bad_request"),
        (NotADirectoryError, 400, "bad_request"),
        (TimeoutError, 504, "timeout"),
        (ValueError, 400, "bad_request"),
        # The modules raise RuntimeError (and subclasses) for "this cannot be done now, and
        # here is what to do about it": a statement about the farm, not a fault in the daemon.
        (RuntimeError, 409, "conflict"),
    ):
        app.add_exception_handler(cls, simple(status, code))


def internal_error_body() -> dict:
    return envelope("internal", "internal error; the daemon log has the details")

"""The HTTP API: `create_app(farm)` returns the FastAPI application.

Everything under /api is JSON (see errors.py for the error shape) and needs authentication
(see auth.py), except /api/health and the login itself. The panel is served from / when it
is installed; it is plain static files and talks to the same origin.

Order of the wrappers around the routes, outermost first:

    Gate    refuses unknown Host headers (DNS rebinding) and oversized JSON bodies, adds the
            security headers to every response, and turns an unexpected exception into a 500
            that says nothing
    CORS    only when server.allowed_origins lists other origins
    Audit   one line per state-changing request, written when its outcome is known

`listen_host` is the address the daemon is bound to. The app keeps it for its whole life and
decides from it, not from the config file, whether callers must authenticate (auth.py).
"""
from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import HTMLResponse, Response
from starlette.routing import Match
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..farm import Farm
from ..paths import PACKAGE_DIR
from . import errors, uploads, ws
from .audit import ANONYMOUS_LOG, AuditLog, AuditMiddleware
from .auth import CSRF_HEADER, Auth, host_allowed, require
from .common import IDEMPOTENCY_HEADER, Idempotency
from .routes import ai, approvals, automation, backups, control, instances, media, net, stream, system

log = logging.getLogger("superfarm.api")

WEB_DIR = PACKAGE_DIR / "web"
# instances last: its `/api/instances/{id}/{action}` would otherwise be asked first about
# the more specific POST routes other modules define under the same prefix.
MODULES = (system, net, control, stream, media, automation, backups, approvals, ai, instances)
SECURITY_HEADERS = [
    # The panel is static files from this origin and nothing else: no inline script, no
    # third-party host, never inside somebody else's frame. data: and blob: cover pictures
    # the page builds itself (thumbnails, video frames).
    (b"content-security-policy", b"default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                                 b"object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
]
# Uploads stream to disk under their own limit (server.max_upload_mb), after authentication.
# Every other request body is JSON that the framework reads into memory before the route,
# and so before the caller has been authenticated: it gets a small fixed bound.
UPLOAD_PATHS = frozenset({"/api/apps/library", "/api/files/push", "/api/files/gallery", "/api/net/profiles", "/api/media"})
MAX_JSON_BODY = 4 * 1024 * 1024
_NO_PANEL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Super Farm</title></head>
<body><h1>Super Farm</h1>
<p>The daemon is running, but the web panel is not installed in this copy.</p>
<p>The API is available under <code>/api</code>; <code>sf --help</code> lists what the command line can do.</p>
</body></html>
"""


class Gate:
    """Pure ASGI wrapper around the whole app (HTTP and WebSocket)."""

    def __init__(self, app, farm: Farm, listen_host: str | None = None):
        self.app, self.farm, self.listen_host = app, farm, listen_host

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"), "")
        if not host_allowed(host, self.farm.config, self.listen_host):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self._plain(send, 403, errors.envelope(
                    "forbidden", "this host name is not one the daemon answers to (server.host / allowed_origins)"))
            return
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        started = False
        api = scope["path"].startswith("/api/")
        if api and scope["path"] not in UPLOAD_PATHS:
            declared = next((v for k, v in scope.get("headers", []) if k == b"content-length"), b"")
            if declared.isdigit() and int(declared) > MAX_JSON_BODY:
                await self._plain(send, 413, _TOO_LARGE)
                return
            receive = _bounded(receive, MAX_JSON_BODY)

        async def guarded(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message.get("headers", []) if k not in _SECURITY_NAMES]
                headers += SECURITY_HEADERS
                if api and not any(k == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, guarded)
        except ClientDisconnect:
            return          # the browser closed a stream or gave up on an upload: nobody to answer, nothing wrong
        except Exception:  # noqa: BLE001 - last line of defence: log it, tell the caller nothing internal
            log.exception("unhandled error in %s %s", scope.get("method"), scope.get("path"))
            if started:
                raise
            await self._plain(send, 500, errors.internal_error_body())

    @staticmethod
    async def _plain(send, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                                (b"cache-control", b"no-store"), *SECURITY_HEADERS]})
        await send({"type": "http.response.body", "body": data})


_SECURITY_NAMES = {k for k, _ in SECURITY_HEADERS}
_TOO_LARGE = errors.envelope("too_large", f"the request body is larger than {MAX_JSON_BODY // (1024 * 1024)} MB")


def _bounded(receive, limit: int):
    """`receive`, refusing to hand over more than `limit` bytes of request body (a body sent
    in chunks announces no length to check up front)."""
    seen = 0

    async def bounded():
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                raise HTTPException(413, _TOO_LARGE["error"]["message"])
        return message
    return bounded


def create_app(farm: Farm, *, web_dir: Path | None = None, listen_host: str | None = None) -> FastAPI:
    config = farm.config
    audit, audit_anonymous = (
        AuditLog(farm.paths.logs / name, max_bytes=config.logging.max_mb * 1024 * 1024,
                 keep=config.logging.keep, enabled=config.server.audit_log)
        for name in ("audit.log", ANONYMOUS_LOG))
    events = ws.EventHub(farm)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        await run_in_threadpool(uploads.sweep, farm)
        try:
            yield
        finally:
            # Sessions that belong to this event loop end here; Farm.close() does the rest.
            await events.close()
            built = getattr(farm, "_built", {})
            if "h264" in built:
                with contextlib.suppress(Exception):
                    await built["h264"].close_all()
            audit.close()
            audit_anonymous.close()

    # The interactive API pages load scripts from a CDN, which this server's own content
    # policy forbids (and a farm may be offline). The schema itself stays available.
    app = FastAPI(title="Super Farm", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.farm = farm
    app.state.auth = Auth(farm, listen_host)
    app.state.audit = audit
    app.state.audit_anonymous = audit_anonymous
    app.state.idempotency = Idempotency()
    app.state.events = events

    def reconfigure() -> None:
        """Called after the config changed: settings this layer keeps a copy of."""
        c = farm.config
        for each in (audit, audit_anonymous):
            each.configure(max_bytes=c.logging.max_mb * 1024 * 1024, keep=c.logging.keep, enabled=c.server.audit_log)

    app.state.reconfigure = reconfigure

    errors.install(app)

    # add_middleware puts each new one outside the previous: Audit innermost, Gate outermost.
    # Inside Audit: it names the AI caller (if any) on the line Audit writes afterwards.
    app.add_middleware(approvals.CallerMiddleware)
    app.add_middleware(AuditMiddleware, audit=audit, anonymous=audit_anonymous)
    if config.server.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(config.server.allowed_origins),
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["authorization", "content-type", CSRF_HEADER, IDEMPOTENCY_HEADER],
                           expose_headers=["idempotency-replayed"], max_age=600)
    app.add_middleware(Gate, farm=farm, listen_host=listen_host)

    app.include_router(system.public)
    for module in MODULES:
        app.include_router(module.router)
    app.include_router(ws.router)

    @app.get("/api/openapi.json", include_in_schema=False, dependencies=[Depends(require)])
    def _openapi() -> dict:
        return app.openapi()

    # Collected from the routers themselves: how an app stores included routes is FastAPI's
    # business and has changed between releases.
    api_routes = [r for router in (system.public, *(m.router for m in MODULES)) for r in router.routes
                  if isinstance(r, APIRoute)]

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def _unknown_api(request: Request) -> Response:
        # Without this an unknown /api path would fall through to the panel's files below.
        # Reaching it means no route took the request: a known path with the wrong method
        # is a 405, anything else a 404.
        allowed = sorted({m for r in api_routes if r.matches(request.scope)[0] == Match.PARTIAL for m in r.methods})
        if allowed:
            return errors.error_response(405, "method_not_allowed", "this route does not take " + request.method,
                                         headers={"Allow": ", ".join(allowed)})
        return errors.error_response(404, "not_found", "no such API route")

    root = Path(web_dir) if web_dir is not None else WEB_DIR
    if (root / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(root), html=True), name="panel")
    else:
        @app.get("/", include_in_schema=False)
        async def _no_panel() -> HTMLResponse:
            return HTMLResponse(_NO_PANEL)

    return app

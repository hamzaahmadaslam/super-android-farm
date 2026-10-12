"""The audit log: one JSON line per request that changed, or tried to change, something.

    {"time": "2025-01-01T12:00:00Z", "client": "127.0.0.1", "auth": "session", "method": "POST",
     "path": "/api/instances/bulk", "status": 202, "outcome": "ok", "ms": 12,
     "selector": "tag:eu", "ids": [1, 2], "task": "t7"}

What is recorded is who, what and how it ended. Request bodies and query strings are never
written: they are where passwords, tokens and typed text travel. A route adds the few facts
worth keeping (which Androids, which task) with note().

The file is logs/audit.log, owner-only, rotated by size (logging.max_mb, logging.keep).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from starlette.requests import Request

from .auth import SAFE_METHODS

log = logging.getLogger("superfarm.api")

# Facts a route may attach. Anything else is dropped, so a careless note() cannot leak a body.
NOTE_KEYS = ("selector", "ids", "task", "name", "action", "archive", "package", "app", "count", "keys", "force")
_MAX_IDS = 200


class _OwnerOnlyHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream

    def handleError(self, record) -> None:
        # The default prints a traceback to stderr. A full disk must not turn every request
        # into an error, but the operator has to be able to find out the audit trail stopped.
        log.error("audit log could not be written: %s", sys.exc_info()[1])


class AuditLog:
    def __init__(self, path: Path, *, max_bytes: int, keep: int, enabled: bool = True):
        self.path, self.enabled = Path(path), enabled
        self._lock = threading.Lock()
        self._handler: RotatingFileHandler | None = None
        self._max_bytes, self._keep = int(max_bytes), int(keep)

    def configure(self, *, max_bytes: int, keep: int, enabled: bool) -> None:
        with self._lock:
            if (max_bytes, keep) != (self._max_bytes, self._keep) and self._handler is not None:
                self._handler.close()
                self._handler = None
            self._max_bytes, self._keep, self.enabled = int(max_bytes), int(keep), enabled

    def write(self, entry: dict[str, Any]) -> None:
        if not self.enabled:
            return
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)
        try:
            with self._lock:
                if self._handler is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._handler = _OwnerOnlyHandler(self.path, maxBytes=self._max_bytes, backupCount=self._keep,
                                                      encoding="utf-8", delay=True)
                    self._handler.setFormatter(logging.Formatter("%(message)s"))
                handler = self._handler
            handler.handle(logging.makeLogRecord({"msg": line, "levelno": logging.INFO, "levelname": "INFO"}))
        except Exception:  # noqa: BLE001 - a full disk must not turn every request into an error
            log.exception("audit log could not be written")

    def close(self) -> None:
        with self._lock:
            if self._handler is not None:
                self._handler.close()
                self._handler = None


def note(request: Request, **facts: Any) -> None:
    """Attach facts about this request to its audit line (see NOTE_KEYS)."""
    kept = request.scope.setdefault("state", {}).setdefault("audit", {})
    for key, value in facts.items():
        if key not in NOTE_KEYS or value is None:
            continue
        if key == "ids":
            value = [int(i) for i in list(value)[:_MAX_IDS]]
        elif isinstance(value, str):
            value = value[:200]
        kept[key] = value


def outcome_of(status: int) -> str:
    if status < 400:
        return "ok"
    return "denied" if status in (401, 403, 429) else "error"


class AuditMiddleware:
    """Pure ASGI: sees the final status of every state-changing HTTP request."""

    def __init__(self, app, audit: AuditLog):
        self.app, self.audit = app, audit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        began = time.monotonic()
        status = 500        # what the caller gets when the app raises before answering
        state = scope.setdefault("state", {})

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            identity = state.get("identity")
            client = scope.get("client")
            entry = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "client": client[0] if client else "unknown",
                "auth": getattr(identity, "method", "anonymous"),
                "method": scope["method"], "path": scope["path"], "status": status,
                "outcome": outcome_of(status), "ms": int((time.monotonic() - began) * 1000),
                **state.get("audit", {}),
            }
            self.audit.write(entry)

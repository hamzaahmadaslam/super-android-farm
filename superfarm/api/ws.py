"""WebSockets: live events for the panel, and the H.264 view of one device.

Authentication never uses the URL (a token in a query string ends up in logs, history and
referrers). A socket is accepted when the handshake carries the session cookie or a bearer
header; otherwise the first message must be `{"auth": "<token>"}` within a few seconds.
A socket lives as long as what let it in: every few seconds (AUTH_RECHECK) it asks whether
its token or session still stands and is closed with 1008 when it does not (the token was
replaced, the session logged out or expired).

/api/events sends at most a few messages per second:

    {"type": "hello"}                                   once, after authentication
    {"type": "update", "tasks": [...], "instances": [...], "removed": [ids]}
                                                        plus "approvals": [...] when an AI approval
                                                        request was created or decided (GET /api/approvals)
    {"type": "resync"}                                  this client fell behind: fetch state again

It is a convenience: everything it reports can also be read with GET /api/tasks and
GET /api/instances, and a panel that only polls works just as well.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from starlette.concurrency import run_in_threadpool

from .auth import Identity, origin_allowed
from .common import instance_id
from .errors import ApiError

log = logging.getLogger("superfarm.api")
router = APIRouter()

AUTH_TIMEOUT = 10.0            # seconds an unauthenticated socket may stay open
AUTH_RECHECK = 5.0             # seconds between checks that an open socket's credential still stands
TASK_INTERVAL = 0.5            # tasks live in memory: cheap to compare often
INSTANCE_EVERY = 4             # instances cost one `docker ps`: every 4th round (2 s)
QUEUE = 32
POLICY_VIOLATION = 1008


async def authenticate(ws: WebSocket) -> Identity | None:
    """Accept the socket and establish who is on it. None when it was closed instead."""
    auth = ws.app.state.auth
    if "token" in ws.query_params or "auth" in ws.query_params:
        await ws.close(code=POLICY_VIOLATION, reason="credentials are not accepted in the URL")
        return None
    if not origin_allowed(ws, auth.config):
        await ws.close(code=POLICY_VIOLATION, reason="cross-origin WebSocket refused")
        return None
    try:
        identity = auth.identify(ws)
        await ws.accept()
        return identity
    except ApiError as e:
        if e.status != 401 or ws.headers.get("authorization"):
            await ws.close(code=POLICY_VIOLATION, reason=e.code)      # wrong bearer, or locked out
            return None
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT)
        token = json.loads(first).get("auth") if first.lstrip().startswith("{") else None
        if not isinstance(token, str) or not token:
            raise ApiError(401, "unauthorized", "expected {\"auth\": \"<token>\"} as the first message")
        client = ws.client.host if ws.client else "unknown"
        auth.check_token(client, token)
        return Identity("token", client, epoch=auth.epoch)
    except ApiError as e:
        await ws.close(code=POLICY_VIOLATION, reason=e.code)
    except (asyncio.TimeoutError, ValueError, AttributeError, KeyError, TypeError):
        # KeyError: the first frame was binary (receive_text has no "text" to return).
        await ws.close(code=POLICY_VIOLATION, reason="unauthorized")
    except WebSocketDisconnect:
        pass
    return None


async def _signed_out(ws: WebSocket, identity: Identity) -> bool:
    """True, with the socket closed, when what let this socket in no longer stands."""
    if ws.app.state.auth.still_valid(identity):
        return False
    with contextlib.suppress(RuntimeError, OSError, WebSocketDisconnect):
        await ws.close(code=POLICY_VIOLATION, reason="signed out")
    return True


# ------------------------------------------------------------------------------- events

def _task_mark(task: dict) -> tuple:
    return (task["state"], task["current"], task["total"], task["message"], len(task["results"]), task["finished"])


def _instance_mark(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str)


class EventHub:
    """Watches tasks and instance states while somebody listens, and tells every listener
    what changed. One watcher for all listeners; nothing runs when nobody is connected.

    A listener is told about every change after its `hello`: the state the watcher compares
    against is read before the first listener is let in, so "fetch everything over HTTP
    after hello, then apply updates" never misses one."""

    def __init__(self, farm) -> None:
        self.farm = farm
        self._subscribers: set[asyncio.Queue] = set()
        self._watcher: asyncio.Task | None = None
        self._starting = asyncio.Lock()
        self._task_marks: dict[str, tuple] = {}
        self._instance_marks: dict[int, str] = {}
        self._approval_marks: dict[str, str] = {}

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(QUEUE)
        async with self._starting:
            if self._watcher is None or self._watcher.done():
                await self._poll(instances=True)            # the baseline; nobody is told about it
                self._watcher = asyncio.create_task(self._watch())
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def close(self) -> None:
        self._subscribers.clear()
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watcher
            self._watcher = None

    def _publish(self, message: dict) -> None:
        text = json.dumps(jsonable_encoder(message), separators=(",", ":"))
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                # A stalled tab. Dropping some updates would leave it showing stale state for
                # good, so drop them all and tell it to read the state again when it wakes.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait('{"type":"resync"}')

    def _tasks(self) -> dict[str, dict]:
        return {t.id: t.to_dict() for t in self.farm.tasks.list(limit=200)}

    def _instances(self) -> dict[int, dict]:
        return {row["id"]: row for row in self.farm.instances.list_status()}

    async def _poll(self, *, instances: bool) -> dict[str, Any]:
        """Read the current state, remember it, and return what differs from last time."""
        message: dict[str, Any] = {"type": "update", "tasks": [], "instances": [], "removed": []}
        try:
            tasks = await run_in_threadpool(self._tasks)
            marks = {tid: _task_mark(t) for tid, t in tasks.items()}
            message["tasks"] = [tasks[tid] for tid, mark in marks.items() if self._task_marks.get(tid) != mark]
            self._task_marks = marks
            if instances:
                rows = await run_in_threadpool(self._instances)
                now = {i: _instance_mark(r) for i, r in rows.items()}
                message["instances"] = [rows[i] for i, mark in now.items() if self._instance_marks.get(i) != mark]
                message["removed"] = sorted(set(self._instance_marks) - set(now))
                self._instance_marks = now
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - Docker being away must not end the feed
            log.debug("events: state could not be read: %s", e)
        try:
            # In memory, no I/O. The key is only present when something changed.
            approvals = {a.id: a.to_dict() for a in self.farm.approvals.list(limit=100)}
            states = {i: a["state"] for i, a in approvals.items()}
            changed = [approvals[i] for i, state in states.items() if self._approval_marks.get(i) != state]
            self._approval_marks = states
            if changed:
                message["approvals"] = changed
        except Exception as e:  # noqa: BLE001 - the approvals queue being unavailable must not end the feed
            log.debug("events: approvals could not be read: %s", e)
        return message

    async def _watch(self) -> None:
        rounds = 0
        while self._subscribers:
            await asyncio.sleep(TASK_INTERVAL)
            rounds += 1
            message = await self._poll(instances=rounds % INSTANCE_EVERY == 0)
            if message["tasks"] or message["instances"] or message["removed"] or message.get("approvals"):
                self._publish(message)


async def _until_closed(ws: WebSocket) -> None:
    """Returns when the client goes away. Anything it sends on this socket is ignored."""
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError):
        return


@router.websocket("/api/events")
async def events(ws: WebSocket) -> None:
    identity = await authenticate(ws)
    if identity is None:
        return
    hub: EventHub = ws.app.state.events
    queue = await hub.subscribe()
    closed = asyncio.create_task(_until_closed(ws))
    waiting: asyncio.Task | None = None
    try:
        await ws.send_text('{"type":"hello"}')
        while True:
            waiting = waiting or asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({waiting, closed}, timeout=AUTH_RECHECK, return_when=asyncio.FIRST_COMPLETED)
            if closed in done or await _signed_out(ws, identity):
                return
            if waiting in done:
                await ws.send_text(waiting.result())
                waiting = None
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        hub.unsubscribe(queue)
        closed.cancel()
        if waiting is not None:
            waiting.cancel()


# -------------------------------------------------------------------------------- h264

async def _control(session, message: dict) -> None:
    """One control message from the browser. Coordinates are 0..1 of the video."""
    if not isinstance(message, dict):
        raise ValueError("a control message is a JSON object")
    control = session.control
    kind = message.get("type")
    if kind == "touch":
        await control.touch(str(message.get("action", "")), float(message["x"]), float(message["y"]),
                            pointer=int(message.get("pointer", 0)))
    elif kind == "tap":
        await control.tap(float(message["x"]), float(message["y"]))
    elif kind == "key":
        await control.key(message["name"], str(message.get("action", "press")))
    elif kind == "text":
        await control.text(str(message["value"])[:10000])
    elif kind == "scroll":
        await control.scroll(float(message["x"]), float(message["y"]), float(message.get("dx", 0)),
                             float(message.get("dy", 0)))
    elif kind == "back":
        await control.back()
    elif kind == "reset":
        await control.reset_video()
    else:
        raise ValueError(f"unknown control message type {str(kind)[:30]!r}")


async def _fail(ws: WebSocket, code: str, message: str) -> None:
    try:
        await ws.send_text(json.dumps({"type": "error", "code": code, "message": message}))
        # 1008: the request itself is wrong. 1013: right request, but not possible right now.
        await ws.close(code=POLICY_VIOLATION if code in ("bad_request", "not_found") else 1013, reason=code)
    except (RuntimeError, OSError, WebSocketDisconnect):
        pass


@router.websocket("/api/instances/{id}/h264")
async def h264(ws: WebSocket, id: str) -> None:
    """Server -> client: one JSON `info` message, then binary frames (see stream.h264 for the
    layout). Client -> server: JSON control messages (touch, tap, key, text, scroll, back, reset)."""
    identity = await authenticate(ws)
    if identity is None:
        return
    farm = ws.app.state.farm
    try:
        n = await run_in_threadpool(instance_id, farm, id)
        streams = await run_in_threadpool(lambda: farm.h264)
        capability = await run_in_threadpool(streams.capability)
    except (ValueError, KeyError) as e:
        await _fail(ws, "not_found" if isinstance(e, KeyError) else "bad_request", str(e.args[0] if e.args else e))
        return
    except RuntimeError as e:
        await _fail(ws, "unavailable", str(e))
        return
    if not capability.get("enabled"):
        await _fail(ws, "h264_unavailable", f"{capability.get('reason') or 'H.264 view is off'}. "
                                            f"{capability.get('fix') or ''}".strip())
        return
    try:
        session = await streams.acquire(n)
    except RuntimeError as e:          # H264Error and friends: the message says what to do
        await _fail(ws, "h264_unavailable", str(e))
        return
    subscription = None
    sender: asyncio.Task | None = None
    receiver: asyncio.Task | None = None
    try:
        await ws.send_text(json.dumps({"type": "info", **jsonable_encoder(session.info),
                                       "size": list(session.size), "id": n}))
        subscription = session.subscribe()

        async def pump() -> None:
            async for frame in subscription:
                await ws.send_bytes(frame)

        sender = asyncio.create_task(pump())
        receiver = asyncio.create_task(ws.receive())
        while True:
            done, _ = await asyncio.wait({sender, receiver}, timeout=AUTH_RECHECK, return_when=asyncio.FIRST_COMPLETED)
            if await _signed_out(ws, identity):
                return
            if not done:
                continue
            if sender in done:
                receiver.cancel()
                error = sender.exception() if not sender.cancelled() else None
                if error is None:
                    await _fail(ws, "ended", session.error or "the stream has ended")
                return
            incoming = receiver.result()
            if incoming["type"] == "websocket.disconnect":
                return
            text = incoming.get("text")
            if text:
                try:
                    await _control(session, json.loads(text))
                except (ValueError, KeyError, TypeError) as e:
                    await ws.send_text(json.dumps({"type": "error", "code": "bad_request",
                                                   "message": str(e.args[0] if e.args else e)[:300]}))
                except RuntimeError as e:
                    await _fail(ws, "h264_unavailable", str(e))
                    return
            receiver = asyncio.create_task(ws.receive())
    except (WebSocketDisconnect, OSError, RuntimeError):
        pass
    finally:
        for pending in (sender, receiver):
            if pending is not None:
                pending.cancel()
        if subscription is not None:
            subscription.close()
        await streams.release(n)

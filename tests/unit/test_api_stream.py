"""Frames, the MJPEG stream, broadcast input, native windows, the event feed and the H.264
socket; camera, media library and listening."""
from __future__ import annotations

import asyncio
import io
import json
import time

import pytest
from api_fakes import WS, api
from PIL import Image
from starlette.websockets import WebSocketDisconnect
from stream_fakes import FakeAdb, FakeManager, offline

from superfarm.api import ws as ws_mod
from superfarm.stream import H264Error, Input, NoDesktopError, ScrcpyError, ScreenHub


@pytest.fixture
def a(tmp_path):
    with api(tmp_path, {"stream": {"max_viewers": 2}}) as made:
        made.core.mgr.create(count=3)
        made.devices = {i: FakeAdb() for i in (1, 2, 3)}
        made.fake_mgr = FakeManager(made.devices)
        made.hub = ScreenHub(made.fake_mgr, made.farm.config, idle_grace=0.2, capture_timeout=2.0)
        made.farm.use("screen", made.hub)
        made.farm.use("input", Input(made.fake_mgr, made.farm.config, screen=made.hub))
        yield made


# ------------------------------------------------------------------------------- frames

def test_frame_is_a_jpeg_of_the_device(a):
    r = a.client.get("/api/instances/1/frame.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert Image.open(io.BytesIO(r.content)).size == (48, 80)
    small = a.client.get("/api/instances/1/frame.jpg", params={"width": 32})
    assert Image.open(io.BytesIO(small.content)).size[0] == 32


def test_frame_of_a_device_that_is_off_is_a_409_with_its_state(a):
    offline(a.devices[2])
    r = a.client.get("/api/instances/2/frame.jpg")
    assert r.status_code == 409 and r.json()["error"]["code"] == "screen_offline"
    assert r.json()["error"]["detail"] == {"state": "offline"}


def test_frame_refuses_unknown_ids_and_silly_widths(a):
    assert a.client.get("/api/instances/9/frame.jpg").status_code == 404
    assert a.client.get("/api/instances/x/frame.jpg").status_code == 400
    assert a.client.get("/api/instances/1/frame.jpg", params={"width": -5}).status_code == 400
    assert a.client.get("/api/instances/1/frame.jpg", params={"width": "wide"}).status_code == 422
    assert a.anonymous.get("/api/instances/1/frame.jpg").status_code == 401


def test_screen_status_and_capabilities(a, monkeypatch):
    a.client.get("/api/instances/1/frame.jpg")
    status = a.client.get("/api/instances/1/screen").json()
    assert status["state"] == "live" and status["size"] == [48, 80]
    a.farm.use("h264", FakeStreams(enabled=False))
    a.farm.use("windows", FakeWindows())
    caps = a.client.get("/api/stream/capabilities").json()
    assert caps["h264"]["enabled"] is False and caps["windows"]["ok"] is True and caps["screen"]["max_viewers"] == 2


# -------------------------------------------------------------------------------- mjpeg

def scope_for(a, path: str, *, spec: str = "2.4", query: bytes = b"") -> dict:
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": spec}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query, "root_path": "",
            "headers": [(b"host", b"127.0.0.1"), (b"authorization", f"Bearer {a.token}".encode())],
            "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 80), "state": {}}


async def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_mjpeg_streams_jpeg_parts(a):
    started, bodies, got = {}, [], asyncio.Event()

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        if message["type"] == "http.response.start":
            started.update(status=message["status"], headers=dict(message["headers"]))
        elif message.get("body"):
            bodies.append(message["body"])
            if len(bodies) >= 3:
                got.set()

    request = asyncio.create_task(a.app(scope_for(a, "/api/instances/1/stream.mjpg", query=b"fps=30"), receive, send))
    await asyncio.wait_for(got.wait(), 5)
    assert started["status"] == 200
    assert started["headers"][b"content-type"] == b"multipart/x-mixed-replace; boundary=sfframe"
    assert started["headers"][b"cache-control"] == b"no-store"
    head, _, rest = bodies[0].partition(b"\r\n\r\n")
    assert head.startswith(b"--sfframe\r\nContent-Type: image/jpeg\r\nContent-Length: ")
    length = int(head.rsplit(b": ", 1)[1])
    assert rest[:2] == b"\xff\xd8" and len(rest) == length + 2 and rest.endswith(b"\xff\xd9\r\n")
    assert a.hub.stats()["viewers"] == 1
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert a.hub.stats()["viewers"] == 0


async def test_mjpeg_ends_and_frees_the_viewer_when_the_socket_breaks(a):
    """Newer servers report a vanished client by failing the next write."""
    sent = 0

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        nonlocal sent
        if message.get("body"):
            sent += 1
            if sent > 2:
                raise OSError("connection lost")

    await asyncio.wait_for(a.app(scope_for(a, "/api/instances/1/stream.mjpg", query=b"fps=30"), receive, send), 5)
    assert sent == 3
    assert a.hub.stats()["viewers"] == 0
    assert await wait_until(lambda: a.hub.stats()["capturing"] == 0)        # the device is left alone again


async def test_mjpeg_ends_and_frees_the_viewer_on_a_disconnect_message(a):
    """Older servers say so with an `http.disconnect` event instead."""
    sent, gone = 0, asyncio.Event()

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal sent
        if message.get("body"):
            sent += 1
            if sent == 2:
                gone.set()

    await asyncio.wait_for(a.app(scope_for(a, "/api/instances/1/stream.mjpg", spec="2.3", query=b"fps=30"),
                                 receive, send), 5)
    assert sent >= 2 and a.hub.stats()["viewers"] == 0


async def test_mjpeg_ends_cleanly_when_the_daemon_shuts_down(a):
    messages = []

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        messages.append(message)
        if sum(1 for m in messages if m.get("body")) == 2:
            await asyncio.to_thread(a.hub.close)

    await asyncio.wait_for(a.app(scope_for(a, "/api/instances/1/stream.mjpg", query=b"fps=30"), receive, send), 5)
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


async def test_a_full_house_is_a_503_not_a_broken_stream(a):
    async def receive():
        await asyncio.sleep(3600)

    async def nothing(message):
        pass

    watchers = [asyncio.create_task(a.app(scope_for(a, f"/api/instances/{i}/stream.mjpg"), receive, nothing))
                for i in (1, 2)]
    assert await wait_until(lambda: a.hub.stats()["viewers"] == 2)
    answer = []

    async def send(message):
        answer.append(message)

    await asyncio.wait_for(a.app(scope_for(a, "/api/instances/3/stream.mjpg"), receive, send), 5)
    assert answer[0]["status"] == 503
    assert json.loads(answer[1]["body"])["error"]["code"] == "viewer_limit"
    for w in watchers:
        w.cancel()
    await asyncio.gather(*watchers, return_exceptions=True)
    assert a.hub.stats()["viewers"] == 0


def test_mjpeg_checks_the_id_and_the_caller_before_streaming(a):
    assert a.client.get("/api/instances/9/stream.mjpg").status_code == 404
    assert a.client.get("/api/instances/..%2f1/stream.mjpg").status_code in (400, 404)
    assert a.anonymous.get("/api/instances/1/stream.mjpg").status_code == 401
    assert a.client.get("/api/instances/1/stream.mjpg", params={"width": -5}).status_code == 400
    assert a.hub.stats()["viewers"] == 0


# -------------------------------------------------------------------------------- input

def test_tap_broadcasts_one_normalized_gesture_to_every_device(a):
    a.devices[2].width, a.devices[2].height = 100, 200
    r = a.client.post("/api/input/tap", json={"ids": [1, 2], "x": 0.5, "y": 0.25})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["results"]["1"]["x"] == 24 and body["results"]["2"] == {
        "ok": True, "detail": "", "x": 50, "y": 50, "ms": body["results"]["2"]["ms"]}
    assert a.devices[1].inputs() == [["input", "tap", "24", "20"]] and a.devices[3].inputs() == []


def test_input_accepts_a_selector_instead_of_ids(a):
    r = a.client.post("/api/input/key", json={"selector": "2-3", "name": "HOME"})
    assert set(r.json()["results"]) == {"2", "3"}
    assert a.devices[2].inputs() == [["input", "keyevent", "KEYCODE_HOME"]]


def test_swipe_long_press_and_text(a):
    assert a.client.post("/api/input/swipe", json={"ids": [1], "x1": 0.5, "y1": 0.75, "x2": 0.5, "y2": 0.25, "ms": 200}).json()["ok"]
    assert a.client.post("/api/input/long-press", json={"ids": [1], "x": 0.5, "y": 0.5}).json()["ok"]
    assert a.client.post("/api/input/text", json={"ids": [1], "value": "hello world; reboot"}).json()["results"]["1"]["method"] == "input-text"
    assert a.devices[1].inputs() == [["input", "swipe", "24", "60", "24", "20", "200"],
                                     ["input", "swipe", "24", "40", "24", "40", "700"],
                                     ["input", "text", "hello%sworld;%sreboot"]]     # one argument, never a shell line


def test_one_device_failing_does_not_fail_the_others(a):
    offline(a.devices[2])
    body = a.client.post("/api/input/tap", json={"ids": [1, 2], "x": 0.5, "y": 0.5}).json()
    assert body["ok"] is False and body["results"]["1"]["ok"] and not body["results"]["2"]["ok"]


@pytest.mark.parametrize("route,body,status", [
    ("tap", {"x": 0.5, "y": 0.5}, 422),                                    # neither ids nor selector
    ("tap", {"ids": [1], "selector": "all", "x": 0.5, "y": 0.5}, 422),     # both
    ("tap", {"ids": [1], "x": 1.5, "y": 0.5}, 400),                        # outside the screen
    ("tap", {"ids": [1], "x": "left", "y": 0.5}, 422),
    ("tap", {"ids": [9], "x": 0.5, "y": 0.5}, 404),
    ("tap", {"ids": [99999], "x": 0.5, "y": 0.5}, 400),
    ("tap", {"selector": "tag:none", "x": 0.5, "y": 0.5}, 400),            # matches nothing
    ("tap", {"ids": [1], "x": 0.5, "y": 0.5, "shell": "rm"}, 422),
    ("key", {"ids": [1], "name": "NOT_A_KEY; reboot"}, 400),
    ("text", {"ids": [1], "value": ""}, 422),
    ("swipe", {"ids": [1], "x1": 0, "y1": 0, "x2": 1, "y2": 1, "ms": 0}, 422),
    ("pinch", {"ids": [1]}, 404),
])
def test_input_refuses_bad_requests(a, route, body, status):
    r = a.client.post(f"/api/input/{route}", json=body)
    assert r.status_code == status, r.text
    assert all(d.inputs() == [] for d in a.devices.values())


# ------------------------------------------------------------------------------ windows

class FakeWindows:
    def __init__(self, error: Exception | None = None, broken: tuple[int, ...] = ()):
        self.open_ids: list[int] = []
        self.error, self.broken = error, broken

    def available(self) -> dict:
        return {"ok": self.error is None, "reason": str(self.error or "")}

    def running(self) -> list[int]:
        return sorted(self.open_ids)

    def open(self, id: int) -> dict:
        if self.error:
            raise self.error
        if id in self.broken:
            raise ScrcpyError(f"scrcpy for Android {id} exited at once")
        already = id in self.open_ids
        self.open_ids.append(id) if not already else None
        return {"id": id, "pid": 1000 + id, "already_open": already}

    def close(self, id: int) -> None:
        if id in self.open_ids:
            self.open_ids.remove(id)

    def close_all(self) -> None:
        self.open_ids.clear()

    def grid(self, ids: list[int]) -> dict:
        self.open_ids = list(ids)
        return {"screen": [1920, 1080], "cols": len(ids), "rows": 1, "opened": len(ids),
                "results": {i: {"ok": True, "detail": ""} for i in ids}}


def test_windows_open_grid_close(a):
    windows = FakeWindows(broken=(3,))
    a.farm.use("windows", windows)
    body = a.client.post("/api/windows/open", json={"ids": [1, 3]}).json()
    assert body["ok"] is False and body["results"]["1"]["pid"] == 1001 and "exited at once" in body["results"]["3"]["detail"]
    assert a.client.get("/api/windows").json() == {"running": [1], "available": {"ok": True, "reason": ""}}
    grid = a.client.post("/api/windows/grid", json={"selector": "1-2"}).json()
    assert grid["opened"] == 2 and set(grid["results"]) == {"1", "2"}
    assert a.client.post("/api/windows/close", json={"ids": [1]}).json()["running"] == [2]
    assert a.client.post("/api/windows/close-all").json() == {"ok": True, "running": []}
    assert a.client.post("/api/windows/close-all", json={}).status_code == 200       # clients that always send a body
    assert a.client.post("/api/windows/minimize", json={"ids": [1]}).status_code == 422
    assert a.client.post("/api/windows/open", json={}).status_code == 422


def test_windows_without_a_desktop_are_unavailable(a):
    a.farm.use("windows", FakeWindows(error=NoDesktopError("the daemon has no desktop session (no DISPLAY)")))
    r = a.client.post("/api/windows/open", json={"ids": [1]})
    assert r.status_code == 503 and "no desktop session" in r.json()["error"]["message"]


# ------------------------------------------------------------------------------- events

@pytest.fixture
def fast_events(monkeypatch):
    monkeypatch.setattr(ws_mod, "TASK_INTERVAL", 0.02)
    monkeypatch.setattr(ws_mod, "INSTANCE_EVERY", 2)


def next_update(ws) -> dict:
    return json.loads(ws.receive_text())


def test_events_push_task_and_instance_changes(a, fast_events):
    with a.client.websocket_connect(WS + "/api/events") as ws:
        assert json.loads(ws.receive_text()) == {"type": "hello"}
        task = a.farm.tasks.submit("test", "a job", lambda t: "done")
        seen = next_update(ws)
        assert seen["type"] == "update" and [t["id"] for t in seen["tasks"]] == [task.id]
        a.core.mgr.start(1)
        states: dict[int, str] = {}
        deadline = time.monotonic() + 5
        while states.get(1) != "booted" and time.monotonic() < deadline:
            states.update({row["id"]: row["state"] for row in next_update(ws)["instances"]})
        assert states == {1: "booted"}                     # only the Android that changed is sent
        a.core.mgr.delete(3)
        removed: list[int] = []
        while not removed and time.monotonic() < deadline:
            removed = next_update(ws)["removed"]
        assert removed == [3]


def test_events_say_nothing_about_what_existed_before_hello(a, fast_events):
    a.farm.tasks.wait(a.farm.tasks.submit("test", "old job", lambda t: None), 5)
    with a.client.websocket_connect(WS + "/api/events") as ws:
        ws.receive_text()
        fresh = a.farm.tasks.submit("test", "new job", lambda t: None)
        assert [t["id"] for t in next_update(ws)["tasks"]] == [fresh.id]


def test_events_coalesce_a_burst_into_one_message(a, monkeypatch):
    monkeypatch.setattr(ws_mod, "TASK_INTERVAL", 0.3)
    with a.client.websocket_connect(WS + "/api/events") as ws:
        ws.receive_text()
        made = [a.farm.tasks.submit("test", f"job {n}", lambda t: None) for n in range(6)]
        for task in made:
            a.farm.tasks.wait(task, 5)
        seen = next_update(ws)
        assert sorted(t["id"] for t in seen["tasks"]) == sorted(t.id for t in made)
        assert all(t["state"] == "done" for t in seen["tasks"])      # six tasks, twelve state changes, one message


def test_events_watcher_stops_when_nobody_listens(a, fast_events):
    hub = a.app.state.events
    with a.client.websocket_connect(WS + "/api/events") as ws:
        ws.receive_text()
        assert len(hub._subscribers) == 1 and not hub._watcher.done()
    deadline = time.monotonic() + 3
    while (hub._subscribers or not hub._watcher.done()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not hub._subscribers and hub._watcher.done()


async def test_a_listener_that_falls_behind_is_told_to_resync(a):
    hub = ws_mod.EventHub(a.farm)
    slow = asyncio.Queue(ws_mod.QUEUE)
    hub._subscribers.add(slow)
    for n in range(ws_mod.QUEUE + 1):
        hub._publish({"type": "update", "tasks": [{"n": n}], "instances": [], "removed": []})
    assert slow.qsize() == 1 and json.loads(slow.get_nowait()) == {"type": "resync"}


def test_polling_alone_shows_the_same_state(a):
    task = a.farm.tasks.submit("test", "a job", lambda t: None)
    a.farm.tasks.wait(task, 5)
    assert a.client.get(f"/api/tasks/{task.id}").json()["task"]["state"] == "done"
    assert len(a.client.get("/api/instances").json()["instances"]) == 3


# --------------------------------------------------------------------------------- h264

class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def touch(self, action, x, y, *, pointer=0):
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("touch coordinates must be within 0..1")
        self.calls.append(("touch", action, x, y, pointer))

    async def tap(self, x, y):
        self.calls.append(("tap", x, y))

    async def key(self, name, action="press"):
        self.calls.append(("key", name, action))

    async def text(self, value):
        self.calls.append(("text", value))

    async def scroll(self, x, y, dx, dy):
        self.calls.append(("scroll", x, y, dx, dy))

    async def back(self):
        self.calls.append(("back",))

    async def reset_video(self):
        self.calls.append(("reset",))
        return True


class FakeSubscription:
    def __init__(self, frames: list[bytes], *, then_end: bool):
        self.frames, self.then_end, self.closed = list(frames), then_end, False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self.frames:
            return self.frames.pop(0)
        if self.then_end:
            raise StopAsyncIteration
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    def close(self) -> None:
        self.closed = True


class FakeSession:
    info = {"codec": "avc1.42c01f", "width": 48, "height": 80, "version": "3.3"}
    size = (48, 80)
    error = ""

    def __init__(self, frames: list[bytes], then_end: bool):
        self.control = FakeControl()
        self.subscription = FakeSubscription(frames, then_end=then_end)

    def subscribe(self) -> FakeSubscription:
        return self.subscription


class FakeStreams:
    def __init__(self, *, enabled: bool = True, frames: list[bytes] | None = None, then_end: bool = False,
                 error: Exception | None = None):
        self.enabled, self.error = enabled, error
        self.session = FakeSession(frames or [b"\x03frame-one", b"\x00frame-two"], then_end)
        self.acquired: list[int] = []
        self.released: list[int] = []

    def capability(self) -> dict:
        return {"enabled": self.enabled, "available": self.enabled, "reason": "" if self.enabled else "scrcpy-server was not found",
                "fix": "" if self.enabled else "install the scrcpy package"}

    async def acquire(self, id: int) -> FakeSession:
        if self.error:
            raise self.error
        self.acquired.append(id)
        return self.session

    async def release(self, id: int) -> None:
        self.released.append(id)


def test_h264_relays_binary_frames_and_takes_control_messages(a):
    streams = FakeStreams()
    a.farm.use("h264", streams)
    with a.client.websocket_connect(WS + "/api/instances/2/h264") as ws:
        info = json.loads(ws.receive_text())
        assert info == {"type": "info", "codec": "avc1.42c01f", "width": 48, "height": 80, "version": "3.3",
                        "size": [48, 80], "id": 2}
        assert ws.receive_bytes() == b"\x03frame-one" and ws.receive_bytes() == b"\x00frame-two"
        for message in ({"type": "touch", "action": "down", "x": 0.5, "y": 0.25, "pointer": 1},
                        {"type": "tap", "x": 0.1, "y": 0.9}, {"type": "key", "name": "BACK"},
                        {"type": "text", "value": "héllo"}, {"type": "scroll", "x": 0.5, "y": 0.5, "dy": -1},
                        {"type": "back"}, {"type": "reset"}):
            ws.send_text(json.dumps(message))
        ws.send_text(json.dumps({"type": "touch", "action": "down", "x": 7, "y": 0}))      # refused, socket stays open
        assert json.loads(ws.receive_text())["code"] == "bad_request"
        ws.send_text(json.dumps({"type": "format-device"}))
        assert "unknown control message" in json.loads(ws.receive_text())["message"]
        ws.send_text("not json")
        assert json.loads(ws.receive_text())["code"] == "bad_request"
        ws.send_text(json.dumps({"type": "tap"}))                                          # missing fields
        assert json.loads(ws.receive_text())["code"] == "bad_request"
    assert streams.session.control.calls == [
        ("touch", "down", 0.5, 0.25, 1), ("tap", 0.1, 0.9), ("key", "BACK", "press"), ("text", "héllo"),
        ("scroll", 0.5, 0.5, 0.0, -1.0), ("back",), ("reset",)]
    deadline = time.monotonic() + 3
    while not streams.released and time.monotonic() < deadline:
        time.sleep(0.01)
    assert streams.acquired == [2] and streams.released == [2] and streams.session.subscription.closed


def test_h264_is_only_offered_when_the_capability_allows(a):
    streams = FakeStreams(enabled=False)
    a.farm.use("h264", streams)
    with a.client.websocket_connect(WS + "/api/instances/1/h264") as ws:
        error = json.loads(ws.receive_text())
        assert error["type"] == "error" and error["code"] == "h264_unavailable"
        assert "scrcpy-server was not found" in error["message"] and "install the scrcpy package" in error["message"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert streams.acquired == [] and streams.released == []


def test_h264_reports_a_stream_that_cannot_start_or_that_ends(a):
    a.farm.use("h264", FakeStreams(error=H264Error("H.264 stream did not start: timed out waiting for the device")))
    with a.client.websocket_connect(WS + "/api/instances/1/h264") as ws:
        assert "did not start" in json.loads(ws.receive_text())["message"]
    ended = FakeStreams(frames=[b"\x01only"], then_end=True)
    a.farm.use("h264", ended)
    with a.client.websocket_connect(WS + "/api/instances/1/h264") as ws:
        assert json.loads(ws.receive_text())["type"] == "info"
        assert ws.receive_bytes() == b"\x01only"
        assert json.loads(ws.receive_text())["code"] == "ended"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert ended.released == [1]


def test_h264_checks_the_id_and_the_caller(a):
    streams = FakeStreams()
    a.farm.use("h264", streams)
    for bad, code in (("9", "not_found"), ("abc", "bad_request")):
        with a.client.websocket_connect(f"{WS}/api/instances/{bad}/h264") as ws:
            assert json.loads(ws.receive_text())["code"] == code
    with a.anonymous.websocket_connect(WS + "/api/instances/1/h264") as ws:
        ws.send_text('{"auth": "wrong"}')
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with a.anonymous.websocket_connect(f"{WS}/api/instances/1/h264?token={a.token}"):
            pass
    assert streams.acquired == []


# ------------------------------------------------------------------------ camera / media

class FakeCamera:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def available(self) -> dict:
        return {"enabled": True, "ok": True, "reasons": []}

    def status(self, *, android: bool = False) -> dict:
        return {"feeds": [], "instances": {}, "android": android}

    def sources(self) -> list[dict]:
        return [{"id": "clip.mp4", "kind": "video"}, {"id": "rtmp", "kind": "live"}]

    def play(self, ids, source, *, sound="source") -> dict:
        if source == "missing.mp4":
            raise KeyError("no media file named missing.mp4")
        self.calls.append(("play", ids, source, sound))
        return {"ok": True, "results": {i: {"ok": True} for i in ids}}

    def stop(self, ids=None) -> dict:
        self.calls.append(("stop", ids))
        return {"ok": True, "ids": ids or []}

    def mic(self, ids, mode) -> dict:
        self.calls.append(("mic", ids, mode))
        return {"ok": True}

    def attach(self, ids) -> dict:
        self.calls.append(("attach", ids))
        return {"ok": True}

    def install_hal(self, ids) -> dict:
        self.calls.append(("install_hal", ids))
        return {"ok": True}

    def unwatch(self) -> None:
        pass

    def down(self) -> None:
        pass


def test_camera_routes_call_the_camera_with_resolved_ids(a):
    camera = FakeCamera()
    a.farm.use("camera", camera)
    assert a.client.get("/api/camera").json() == {"available": {"enabled": True, "ok": True, "reasons": []},
                                                  "status": {"feeds": [], "instances": {}, "android": False}}
    assert [s["id"] for s in a.client.get("/api/camera/sources").json()["sources"]] == ["clip.mp4", "rtmp"]
    assert a.client.post("/api/camera/play", json={"selector": "1-2", "source": "clip.mp4", "sound": "none"}).json()["ok"]
    a.client.post("/api/camera/stop", json={"ids": [2]})
    a.client.post("/api/camera/stop")
    a.client.post("/api/camera/stop", json={})
    a.client.post("/api/camera/mic", json={"ids": [1], "mode": "solo"})
    a.client.post("/api/camera/mic", json={"ids": [], "mode": "unmute"})
    a.client.post("/api/camera/attach", json={"selector": "3"})
    a.client.post("/api/camera/install-hal", json={"ids": [3]})
    assert camera.calls == [("play", [1, 2], "clip.mp4", "none"), ("stop", [2]), ("stop", None), ("stop", None),
                            ("mic", [1], "solo"), ("mic", [], "unmute"), ("attach", [3]), ("install_hal", [3])]


@pytest.mark.parametrize("route,body,status", [
    ("play", {"ids": [1]}, 422), ("play", {"ids": [1], "source": "missing.mp4"}, 404),
    ("play", {"ids": [9], "source": "clip.mp4"}, 404), ("mic", {"ids": [1], "mode": "loud"}, 422),
    ("mic", {"ids": [], "mode": "solo"}, 400), ("stop", {"ids": [1], "selector": "all"}, 422),
    ("attach", {}, 422), ("zoom", {"ids": [1]}, 404),
])
def test_camera_routes_refuse_bad_requests(a, route, body, status):
    camera = FakeCamera()
    a.farm.use("camera", camera)
    assert a.client.post(f"/api/camera/{route}", json=body).status_code == status
    assert camera.calls == []


def test_a_camera_that_cannot_be_built_is_503(a, monkeypatch):
    from superfarm.farm import Farm
    monkeypatch.setitem(Farm._FACTORIES, "camera", lambda self: (_ for _ in ()).throw(ImportError("no v4l2 bindings")))
    a.farm._built.pop("camera", None)
    r = a.client.get("/api/camera")
    assert r.status_code == 503 and r.json()["error"]["detail"]["component"] == "camera"
    assert a.client.get("/api/instances").status_code == 200           # the rest of the farm is unaffected


def test_media_upload_lands_in_the_library_under_a_safe_name(a):
    r = a.client.post("/api/media", files={"file": ("../../My Clip (1).MP4", b"not really a video", "video/mp4")})
    assert r.status_code == 201 and r.json() == {"name": "My_Clip_1_.MP4", "size": 18}
    assert [p.name for p in a.farm.paths.media.iterdir()] == ["My_Clip_1_.MP4"]
    assert a.client.post("/api/media", files={"file": ("evil.sh", b"#!/bin/sh", "text/plain")}).status_code == 400
    assert a.client.post("/api/media", files={"file": ("....", b"x", "video/mp4")}).status_code == 400
    assert [p.name for p in a.farm.paths.media.iterdir()] == ["My_Clip_1_.MP4"]
    uploads = a.farm.paths.run / "uploads"
    assert not uploads.exists() or list(uploads.iterdir()) == []
    assert a.client.delete("/api/media/My_Clip_1_.MP4").json() == {"deleted": "My_Clip_1_.MP4"}
    assert a.client.delete("/api/media/My_Clip_1_.MP4").status_code == 404


@pytest.mark.parametrize("name", ["..", "..%2F..%2Fconfig.yaml", "%2e%2e%2fsecrets%2fapi-token", "..%5c..%5cconfig.yaml",
                                  "config.yaml", ".hidden.mp4"])
def test_media_delete_cannot_reach_other_files(a, name):
    (a.farm.paths.media / ".hidden.mp4").write_bytes(b"x") if name == ".hidden.mp4" else None
    (a.farm.paths.home / "config.yaml").touch()
    r = a.client.delete(f"/api/media/{name}")
    assert r.status_code in (400, 404), r.text
    assert (a.farm.paths.home / "config.yaml").exists() and (a.farm.paths.secrets / "api-token").exists()


class FakeListener:
    def __init__(self) -> None:
        self.id: int | None = None

    def status(self) -> dict:
        return {"listening": self.id}

    def start(self, id: int) -> dict:
        self.id = id
        return {"listening": id, "port": 27999}

    def stop(self) -> None:
        self.id = None


def test_listen_to_one_device(a):
    a.farm.use("listener", FakeListener())
    assert a.client.get("/api/listen").json() == {"listening": None}
    assert a.client.post("/api/listen/2").json()["listening"] == 2
    assert a.client.get("/api/listen").json() == {"listening": 2}
    assert a.client.delete("/api/listen").json() == {"listening": None}
    assert a.client.post("/api/listen/9").status_code == 404
    assert a.client.post("/api/listen/abc").status_code == 400

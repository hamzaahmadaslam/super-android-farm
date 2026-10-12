"""The error envelope and the mapping from the modules' exceptions to HTTP statuses."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from superfarm.api import errors
from superfarm.api.errors import ApiError, validation_detail
from superfarm.control import LibraryError, Refused
from superfarm.core.adb import AdbError
from superfarm.core.backup import BackupError
from superfarm.core.images import ImageError
from superfarm.core.instances import InstanceBusy, InstanceError, NeedsForce, OverCapacity, StartError
from superfarm.core.models import InstanceSpec
from superfarm.farm import Unavailable
from superfarm.stream import H264Error, NoDesktopError, ScrcpyError, ScreenError, ViewerLimit
from superfarm.util.proc import CommandError

CASES = [
    (KeyError("there is no Android 7"), 404, "not_found", "there is no Android 7"),
    (FileNotFoundError("/sdcard/x does not exist on Android 1"), 404, "not_found", None),
    (ValueError("bad selector part 'x'"), 400, "bad_request", "bad selector part 'x'"),
    (IsADirectoryError("that is a folder"), 400, "bad_request", None),
    (NotADirectoryError("that is a file"), 400, "bad_request", None),
    (LibraryError("unsupported file type"), 400, "bad_request", None),
    (ImageError("bad Android version name"), 400, "bad_request", None),
    (Refused("refused: wipes data"), 409, "refused", None),
    (InstanceBusy("Android 1 is busy"), 409, "busy", None),
    (NeedsForce("data at risk"), 409, "needs_force", None),
    (InstanceError("stop it first"), 409, "conflict", None),
    (StartError("did not boot"), 409, "conflict", None),
    (BackupError("target must be stopped"), 409, "conflict", None),
    (AdbError("Android 3 does not answer on ADB"), 409, "device_unavailable", None),
    (ScreenError("offline", "the Android is stopped"), 409, "screen_offline", None),
    (ScreenError("booting", ""), 409, "screen_booting", None),
    (ScreenError("error", "capture failed"), 502, "screen_error", None),
    (ViewerLimit("64 live views are already open"), 503, "viewer_limit", None),
    (H264Error("scrcpy-server was not found"), 409, "h264_unavailable", None),
    (NoDesktopError("no desktop session"), 503, "unavailable", None),
    (ScrcpyError("scrcpy exited at once"), 409, "conflict", None),
    (RuntimeError("ffmpeg is not installed"), 409, "conflict", None),
    (Unavailable("camera", "v4l2loopback is not loaded"), 503, "unavailable", None),
    (CommandError(["docker", "ps"], 1, "", "cannot connect to the Docker daemon"), 502, "command_failed", None),
    (CommandError(["adb", "shell"], None, "", "", timed_out=True), 504, "timeout", None),
    (TimeoutError("no result after 60s"), 504, "timeout", None),
    (ApiError(413, "too_large", "too big"), 413, "too_large", "too big"),
    (HTTPException(418, "teapot"), 418, "error", "teapot"),
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str
    count: int = 1


@pytest.fixture
def client():
    app = FastAPI()
    errors.install(app)
    raised: dict = {}
    app.state.raised = raised

    @app.get("/raise")
    def _raise():
        raise raised["exc"]

    @app.post("/body")
    def _body(body: Strict):
        return {}

    @app.get("/spec")
    def _spec():
        InstanceSpec.model_validate({"id": 1, "fps": 9999, "notes": "hunter2-secret"})

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("exc,status,code,message", CASES, ids=[type(c[0]).__name__ + ":" + c[2] for c in CASES])
def test_exception_mapping(client, exc, status, code, message):
    client.app.state.raised["exc"] = exc
    r = client.get("/raise")
    assert r.status_code == status
    error = r.json()["error"]
    assert error["code"] == code
    assert error["message"] and (message is None or error["message"] == message)
    assert set(r.json()) == {"error"}


def test_over_capacity_carries_the_capacity_report(client):
    capacity = {"running": 20, "headroom": 0, "over": True, "estimate": {"max_instances": 20}}
    client.app.state.raised["exc"] = OverCapacity("not starting Android 21: over capacity", capacity)
    r = client.get("/raise")
    assert r.status_code == 409
    assert r.json()["error"] == {"code": "over_capacity", "message": "not starting Android 21: over capacity",
                                 "detail": "not starting Android 21: over capacity", "capacity": capacity}


def test_unavailable_names_the_component(client):
    client.app.state.raised["exc"] = Unavailable("windows", "scrcpy is not installed")
    assert client.get("/raise").json()["error"]["detail"] == {"component": "windows", "reason": "scrcpy is not installed"}


def test_request_validation_never_echoes_values(client):
    r = client.post("/body", json={"password": 12345, "count": "many", "token": "hunter2-secret"})
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert {d["field"] for d in error["detail"]} == {"password", "count", "token"}
    assert "hunter2" not in r.text and "12345" not in r.text and "many" not in r.text


def test_malformed_json_is_422_with_the_envelope(client):
    r = client.post("/body", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_a_module_rejecting_a_model_is_400_without_the_values(client):
    r = client.get("/spec")
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "bad_request" and error["detail"][0]["field"] == "fps"
    assert "hunter2" not in r.text and "9999" not in r.text


def test_unknown_route_and_wrong_method_use_the_envelope(client):
    assert client.get("/nope").json() == {"error": {"code": "not_found", "message": "Not Found"}}
    r = client.post("/raise")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"


def test_validation_detail_drops_the_location_prefix():
    assert validation_detail([{"loc": ("body", "a", 0, "b"), "msg": "bad"}]) == [{"field": "a.0.b", "message": "bad"}]
    assert validation_detail([{"loc": ("query", "tail"), "msg": "bad"}]) == [{"field": "tail", "message": "bad"}]

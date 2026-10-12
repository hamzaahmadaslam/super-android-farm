"""The Pilot as two tools for the registry in tools.py.

Starting a session is one changing action: the operator approves "teach this goal on
these Androids" once, and the taps the Pilot then makes on those Androids are part of
what was approved (they are bounded by the session's own limits and by the app's
never-tap list, not by one approval each). Reading a session's progress is a read.

    Tool("pilot_start", "Teach a task with the AI Pilot", START_DESCRIPTION, START_SCHEMA, CHANGE,
         ("POST /api/ai/pilot",), pilot_start, describe_start, target="selector"),
    Tool("pilot_status", "Progress of a Pilot session", STATUS_DESCRIPTION, STATUS_SCHEMA, READ,
         ("GET /api/ai/pilot/sessions/{id}",), pilot_status),
"""
from __future__ import annotations

from typing import Any

from .safety import device_block, one_line, quote

START_DESCRIPTION = (
    "Start an AI Pilot session: a model operates one app on the selected Androids toward a goal stated in plain "
    "words, and every successful run is saved as a recording. Train the recordings afterwards (they become a "
    "program that replays without any model). Returns the session id; follow it with pilot_status.")
START_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["goal", "package", "selector"],
    "properties": {
        "goal": {"type": "string", "minLength": 3, "maxLength": 600, "description": "What to achieve in the app, in plain words."},
        "package": {"type": "string", "minLength": 3, "maxLength": 200, "pattern": r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$",
                    "description": "Package name of the app, e.g. com.android.settings."},
        "selector": {"type": "string", "minLength": 1, "maxLength": 500,
                     "description": "Which Androids: all | running | tag:x | 5 | 1-10 | comma-separated."},
        "success": {"type": "string", "maxLength": 300,
                    "description": "What must be on screen when the goal is reached: text:..., id:..., desc:..., "
                                   "package:... or activity:.... Without it a run can only be self-checked."},
        "runs": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Runs per Android (default 1)."},
        "max_steps": {"type": "integer", "minimum": 1, "maximum": 200},
        "model": {"type": "string", "maxLength": 32, "description": "Model profile to use (default: the farm's default)."},
        "dataset": {"type": "string", "maxLength": 64, "pattern": "^[a-z0-9][a-z0-9-]*$",
                    "description": "Dataset to record into (default: named after the goal)."},
    }}
STATUS_DESCRIPTION = "Progress and results of one AI Pilot session: per Android its state, step, and the outcome of each run."
STATUS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["session"],
                 "properties": {"session": {"type": "string", "pattern": "^t[0-9]{1,12}$",
                                            "description": "The session id returned by pilot_start."}}}


def describe_start(args: dict, who: Any) -> str:
    """The line the operator approves, built from the validated arguments."""
    runs = int(args.get("runs") or 1)
    return (f"Let a model operate {quote(args.get('package'), 80)} on {who.noun} toward the goal "
            f"{quote(args.get('goal'), 200)}" + (f", {runs} runs each" if runs > 1 else "")
            + ", recording what it does")


def _view(session: dict) -> tuple[dict, list[str]]:
    devices, lines = {}, []
    for key, device in sorted((session.get("devices") or {}).items(), key=lambda kv: int(kv[0])):
        devices[key] = {"state": device.get("state"), "run": device.get("run"), "runs": device.get("runs"),
                        "step": device.get("step"), "waiting": bool(device.get("waiting")),
                        "results": [{"state": r.get("state"), "steps": r.get("step"), "model_calls": r.get("model_calls")}
                                    for r in device.get("results") or []]}
        lines.append(f"Android {key}: {device.get('state')}, run {device.get('run')}/{device.get('runs')}, "
                     f"step {device.get('step')} - {one_line(device.get('message') or device.get('step_label') or '', 200)}")
    info = {"id": session.get("id"), "state": session.get("state"), "dataset": session.get("dataset"),
            "counts": session.get("counts") or {}, "recordings": len(session.get("recordings") or []),
            "unverified": len(session.get("unverified") or []), "waiting": session.get("waiting") or []}
    return {"session": info, "devices": devices}, lines


def pilot_start(api: Any, a: dict, lim: Any) -> Any:
    from .tools import ToolResult
    body = {k: a[k] for k in ("goal", "package", "selector", "success", "runs", "max_steps", "model", "dataset") if k in a}
    task = api.post("/api/ai/pilot", body)["task"]
    return ToolResult({"ok": True, "session": task["id"], "next": f"pilot_status(session={task['id']!r})"},
                      f"Pilot session {task['id']} started on {len(task.get('targets') or [])} Android(s). Call "
                      f"pilot_status with session \"{task['id']}\" to follow it; when runs are verified, the dataset "
                      "can be trained into a program.")


def pilot_status(api: Any, a: dict, lim: Any) -> Any:
    from .tools import ToolResult
    session = api.get(f"/api/ai/pilot/sessions/{a['session']}")["session"]
    structured, lines = _view(session)
    info = structured["session"]
    head = (f"Pilot session {info['id']} is {info['state']}: {info['recordings']} verified recording(s), "
            f"{info['unverified']} unverified, dataset {info['dataset']}."
            + (f" Waiting for the operator on Android {', '.join(str(i) for i in info['waiting'])}." if info["waiting"] else ""))
    # What a device shows ends up in the per-device message; keep it inside the marked block.
    return ToolResult(structured, head + "\n" + device_block(f"devices of pilot session {info['id']}", lines),
                      untrusted=True)

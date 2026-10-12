"""Virtual camera and microphone, and listening to one Android on this machine's speakers."""
from __future__ import annotations

import argparse
from typing import Any

from ..common import EXIT_FAILED, EXIT_OK, Ctx, one_id, report_results
from ..render import size


def camera_status(ctx: Ctx) -> int:
    response = ctx.api.get("/api/camera")
    sources = None if ctx.out.json_mode else ctx.api.get("/api/camera/sources")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    available, status = response.get("available") or {}, response.get("status") or {}
    if available.get("ok"):
        ctx.out.print("Virtual camera: ready")
    else:
        ctx.out.print("Virtual camera: not ready")
        for reason in available.get("reasons") or []:
            ctx.out.print(f"  - {reason}")
    feeds = status.get("feeds") or []
    ctx.out.print()
    ctx.out.table(("FEED", "SOURCE", "RUNNING", "ANDROIDS"),
                  [(f.get("id"), f.get("source"), bool(f.get("running")), f.get("ids") or []) for f in feeds],
                  empty="Nothing is playing. Start with: sf camera play <selector> <source>")
    muted = status.get("muted") or []
    if muted:
        ctx.out.print(f"\nMicrophone muted on: {', '.join(str(i) for i in muted)}")
    ctx.out.print("\nSources:")
    ctx.out.table(("SOURCE", "KIND", "SIZE"),
                  [(s.get("id"), s.get("kind"), size(s["size"]) if s.get("size") is not None else "-")
                   for s in (sources or {}).get("sources") or []])
    return EXIT_OK


def camera_play(ctx: Ctx) -> int:
    body = {"selector": ctx.args.selector, "source": ctx.args.source, "sound": ctx.args.sound}
    return report_results(ctx, ctx.api.request("POST", "/api/camera/play", body=body, timeout=180))


def camera_stop(ctx: Ctx) -> int:
    body = {"selector": ctx.args.selector} if ctx.args.selector else {}
    response = ctx.api.post("/api/camera/stop", body)
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    stopped = (response or {}).get("ids") or []
    ctx.out.note(f"camera stopped on {len(stopped)} Android(s)" if stopped else "nothing was playing there")
    return EXIT_OK


def camera_mic(ctx: Ctx) -> int:
    response = ctx.api.post("/api/camera/mic", {"selector": ctx.args.selector, "mode": ctx.args.mode})
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK if (response or {}).get("ok", True) else EXIT_FAILED
    muted = (response or {}).get("muted") or []
    ctx.out.print("microphone muted on: " + (", ".join(str(i) for i in muted) if muted else "none"))
    return EXIT_OK if (response or {}).get("ok", True) else EXIT_FAILED


def listen(ctx: Ctx) -> int:
    target = ctx.args.target
    if target in ("off", "stop", "none"):
        response = ctx.api.delete("/api/listen")
        return ctx.finish(response, done="listening stopped")
    if not target:
        response = ctx.api.get("/api/listen")
        if ctx.out.json_mode:
            ctx.out.json(response)
        else:
            current = (response or {}).get("listening")
            ctx.out.print(f"listening to Android {current}" if current is not None else "not listening to any Android")
        return EXIT_OK
    instance_id = one_id(target)
    response = ctx.api.request("POST", f"/api/listen/{instance_id}", body={}, timeout=120)
    return ctx.finish(response, done=f"listening to Android {instance_id} on the daemon's speakers "
                                     "(stop with: sf listen off)")


def register(b: Any, sub: argparse._SubParsersAction) -> None:
    cam = b.group(sub, "camera", "feed a video or live stream into the camera and microphone of Androids")
    b.add(cam, "status", camera_status, "show whether the virtual camera is ready, what plays where, and the sources",
          "sf camera status")
    p = b.add(cam, "play", camera_play, "play a media file (or the live RTMP ingest) as the camera of Androids",
              "sf camera play 1-5 intro.mp4\nsf camera play 3 rtmp --sound none")
    p.add_argument("selector", help="which Androids")
    p.add_argument("source", help="a file from the media library (see `sf camera status`), or `rtmp` for the live ingest")
    p.add_argument("--sound", default="source", metavar="SOUND",
                   help="microphone sound: `source` (the video's own, default), `none`, or another library file")
    p = b.add(cam, "stop", camera_stop, "stop the camera feed", "sf camera stop 1-5")
    p.add_argument("selector", nargs="?", help="which Androids (default: every Android)")
    p = b.add(cam, "mic", camera_mic, "mute, unmute or solo the microphone of Androids", "sf camera mic 3 solo")
    p.add_argument("selector", help="which Androids")
    p.add_argument("mode", choices=("mute", "unmute", "solo"), help="solo: unmute these and mute every other")

    p = b.add(sub, "listen", listen, "hear one Android on the speakers of the machine the daemon runs on",
              "sf listen 3\nsf listen off")
    p.add_argument("target", nargs="?", metavar="ID|off", help="Android id, or `off` to stop (nothing: show the current one)")

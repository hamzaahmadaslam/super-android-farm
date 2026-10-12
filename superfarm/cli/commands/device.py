"""Working on the Androids themselves: shell, screenshots, apps, files, input, native windows."""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
from typing import Any

from ..client import CliError
from ..common import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Ctx, one_id, report_results, task_failed, wait_for
from ..render import size, when


def _upload(ctx: Ctx, path: str, file: Path, **fields: Any) -> Any:
    def progress(sent: int, total: int) -> None:
        ctx.out.status(f"uploading {file.name}: {100 * sent // total if total else 100}% of {size(total)}")
    try:
        return ctx.api.upload(path, file, fields=fields, progress=progress)
    finally:
        ctx.out.clear_status()


def _local_file(name: str) -> Path:
    path = Path(name).expanduser()
    if not path.is_file():
        raise CliError(f"{name}: no such file", EXIT_USAGE)
    return path


# ------------------------------------------------------------------ shell

def shell(ctx: Ctx) -> int:
    argv = list(ctx.args.rest)
    if not argv:
        raise CliError("give the command after `--`, e.g.  sf shell all -- getprop ro.build.version.release", EXIT_USAGE)
    body: dict[str, Any] = {"selector": ctx.args.selector, "argv": argv, "root": ctx.args.root, "force": ctx.args.force}
    if ctx.args.timeout is not None:
        body["timeout"] = ctx.args.timeout
    response = ctx.api.post("/api/shell", body)
    if not ctx.wait:
        return ctx.finish(response)
    task = wait_for(ctx, response["task"])
    result = ctx.api.get(f"/api/shell/{task['id']}/output")
    code = EXIT_FAILED if task_failed(task) else EXIT_OK
    if ctx.out.json_mode:
        ctx.out.json(result)
        return code
    outputs = result.get("outputs") or {}
    results = task.get("results") or {}
    keys = sorted(set(outputs) | set(results), key=lambda k: int(k) if str(k).isdigit() else 0)
    for key in keys:
        entry = outputs.get(key) or {}
        outcome = results.get(key) or {}
        if entry:
            how = "timed out" if entry.get("timed_out") else f"exit {entry.get('code')}"
            how += ", output cut" if entry.get("truncated") else ""
        else:
            how = f"not run: {outcome.get('detail', 'unknown')}"
        if len(keys) > 1 or not entry or entry.get("code") != 0:
            ctx.out.print(f"==> Android {key} ({how}) <==")
        text = str(entry.get("output", "")).rstrip("\n")
        if text:
            ctx.out.print(text)
    if task.get("state") != "done":
        ctx.out.error(f"task {task.get('id')} {task.get('state')}: {task.get('error') or ''}".rstrip(": "))
    return code


# ------------------------------------------------------------------ screenshot

def screenshot(ctx: Ctx) -> int:
    instance_id = one_id(ctx.args.id)
    dest = Path(ctx.args.output or f"android-{instance_id}.png").expanduser()
    written = ctx.api.download(f"/api/instances/{instance_id}/screenshot", dest)
    if ctx.out.json_mode:
        ctx.out.json({"id": instance_id, "file": str(dest), "bytes": written})
    else:
        ctx.out.print(str(dest))
    return EXIT_OK


# ------------------------------------------------------------------ app library

def apps_ls(ctx: Ctx) -> int:
    response = ctx.api.get("/api/apps/library")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    ctx.out.table(("APP ID", "PACKAGE", "VERSION", "NAME", "FORMAT", "SIZE", "ADDED"),
                  [(a.get("id"), a.get("package"), a.get("version_name") or a.get("version_code"), a.get("label"),
                    a.get("format"), size(a.get("size")), when(a.get("added"))) for a in response.get("apps") or []],
                  empty="The app library is empty. Add an app with: sf apps add app.apk")
    return EXIT_OK


def apps_add(ctx: Ctx) -> int:
    added, code = [], EXIT_OK
    for name in ctx.args.files:
        file = _local_file(name)
        added.append(_upload(ctx, "/api/apps/library", file))
    if ctx.out.json_mode:
        ctx.out.json(added if len(added) != 1 else added[0])
        return code
    ctx.out.table(("APP ID", "PACKAGE", "VERSION", "NAME"),
                  [((r.get("app") or {}).get("id"), (r.get("app") or {}).get("package"),
                    (r.get("app") or {}).get("version_name"), (r.get("app") or {}).get("label")) for r in added])
    ctx.out.note("install it with: sf app install <selector> <app id or package>")
    return code


def apps_rm(ctx: Ctx) -> int:
    response = ctx.api.delete(f"/api/apps/library/{ctx.args.app_id}")
    return ctx.finish(response, done=f"removed {ctx.args.app_id} from the library (installed copies stay on the Androids)")


# ------------------------------------------------------------------ apps on Androids

def library_id(ctx: Ctx, wanted: str) -> str:
    """An app id as given, or the newest library entry of a package name."""
    apps = (ctx.api.get("/api/apps/library") or {}).get("apps") or []
    if any(a.get("id") == wanted for a in apps):
        return wanted
    matches = sorted((a for a in apps if a.get("package") == wanted), key=lambda a: -int(a.get("version_code") or 0))
    if not matches:
        raise CliError(f"no app {wanted!r} in the library (see: sf apps ls; add one with: sf apps add FILE)")
    return str(matches[0]["id"])


def app_install(action: str):
    def run(ctx: Ctx) -> int:
        body: dict[str, Any] = {"selector": ctx.args.selector, "app_id": library_id(ctx, ctx.args.app),
                                "grant": not ctx.args.no_grant}
        if action == "install":
            body["downgrade"] = ctx.args.downgrade
        return ctx.finish(ctx.api.post(f"/api/apps/{action}", body))
    return run


def app_package(action: str):
    def run(ctx: Ctx) -> int:
        body: dict[str, Any] = {"selector": ctx.args.selector, "package": ctx.args.package}
        if action == "uninstall":
            body["keep_data"] = ctx.args.keep_data
        return ctx.finish(ctx.api.post(f"/api/apps/{action}", body))
    return run


def app_matrix(ctx: Ctx) -> int:
    response = ctx.api.get("/api/apps/matrix", selector=ctx.args.selector)
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    matrix = response.get("matrix") or {}
    ids = sorted({int(i) for versions in matrix.values() for i in versions})
    ctx.out.table(("PACKAGE", *(str(i) for i in ids)),
                  [(package, *((versions.get(str(i)) or versions.get(i) or "-") for i in ids))
                   for package, versions in sorted(matrix.items())],
                  empty="No third-party apps are installed on the selected (running) Androids.")
    return EXIT_OK


# ------------------------------------------------------------------ files

def push(ctx: Ctx) -> int:
    file = _local_file(ctx.args.local)
    return ctx.finish(_upload(ctx, "/api/files/push", file, selector=ctx.args.selector, remote=ctx.args.remote))


def gallery(ctx: Ctx) -> int:
    file = _local_file(ctx.args.local)
    return ctx.finish(_upload(ctx, "/api/files/gallery", file, selector=ctx.args.selector))


def pull(ctx: Ctx) -> int:
    instance_id = one_id(ctx.args.id)
    name = PurePosixPath(ctx.args.remote).name
    if not name:
        raise CliError(f"{ctx.args.remote!r} is not a file path", EXIT_USAGE)
    dest = Path(ctx.args.output).expanduser() if ctx.args.output else Path(name)
    if dest.is_dir():
        dest = dest / name
    written = ctx.api.download(f"/api/instances/{instance_id}/files/pull", dest, query={"path": ctx.args.remote})
    if ctx.out.json_mode:
        ctx.out.json({"id": instance_id, "remote": ctx.args.remote, "file": str(dest), "bytes": written})
    else:
        ctx.out.print(str(dest))
        ctx.out.note(f"{size(written)} from Android {instance_id}")
    return EXIT_OK


# ------------------------------------------------------------------ input / windows

def _point(args: argparse.Namespace) -> dict[str, Any]:
    if not args.pixels:
        for name in ("x", "y"):
            if not 0.0 <= getattr(args, name) <= 1.0:
                raise CliError(f"{name} must be between 0 and 1 (a fraction of the screen, so the same tap fits "
                               "every resolution); pass --pixels for exact pixels", EXIT_USAGE)
    return {"x": args.x, "y": args.y, "normalized": not args.pixels}


def input_tap(ctx: Ctx) -> int:
    body = {"selector": ctx.args.selector, **_point(ctx.args)}
    return report_results(ctx, ctx.api.post("/api/input/tap", body))


def input_text(ctx: Ctx) -> int:
    return report_results(ctx, ctx.api.post("/api/input/text", {"selector": ctx.args.selector, "value": ctx.args.text}))


def input_key(ctx: Ctx) -> int:
    return report_results(ctx, ctx.api.post("/api/input/key", {"selector": ctx.args.selector, "name": ctx.args.key}))


def window(action: str):
    def run(ctx: Ctx) -> int:
        return report_results(ctx, ctx.api.post(f"/api/windows/{action}", {"selector": ctx.args.selector}))
    return run


def window_close(ctx: Ctx) -> int:
    if ctx.args.selector == "all":
        response = ctx.api.post("/api/windows/close-all")
        return ctx.finish(response, done="closed every device window")
    return report_results(ctx, ctx.api.post("/api/windows/close", {"selector": ctx.args.selector}))


# ------------------------------------------------------------------ parser

def register(b: Any, sub: argparse._SubParsersAction) -> None:
    p = b.add(sub, "shell", shell, "run a command inside Androids",
              "sf shell all -- getprop ro.build.version.release\nsf shell 3 --root -- ls /data/app",
              description="Run one command on every selected Android and show each one's output. The words "
                          "after `--` are the command, passed as separate arguments (no shell on this machine "
                          "ever sees them). Obviously destructive commands are refused unless --force.")
    p.add_argument("selector", help="which Androids")
    p.add_argument("--root", action="store_true", help="run as root inside the Android")
    p.add_argument("--timeout", type=float, metavar="SECONDS", help="time limit per Android (default: control.shell_timeout)")
    p.add_argument("--force", action="store_true", help="run a command the safety denylist would refuse")

    p = b.add(sub, "screenshot", screenshot, "save a screenshot of one Android", "sf screenshot 3 -o shot.png")
    p.add_argument("id", help="Android id")
    p.add_argument("-o", "--output", metavar="FILE", help="where to save (default: android-<id>.png)")

    apps = b.group(sub, "apps", "the app library: APK / XAPK / APKS / APKM files kept by the farm")
    b.add(apps, "ls", apps_ls, "list the apps in the library", "sf apps ls")
    p = b.add(apps, "add", apps_add, "upload an app file into the library", "sf apps add ~/Downloads/shop-4.2.apk")
    p.add_argument("files", nargs="+", metavar="FILE", help="APK, XAPK, APKS or APKM file")
    p = b.add(apps, "rm", apps_rm, "remove an app from the library", "sf apps rm com.example.shop-42")
    p.add_argument("app_id", help="app id as shown by `sf apps ls`")

    app = b.group(sub, "app", "apps on the Androids: install, launch, stop, clear, compare versions")
    for action, summary, example in (
            ("install", "install an app from the library", "sf app install 1-10 com.example.shop"),
            ("ensure", "install an app only where it is missing or older", "sf app ensure all com.example.shop")):
        p = b.add(app, action, app_install(action), summary, example)
        p.add_argument("selector", help="which Androids")
        p.add_argument("app", help="app id from `sf apps ls`, or a package name (its newest version is used)")
        p.add_argument("--no-grant", action="store_true", help="do not grant the app's runtime permissions")
        if action == "install":
            p.add_argument("--downgrade", action="store_true", help="allow replacing a newer installed version")
    for action, summary, example in (
            ("uninstall", "remove an app", "sf app uninstall tag:test com.example.shop"),
            ("launch", "open an app", "sf app launch all com.example.shop"),
            ("stop", "force-stop an app", "sf app stop all com.example.shop"),
            ("clear", "erase an app's data (as after a fresh install)", "sf app clear 3 com.example.shop")):
        p = b.add(app, action, app_package(action), summary, example)
        p.add_argument("selector", help="which Androids")
        p.add_argument("package", help="package name, e.g. com.example.shop")
        if action == "uninstall":
            p.add_argument("--keep-data", action="store_true", help="keep the app's data and cache")
    p = b.add(app, "matrix", app_matrix, "which version of which app is installed where", "sf app matrix running")
    p.add_argument("selector", nargs="?", default="all", help="which Androids (default: all)")

    p = b.add(sub, "push", push, "copy a local file onto Androids", "sf push all notes.txt /sdcard/Download/")
    p.add_argument("selector", help="which Androids")
    p.add_argument("local", help="file on this machine")
    p.add_argument("remote", nargs="?", default="/sdcard/Download/",
                   help="folder (ending in /) or full path on the Android (default: /sdcard/Download/)")
    p = b.add(sub, "pull", pull, "copy a file from one Android to this machine", "sf pull 3 /sdcard/Download/report.pdf")
    p.add_argument("id", help="Android id")
    p.add_argument("remote", help="file path on the Android")
    p.add_argument("-o", "--output", metavar="PATH", help="local file or folder (default: the file's name, here)")
    p = b.add(sub, "gallery", gallery, "add a picture or video to the gallery of Androids", "sf gallery 1-5 photo.jpg")
    p.add_argument("selector", help="which Androids")
    p.add_argument("local", help="picture or video on this machine")

    inp = b.group(sub, "input", "send the same touch or key to many Androids at once")
    p = b.add(inp, "tap", input_tap, "tap a point of the screen", "sf input tap 1-10 0.5 0.9",
              description="Tap one point on every selected Android. Coordinates are fractions of the screen "
                          "(0.5 0.9 = centre, near the bottom), so one command fits every resolution.")
    p.add_argument("selector", help="which Androids")
    p.add_argument("x", type=float, help="0 (left) to 1 (right)")
    p.add_argument("y", type=float, help="0 (top) to 1 (bottom)")
    p.add_argument("--pixels", action="store_true", help="x and y are pixels, not fractions")
    p = b.add(inp, "text", input_text, "type text into the focused field", "sf input text 3 'hello world'")
    p.add_argument("selector", help="which Androids")
    p.add_argument("text", help="text to type (it appears in this machine's process list; never type secrets this way)")
    p = b.add(inp, "key", input_key, "press a key", "sf input key all HOME")
    p.add_argument("selector", help="which Androids")
    p.add_argument("key", help="key name (HOME, BACK, ENTER, APP_SWITCH, ...) or an Android key code")

    win = b.group(sub, "window", "native device windows (scrcpy) on the desktop the daemon runs on")
    p = b.add(win, "open", window("open"), "open a window for each selected Android", "sf window open 3")
    p.add_argument("selector", help="which Androids")
    p = b.add(win, "grid", window("grid"), "open windows tiled to fill the screen", "sf window grid 1-12")
    p.add_argument("selector", help="which Androids")
    p = b.add(win, "close", window_close, "close device windows", "sf window close all")
    p.add_argument("selector", help="which Androids (`all` closes every window, also of deleted Androids)")

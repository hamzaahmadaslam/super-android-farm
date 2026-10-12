"""Apps, files, device settings and shell commands across the fleet."""
from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, Response

from ...control.files import validate_remote
from ...util.files import safe_filename
from ..audit import note
from ..auth import require
from ..common import SelectorBody, accepted, bounded, cleanup_after, farm_of, instance_id
from ..uploads import receive_upload

router = APIRouter(dependencies=[Depends(require)])


# -------------------------------------------------------------------------- app library

@router.get("/api/apps/library")
def library(request: Request) -> dict:
    return {"apps": farm_of(request).library.list()}


@router.post("/api/apps/library", status_code=201)
async def library_upload(request: Request) -> dict:
    farm = farm_of(request)
    upload = await receive_upload(request, farm, default_name="app.apk")
    try:
        meta = await run_in_threadpool(farm.library.add, upload.filename, upload.path, move=True)
    finally:
        upload.discard()
    note(request, app=meta.get("id"), package=meta.get("package"))
    return {"app": meta}


@router.delete("/api/apps/library/{app_id}")
def library_delete(app_id: str, request: Request) -> dict:
    farm_of(request).library.remove(app_id)
    note(request, app=app_id)
    return {"deleted": app_id}


# ------------------------------------------------------------------------- app actions

class Install(SelectorBody):
    app_id: str = Field(..., min_length=1, max_length=200)
    grant: bool = True
    replace: bool = True
    downgrade: bool = False
    all_locales: bool = False


class Ensure(SelectorBody):
    app_id: str = Field(..., min_length=1, max_length=200)
    grant: bool = True


class Package(SelectorBody):
    package: str = Field(..., min_length=1, max_length=255)


class Uninstall(Package):
    keep_data: bool = False


class Launch(Package):
    activity: str | None = Field(None, max_length=255)


class Permission(Package):
    permission: str = Field("all", max_length=200, description="One permission name, or `all` for every runtime permission the app requests.")


@router.post("/api/apps/install")
def app_install(body: Install, request: Request):
    task = farm_of(request).apps.install(body.selector, body.app_id, grant=body.grant, replace=body.replace,
                                         downgrade=body.downgrade, all_locales=body.all_locales)
    return accepted(request, task, selector=body.selector, app=body.app_id)


@router.post("/api/apps/ensure")
def app_ensure(body: Ensure, request: Request):
    task = farm_of(request).apps.ensure(body.selector, body.app_id, grant=body.grant)
    return accepted(request, task, selector=body.selector, app=body.app_id)


@router.post("/api/apps/uninstall")
def app_uninstall(body: Uninstall, request: Request):
    task = farm_of(request).apps.uninstall(body.selector, body.package, keep_data=body.keep_data)
    return accepted(request, task, selector=body.selector, package=body.package)


@router.post("/api/apps/launch")
def app_launch(body: Launch, request: Request):
    task = farm_of(request).apps.launch(body.selector, body.package, activity=body.activity)
    return accepted(request, task, selector=body.selector, package=body.package)


@router.post("/api/apps/stop")
def app_stop(body: Package, request: Request):
    return accepted(request, farm_of(request).apps.stop(body.selector, body.package),
                    selector=body.selector, package=body.package)


@router.post("/api/apps/clear")
def app_clear(body: Package, request: Request):
    return accepted(request, farm_of(request).apps.clear(body.selector, body.package),
                    selector=body.selector, package=body.package)


@router.post("/api/apps/enable")
def app_enable(body: Package, request: Request):
    return accepted(request, farm_of(request).apps.enable(body.selector, body.package),
                    selector=body.selector, package=body.package)


@router.post("/api/apps/disable")
def app_disable(body: Package, request: Request):
    return accepted(request, farm_of(request).apps.disable(body.selector, body.package),
                    selector=body.selector, package=body.package)


@router.post("/api/apps/grant")
def app_grant(body: Permission, request: Request):
    return accepted(request, farm_of(request).apps.grant(body.selector, body.package, body.permission),
                    selector=body.selector, package=body.package)


@router.post("/api/apps/revoke")
def app_revoke(body: Permission, request: Request):
    return accepted(request, farm_of(request).apps.revoke(body.selector, body.package, body.permission),
                    selector=body.selector, package=body.package)


@router.get("/api/apps/matrix")
def app_matrix(request: Request, selector: str = "all", detail: bool = False) -> dict:
    """package -> {instance id: version}. With `detail`, also version codes, enabled flags
    and the Androids that could not be read."""
    apps = farm_of(request).apps
    if detail:
        return apps.matrix_detail(selector)
    return {"matrix": apps.matrix(selector)}


@router.get("/api/instances/{id}/apps")
def instance_apps(id: str, request: Request, third_party: bool = True) -> dict:
    farm = farm_of(request)
    return {"apps": farm.apps.installed(instance_id(farm, id), third_party=third_party)}


@router.get("/api/instances/{id}/apps/{package}/permissions")
def instance_app_permissions(id: str, package: str, request: Request) -> dict:
    farm = farm_of(request)
    return farm.apps.permissions(instance_id(farm, id), package)


# -------------------------------------------------------------------------------- files

def _selector_field(fields: dict[str, str], request: Request) -> str:
    selector = fields.get("selector") or request.query_params.get("selector") or ""
    if not selector or len(selector) > 500:
        raise ValueError("a `selector` (form field or query parameter) is required")
    return selector


@router.post("/api/files/push")
async def files_push(request: Request):
    """Upload one file and push it to every selected Android. Fields: `selector`, and
    `remote` (a folder ending in `/`, or a full path; default /sdcard/Download/)."""
    farm = farm_of(request)
    upload = await receive_upload(request, farm)
    try:
        selector = _selector_field(upload.fields, request)
        remote = upload.fields.get("remote") or request.query_params.get("remote") or "/sdcard/Download/"
        # A folder as destination keeps the file's own name, so the local copy must carry it.
        local = upload.named()
        task = await run_in_threadpool(farm.files.push, selector, local, remote)
    except BaseException:
        upload.discard()
        raise
    cleanup_after(farm, task, upload)
    return accepted(request, task, selector=selector, name=upload.filename)


@router.post("/api/files/gallery")
async def files_gallery(request: Request):
    """Upload one picture or video and add it to the gallery of every selected Android."""
    farm = farm_of(request)
    upload = await receive_upload(request, farm)
    try:
        selector = _selector_field(upload.fields, request)
        task = await run_in_threadpool(lambda: farm.files.to_gallery(selector, upload.path, name=upload.filename))
    except BaseException:
        upload.discard()
        raise
    cleanup_after(farm, task, upload)
    return accepted(request, task, selector=selector, name=upload.filename)


@router.get("/api/instances/{id}/files")
def files_list(id: str, request: Request, path: str = "/sdcard") -> dict:
    farm = farm_of(request)
    n = instance_id(farm, id)
    return {"path": validate_remote(path), "entries": farm.files.list(n, path)}


@router.get("/api/instances/{id}/files/pull")
def files_pull(id: str, request: Request, path: str):
    farm = farm_of(request)
    local = farm.files.pull(instance_id(farm, id), path)
    # Files.pull hands over a fresh folder holding just this file; it goes once the download is sent.
    return FileResponse(local, filename=safe_filename(local.name), media_type="application/octet-stream",
                        background=BackgroundTask(shutil.rmtree, local.parent, ignore_errors=True))


# ----------------------------------------------------------------------- device settings

class Locale(SelectorBody):
    locale: str = Field(..., min_length=2, max_length=35)
    restart: bool = Field(True, description="Restart the Android framework so every app sees the new language.")


class Timezone(SelectorBody):
    timezone: str = Field(..., min_length=1, max_length=100)


class Geo(SelectorBody):
    lat: float | None = Field(None, description="Latitude. Leave out (with lon) to remove the mock location.")
    lon: float | None = None
    accuracy: float = Field(10.0, gt=0, le=10000)


class Proxy(SelectorBody):
    proxy: str = Field("", max_length=300, description="host:port, or empty to switch the proxy off.")


class Clipboard(SelectorBody):
    text: str = Field(..., min_length=1)


class Toggle(SelectorBody):
    enabled: bool


class FontScale(SelectorBody):
    scale: float = Field(..., ge=0.5, le=2.0)


class OpenUrl(SelectorBody):
    url: str = Field(..., min_length=1, max_length=2048)
    package: str | None = Field(None, max_length=255)


class Reboot(SelectorBody):
    mode: str = Field("soft", description="soft: restart the Android framework. container: restart the containers.")
    force: bool = False


@router.post("/api/device/locale")
def device_locale(body: Locale, request: Request):
    return accepted(request, farm_of(request).device.set_locale(body.selector, body.locale, restart=body.restart),
                    selector=body.selector)


@router.post("/api/device/timezone")
def device_timezone(body: Timezone, request: Request):
    return accepted(request, farm_of(request).device.set_timezone(body.selector, body.timezone), selector=body.selector)


@router.post("/api/device/geo")
def device_geo(body: Geo, request: Request):
    task = farm_of(request).device.set_geo(body.selector, body.lat, body.lon, accuracy=body.accuracy)
    return accepted(request, task, selector=body.selector)


@router.post("/api/device/proxy")
def device_proxy(body: Proxy, request: Request):
    return accepted(request, farm_of(request).device.set_proxy(body.selector, body.proxy), selector=body.selector)


@router.post("/api/device/clipboard")
def device_clipboard(body: Clipboard, request: Request):
    return accepted(request, farm_of(request).device.set_clipboard(body.selector, body.text), selector=body.selector)


@router.post("/api/device/animations")
def device_animations(body: Toggle, request: Request):
    return accepted(request, farm_of(request).device.set_animations(body.selector, body.enabled), selector=body.selector)


@router.post("/api/device/dark-mode")
def device_dark_mode(body: Toggle, request: Request):
    return accepted(request, farm_of(request).device.set_dark_mode(body.selector, body.enabled), selector=body.selector)


@router.post("/api/device/font-scale")
def device_font_scale(body: FontScale, request: Request):
    return accepted(request, farm_of(request).device.set_font_scale(body.selector, body.scale), selector=body.selector)


@router.post("/api/device/open-url")
def device_open_url(body: OpenUrl, request: Request):
    task = farm_of(request).device.open_url(body.selector, body.url, package=body.package)
    return accepted(request, task, selector=body.selector)


@router.post("/api/device/wake")
def device_wake(body: SelectorBody, request: Request):
    return accepted(request, farm_of(request).device.wake(body.selector), selector=body.selector)


@router.post("/api/device/reboot")
def device_reboot(body: Reboot, request: Request):
    task = farm_of(request).device.reboot(body.selector, mode=body.mode, force=body.force)
    return accepted(request, task, selector=body.selector, action=body.mode, force=body.force or None)


@router.get("/api/instances/{id}/info")
def device_info(id: str, request: Request) -> dict:
    farm = farm_of(request)
    return farm.device.info(instance_id(farm, id))


@router.get("/api/instances/{id}/screenshot")
def device_screenshot(id: str, request: Request) -> Response:
    farm = farm_of(request)
    return Response(farm.device.screenshot(instance_id(farm, id)), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.get("/api/instances/{id}/logcat")
def device_logcat(id: str, request: Request, lines: int = 200, filter: str | None = None,  # noqa: A002 - the query name
                  priority: str | None = None, package: str | None = None) -> dict:
    """`filter` keeps the lines containing that text; `priority` is the minimum level (V D I W E F)."""
    farm = farm_of(request)
    n = instance_id(farm, id)
    lines = bounded(lines, 1, 5000, "lines")
    if filter is not None and len(filter) > 200:
        raise ValueError("filter is longer than 200 characters")
    return {"id": n, "text": farm.device.logcat(n, lines, priority=priority, package=package, grep=filter or None)}


# -------------------------------------------------------------------------------- shell

class ShellRun(SelectorBody):
    argv: list[str] = Field(..., min_length=1, max_length=256, description="The command as separate arguments; nothing is passed through a shell on the host.")
    timeout: float | None = Field(None, gt=0, le=3600)
    root: bool = False
    force: bool = Field(False, description="Run a command the denylist would refuse.")


@router.post("/api/shell")
def shell_run(body: ShellRun, request: Request):
    task = farm_of(request).shell.run(body.selector, body.argv, timeout=body.timeout, force=body.force, root=body.root)
    return accepted(request, task, selector=body.selector, force=body.force or None)


@router.get("/api/shell/{task_id}/output")
def shell_output(task_id: str, request: Request) -> dict:
    farm = farm_of(request)
    task = farm.tasks.get(task_id)
    if task is None or task.kind != "shell":
        raise KeyError(f"no shell task {task_id[:40]!r}")
    return {"task": task.to_dict(), "outputs": {str(k): v for k, v in farm.shell.outputs(task_id).items()}}

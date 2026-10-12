"""Backups: create, list, verify, restore, delete, download.

A backup is named as `sf backup list` shows it: `android-<id>/<time>` or `farm/<time>`,
always inside the backup directory. The API accepts nothing else: no paths, so no request
can make the daemon read, restore or delete a folder somewhere else on the machine.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.responses import FileResponse

from ..audit import note
from ..auth import require
from ..common import Body, SelectorBody, accepted, farm_of, instance_id
from ..errors import ApiError

router = APIRouter(dependencies=[Depends(require)])

_NAME = re.compile(r"^(android-\d{1,4}|farm)/\d{8}-\d{6}(-\d+)?$")


class Archive(Body):
    archive: str = Field(..., min_length=1, max_length=80, description="A backup name as listed: android-<id>/<time> or farm/<time>.")


class Verify(Archive):
    deep: bool = Field(False, description="Also read the data archive end to end (slow).")


class Restore(Archive):
    id: int | None = Field(None, description="Restore over this Android (it must be stopped; needs force). Leave out to restore as a new Android.")
    force: bool = False
    keep_network: bool = Field(False, description="Restoring as new next to the original: keep the copy's network on. Off by default so one identity is never online twice.")
    trust: bool = Field(False, description="Farm backup made by another farm: also take custom images, boot arguments, properties, adopted volumes, state and schedules from it. Off by default because those can start a privileged container of the archive's choosing.")
    restore_config: bool = Field(False, description="Farm backup: also replace this farm's config.yaml with the archived one.")
    restore_secrets: bool = Field(False, description="Farm backup: also restore VPN profiles, credentials and tokens from the archive.")


class FarmBackup(Body):
    include_secrets: bool | None = Field(None, description="Include VPN profiles, credentials and tokens. Default: backup.include_secrets.")


def _known(farm, name: str) -> tuple[str, dict]:
    """A client-supplied backup name -> (name, manifest). 400 when it is not a name, 404 when
    there is no such backup."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError("bad backup name: use one as listed (android-<id>/<time> or farm/<time>)")
    manifest = farm.backups.manifest(name)
    if manifest is None:
        raise KeyError(f"no backup named {name}")
    return name, manifest


@router.get("/api/backups")
def backups(request: Request) -> dict:
    store = farm_of(request).backups
    return {"backups": store.list(kind="all"), "directory": str(store.directory), "schedule": store.schedule_status()}


@router.post("/api/backups")
def backup_create(body: SelectorBody, request: Request):
    # exclusive: refused with 409 `busy` while another task still works on one of these Androids.
    return accepted(request, farm_of(request).backups.create_many(body.selector, exclusive=True), selector=body.selector)


@router.post("/api/backups/farm", status_code=201)
def backup_farm(request: Request, body: FarmBackup | None = None) -> dict:
    store = farm_of(request).backups
    folder = store.create_farm(include_secrets=body.include_secrets if body else None)
    name = f"{folder.parent.name}/{folder.name}"
    note(request, archive=name)
    return {"archive": name, "dir": str(folder), "includes_secrets": bool((store.manifest(folder) or {}).get("includes_secrets"))}


@router.post("/api/backups/verify")
def backup_verify(body: Verify, request: Request) -> dict:
    farm = farm_of(request)
    name, _ = _known(farm, body.archive)
    return {"archive": name, "ok": farm.backups.verify(name, deep=body.deep)}


@router.post("/api/backups/prune")
def backup_prune(request: Request) -> dict:
    removed = farm_of(request).backups.prune()
    note(request, count=len(removed))
    return {"removed": [f"{p.parent.name}/{p.name}" for p in removed]}


@router.post("/api/backups/restore")
def backup_restore(body: Restore, request: Request):
    farm = farm_of(request)
    name, manifest = _known(farm, body.archive)
    note(request, archive=name, force=body.force or None)
    if manifest.get("kind") == "superfarm-farm-backup":
        if body.id is not None:
            raise ValueError("a farm backup restores settings, not one Android: leave `id` out")

        def farm_work(_task) -> dict:
            result = farm.backups.restore_farm(name, force=body.force, trust=body.trust,
                                                      restore_config=body.restore_config,
                                                      restore_secrets=body.restore_secrets)
            farm.reload_config()        # config.yaml may be among what was restored
            return result

        return accepted(request, farm.tasks.submit("backup.restore-farm", f"restore farm settings from {name}", farm_work))
    target = instance_id(farm, body.id) if body.id is not None else None

    def work(task) -> dict:
        # Where the data being replaced is copied to first (backup.auto_before_risky). The
        # farm keeps the latest one in the Android's state; only a new value is this restore's.
        earlier = farm.store.state_get(target, "safety_backup") if target is not None else None
        restored = farm.backups.restore(name, target, task=task, force=body.force, keep_network=body.keep_network)
        safety = farm.store.state_get(restored, "safety_backup") if target is not None else None
        task.set_result(restored, True, f"restored from {name}")
        return {"id": restored, "archive": name, "new": target is None,
                "safety_backup": str(safety) if safety and safety != earlier else ""}

    title = f"restore {name} " + (f"over Android {target}" if target is not None else "as a new Android")
    # exclusive: of two restores over one Android (a double click) the second is refused with 409 `busy`.
    return accepted(request, farm.tasks.submit("backup.restore", title, work, exclusive=True,
                                               targets=[target] if target is not None else []))


@router.delete("/api/backups")
def backup_delete(request: Request, body: Archive | None = None, archive: str | None = None) -> dict:
    """The name comes in the body or as `?archive=` (not every client sends a body with DELETE)."""
    farm = farm_of(request)
    name, _ = _known(farm, body.archive if body else archive or "")
    note(request, archive=name)
    farm.backups.delete(name)
    return {"deleted": name}


@router.get("/api/backups/download")
def backup_download(archive: str, request: Request) -> FileResponse:
    farm = farm_of(request)
    name, manifest = _known(farm, archive)
    if manifest.get("includes_secrets"):
        # Secrets are write-only through the API. This archive holds VPN credentials and
        # tokens; whoever may read them can copy the file from the backup directory.
        raise ApiError(403, "forbidden", f"{name} was made with secrets included and is not served over the API; "
                                         "copy it from the backup directory on the machine")
    path = farm.backups.data_file(name)
    if not path.is_file():
        raise KeyError(f"the data file of {name} is missing")
    return FileResponse(path, filename=f"{name.replace('/', '-')}-{path.name}", media_type="application/octet-stream")

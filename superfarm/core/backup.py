"""Backups: the farm's safety net.

A backup of one Android is a folder:

    <backup dir>/android-<id>/<UTC time>/
        data.tar.zst     the data volume (data.tar.gz / data.tar with other backup.compression)
        instance.yaml    the Android's settings, including its device identity (it belongs to the data)
        manifest.json    Android version, image, size and sha256 of every file, farm version

A farm backup holds the settings of the whole farm and no Android data:

    <backup dir>/farm/<UTC time>/
        farm.tar.gz      config.yaml, every instance.yaml + state.json, automation/, vpn-profiles.json
        manifest.json    with a seal: the archive's checksum signed with this farm's own key

A farm backup decides what runs here (which image, privileged, at the next start), so one
that does not carry this farm's seal is restored in a safe mode: see restore_farm().

The manifest is written last: a folder without one is an interrupted backup and is ignored.
A backup is named by its folder; `android-3/20250101-040000` is enough for one in the backup
directory.

How the copy is made matters more than where it goes:

  * tar runs in a helper container with the volume mounted, because /data is owned by
    Android's users and unreadable for the farm's own user. Its output is streamed to a file
    this process opened, so the archive belongs to the farm user and never passes through RAM.
  * GNU tar with --xattrs --xattrs-include='*' --acls --numeric-owner --sparse. Android keys
    file access on numeric owners and extended attributes; a /data restored without them
    boots but apps lose their data or crash.
  * A running Android is stopped for the copy by default (backup.stop_for_backup) and started
    again afterwards. A live copy is possible but is marked as not consistent.
  * Restoring never writes into a running Android, never replaces existing data without
    `force`, and a copy restored *next to* its original comes up with networking off: two
    Androids with one identity must not be online together.

Safety backups (backup.auto_before_risky): before an Android's data is deleted, replaced by
a restore, or booted on another Android version, safety() takes a backup first. If that
backup cannot be made the operation is refused, unless the operator forced it.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import yaml

from .. import __version__
from ..config import Config
from ..util.files import atomic_write, inside, locked, read_json, read_yaml, write_json
from ..util.proc import kill_tree, spawn
from . import device_profile
from .images import ImageError
from .models import DeviceIdentity, InstanceSpec, check_id
from .tasks import Cancelled, Task

if TYPE_CHECKING:
    from .instances import InstanceManager

log = logging.getLogger("superfarm.backup")

MANIFEST_KIND = "superfarm-android-backup"
FARM_KIND = "superfarm-farm-backup"
MANIFEST_FORMAT = 2
MANIFEST = "manifest.json"
SPEC_FILE = "instance.yaml"
FARM_ARCHIVE = "farm.tar.gz"
VPN_NAMES = "vpn-profiles.json"
SCHEDULE_STATE = ".schedule.json"
# <home>/secrets/farm-key: the key this farm seals its own farm backups with, so a restore
# can tell them from an archive somebody else made. It never goes into a backup.
FARM_KEY = "farm-key"
TAR_FLAGS = ["--xattrs", "--xattrs-include=*", "--acls", "--numeric-owner", "--sparse"]
_DATA_FILE = {"zstd": "data.tar.zst", "gzip": "data.tar.gz", "none": "data.tar"}
_COMPRESSION = {name: kind for kind, name in _DATA_FILE.items()}
# A backup in the backup directory, by name. Nothing else is accepted as a relative name, so
# a name can never point outside that directory.
_HANDLE = re.compile(r"^(android-\d{1,4}|farm)/\d{8}-\d{6}(-\d+)?$")
_PLAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
# What a farm backup may contain, and so what a restore will write. `vpn` and `secrets` only
# when the backup was made with include_secrets.
_FARM_TOP = ("config.yaml", "instances", "automation", "vpn", "secrets", VPN_NAMES)
_AUTOMATION_PARTS = ("datasets", "programs", "flows", "profiles", "values", "schedules.json")
REASONS = ("manual", "scheduled", "before-delete", "before-restore", "before-version-change")


class BackupError(RuntimeError):
    """A backup or restore could not be completed; the message says what to do."""


def sha256_file(path: Path, task: Task | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if task is not None:
                task.check_cancel()
            block = f.read(4 * 1024 * 1024)
            if not block:
                return h.hexdigest()
            h.update(block)


def create_arguments(compression: str, level: int) -> list[str]:
    """tar arguments that write an archive of /data to stdout."""
    if compression == "zstd":
        # Four threads: quick enough, and it leaves the machine to the Androids still running.
        pack = ["-I", f"zstd -T4 -{max(1, min(19, int(level)))}"]
    elif compression == "gzip":
        pack = ["-I", f"pigz -p 4 -{max(1, min(9, int(level)))}"]
    elif compression == "none":
        pack = []
    else:
        raise BackupError(f"unknown compression {compression!r}")
    # A file that changes or disappears while a live Android is read is expected, not an error.
    return ["tar", "-C", "/data", *TAR_FLAGS, "--warning=no-file-changed", "--warning=no-file-removed",
            "--warning=no-file-ignored", *pack, "-cf", "-", "."]


def extract_arguments(compression: str) -> list[str]:
    """tar arguments that unpack an archive from stdin into /data. The compression has to be
    named: tar cannot detect it on a pipe."""
    unpack = {"zstd": ["--zstd"], "gzip": ["--gzip"], "none": []}
    if compression not in unpack:
        raise BackupError(f"unknown compression {compression!r}")
    return ["tar", "-C", "/data", *TAR_FLAGS, *unpack[compression], "-xpf", "-"]


def select_for_pruning(entries: list[dict], keep: int) -> list[dict]:
    """Which backups retention removes: all but the newest `keep` of each Android (and of
    the farm backups). keep = 0 keeps everything.

    An Android is its id *and* its android_id: after an Android is deleted its number can
    be given to a new one, and that newcomer's backups must not push out the last copies
    of the one that is gone."""
    if keep <= 0:
        return []
    seen: dict[tuple, int] = {}
    out = []
    for entry in sorted(entries, key=lambda e: e.get("created", 0), reverse=True):
        key = (entry.get("kind"), entry.get("id"), entry.get("android_id"))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > keep:
            out.append(entry)
    return out


def _new_folder(parent: Path) -> Path:
    """A fresh <parent>/<UTC time> folder, owner-only. Two backups within one second get
    different names."""
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    n = 1
    while True:
        folder = parent / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            folder.mkdir(mode=0o700)
            return folder
        except FileExistsError:
            n += 1


def _file_entry(path: Path, task: Task | None = None) -> dict:
    return {"size": path.stat().st_size, "sha256": sha256_file(path, task)}


class Backups:
    timeout = 6 * 3600          # one volume; a full Android is a few GB and takes minutes

    def __init__(self, mgr: InstanceManager):
        self.mgr = mgr
        self._scheduled: Task | None = None

    @property
    def directory(self) -> Path:
        configured = self.mgr.config.backup.dir
        return Path(configured).expanduser() if configured else self.mgr.paths.backups

    # ------------------------------------------------------------------ plumbing
    def _helper(self, id: int, mounts: list[str], command: list[str], *, stdin: IO | None = None,
                stdout: IO | None = None, task: Task | None = None, ok_codes: tuple[int, ...] = (0,)) -> None:
        """Run tar in a helper container with a file of ours on its stdin or stdout."""
        mgr = self.mgr
        try:
            image = mgr.images.helper(gnu_tar=True, task=task)
        except ImageError as e:
            raise BackupError(str(e)) from None
        name = f"{mgr.prefix}-backup-{int(id)}-{secrets.token_hex(3)}"
        argv = [mgr.docker.binary, "run", "--rm", "--name", name, "--network", "none"]
        if stdin is not None:
            argv.append("-i")
        for key, value in mgr.names(id).labels("helper").items():
            argv += ["--label", f"{key}={value}"]
        for mount in mounts:
            argv += ["-v", mount]
        argv += [image, *command]
        with tempfile.TemporaryFile() as errors:
            child = spawn(argv, stdin=stdin if stdin is not None else subprocess.DEVNULL, stdout=stdout, stderr=errors)
            deadline = time.monotonic() + self.timeout
            try:
                while True:
                    try:
                        child.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if task is not None and task.cancelled:
                        raise Cancelled()
                    if time.monotonic() > deadline:
                        raise BackupError(f"gave up after {self.timeout // 3600} h; the helper container was removed")
            except BaseException:
                # Stopping the client does not stop the container it started.
                kill_tree(child.pid)
                mgr.docker.rm(name)
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                raise
            if child.returncode not in ok_codes:
                errors.seek(0)
                tail = errors.read().decode(errors="replace").strip().splitlines()[-3:]
                raise BackupError(f"tar failed (exit {child.returncode}): {' | '.join(tail)[:500] or 'no output'}")

    def locate(self, archive: str | os.PathLike) -> Path:
        """The folder of a backup, from its name (`android-3/20250101-040000`, in the backup
        directory), its folder, or any file inside it."""
        text = str(archive)
        path = Path(text).expanduser()
        if not path.is_absolute():
            if not _HANDLE.match(text):
                raise BackupError(f"bad backup name {text!r}: use a name as shown by `sf backup list` "
                                  "(android-<id>/<time>) or a full path")
            path = self.directory / text
        if path.is_file():
            path = path.parent
        if not (path / MANIFEST).is_file():
            raise BackupError(f"{path} is not a backup (it has no {MANIFEST}); `sf backup list` shows what is available")
        return path

    def manifest(self, archive: str | os.PathLike) -> dict | None:
        """The manifest of a backup, or None when there is none or it is not ours."""
        try:
            data = read_json(self.locate(archive) / MANIFEST, None)
        except BackupError:
            return None
        if not isinstance(data, dict) or data.get("kind") not in (MANIFEST_KIND, FARM_KIND):
            return None
        files = data.get("files")
        if not isinstance(files, dict) or not all(isinstance(n, str) and _PLAIN.match(n) for n in files):
            return None           # file names are joined to the folder: plain names only
        return data

    def data_file(self, archive: str | os.PathLike) -> Path:
        """The one large file of a backup (what a download serves)."""
        folder = self.locate(archive)
        manifest = self.manifest(folder) or {}
        name = str(manifest.get("data") or "")
        if name not in manifest.get("files", {}):
            raise BackupError(f"{folder} has no usable {MANIFEST}")
        return folder / name

    # -------------------------------------------------------------------- create
    def create(self, id: int, dest: str | os.PathLike | None = None, task: Task | None = None, *,
               reason: str = "manual") -> Path:
        """Back up one Android's /data. Returns the backup's folder."""
        mgr = self.mgr
        spec = mgr.store.get(check_id(id))
        names = mgr.names(spec.id)
        volume = mgr.volume_of(spec)
        root = Path(dest).expanduser() if dest else self.directory
        cfg = mgr.config.backup
        with mgr.locked(spec.id, "backup"):
            if not mgr.docker.volume_exists(volume):
                raise BackupError(f"Android {spec.id} has no data yet (volume {volume} does not exist); start it once first")
            was_running = mgr._ours(names.android) and mgr.docker.running(names.android)
            stopped = was_running and cfg.stop_for_backup
            if stopped:
                _say(task, f"Android {spec.id}: stopping for a consistent copy")
                mgr._stop(spec.id)
            try:
                folder = self._write(spec, volume, root, consistent=not was_running or stopped, reason=reason, task=task)
            finally:
                if stopped:
                    try:
                        mgr._start(spec.id, None, recreate=False, replacing=True)
                    except Exception as e:  # noqa: BLE001 - the backup itself succeeded or failed on its own
                        log.error("Android %s was stopped for its backup and could not be started again: %s", spec.id, e)
                        if task is not None:
                            task.step(f"Android {spec.id}: backup done, but it did not start again: {e}"[:300])
        return folder

    def _write(self, spec: InstanceSpec, volume: str, root: Path, *, consistent: bool, reason: str,
               task: Task | None) -> Path:
        mgr, cfg = self.mgr, self.mgr.config.backup
        if reason not in REASONS:
            raise BackupError(f"unknown backup reason {reason!r}")
        folder = _new_folder(root / f"android-{spec.id}")
        data = folder / _DATA_FILE[cfg.compression]
        _say(task, f"Android {spec.id}: copying data")
        try:
            # 0600 from the first byte: the archive holds every app's logins.
            fd = os.open(data, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                # Exit 1 is tar's "a file changed while I read it": normal for a live Android,
                # a real problem for a stopped one.
                self._helper(spec.id, [f"{volume}:/data:ro"], create_arguments(cfg.compression, cfg.level),
                             stdout=out, task=task, ok_codes=(0,) if consistent else (0, 1))
                out.flush()
                os.fsync(out.fileno())
            atomic_write(folder / SPEC_FILE, yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False,
                                                            allow_unicode=True), mode=0o600)
            _say(task, f"Android {spec.id}: computing checksums")
            state = mgr.store.state(spec.id)
            try:
                r = mgr._resolve(spec)
                version, image = r.android_version, r.image
            except ValueError:
                version, image = spec.android_version or "", spec.image or ""
            manifest = {
                "kind": MANIFEST_KIND, "format": MANIFEST_FORMAT, "superfarm_version": __version__,
                "id": spec.id, "name": spec.display_name, "android_id": spec.device.android_id,
                "created": time.time(), "reason": reason, "consistent": consistent,
                # The release that wrote this /data, when the farm knows it; else the configured one.
                "android_version": str(state.get("data_android_version") or version), "image": image,
                "farm": mgr.prefix, "data_volume": volume, "renderer": state.get("renderer") or "",
                "compression": cfg.compression, "data": data.name,
                "files": {data.name: _file_entry(data, task), SPEC_FILE: _file_entry(folder / SPEC_FILE)},
            }
            atomic_write(folder / MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False), mode=0o600)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        _say(task, f"Android {spec.id}: backup {self._name(folder)} ({manifest['files'][data.name]['size'] // 1048576} MB)")
        return folder

    def _name(self, folder: Path) -> str:
        return f"{folder.parent.name}/{folder.name}"

    def create_many(self, selector: str, *, reason: str = "manual", prune: bool = False,
                    exclusive: bool = False) -> Task:
        """Back up every Android a selector names, one after the other (each copy is heavy
        on the disk), as one task with a result per Android. An Android that was never
        started has nothing to back up and is reported as skipped, not as failed.

        `exclusive` is for an operator's own request (see core.tasks): TaskBusy is raised
        while another task has not finished with one of these Androids, so a repeated click
        does not copy everything twice. The scheduled run leaves it off and takes each
        Android as it comes free."""
        mgr = self.mgr
        ids = mgr.select(selector)

        def work(task: Task) -> dict:
            task.step(f"0/{len(ids)}", current=0, total=len(ids))
            for id in ids:
                task.check_cancel()
                try:
                    if not mgr.docker.volume_exists(mgr.volume_of(mgr.store.get(id))):
                        task.set_result(id, True, "skipped: no data yet")
                    else:
                        task.set_result(id, True, str(self.create(id, task=task, reason=reason)))
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - one Android's failure is a result, not a crash
                    task.set_result(id, False, f"{type(e).__name__}: {e}")
                task.advance()
            removed = self.prune() if prune else []
            failed = sum(1 for r in task.results.values() if not r["ok"])
            task.step(f"{len(ids) - failed} of {len(ids)} backed up" + (f", {len(removed)} old backup(s) removed" if removed else ""))
            return {"ok": len(ids) - failed, "failed": failed, "pruned": [str(p) for p in removed]}

        return mgr.tasks.submit(f"backup.{reason}", f"back up {len(ids)} Android(s)", work, targets=ids,
                                exclusive=exclusive)

    # -------------------------------------------------------------------- safety
    def safety(self, spec: InstanceSpec, reason: str, *, force: bool = False, task: Task | None = None) -> tuple[str, str]:
        """The backup taken before an operation that can lose an Android's data.

        The caller holds the Android's lock and has stopped it. Returns (backup folder,
        warning); both are "" when backup.auto_before_risky is off or there is no data.
        When the backup fails the operation must not go ahead: NeedsForce is raised, unless
        `force`, in which case the failure comes back as the warning."""
        mgr = self.mgr
        if not mgr.config.backup.auto_before_risky:
            return "", ""
        volume = mgr.volume_of(spec)
        if not mgr.docker.volume_exists(volume):
            return "", ""
        try:
            folder = self._write(spec, volume, self.directory, consistent=True, reason=reason, task=task)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - whatever stopped the backup stops the operation
            if not force:
                from .instances import NeedsForce
                raise NeedsForce(
                    f"Android {spec.id}: the safety backup ({reason}) could not be made, so nothing was changed: {e}. "
                    f"Fix that and try again, repeat with force to go ahead without a backup, or turn "
                    f"backup.auto_before_risky off") from None
            log.error("Android %s: safety backup (%s) failed, going ahead because the operation was forced: %s",
                      spec.id, reason, e)
            return "", f"the safety backup ({reason}) failed and the operation went ahead because it was forced: {e}"[:400]
        log.info("Android %s: safety backup (%s) at %s", spec.id, reason, folder)
        return str(folder), ""

    # -------------------------------------------------------------------- verify
    def verify(self, archive: str | os.PathLike, *, deep: bool = False, task: Task | None = None) -> bool:
        """True when every file of the backup matches the size and checksum recorded when it
        was made. With `deep` the data archive is also read end to end by tar, which catches
        a backup that was already damaged when its checksum was taken."""
        try:
            folder = self.locate(archive)
        except BackupError:
            return False
        manifest = self.manifest(folder)
        if not manifest or not manifest["files"]:
            return False
        for name, recorded in manifest["files"].items():
            path = folder / name
            if not path.is_file() or not isinstance(recorded, dict) or recorded.get("size") != path.stat().st_size:
                return False
            if sha256_file(path, task) != recorded.get("sha256"):
                return False
        if deep and manifest["kind"] == MANIFEST_KIND:
            data = folder / str(manifest.get("data"))
            unpack = {"zstd": ["--zstd"], "gzip": ["--gzip"], "none": []}.get(_COMPRESSION.get(data.name, ""), [])
            try:
                with open(data, "rb") as source, open(os.devnull, "wb") as sink:
                    self._helper(0, [], ["tar", *unpack, "-tf", "-"], stdin=source, stdout=sink, task=task)
            except (BackupError, OSError):
                return False
        return True

    # ------------------------------------------------------------------- restore
    def restore(self, archive: str | os.PathLike, id: int | None = None, task: Task | None = None, *,
                force: bool = False, keep_network: bool = False) -> int:
        """Restore a backup. Returns the id of the Android that now holds the data.

        id=None    a *new* Android is created for the data; nothing existing is touched. If
                   the Android the backup came from still exists here, the copy is created
                   with networking off (two devices with one identity must not be online
                   together) unless `keep_network`.
        id=N       Android N's current data is replaced. It must be stopped, and `force`
                   must be given; its current data is backed up first
                   (backup.auto_before_risky)."""
        mgr = self.mgr
        folder = self.locate(archive)
        manifest = self.manifest(folder)
        label = self._name(folder)
        if manifest is None or manifest["kind"] != MANIFEST_KIND:
            raise BackupError(f"{label} is not a backup of an Android (no usable {MANIFEST})")
        if id is not None and not force:
            from .instances import NeedsForce
            target = mgr.store.get(check_id(id)).id
            raise NeedsForce(
                f"restoring {label} over Android {target} replaces everything that Android holds now. "
                f"Repeat with force to do that, or restore without an id to get the backup as a new Android")
        _say(task, f"checking {label}")
        if not self.verify(folder, task=task):
            raise BackupError(f"{label} does not match its recorded checksums; it is damaged or incomplete "
                              "and was not restored")
        saved = read_yaml(folder / SPEC_FILE, {}) if SPEC_FILE in manifest["files"] else {}
        saved = saved if isinstance(saved, dict) else {}
        try:
            identity = DeviceIdentity.model_validate(saved.get("device") or {})
        except ValueError:
            identity = DeviceIdentity()
        data = folder / str(manifest.get("data"))
        compression = _COMPRESSION.get(data.name)
        if compression is None or data.name not in manifest["files"]:
            raise BackupError(f"{label}: the manifest names no data archive")
        written_by = str(manifest.get("android_version") or "")

        created = id is None
        if created:
            target = self._new_instance(saved, identity, label, written_by=written_by, keep_network=keep_network)
        else:
            target = mgr.store.get(check_id(id)).id
        try:
            with mgr.locked(target, "restoring"):
                spec = mgr.store.get(target)
                names = mgr.names(target)
                if mgr._ours(names.android) and mgr.docker.running(names.android):
                    raise BackupError(f"Android {target} is running; stop it first (`sf stop {target}`). "
                                      "A backup is never restored over a running Android")
                volume = mgr.volume_of(spec)
                if not mgr.docker.volume_exists(volume):
                    if spec.data_volume:
                        raise BackupError(f"adopted volume {volume} of Android {target} does not exist")
                    mgr.docker.volume_create(volume, names.labels("data"))
                elif not created:
                    _say(task, f"Android {target}: backing up its current data first")
                    saved_to, warning = self.safety(spec, "before-restore", force=force, task=task)
                    mgr.store.state_set(target, "safety_backup", saved_to or None)
                    if warning and task is not None:
                        task.step(f"Android {target}: {warning}"[:300])
                _say(task, f"Android {target}: clearing old data")
                self._helper(target, [f"{volume}:/data"], ["find", "/data", "-mindepth", "1", "-delete"], task=task)
                _say(task, f"Android {target}: restoring {label}")
                try:
                    with open(data, "rb") as source:
                        self._helper(target, [f"{volume}:/data"], extract_arguments(compression), stdin=source, task=task)
                except BaseException as e:
                    if isinstance(e, BackupError):
                        raise BackupError(f"{e}. The data of Android {target} is now incomplete: run the restore again") from None
                    raise
                if not created and identity != spec.device:
                    # The identity travels with the data: apps in the restored /data know the
                    # device they were set up on.
                    spec.device = identity
                    mgr.store.save(spec)
                # What the farm remembered about the old data no longer holds; the markers
                # inside the restored /data are the truth and are re-read at the next start.
                for key in ("renderer", "trim", "proxy", "last_error", "warnings"):
                    mgr.store.state_set(target, key, None)
                mgr.store.state_set(target, "data_android_version", written_by or None)
        except BaseException:
            if created:
                mgr._discard_new(target)
            raise
        _say(task, f"Android {target}: restored from {label}")
        return target

    def _new_instance(self, saved: dict, identity: DeviceIdentity, source: str, *, written_by: str,
                      keep_network: bool) -> int:
        """Create the Android a backup is restored into, as close to the saved one as this farm allows."""
        mgr = self.mgr
        keep = ("name", "tags", "notes", "android_version", "image", "width", "height", "dpi", "fps", "gpu_mode",
                "gpu_node", "resources", "network", "camera", "extra_boot_args", "extra_props")
        template = {k: saved[k] for k in keep if saved.get(k) not in (None, "", [], {})}
        if "android_version" not in template and written_by in mgr.config.android.images:
            # The saved Android followed the farm default of its day; this farm's default may
            # be another release by now, and the data must come up on the one that wrote it.
            template["android_version"] = written_by
        image = template.get("image")
        if image and not mgr.docker.image_exists(str(image)):
            # A manifest is a plain file next to the archive. It may describe how an Android
            # was set up, but it must not be able to make this farm fetch and run (privileged)
            # an image it names; only an image already on this machine is taken over.
            template.pop("image")
        twin = next((i for i, s in mgr.store.list().items()
                     if identity.android_id and s.device.android_id == identity.android_id), None)
        note = f"Restored from {source}."
        offline = twin is not None and not keep_network
        if offline:
            # The original still exists here. Same identity, same accounts: the copy must not
            # appear online beside it until the operator decides which one is to live.
            template["network"] = {"mode": "none"}
            note += (f" Android {twin} has the same identity, so this copy starts with networking off;"
                     " change its network mode once only one of them is in use.")
        template["notes"] = (str(template.get("notes") or "") + "\n" + note).strip()[:2000]
        device = identity.model_dump()
        if device["profile"] not in device_profile.profiles():
            device["profile"] = "none"      # a preset this version does not know; its properties are kept as they are
        template["device"] = device
        try:
            return mgr.create(template=template)[0].id
        except ValueError as e:
            # Saved settings can point at things this farm does not have (a gateway, an image).
            log.warning("restore: saved settings not usable here (%s); using farm defaults", e)
            fallback = {"name": template.get("name", ""), "notes": template["notes"], "device": template["device"]}
            if offline:
                fallback["network"] = {"mode": "none"}
            return mgr.create(template=fallback)[0].id

    # ---------------------------------------------------------------- list/prune
    def list(self, folder: str | os.PathLike | None = None, *, kind: str = "android") -> list[dict]:
        """Backups in the backup directory, newest first. kind: android | farm | all."""
        root = Path(folder).expanduser() if folder else self.directory
        out: list[dict] = []
        if not root.is_dir():
            return out
        wanted = {"android": (MANIFEST_KIND,), "farm": (FARM_KIND,), "all": (MANIFEST_KIND, FARM_KIND)}[kind]
        for meta in root.glob(f"*/*/{MANIFEST}"):
            name = f"{meta.parent.parent.name}/{meta.parent.name}"
            data = read_json(meta, None)
            if not _HANDLE.match(name) or not isinstance(data, dict) or data.get("kind") not in wanted:
                continue
            files = data.get("files") if isinstance(data.get("files"), dict) else {}
            main = str(data.get("data") or "")
            entry = files.get(main) if isinstance(files.get(main), dict) else {}
            out.append({
                "name": name, "dir": str(meta.parent), "file": str(meta.parent / main) if main else "",
                "kind": "farm" if data["kind"] == FARM_KIND else "android",
                "id": data.get("id"), "instance_name": data.get("name", ""), "android_id": data.get("android_id", ""),
                "created": data.get("created", 0), "reason": data.get("reason", ""),
                "size": entry.get("size", 0), "sha256": entry.get("sha256", ""),
                "compression": data.get("compression", ""), "consistent": bool(data.get("consistent")),
                "android_version": data.get("android_version", ""),
                "includes_secrets": bool(data.get("includes_secrets")),
                "present": bool(main) and (meta.parent / main).is_file(),
            })
        return sorted(out, key=lambda b: b["created"], reverse=True)

    def delete(self, archive: str | os.PathLike) -> None:
        """Remove one backup. Only a backup inside the backup directory, and only the files
        its manifest lists: this is reachable from the API, and must never become a way to
        delete some other folder."""
        folder = self.locate(archive)
        if folder.resolve().parent.parent != self.directory.resolve() or not _HANDLE.match(self._name(folder)):
            raise BackupError(f"{folder} is not a backup inside the backup directory {self.directory}; "
                              "remove it by hand")
        manifest = self.manifest(folder)
        if manifest is None:
            raise BackupError(f"{folder} has no usable {MANIFEST}; remove it by hand")
        for name in [*manifest["files"], MANIFEST]:
            (folder / name).unlink(missing_ok=True)
        for leftover in (folder, folder.parent):
            try:
                leftover.rmdir()            # only when empty: a stranger's file is never removed
            except OSError:
                break

    def prune(self) -> list[Path]:
        """Apply retention: delete all but the newest backup.keep backups of each Android
        (and of the farm backups) in the backup directory. Returns the removed folders."""
        removed: list[Path] = []
        for entry in select_for_pruning(self.list(kind="all"), self.mgr.config.backup.keep):
            try:
                self.delete(entry["dir"])
            except BackupError as e:
                log.warning("retention: %s", e)
                continue
            removed.append(Path(entry["dir"]))
        return removed

    # ------------------------------------------------------------------ schedule
    def tick(self, now: float | None = None) -> Task | None:
        """Called by the daemon about every half minute. Starts the scheduled backup
        (backup.schedule, a cron line; backup.schedule_selector) when it is due and returns
        its task; otherwise None. Retention is applied at the end of each scheduled run.

        The next due time is kept on disk, so a daemon restart neither repeats nor forgets a
        run. A run missed while the daemon was down is made up once, not once per missed
        occurrence; a run that is due while the previous one is still going is skipped."""
        cfg = self.mgr.config.backup
        expression = cfg.schedule.strip()
        if not expression:
            return None
        from ..automation.schedule import cron_next, parse_cron      # one cron dialect for the whole farm
        now = time.time() if now is None else float(now)
        try:
            following = cron_next(parse_cron(expression), datetime.fromtimestamp(now)).timestamp()
        except ValueError as e:
            log.warning("backup.schedule %r is not usable: %s", expression, e)
            return None
        state_file = self.directory / SCHEDULE_STATE
        with locked(state_file):
            state = read_json(state_file, {}) or {}
            known = state.get("schedule") == expression and isinstance(state.get("next_run"), (int, float))
            due = known and now >= state["next_run"]
            busy = self._scheduled is not None and self._scheduled.active
            if not known:
                state = {"schedule": expression, "last_run": 0, "skipped": 0}      # first sight: wait for the next occurrence
            elif due and busy:
                state["skipped"] = int(state.get("skipped", 0)) + 1
            elif due:
                state["last_run"] = now
            if not known or due:
                state["next_run"] = following
                write_json(state_file, state, mode=0o600)
        if not due or busy:
            return None
        self._scheduled = self.create_many(cfg.schedule_selector, reason="scheduled", prune=True)
        return self._scheduled

    def schedule_status(self) -> dict:
        """{"schedule", "selector", "next_run", "last_run", "skipped", "error"} for the panel."""
        cfg = self.mgr.config.backup
        out: dict[str, Any] = {"schedule": cfg.schedule.strip(), "selector": cfg.schedule_selector, "next_run": 0,
                               "last_run": 0, "skipped": 0, "error": ""}
        if not out["schedule"]:
            return out
        from ..automation.schedule import parse_cron
        try:
            parse_cron(out["schedule"])
        except ValueError as e:
            out["error"] = str(e)
            return out
        state = read_json(self.directory / SCHEDULE_STATE, {}) or {}
        if state.get("schedule") == out["schedule"]:
            out.update({k: state.get(k, 0) for k in ("next_run", "last_run", "skipped")})
        return out

    # --------------------------------------------------------------- farm backup
    def _farm_members(self, include_secrets: bool) -> list[tuple[str, Path | bytes]]:
        """(name in the archive, file or content) for everything a farm backup holds."""
        mgr, home = self.mgr, self.mgr.paths.home
        members: list[tuple[str, Path | bytes]] = []

        def add_tree(top: Path, skip: Any = None) -> None:
            for folder, dirs, files in os.walk(top, followlinks=False):
                dirs.sort()
                for name in sorted(files):
                    path = Path(folder) / name
                    # Regular files only: a link is never followed out of the farm home.
                    if path.is_symlink() or not path.is_file() or name.endswith(".lock") or (skip and skip(path)):
                        continue
                    members.append((str(path.relative_to(home)), path))

        if mgr.paths.config.is_file():
            members.append(("config.yaml", mgr.paths.config))
        for id in mgr.store.ids():
            for name in ("instance.yaml", "state.json"):
                path = mgr.paths.instance(id) / name
                if path.is_file():
                    members.append((f"instances/{id}/{name}", path))

        from ..automation.values import looks_secret
        words = mgr.config.automation.forbidden_field_words
        values = mgr.paths.automation / "values"

        def is_values_json(path: Path) -> bool:
            return path.suffix == ".json" and (path.parent == values or path.parent == values / "instances")

        def skip_value_file(path: Path) -> bool:
            # The automation module refuses to store secret-named values, so there should be
            # none; a hand-edited file is filtered below and a secret-named pool left out.
            if is_values_json(path):
                return True
            return path.parent == values / "pools" and looks_secret(path.name.split(".")[0], words)

        for part in _AUTOMATION_PARTS:
            path = mgr.paths.automation / part
            if path.is_file() and not path.is_symlink():
                members.append((str(path.relative_to(home)), path))
            elif path.is_dir():
                add_tree(path, skip_value_file if part == "values" else None)
        for path in sorted([values / "shared.json", *(values / "instances").glob("*.json")]):
            data = read_json(path, None)
            if isinstance(data, dict):
                kept = {k: v for k, v in data.items() if not looks_secret(str(k), words)}
                members.append((str(path.relative_to(home)), json.dumps(kept, indent=2, ensure_ascii=False).encode()))

        profiles = sorted(p.name for p in mgr.paths.vpn.iterdir() if p.is_file()) if mgr.paths.vpn.is_dir() else []
        members.append((VPN_NAMES, json.dumps(profiles, indent=2).encode()))
        if include_secrets:
            for top in (mgr.paths.vpn, mgr.paths.secrets):
                if top.is_dir():
                    add_tree(top, lambda path: path == mgr.paths.secrets / FARM_KEY)
        return members

    def _seal(self, digest: str, *, create: bool = False) -> str:
        """The checksum of a farm archive signed with this farm's key (HMAC-SHA256), or ""
        when the farm has no key. The checksum alone proves nothing about where an archive
        comes from: it sits in a plain file next to it. The key never leaves the farm home."""
        path = self.mgr.paths.secrets / FARM_KEY
        if create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with locked(path):
                if not path.is_file():
                    atomic_write(path, secrets.token_hex(32), mode=0o600)
        try:
            key = path.read_bytes().strip()
        except OSError:
            return ""
        if len(key) < 32:
            return ""                 # a damaged key file seals nothing
        return hmac.new(key, f"{FARM_KIND}:{digest}".encode(), hashlib.sha256).hexdigest()

    def create_farm(self, dest: str | os.PathLike | None = None, *, include_secrets: bool | None = None) -> Path:
        """Back up the farm's settings: config.yaml, every Android's instance.yaml and
        state.json, automation (datasets, programs, flows, profiles, schedules, values), and
        the *names* of the VPN profiles. No Android data. Returns the backup's folder.

        VPN profiles, credentials and tokens are left out unless `include_secrets` (default:
        backup.include_secrets); the manifest records which it was. Values whose name looks
        like a secret are never included."""
        mgr = self.mgr
        include = mgr.config.backup.include_secrets if include_secrets is None else bool(include_secrets)
        root = Path(dest).expanduser() if dest else self.directory
        folder = _new_folder(root / "farm")
        archive = folder / FARM_ARCHIVE
        try:
            members = self._farm_members(include)
            # Owner-only whether or not secrets are inside: pools and values are private too.
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
                for name, source in members:
                    info = tarfile.TarInfo(name)
                    if isinstance(source, bytes):
                        info.size, info.mode, info.mtime = len(source), 0o600, int(time.time())
                        tar.addfile(info, io.BytesIO(source))
                        continue
                    try:
                        st = source.stat()
                        with open(source, "rb") as f:
                            info.size, info.mode, info.mtime = st.st_size, st.st_mode & 0o777, int(st.st_mtime)
                            tar.addfile(info, f)
                    except FileNotFoundError:
                        continue                  # removed while we were reading the tree
            entry = _file_entry(archive)
            manifest = {
                "kind": FARM_KIND, "format": MANIFEST_FORMAT, "superfarm_version": __version__,
                "created": time.time(), "reason": "manual", "farm": mgr.prefix, "includes_secrets": include,
                "instances": mgr.store.ids(), "entries": len(members), "data": FARM_ARCHIVE,
                "files": {FARM_ARCHIVE: entry}, "seal": self._seal(entry["sha256"], create=True),
            }
            atomic_write(folder / MANIFEST, json.dumps(manifest, indent=2), mode=0o600)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return folder

    def restore_farm(self, archive: str | os.PathLike, *, force: bool = False, trust: bool = False,
                     restore_config: bool = False, restore_secrets: bool = False) -> dict:
        """Merge a farm backup into this farm. Nothing that exists is overwritten unless
        `force`: an Android whose id is taken, a file that is already there, and config.yaml
        are skipped and listed. No Android data is touched either way.

        A backup this farm made itself (it carries the farm's seal) is restored in full. Any
        other archive is somebody else's word about what should run on this machine, so by
        default only what cannot do harm is taken from it:

          * Android definitions lose what decides *what* is run and *when*: an image that is
            not on this machine, an adopted data volume, extra boot arguments and
            properties, autostart. Their saved runtime state and the schedules are left out.
            `trust` keeps all of it as saved (your own farm before a reinstall).
          * config.yaml is taken only with `restore_config`, VPN profiles, credentials and
            tokens only with `restore_secrets`. Neither is implied by `force` or `trust`.

        Returns {"restored": [...], "skipped": [...], "adjusted": [...], "instances": [ids
        restored], "missing_vpn_profiles": [...], "includes_secrets": bool, "own": bool}.
        `skipped` and `adjusted` say, with the reason, everything that was not taken as it
        was. Reload the configuration afterwards when `config.yaml` is among the restored."""
        from ..automation import plainyaml          # the settings inside are text from outside
        mgr, home = self.mgr, self.mgr.paths.home
        folder = self.locate(archive)
        manifest = self.manifest(folder)
        if manifest is None or manifest["kind"] != FARM_KIND:
            raise BackupError(f"{self._name(folder)} is not a farm backup")
        if not self.verify(folder):
            raise BackupError(f"{self._name(folder)} does not match its recorded checksums and was not restored")
        recorded = manifest["files"].get(str(manifest.get("data")))
        if not isinstance(recorded, dict):
            raise BackupError(f"{self._name(folder)} does not say which file is its archive and was not restored")
        seal = self._seal(str(recorded.get("sha256")))
        own = bool(seal) and hmac.compare_digest(str(manifest.get("seal") or ""), seal)
        trusted = own or bool(trust)
        elsewhere = "made by another farm: "
        existing = set(mgr.store.ids())
        invalid: set[int] = set()
        profiles: list[str] = []
        out: dict[str, Any] = {"restored": [], "skipped": [], "adjusted": [], "instances": [], "missing_vpn_profiles": [],
                               "includes_secrets": bool(manifest.get("includes_secrets")), "own": own}
        with tarfile.open(folder / str(manifest["data"]), mode="r:gz") as tar:
            for member in tar:
                parts = Path(member.name).parts
                # Members are written one by one to checked paths; nothing is ever "extracted",
                # so links, devices and names that climb out of the home cannot do anything.
                # Every decision below is made on this spelling, the one the file is written
                # under: `./config.yaml` and `secrets//x` are config.yaml and secrets/x.
                name = "/".join(parts)
                if (not member.isfile() or not parts or parts[0] not in _FARM_TOP or member.name.startswith("/")
                        or any(p in ("..", "") for p in parts) or name == f"secrets/{FARM_KEY}"):
                    out["skipped"].append(f"{member.name} (not a farm file)")
                    continue
                source = tar.extractfile(member)
                content = source.read() if source is not None else b""
                if name == VPN_NAMES:
                    try:
                        profiles = [str(n) for n in json.loads(content)]
                    except (ValueError, TypeError):
                        profiles = []
                    continue
                try:
                    target = inside(home, home / name)
                except ValueError:
                    out["skipped"].append(f"{name} (outside the farm home)")
                    continue
                if parts[0] == "instances":
                    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("instance.yaml", "state.json"):
                        out["skipped"].append(f"{name} (not a farm file)")
                        continue
                    if int(parts[1]) in invalid:
                        continue
                    if int(parts[1]) in existing and not force:
                        if parts[2] == "instance.yaml":
                            out["skipped"].append(f"Android {parts[1]} (exists)")
                        continue
                    if parts[2] == "instance.yaml":
                        try:
                            spec = InstanceSpec.model_validate({**(plainyaml.load(content) or {}), "id": int(parts[1])})
                        except (ValueError, TypeError, yaml.YAMLError) as e:
                            out["skipped"].append(f"Android {parts[1]} (settings not valid here: {str(e)[:120]})")
                            invalid.add(int(parts[1]))         # and its state.json with it
                            continue
                        if not trusted:
                            changes = self._neutralise(spec)
                            if changes:
                                out["adjusted"].append(f"Android {spec.id}: " + "; ".join(changes))
                            # What was checked is what is written, not the archive's own text.
                            content = yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False, allow_unicode=True).encode()
                        out["instances"].append(int(parts[1]))
                    elif not trusted:
                        # What another farm observed about its own data says nothing about ours.
                        out["skipped"].append(f"{name} ({elsewhere}restore with `trust` to take it)")
                        continue
                elif name == "config.yaml" and not (own or restore_config):
                    out["skipped"].append(f"{name} ({elsewhere}restore with `restore_config` to take it)")
                    continue
                elif parts[0] in ("secrets", "vpn") and not (own or restore_secrets):
                    out["skipped"].append(f"{name} ({elsewhere}restore with `restore_secrets` to take it)")
                    continue
                elif name == "automation/schedules.json" and not trusted:
                    # A schedule starts runs on its own; nobody here asked for these.
                    out["skipped"].append(f"{name} ({elsewhere}restore with `trust` to take it)")
                    continue
                elif target.exists() and not force:
                    out["skipped"].append(f"{name} (exists)")
                    continue
                if name == "config.yaml":
                    try:
                        Config.model_validate(plainyaml.load(content) or {})
                    except (ValueError, TypeError, yaml.YAMLError) as e:
                        # A config the daemon cannot load would stop it at its next start.
                        out["skipped"].append(f"{name} (not a valid configuration: {str(e)[:120]})")
                        continue
                private = parts[0] in ("secrets", "vpn") or "values" in parts
                if parts[0] == "secrets":
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write(target, content, mode=0o600 if private else 0o644)
                out["restored"].append(name)
        out["instances"].sort()
        # Judged at the end: a backup made with secrets has just brought its profiles along.
        out["missing_vpn_profiles"] = [n for n in profiles if n == os.path.basename(n) and not (mgr.paths.vpn / n).is_file()]
        return out

    def _neutralise(self, spec: InstanceSpec) -> list[str]:
        """Take out of an Android definition from somebody else's archive everything that
        decides what is run on this machine and when. Returns what was changed, in words."""
        changes: list[str] = []
        if spec.image and not self.mgr.docker.image_exists(spec.image):
            # Same rule as _new_instance: a file must not be able to make this farm fetch and
            # run (privileged) an image it names; only one already on this machine is kept.
            changes.append(f"image {spec.image} is not on this machine, the farm's own image is used")
            spec.image = None
        if spec.data_volume:
            changes.append(f"data volume {spec.data_volume} not adopted")
            spec.data_volume = None
        if spec.extra_boot_args:
            changes.append("extra boot arguments dropped (" + " ".join(spec.extra_boot_args)[:120] + ")")
            spec.extra_boot_args = []
        if spec.extra_props:
            changes.append("extra properties dropped (" + " ".join(sorted(spec.extra_props))[:120] + ")")
            spec.extra_props = {}
        if spec.autostart:
            changes.append("autostart switched off")
            spec.autostart = False
        return changes

def _say(task: Task | None, message: str) -> None:
    if task is not None:
        task.check_cancel()
        task.step(message)

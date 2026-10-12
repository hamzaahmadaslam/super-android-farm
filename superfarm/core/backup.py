"""Backups of an Android's /data: the one part of an Android that cannot be recreated.

A backup is two files next to each other:

    android-<id>-<UTC time>.tar.zst        the data volume
    android-<id>-<UTC time>.tar.zst.json   manifest: checksum, size, and the instance's settings
                                           (including its device identity, which belongs to the data)

How the copy is made matters more than where it goes:

  * tar runs in a helper container with the volume mounted, because /data is owned by
    Android's users and unreadable for the farm's own user. Its output is streamed to a file
    this process opened, so the archive belongs to the farm user and never passes through RAM.
  * GNU tar with --xattrs --xattrs-include='*' --acls --numeric-owner --sparse. Android keys
    file access on numeric owners and extended attributes; a /data restored without them
    boots but apps lose their data or crash.
  * A running Android is stopped for the copy by default (backup.stop_for_backup) and started
    again afterwards. A live copy is possible but is marked as not consistent.
  * Restoring never writes into a running Android, and a copy restored *next to* its original
    comes up with networking off: two Androids with one identity must not be online together.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING

from ..util.files import atomic_write, read_json, safe_filename
from ..util.proc import kill_tree, spawn
from . import device_profile
from .images import ImageError
from .models import DeviceIdentity, check_id
from .tasks import Cancelled, Task

if TYPE_CHECKING:
    from .instances import InstanceManager

log = logging.getLogger("superfarm.backup")

MANIFEST_KIND = "superfarm-android-backup"
MANIFEST_FORMAT = 1
TAR_FLAGS = ["--xattrs", "--xattrs-include=*", "--acls", "--numeric-owner", "--sparse"]
_EXTENSION = {"zstd": ".tar.zst", "gzip": ".tar.gz", "none": ".tar"}
_ARCHIVE = re.compile(r"^android-(\d{1,4})-(\d{8}-\d{6})(?:-\d+)?\.tar(?:\.zst|\.gz)?$")


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


def compression_of(archive: Path) -> str:
    name = archive.name
    return "zstd" if name.endswith(".zst") else "gzip" if name.endswith(".gz") else "none"


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


class Backups:
    timeout = 6 * 3600          # one volume; a full Android is a few GB and takes minutes

    def __init__(self, mgr: InstanceManager):
        self.mgr = mgr

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

    def _archive_path(self, archive: str | os.PathLike) -> Path:
        path = Path(archive).expanduser()
        if not path.is_absolute() and len(path.parts) == 1:
            path = self.directory / safe_filename(path.name)       # a bare name means "in the backup folder"
        if not path.is_file():
            raise BackupError(f"backup {path} does not exist; `sf backup list` shows what is available")
        return path

    @staticmethod
    def manifest_path(archive: Path) -> Path:
        return archive.with_name(archive.name + ".json")

    def manifest(self, archive: str | os.PathLike) -> dict | None:
        data = read_json(self.manifest_path(Path(archive)), None)
        return data if isinstance(data, dict) and data.get("kind") == MANIFEST_KIND else None

    # -------------------------------------------------------------------- create
    def create(self, id: int, dest: str | os.PathLike | None = None, task: Task | None = None) -> Path:
        """Back up one Android's /data. Returns the archive path."""
        mgr = self.mgr
        spec = mgr.store.get(check_id(id))
        names = mgr.names(spec.id)
        volume = mgr.volume_of(spec)
        folder = Path(dest).expanduser() if dest else self.directory
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
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
                archive = self._write(spec, volume, folder, consistent=not was_running or stopped, task=task)
            finally:
                if stopped:
                    try:
                        mgr._start(spec.id, None, force=False)
                    except Exception as e:  # noqa: BLE001 - the backup itself succeeded or failed on its own
                        log.error("Android %s was stopped for its backup and could not be started again: %s", spec.id, e)
                        if task is not None:
                            task.step(f"Android {spec.id}: backup done, but it did not start again: {e}"[:300])
        return archive

    def _write(self, spec, volume: str, folder: Path, *, consistent: bool, task: Task | None) -> Path:
        mgr, cfg = self.mgr, self.mgr.config.backup
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        archive = folder / f"android-{spec.id}-{stamp}{_EXTENSION[cfg.compression]}"
        n = 1
        while archive.exists() or self.manifest_path(archive).exists():       # two backups within one second
            n += 1
            archive = folder / f"android-{spec.id}-{stamp}-{n}{_EXTENSION[cfg.compression]}"
        partial = archive.with_name(archive.name + ".partial")
        _say(task, f"Android {spec.id}: copying data")
        try:
            # 0600 from the first byte: the archive holds every app's logins.
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                # Exit 1 is tar's "a file changed while I read it": normal for a live Android,
                # a real problem for a stopped one.
                self._helper(spec.id, [f"{volume}:/data:ro"], create_arguments(cfg.compression, cfg.level),
                             stdout=out, task=task, ok_codes=(0,) if consistent else (0, 1))
                out.flush()
                os.fsync(out.fileno())
            _say(task, f"Android {spec.id}: computing checksum")
            digest = sha256_file(partial, task)
            size = partial.stat().st_size
            os.replace(partial, archive)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        state = mgr.store.state(spec.id)
        try:
            r = mgr._resolve(spec)
            version, image = r.android_version, r.image
        except ValueError:
            version, image = spec.android_version or "", spec.image or ""
        manifest = {
            "kind": MANIFEST_KIND, "format": MANIFEST_FORMAT, "id": spec.id, "name": spec.display_name,
            "created": time.time(), "archive": archive.name, "compression": cfg.compression, "size": size,
            "sha256": digest, "consistent": consistent, "android_version": version, "image": image,
            "farm": mgr.prefix, "data_volume": volume, "renderer": state.get("renderer") or "",
            "spec": spec.model_dump(mode="json"),
        }
        atomic_write(self.manifest_path(archive), json.dumps(manifest, indent=2, ensure_ascii=False), mode=0o600)
        _say(task, f"Android {spec.id}: backup {archive.name} ({size // 1048576} MB)")
        return archive

    # -------------------------------------------------------------------- verify
    def verify(self, archive: str | os.PathLike, *, deep: bool = False, task: Task | None = None) -> bool:
        """True when the archive matches the checksum recorded when it was made. With `deep`
        the archive is also read end to end by tar, which catches a backup that was already
        damaged when its checksum was taken."""
        try:
            path = self._archive_path(archive)
        except BackupError:
            return False
        manifest = self.manifest(path)
        if not manifest or manifest.get("size") != path.stat().st_size:
            return False
        if sha256_file(path, task) != manifest.get("sha256"):
            return False
        if deep:
            compression = manifest.get("compression") or compression_of(path)
            unpack = {"zstd": ["--zstd"], "gzip": ["--gzip"], "none": []}.get(compression, [])
            try:
                with open(path, "rb") as source, open(os.devnull, "wb") as sink:
                    self._helper(0, [], ["tar", *unpack, "-tf", "-"], stdin=source, stdout=sink, task=task)
            except BackupError:
                return False
        return True

    # ------------------------------------------------------------------- restore
    def restore(self, archive: str | os.PathLike, id: int | None = None, task: Task | None = None) -> int:
        """Restore a backup into Android `id`, or into a new Android when `id` is None.
        Returns the id that now holds the data.

        The target's current /data is replaced. It must be stopped: a running Android keeps
        files open and caches state in memory, and would overwrite the restored data with its own."""
        mgr = self.mgr
        path = self._archive_path(archive)
        manifest = self.manifest(path)
        if manifest is None:
            raise BackupError(f"{path.name} has no manifest ({self.manifest_path(path).name}); without its checksum "
                              "the archive cannot be trusted. Keep both files of a backup together")
        _say(task, f"checking {path.name}")
        if not self.verify(path, task=task):
            raise BackupError(f"{path.name} does not match its recorded checksum; it is damaged or incomplete "
                              "and was not restored")
        saved = manifest.get("spec") if isinstance(manifest.get("spec"), dict) else {}
        try:
            identity = DeviceIdentity.model_validate(saved.get("device") or {})
        except ValueError:
            identity = DeviceIdentity()
        compression = manifest.get("compression") or compression_of(path)

        created = id is None
        if created:
            target = self._new_instance(saved, identity, path.name)
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
                _say(task, f"Android {target}: clearing old data")
                self._helper(target, [f"{volume}:/data"], ["find", "/data", "-mindepth", "1", "-delete"], task=task)
                _say(task, f"Android {target}: restoring {path.name}")
                try:
                    with open(path, "rb") as source:
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
                for key in ("renderer", "trim", "proxy", "last_error"):
                    mgr.store.state_set(target, key, None)
        except BaseException:
            if created:
                mgr._discard_new(target)
            raise
        _say(task, f"Android {target}: restored from {path.name}")
        return target

    def _new_instance(self, saved: dict, identity: DeviceIdentity, source: str) -> int:
        """Create the Android a backup is restored into, as close to the saved one as this farm allows."""
        mgr = self.mgr
        keep = ("name", "tags", "notes", "android_version", "image", "width", "height", "dpi", "fps", "gpu_mode",
                "gpu_node", "resources", "network", "camera", "extra_boot_args", "extra_props")
        template = {k: saved[k] for k in keep if saved.get(k) not in (None, "", [], {})}
        image = template.get("image")
        if image and not mgr.docker.image_exists(str(image)):
            # A manifest is a plain file next to the archive. It may describe how an Android
            # was set up, but it must not be able to make this farm fetch and run (privileged)
            # an image it names; only an image already on this machine is taken over.
            template.pop("image")
        twin = next((i for i, s in mgr.store.list().items()
                     if identity.android_id and s.device.android_id == identity.android_id), None)
        note = f"Restored from {source}."
        if twin is not None:
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
            if twin is not None:
                fallback["network"] = {"mode": "none"}
            return mgr.create(template=fallback)[0].id

    # ---------------------------------------------------------------- list/prune
    def list(self, folder: str | os.PathLike | None = None) -> list[dict]:
        """Backups in the backup folder, newest first."""
        root = Path(folder).expanduser() if folder else self.directory
        out: list[dict] = []
        if not root.is_dir():
            return out
        for meta in root.glob("android-*.json"):
            data = read_json(meta, None)
            if not isinstance(data, dict) or data.get("kind") != MANIFEST_KIND:
                continue
            archive = meta.with_name(meta.name[: -len(".json")])
            if not _ARCHIVE.match(archive.name):
                continue
            out.append({
                "file": str(archive), "name": archive.name, "id": data.get("id"), "instance_name": data.get("name", ""),
                "created": data.get("created", 0), "size": data.get("size", 0), "compression": data.get("compression", ""),
                "sha256": data.get("sha256", ""), "consistent": bool(data.get("consistent")),
                "android_version": data.get("android_version", ""), "present": archive.is_file(),
            })
        return sorted(out, key=lambda b: b["created"], reverse=True)

    def prune(self) -> list[Path]:
        """Delete all but the newest backup.keep backups of each Android in the backup folder.
        Returns what was removed. keep = 0 keeps everything."""
        keep = self.mgr.config.backup.keep
        removed: list[Path] = []
        if keep <= 0:
            return removed
        seen: dict[object, int] = {}
        for entry in self.list():
            seen[entry["id"]] = seen.get(entry["id"], 0) + 1
            if seen[entry["id"]] <= keep:
                continue
            archive = Path(entry["file"])
            for victim in (archive, self.manifest_path(archive)):
                victim.unlink(missing_ok=True)
            removed.append(archive)
        return removed


def _say(task: Task | None, message: str) -> None:
    if task is not None:
        task.check_cancel()
        task.step(message)

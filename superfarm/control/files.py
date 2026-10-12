"""Files to and from Androids: push, pull, list, and "put this in the gallery".

Remote paths are confined to shared storage and ADB's scratch folder unless the caller
explicitly allows anything. That is not about distrust of the operator: a fleet push
runs against many devices at once, ADB is root on most container images, and a slip
such as `/system/...` or `/data/data/...` would damage every selected Android together.

All device commands are argument lists (quoted per argument), so a file name with
spaces, quotes or `$(...)` in it is just a file name.
"""
from __future__ import annotations

import os
import posixpath
import re
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.adb import Adb, AdbError
from ..core.tasks import Task, TaskManager
from ..paths import Paths, get_paths
from ..util.files import safe_filename
from .shell import fleet, reach, settings, sh

if TYPE_CHECKING:
    from ..core.instances import InstanceManager

ALLOWED_ROOTS = ("/sdcard", "/storage/emulated/0", "/data/local/tmp")
MAX_LISTING = 5000
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}
_VIDEOS = {".mp4", ".mkv", ".webm", ".mov", ".3gp", ".avi", ".m4v"}
_STAT_FORMAT = "%F|%s|%Y|%a|%n"
_KINDS = {"regular file": "file", "regular empty file": "file", "directory": "dir", "symbolic link": "link"}


def validate_remote(path: str, *, allow_any: bool = False) -> str:
    """Normalise a device path and insist it stays inside the allowed folders.

    `..` is resolved before the check, so `/sdcard/../data/x` is judged as `/data/x`.
    Control characters are refused outright: they have no place in a path typed by a
    person and they are how a "file name" breaks line-based output parsing."""
    if not isinstance(path, str) or not path.startswith("/") or _CONTROL.search(path):
        raise ValueError(f"bad device path {str(path)[:120]!r}: it must be absolute and contain no control characters")
    if len(path.encode()) > 1024:
        raise ValueError("device path is longer than 1024 bytes")
    norm = posixpath.normpath(path)
    norm = "/" + norm.lstrip("/")      # normpath keeps a leading `//`
    if not allow_any and not any(norm == root or norm.startswith(root + "/") for root in ALLOWED_ROOTS):
        raise ValueError(f"{norm} is outside {', '.join(ALLOWED_ROOTS)}; pass allow_any to use other folders")
    return norm


def gallery_folder(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in _IMAGES:
        return "/sdcard/Pictures"
    if ext in _VIDEOS:
        return "/sdcard/Movies"
    raise ValueError(f"{filename}: not a picture or video the gallery shows "
                     f"({', '.join(sorted(_IMAGES | _VIDEOS))})")


def parse_listing(text: str, folder: str) -> list[dict]:
    """Lines of `stat -c '%F|%s|%Y|%a|%n'` -> entries. The name is last, so a `|` inside a
    file name is harmless."""
    out = []
    for line in text.splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5 or not parts[1].isdigit() or not parts[2].isdigit():
            continue
        name = posixpath.basename(parts[4].rstrip("/"))
        if not name:
            continue
        out.append({"name": name, "path": posixpath.join(folder, name), "type": _KINDS.get(parts[0], "other"),
                    "size": int(parts[1]), "modified": int(parts[2]), "mode": parts[3]})
    return sorted(out, key=lambda e: (e["type"] != "dir", e["name"].lower()))


# --------------------------------------------------------------------------- one device

def remote_stat(adb: Adb, path: str) -> dict | None:
    """Type and size of one device path, or None when it does not exist."""
    out = sh(adb, ["stat", "-L", "-c", "%F|%s", path], timeout=15)
    kind, _, size = out.stdout.strip().partition("|")
    if not out.ok or not size.isdigit():
        return None
    return {"type": _KINDS.get(kind, "other"), "size": int(size)}


def push_file(adb: Adb, local: Path, remote: str, *, timeout: float = 600) -> str:
    """Copy one host file to an exact device path, creating the folder. A destination that
    is an existing folder (or ends in `/`) receives the file under its own name."""
    stat = remote_stat(adb, remote)
    if remote.endswith("/") or (stat and stat["type"] == "dir"):
        remote = posixpath.join(remote.rstrip("/") or "/", safe_filename(local.name))
    parent = posixpath.dirname(remote)
    made = sh(adb, ["mkdir", "-p", parent], timeout=20)
    if not made.ok:
        raise AdbError(f"cannot create {parent} on the device: {made.last}")
    adb.push(str(local), remote, timeout=timeout)
    return remote


def list_dir(adb: Adb, remote: str) -> list[dict]:
    """Entries of one device folder (hidden ones included), folders first."""
    stat = remote_stat(adb, remote)
    if stat is None:
        raise FileNotFoundError(f"{remote} does not exist on the device")
    if stat["type"] != "dir":
        raise NotADirectoryError(f"{remote} is not a folder")
    # `find ... -exec stat {} +` instead of `ls`: no shell glob is needed, and the output
    # format is ours rather than something to guess per Android version. `-H`: /sdcard is
    # itself a symbolic link, and find lists nothing under a link it was not told to follow.
    out = sh(adb, ["find", "-H", remote, "-maxdepth", "1", "-mindepth", "1", "-exec", "stat", "-c", _STAT_FORMAT, "{}", "+"],
             timeout=30)
    if not out.ok and not out.stdout.strip():
        raise AdbError(f"cannot list {remote}: {out.last}")
    return parse_listing(out.stdout, remote)[:MAX_LISTING]


def scan_media(adb: Adb, remote: str) -> bool:
    """Tell Android's media index about a new file, then check that it is listed.

    Android 11+ indexes files written through shared storage on its own (verified on
    Android 13: the file is in MediaStore before any scan is requested). Older versions
    need to be told, so both the per-file broadcast and the volume scan are sent; they
    are harmless where they are redundant. Returns whether MediaStore lists the file."""
    real = remote.replace("/sdcard/", "/storage/emulated/0/", 1) if remote.startswith("/sdcard/") else remote
    sh(adb, ["am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", "file://" + real], timeout=20)
    sh(adb, ["content", "call", "--uri", "content://media/", "--method", "scan_volume", "--arg", "external_primary"], timeout=30)
    collection = "images" if posixpath.dirname(remote).endswith("Pictures") else "video"
    listed = sh(adb, ["content", "query", "--uri", f"content://media/external/{collection}/media", "--projection", "_data"],
                timeout=30)
    return any(line.rstrip().endswith("_data=" + real) for line in listed.stdout.splitlines())


# --------------------------------------------------------------------------- the fleet API

class Files:
    def __init__(self, mgr: InstanceManager, tasks: TaskManager, paths: Paths | None = None):
        self.mgr, self.tasks = mgr, tasks
        self.paths = paths or get_paths()

    def _local(self, local: Path | str) -> Path:
        path = Path(local)
        if not path.is_file():
            raise FileNotFoundError(f"{path} is not a file on this host")
        limit = settings(self.mgr.config).max_transfer_mb
        if path.stat().st_size > limit * 1024 * 1024:
            raise ValueError(f"{path.name} is larger than {limit} MB (control.max_transfer_mb)")
        return path

    def push(self, selector: str | int | Iterable[int], local: Path | str, remote: str, *, allow_any: bool = False) -> Task:
        path, target = self._local(local), validate_remote(remote, allow_any=allow_any)
        if remote.endswith("/"):
            target += "/"       # normalising dropped the "this is a folder" marker
        return fleet(self.mgr, self.tasks, "file-push", f"Push {path.name} to {target}", selector,
                     lambda _id, adb, _t: "saved as " + push_file(adb, path, target))

    def to_gallery(self, selector: str | int | Iterable[int], local: Path | str, *, name: str | None = None) -> Task:
        """Put a picture or video where gallery apps and upload dialogs find it."""
        path = self._local(local)
        filename = safe_filename(name or path.name)
        target = posixpath.join(gallery_folder(filename), filename)

        def one(_id: int, adb: Adb, _task: Task) -> str:
            push_file(adb, path, target)
            return f"{target} " + ("(in the media index)" if scan_media(adb, target) else "(pushed; not in the media index yet)")

        return fleet(self.mgr, self.tasks, "file-gallery", f"Add {filename} to the gallery", selector, one)

    def pull(self, id: int, remote: str, *, allow_any: bool = False) -> Path:
        """Fetch one file from one Android into a fresh folder under <home>/run/pulls and
        return its path. The caller owns the file (and should delete its folder when done)."""
        target = validate_remote(remote, allow_any=allow_any)
        adb = reach(self.mgr, id)
        stat = remote_stat(adb, target)
        if stat is None:
            raise FileNotFoundError(f"{target} does not exist on Android {id}")
        if stat["type"] != "file":
            raise IsADirectoryError(f"{target} is not a regular file; pull files one at a time")
        limit = settings(self.mgr.config).max_transfer_mb
        if stat["size"] > limit * 1024 * 1024:
            raise ValueError(f"{target} is {stat['size'] >> 20} MB, larger than {limit} MB (control.max_transfer_mb)")
        root = self.paths.run / "pulls"
        root.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=f"{int(id)}-", dir=root))
        dest = folder / safe_filename(posixpath.basename(target))
        try:
            adb.pull(target, str(dest), timeout=600)
            if not dest.is_file():
                raise AdbError(f"pull of {target} produced no file")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return dest

    def list(self, id: int, remote: str = "/sdcard", *, allow_any: bool = False) -> list[dict]:
        return list_dir(reach(self.mgr, id), validate_remote(remote, allow_any=allow_any))

    def delete(self, selector: str | int | Iterable[int], remote: str, *, allow_any: bool = False) -> Task:
        """Remove one file (not a folder) from every selected Android."""
        target = validate_remote(remote, allow_any=allow_any)
        if target in ALLOWED_ROOTS:
            raise ValueError(f"refusing to delete {target} itself")

        def one(_id: int, adb: Adb, _task: Task) -> str:
            stat = remote_stat(adb, target)
            if stat is None:
                return "was not there"
            if stat["type"] == "dir":
                raise IsADirectoryError(f"{target} is a folder; only single files are deleted here")
            out = sh(adb, ["rm", "-f", "--", target], timeout=30)
            if not out.ok:
                raise AdbError(f"cannot delete {target}: {out.last}")
            return "deleted"

        return fleet(self.mgr, self.tasks, "file-delete", f"Delete {target}", selector, one)

"""The app library: every app the farm can install, unpacked and described.

Apps arrive in four shapes:

    .apk    one file, everything inside.
    .xapk   zip: base.apk + config splits (+ Android/obb/... expansion files) + manifest.json
    .apks   zip: what bundletool builds (splits/base-master.apk, splits/base-arm64_v8a.apk,
            standalones/..., toc.pb)
    .apkm   zip: base.apk + split_config.*.apk + info.json

The last three are "split" apps: a base plus pieces per CPU type, screen density and
language. A device must get the base and exactly the pieces that fit it, in one
transaction (`adb install-multiple`). So an upload is unpacked once, every APK's own
manifest is read (the bundle's json files are only hints; they are not trusted for
anything that matters), and the result is stored as

    apps/<package>-<versionCode>/base.apk, split_*.apk, obb/*.obb
    apps/index.json      what each app is, so listing never opens an APK

Uploads are untrusted input. Nothing in an archive chooses where a file is written
(names are reduced to a plain file name under the app's own folder), links are refused,
and member count, per-file size and total unpacked size are capped before and while
unpacking, so neither a path trick nor a zip bomb gets anywhere.

select_splits() is the other half: given an app and what a device reports about
itself, which files to install.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import Config
from ..paths import Paths
from ..util.files import inside, locked, read_json, safe_filename, write_json
from .apkinfo import DENSITIES, ApkError, ApkInfo, classify_split, parse_apk
from .shell import settings

FORMATS = (".apk", ".xapk", ".apks", ".apkm")
MAX_MEMBERS = 4096              # a real bundle has tens of files
MAX_HINT_BYTES = 1024 * 1024    # manifest.json / info.json
_COPY_CHUNK = 1024 * 1024
_APP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,254}-\d{1,20}\Z")
_ARM = ("arm64-v8a", "armeabi-v7a", "armeabi")


class LibraryError(ValueError):
    """The upload or the request is not acceptable; the message says why and what to do."""


@dataclass
class DeviceTraits:
    """What decides which split APKs a device needs."""
    abis: list[str] = field(default_factory=list)       # in the device's order of preference
    density: int = 160
    locales: list[str] = field(default_factory=list)    # BCP-47 tags, e.g. ["en-US"]
    sdk: int = 0
    native_bridge: bool = True                          # can it run ARM code on an x86 CPU?


@dataclass
class Selection:
    files: list[str]
    warnings: list[str] = field(default_factory=list)
    abi: str = ""


def check_app_id(app_id: str) -> str:
    """App ids become folder names; only the shape this module generates is accepted."""
    if not isinstance(app_id, str) or not _APP_ID.match(app_id):
        raise LibraryError(f"bad app id {str(app_id)[:80]!r}: expected <package>-<versionCode>")
    return app_id


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_COPY_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _unique(folder: Path, name: str) -> Path:
    """A free file name in folder: two archive members may reduce to the same safe name."""
    target, n = folder / name, 1
    stem, suffix = os.path.splitext(name)
    while target.exists():
        target, n = folder / f"{stem}_{n}{suffix}", n + 1
    return target


def _language(tag: str) -> str:
    return re.split(r"[-_]", tag.strip())[0].lower()


def _family(abi: str) -> str:
    return "arm" if abi.startswith("arm") else "x86" if abi.startswith("x86") else abi


# --------------------------------------------------------------------------- split selection

def select_splits(meta: dict, traits: DeviceTraits, *, all_locales: bool = False) -> Selection:
    """Choose the files of one library app that one device should receive.

    Mirrors what an app store does: the base, every feature module, and per module one
    ABI split (the first of the device's ABIs the app has), one density split (the
    smallest that is at least the screen's density), and the device's languages.
    Raises LibraryError when the app cannot run on the device at all.
    """
    files = meta.get("files") or []
    base = next((f for f in files if f["kind"] == "base"), None)
    if base is None:
        raise LibraryError(f"library entry {meta.get('id')} has no base APK; remove it and add the app again")
    if traits.sdk and meta.get("min_sdk", 1) > traits.sdk:
        raise LibraryError(f"{meta['package']} needs Android API {meta['min_sdk']} or newer; this device is API {traits.sdk}")

    app_abis = list(meta.get("abis") or [])
    chosen_abi = next((a for a in traits.abis if a in app_abis), "")
    warnings: list[str] = []
    if app_abis and traits.abis and not chosen_abi:
        hint = (" The app is ARM-only and this image has no ARM translation layer: use an Android image with a "
                "native bridge, or an x86 build of the app.") if all(a in _ARM for a in app_abis) else ""
        raise LibraryError(f"{meta['package']} has native code for {', '.join(app_abis)} but the device runs "
                           f"{', '.join(traits.abis)}.{hint}")
    if chosen_abi and traits.abis and _family(chosen_abi) != _family(traits.abis[0]) and not traits.native_bridge:
        warnings.append(f"app is {chosen_abi}-only and the image reports no native bridge "
                        "(ro.dalvik.vm.native.bridge): it will install but its native code cannot run")

    picked = [base["file"]]
    languages = {_language(tag) for tag in traits.locales if tag}
    groups: dict[str, list[dict]] = {}
    for f in files:
        if f["kind"] == "feature":
            picked.append(f["file"])
        elif f["kind"] != "base":
            groups.setdefault(f.get("for", ""), []).append(f)
    for parent, members in groups.items():
        abi_splits = [f for f in members if f["kind"] == "abi"]
        if abi_splits:
            match = next((f for a in traits.abis for f in abi_splits if f["value"] == a), None)
            if match is None:
                have = ", ".join(sorted(f["value"] for f in abi_splits))
                raise LibraryError(f"{meta['package']}: no {parent or 'base'} split for this device's CPU "
                                   f"({', '.join(traits.abis) or 'unknown'}); the bundle has {have}")
            picked.append(match["file"])
        density_splits = sorted((f for f in members if f["kind"] == "density" and f["value"] in DENSITIES),
                                key=lambda f: DENSITIES[f["value"]])
        if density_splits:
            fit = next((f for f in density_splits if DENSITIES[f["value"]] >= traits.density), density_splits[-1])
            picked.append(fit["file"])
        for f in members:
            if f["kind"] == "locale" and (all_locales or _language(f["value"]) in languages):
                picked.append(f["file"])
            elif f["kind"] == "other" or (f["kind"] == "density" and f["value"] not in DENSITIES):
                picked.append(f["file"])    # unknown purpose: Android ignores what it does not need
    return Selection(files=list(dict.fromkeys(picked)), warnings=warnings, abi=chosen_abi)


# --------------------------------------------------------------------------- the library

class AppLibrary:
    def __init__(self, paths: Paths, config: Config | None = None):
        self.paths = paths
        self.root = paths.apps
        self._config = config
        self._index = self.root / "index.json"

    def reload_config(self, config: Config) -> None:
        self._config = config

    # ---------------------------------------------------------------- reading
    def _load(self) -> dict[str, dict]:
        data = read_json(self._index, {}) or {}
        apps = data.get("apps") if isinstance(data, dict) else None
        return apps if isinstance(apps, dict) else {}

    def list(self) -> list[dict]:
        """Every app, newest upload first."""
        return sorted(self._load().values(), key=lambda m: (-float(m.get("added", 0)), m.get("id", "")))

    def get(self, app_id: str) -> dict:
        meta = self._load().get(check_app_id(app_id))
        if meta is None:
            raise KeyError(f"no app {app_id} in the library")
        return meta

    def find(self, package: str) -> list[dict]:
        """All stored versions of a package, highest version first."""
        return sorted((m for m in self._load().values() if m.get("package") == package),
                      key=lambda m: -int(m.get("version_code", 0)))

    def dir(self, app_id: str) -> Path:
        return inside(self.root, self.root / check_app_id(app_id))

    def file(self, app_id: str, name: str) -> Path:
        """Path of one stored file (an APK, or `obb/<name>`), refusing anything outside the app's folder."""
        folder = self.dir(app_id)
        path = inside(folder, folder / name)
        if not path.is_file():
            raise LibraryError(f"{app_id}: {name} is missing from the library folder; remove the app and add it again")
        return path

    # ---------------------------------------------------------------- writing
    def _limit_bytes(self) -> int:
        return settings(self._config).max_app_mb * 1024 * 1024

    def add(self, filename: str, data_path: Path, *, move: bool = False) -> dict:
        """Take an uploaded file into the library and return its description.

        `filename` is the name the user gave it (it decides the format); `data_path` is
        where the bytes are. Adding a package+version that already exists replaces it.
        """
        original = safe_filename(filename, "app.apk")
        ext = os.path.splitext(original)[1].lower()
        if ext not in FORMATS:
            raise LibraryError(f"{original}: unsupported file type; the library takes {', '.join(FORMATS)}")
        data_path = Path(data_path)
        st = os.lstat(data_path)
        if not stat.S_ISREG(st.st_mode):
            raise LibraryError("upload is not a regular file")
        limit = self._limit_bytes()
        if st.st_size > limit:
            raise LibraryError(f"{original} is {st.st_size >> 20} MB; the limit is {limit >> 20} MB (control.max_app_mb)")
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        try:
            if not zipfile.is_zipfile(data_path):
                raise LibraryError(f"{original} is not a zip archive, so it is not an Android app package "
                                   "(old encrypted .apkm files must be converted first)")
            with zipfile.ZipFile(data_path) as zf:
                single = "AndroidManifest.xml" in zf.namelist()
                hints = {} if single else self._unpack(zf, staging, limit)
            if single:
                # A file named .xapk that is really one APK (it happens) is still one APK.
                target = staging / "base.apk"
                if move:
                    shutil.move(str(data_path), target)
                else:
                    shutil.copyfile(data_path, target)
            # Trust the content over the name: a bundle uploaded as `.apk` is still a bundle.
            fmt = "apk" if single else ("xapk" if ext == ".apk" else ext.lstrip("."))
            meta = self._describe(staging, fmt, original, hints)
            return self._commit(staging, meta)
        except zipfile.BadZipFile:
            raise LibraryError(f"{original} is a damaged zip archive") from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _unpack(self, zf: zipfile.ZipFile, staging: Path, limit: int) -> dict:
        """Copy the APKs and OBB files out of a bundle. Returns the bundle's own hints."""
        members = zf.infolist()
        if len(members) > MAX_MEMBERS:
            raise LibraryError(f"archive has {len(members)} entries; an app bundle never has more than {MAX_MEMBERS}")
        total = 0
        hints: dict[str, Any] = {"origins": {}}
        for m in members:
            name = m.filename
            parts = PurePosixPath(name).parts
            if name.startswith("/") or "\\" in name or "\x00" in name or ".." in parts:
                raise LibraryError(f"archive entry {name[:120]!r} tries to leave the archive; refusing the whole file")
            if stat.S_ISLNK(m.external_attr >> 16):
                raise LibraryError(f"archive entry {name[:120]!r} is a symbolic link; refusing the whole file")
            if m.is_dir():
                continue
            if m.flag_bits & 0x1:
                raise LibraryError("archive is password-protected")
            lower = name.lower()
            base = safe_filename(parts[-1] if parts else "", "")
            if lower.endswith(".apk") and base:
                target = _unique(staging, base if base.lower().endswith(".apk") else base + ".apk")
            elif lower.endswith(".obb") and base:
                (staging / "obb").mkdir(exist_ok=True)
                target = _unique(staging / "obb", base)
            elif len(parts) == 1 and lower in ("manifest.json", "info.json") and m.file_size <= MAX_HINT_BYTES:
                try:
                    loaded = json.loads(zf.read(m))
                    if isinstance(loaded, dict):
                        hints[lower] = loaded
                except (ValueError, UnicodeDecodeError):
                    pass    # hints only; a broken one changes nothing
                continue
            else:
                continue    # icons, toc.pb, signatures: nothing the farm installs
            total += m.file_size
            if total > limit:
                raise LibraryError(f"archive unpacks to more than {limit >> 20} MB (control.max_app_mb)")
            written = 0
            with zf.open(m) as src, open(target, "xb") as dst:
                while chunk := src.read(_COPY_CHUNK):
                    written += len(chunk)
                    # The declared size is the archive's own claim; count what really comes out.
                    if written > m.file_size:
                        raise LibraryError(f"archive entry {name[:120]!r} is larger than it declares")
                    dst.write(chunk)
            if target.parent == staging:
                hints["origins"][target.name] = name
        return hints

    def _describe(self, staging: Path, fmt: str, original: str, hints: dict) -> dict:
        """Read every unpacked APK, pick the base, name the files and build the index entry."""
        infos: dict[str, ApkInfo] = {}
        warnings: list[str] = []
        for apk in sorted(staging.glob("*.apk")):
            try:
                infos[apk.name] = parse_apk(apk, use_aapt=False)
            except ApkError as e:
                if fmt == "apk":
                    raise LibraryError(f"{original}: {e}") from None
                warnings.append(f"skipped {apk.name}: {e}")
                apk.unlink()
        origins: dict[str, str] = hints.get("origins", {})
        bases = [n for n, i in infos.items() if i.is_base]
        if not bases:
            if fmt == "apk" and infos:
                split = next(iter(infos.values())).split
                raise LibraryError(f"{original} is only the split '{split}' of an app, not the app itself; "
                                   "add the whole bundle (.xapk/.apks/.apkm)")
            raise LibraryError(f"{original} contains no base APK")

        def base_rank(name: str) -> tuple:
            origin = origins.get(name, name).lower()
            # bundletool also ships "standalone" APKs for old devices: complete apps that
            # must never be mixed with the splits.
            return ("standalone" in origin, not origin.startswith("splits/"), -infos[name].size)

        base_name = sorted(bases, key=base_rank)[0]
        base = infos[base_name]
        if base.source == "axml" and not base.label:
            try:    # the label lives in resources.arsc; only now is a second opinion worth asking for
                base.label = parse_apk(staging / base_name).label
            except ApkError:
                pass
        files: list[dict] = []
        for name, info in infos.items():
            path = staging / name
            if name != base_name and (info.is_base or info.package != base.package or info.version_code != base.version_code):
                if not info.is_base:
                    warnings.append(f"dropped {name}: it belongs to {info.package} {info.version_code}, not to this app")
                path.unlink()
                continue
            kind, value = classify_split(info.split, is_feature=info.is_feature_split, abis=info.abis)
            final = "base.apk" if name == base_name else "split_" + safe_filename(info.split, "split") + ".apk"
            if final != name:
                path = path.rename(_unique(staging, final))
            files.append({"file": path.name, "split": info.split, "kind": kind, "value": value,
                          "for": info.config_for_split, "size": info.size, "abis": info.abis})
        files.sort(key=lambda f: (f["kind"] != "base", f["file"]))
        obb_dir = staging / "obb"
        obb = [{"file": p.name, "size": p.stat().st_size} for p in sorted(obb_dir.iterdir())] if obb_dir.is_dir() else []

        abis = list(base.abis)
        for f in files:
            for a in ([f["value"]] if f["kind"] == "abi" else []) + f["abis"]:
                if a not in abis:
                    abis.append(a)
        xapk, apkm = hints.get("manifest.json", {}), hints.get("info.json", {})
        label = base.label or str(xapk.get("name") or apkm.get("app_name") or apkm.get("apk_title") or "")[:200]
        app_id = check_app_id(f"{base.package}-{base.version_code}")
        return {
            "id": app_id, "package": base.package, "version_code": base.version_code,
            "version_name": base.version_name, "label": label or base.package,
            "min_sdk": base.min_sdk, "target_sdk": base.target_sdk, "abis": abis,
            "permissions": base.permissions, "launcher": base.launcher, "debuggable": base.debuggable,
            "split_required": base.split_required, "format": fmt, "original": original, "added": time.time(),
            "size": sum(f["size"] for f in files) + sum(o["size"] for o in obb),
            "sha256": _sha256(staging / "base.apk"), "files": files,
            "splits": [f["split"] for f in files if f["split"]], "obb": obb, "warnings": warnings,
        }

    def _commit(self, staging: Path, meta: dict) -> dict:
        """Swap the staged folder in and record it, under one lock so two uploads of the
        same app cannot interleave."""
        final = self.root / meta["id"]
        with locked(self._index):
            trash = None
            if final.exists():
                trash = Path(tempfile.mkdtemp(prefix=".trash-", dir=self.root))
                os.rename(final, trash / "old")
            os.rename(staging, final)
            # mkdtemp made it owner-only; the farm home is readable by the owner's group.
            os.chmod(final, 0o750)  # noqa: S103
            apps = self._load()
            apps[meta["id"]] = meta
            write_json(self._index, {"version": 1, "apps": apps})
        if trash:
            shutil.rmtree(trash, ignore_errors=True)
        return meta

    def remove(self, app_id: str) -> None:
        folder = self.dir(app_id)
        with locked(self._index):
            apps = self._load()
            known = apps.pop(app_id, None)
            write_json(self._index, {"version": 1, "apps": apps})
        if known is None and not folder.exists():
            raise KeyError(f"no app {app_id} in the library")
        shutil.rmtree(folder, ignore_errors=True)

    def reindex(self) -> list[dict]:
        """Rebuild index.json from the folders (after a restore, or files copied in by hand).

        Each folder is described in place: files get their canonical names and APKs that
        are unreadable or belong to another app are removed, exactly as on upload. Upload
        details that only the index knew (original name, date) are kept when present."""
        self.root.mkdir(parents=True, exist_ok=True)
        with locked(self._index):
            old = self._load()
            apps: dict[str, dict] = {}
            for folder in sorted(self.root.iterdir()):
                if folder.name.startswith(".") or folder.is_symlink() or not folder.is_dir():
                    continue
                prior = old.get(folder.name, {})
                try:
                    meta = self._describe(folder, prior.get("format", "apk"), prior.get("original", folder.name), {})
                except (LibraryError, OSError):
                    continue    # one unreadable folder must not hide the rest of the library
                if meta["id"] != folder.name:
                    continue    # a folder is only trusted under the name its own manifest gives it
                meta["added"] = prior.get("added", folder.stat().st_mtime)
                apps[meta["id"]] = meta
            write_json(self._index, {"version": 1, "apps": apps})
        return self.list()

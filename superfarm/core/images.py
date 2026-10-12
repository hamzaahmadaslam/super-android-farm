"""The images a farm runs: one per Android version, plus a small helper image.

An Android image is an upstream base (android.images.<version>.base) with the farm's
*overlay* copied on top: every file under <home>/overlays/android/<version>/ lands at the
same path inside Android (camera HAL, GPU runtime, a patched binary). The result is tagged
superfarm/android:<version>. Setting android.images.<version>.tag instead says "run exactly
this local image" and nothing is built.

The helper image ("tools") is what works on a data volume while its Android is stopped:
backups, restores, clones, cache clearing.

A farm may mix Android versions freely, but two features are native code built against one
release and exist for that release only (VERSION_BOUND). status() says so per version, so
the panel can show "not available on Android 12" instead of an Android that dies at boot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from pathlib import Path

from ..config import Config
from ..paths import Paths
from ..resources import resource_path
from ..util.proc import CommandError
from .docker import Docker
from .models import image_tag
from .tasks import Task

TOOLS_TAG = "superfarm/tools:1"       # bump the number when Dockerfile.tools changes
TOOLS_BASE = "alpine:3.22"
LABEL_OVERLAY = "io.superfarm.overlay"
LABEL_BASE_ID = "io.superfarm.base-id"

# Feature -> Android releases it is built for. Both are binaries compiled against Android 13
# (API 33): the camera/microphone HAL taken from an Android 13 build, and the Venus Vulkan
# driver + gralloc that NVIDIA rendering needs.
VERSION_BOUND: dict[str, tuple[str, ...]] = {"camera": ("13",), "nvidia": ("13",)}
_FEATURE_NAME = {"camera": "the virtual camera and microphone", "nvidia": "NVIDIA rendering"}

_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,31}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/:@]{0,254}$")

DOCKERFILE_DIR = resource_path("deploy", "docker", "android")
# The same instructions as deploy/docker/android/Dockerfile*, for installs that do not ship
# the deploy/ folder (a wheel). A unit test keeps the two from drifting apart.
ANDROID_DOCKERFILE = """\
ARG BASE=redroid/redroid:13.0.0-latest
FROM ${BASE}
ARG BASE
ARG BASE_ID=unknown
ARG OVERLAY_HASH=none
LABEL io.superfarm.role="android-image" \\
      io.superfarm.base="${BASE}" \\
      io.superfarm.base-id="${BASE_ID}" \\
      io.superfarm.overlay="${OVERLAY_HASH}"
COPY overlay/ /
"""
TOOLS_DOCKERFILE = """\
ARG BASE=alpine:3.22
FROM ${BASE}
RUN apk add --no-cache tar zstd pigz
LABEL io.superfarm.role="tools"
"""


class ImageError(RuntimeError):
    """An image is missing or could not be built; the message says how to fix it."""


def dockerfile_text(name: str, builtin: str) -> str:
    try:
        return (DOCKERFILE_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return builtin


def _step(task: Task | None, message: str) -> None:
    if task is not None:
        task.check_cancel()
        task.step(message)


class Images:
    tools_tag = TOOLS_TAG
    tools_base = TOOLS_BASE

    def __init__(self, docker: Docker, config: Config, paths: Paths):
        self.docker, self.config, self.paths = docker, config, paths
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        # Ten Androids starting together must not run ten identical builds or pulls.
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    # ------------------------------------------------------------------ overlay
    def _entry(self, version: str):
        version = str(version)
        if not _VERSION.match(version):
            raise ImageError(f"bad Android version name {version!r}")
        if version not in self.config.android.images:
            known = ", ".join(sorted(self.config.android.images)) or "none"
            raise ImageError(f"Android {version} has no image configured; add android.images.{version}.base "
                             f"to the config (configured: {known})")
        return self.config.android.images[version]

    def overlay_dir(self, version: str) -> Path:
        if not _VERSION.match(str(version)):
            raise ImageError(f"bad Android version name {version!r}")
        return self.paths.overlays / "android" / str(version)

    def _overlay_entries(self, version: str) -> list[tuple[str, os.stat_result]]:
        root = self.overlay_dir(version)
        out: list[tuple[str, os.stat_result]] = []
        if not root.is_dir():
            return out
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs.sort()
            for name in sorted(files) + [d for d in dirs if os.path.islink(os.path.join(folder, d))]:
                full = os.path.join(folder, name)
                st = os.lstat(full)
                if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                    out.append((os.path.relpath(full, root), st))
        return sorted(out, key=lambda e: e[0])

    def overlay_files(self, version: str) -> list[str]:
        return [rel for rel, _ in self._overlay_entries(version)]

    def overlay_hash(self, version: str) -> str:
        """Changes whenever a file in the overlay is added, removed, replaced or re-moded.

        Built from names, sizes, modes and modification times rather than contents: it is
        checked at every start and an overlay can hold tens of megabytes of libraries. A
        false alarm only costs a build that Docker answers from its layer cache."""
        entries = self._overlay_entries(version)
        if not entries:
            return "none"
        h = hashlib.sha256()
        for rel, st in entries:
            target = os.readlink(self.overlay_dir(version) / rel) if stat.S_ISLNK(st.st_mode) else ""
            h.update(f"{rel}\0{st.st_size}\0{stat.S_IMODE(st.st_mode)}\0{st.st_mtime_ns}\0{target}\n".encode())
        return h.hexdigest()[:16]

    # ------------------------------------------------------------------- status
    def _labels(self, ref: str) -> dict[str, str]:
        r = self.docker.run(["image", "inspect", "--format", "{{json .Config.Labels}}", ref], timeout=30, check=False)
        if not r.ok:
            return {}
        try:
            return json.loads(r.out or "null") or {}
        except json.JSONDecodeError:
            return {}

    def _stale(self, version: str, tag: str, base: str) -> str:
        """Why the built image no longer matches its inputs, or "" when it does."""
        labels = self._labels(tag)
        if labels.get(LABEL_OVERLAY, "none") != self.overlay_hash(version):
            return "overlay changed"
        base_id = self.docker.image_id(base)
        if base_id and labels.get(LABEL_BASE_ID) and labels[LABEL_BASE_ID] != base_id:
            return "base image changed"
        return ""

    # ----------------------------------------------------------------- features
    def _camera_missing(self, version: str) -> list[str]:
        """Files of the camera HAL overlay that are absent for this version (same folder
        and same list the camera module installs from)."""
        from ..media.camera import OVERLAY_REQUIRED
        configured = self.config.media.camera.hal_overlay
        folder = Path(configured).expanduser() if configured else self.paths.overlays / "camera" / str(version)
        return [str(folder / name) for name in OVERLAY_REQUIRED if not (folder / name).is_file()]

    def _nvidia_missing(self) -> list[str]:
        from ..host import venus
        status = venus.runtime_status(self.config, self.paths)
        return [] if status.get("ready") else [str(m) for m in status.get("missing") or ["the Venus runtime"]]

    def feature(self, version: str, name: str, *, missing: list[str] | None = None) -> dict:
        """Whether a version-bound feature can be used on one Android version.

            supported    the feature exists for this Android release at all
            available    supported, and its files are present on this machine
            unsupported  "" or the sentence to show ("not available on Android 12 ...")
            reason       "" when available, otherwise what is in the way
        """
        built_for = VERSION_BOUND[name]
        major = _major(version)
        unsupported = "" if major in built_for else (
            f"not available on Android {version}: {_FEATURE_NAME[name]} is built for Android {'/'.join(built_for)} only")
        if unsupported:
            return {"supported": False, "available": False, "unsupported": unsupported, "reason": unsupported}
        if missing is None:
            try:
                missing = self._camera_missing(version) if name == "camera" else self._nvidia_missing()
            except Exception as e:  # noqa: BLE001 - a status line must not fail because a neighbour module did
                missing = [f"could not be checked ({type(e).__name__}: {e})"]
        hint = "native/camera/extract_hal.sh" if name == "camera" else "native/venus/build.sh"
        reason = f"not built yet ({missing[0]} is missing{' and more' if len(missing) > 1 else ''}); see {hint}" if missing else ""
        return {"supported": True, "available": not missing, "unsupported": "", "reason": reason}

    def status(self) -> list[dict]:
        """One row per configured Android version: its images, its overlay, and which
        version-bound features work on it."""
        out = []
        try:
            nvidia_missing: list[str] | None = self._nvidia_missing()
        except Exception as e:  # noqa: BLE001 - see feature()
            nvidia_missing = [f"could not be checked ({type(e).__name__}: {e})"]
        for version, entry in sorted(self.config.android.images.items(), key=lambda kv: _version_key(kv[0])):
            custom = bool(entry.tag)
            tag = entry.tag or image_tag(self.config.docker.name_prefix, version)
            built = self.docker.image_exists(tag)
            files = [] if custom or not _VERSION.match(version) else self.overlay_files(version)
            stale = "" if custom or not built else self._stale(version, tag, entry.base)
            features = {"camera": self.feature(version, "camera"),
                        "nvidia": self.feature(version, "nvidia", missing=nvidia_missing)}
            out.append({
                "version": version, "base": entry.base, "tag": tag, "custom": custom, "built": built,
                "base_present": self.docker.image_exists(entry.base),
                "overlay_dir": "" if custom else str(self.overlay_dir(version)) if _VERSION.match(version) else "",
                "overlays": files, "stale": bool(stale), "stale_reason": stale,
                "arm_translation": entry.arm_translation,
                "overlays_available": {"camera_hal": features["camera"]["available"],
                                       "nvidia_runtime": features["nvidia"]["available"]},
                "features": features,
            })
        return out

    # ------------------------------------------------------------------- ensure
    def ensure_ref(self, ref: str, *, pull: bool = True, task: Task | None = None) -> str:
        """Make sure an exact image reference exists locally; pull it when allowed."""
        if not _REF.match(ref or ""):
            raise ImageError(f"bad image reference {ref!r}")
        if self.docker.image_exists(ref):
            return ref
        if not pull:
            raise ImageError(f"image {ref} is not on this machine; fetch it with `docker pull {ref}` "
                             "or load it with `docker load`")
        with self._lock(f"pull:{ref}"):
            if not self.docker.image_exists(ref):
                _step(task, f"pulling {ref} (this can take several minutes)")
                try:
                    self.docker.pull(ref, timeout=3600)
                except CommandError as e:
                    raise ImageError(f"could not pull {ref}: {e}. Check the name and the internet connection, "
                                     f"or point android.images.<version>.tag at an image you already have") from None
        return ref

    def ensure(self, version: str, *, pull: bool = True, task: Task | None = None) -> str:
        """Return an image reference that can be run for this Android version, building or
        pulling whatever is missing."""
        entry = self._entry(version)
        if entry.tag:
            return self.ensure_ref(entry.tag, pull=pull, task=task)
        tag = image_tag(self.config.docker.name_prefix, version)
        with self._lock(f"build:{version}"):
            if self.docker.image_exists(tag) and not self._stale(version, tag, entry.base):
                return tag
            self.ensure_ref(entry.base, pull=pull, task=task)
            return self._build_locked(version, task)

    def build(self, version: str, task: Task | None = None) -> str:
        """Build superfarm/android:<version> from its base and overlay, unconditionally."""
        entry = self._entry(version)
        if entry.tag:
            raise ImageError(f"Android {version} is set to run the ready-made image {entry.tag} "
                             f"(android.images.{version}.tag); clear that setting to build from {entry.base}")
        with self._lock(f"build:{version}"):
            self.ensure_ref(entry.base, pull=True, task=task)
            return self._build_locked(version, task)

    def _build_locked(self, version: str, task: Task | None) -> str:
        entry = self._entry(version)
        tag = image_tag(self.config.docker.name_prefix, version)
        self.paths.run.mkdir(parents=True, exist_ok=True)
        context = Path(tempfile.mkdtemp(prefix="image-build-", dir=self.paths.run))
        try:
            (context / "Dockerfile").write_text(dockerfile_text("Dockerfile", ANDROID_DOCKERFILE), encoding="utf-8")
            skipped = _stage_overlay(self.overlay_dir(version), context / "overlay")
            _step(task, f"building {tag} from {entry.base}")
            try:
                self.docker.build(tag, str(context), build_args={
                    "BASE": entry.base, "BASE_ID": self.docker.image_id(entry.base) or "unknown",
                    "OVERLAY_HASH": self.overlay_hash(version)}, timeout=1800)
            except CommandError as e:
                raise ImageError(f"building {tag} failed: {e}. Run `docker build` by hand with "
                                 f"{DOCKERFILE_DIR / 'Dockerfile'} to see the full output") from None
            if skipped and task is not None:
                task.step(f"built {tag}; skipped {len(skipped)} special file(s) in the overlay: {', '.join(skipped[:5])}")
        finally:
            shutil.rmtree(context, ignore_errors=True)
        return tag

    # ------------------------------------------------------------------- helper
    def build_tools(self, task: Task | None = None) -> str:
        with self._lock("tools"):
            self.paths.run.mkdir(parents=True, exist_ok=True)
            context = Path(tempfile.mkdtemp(prefix="tools-build-", dir=self.paths.run))
            try:
                (context / "Dockerfile").write_text(dockerfile_text("Dockerfile.tools", TOOLS_DOCKERFILE), encoding="utf-8")
                _step(task, f"building the helper image {self.tools_tag}")
                try:
                    self.docker.build(self.tools_tag, str(context), build_args={"BASE": self.tools_base}, timeout=900)
                except CommandError as e:
                    raise ImageError(
                        f"building the helper image {self.tools_tag} failed: {e}. It needs the internet once "
                        f"(to fetch GNU tar and zstd); retry when online, or build it by hand: "
                        f"docker build -t {self.tools_tag} -f {DOCKERFILE_DIR / 'Dockerfile.tools'} {DOCKERFILE_DIR}") from None
            finally:
                shutil.rmtree(context, ignore_errors=True)
        return self.tools_tag

    def helper(self, *, gnu_tar: bool = False, task: Task | None = None) -> str:
        """Image for a short-lived container that works on a data volume.

        gnu_tar=True   backups and clones: needs the tools image (GNU tar keeps xattrs/ACLs).
        gnu_tar=False  deleting or reading a few files: any shell will do, so a plain base
                       image already on the machine is used rather than making an Android's
                       start wait for (or fail on) an image build."""
        if self.docker.image_exists(self.tools_tag):
            return self.tools_tag
        if not gnu_tar:
            for ref in (self.tools_base, "alpine:latest"):
                if self.docker.image_exists(ref):
                    return ref
        return self.build_tools(task)


def _major(version: str) -> str:
    """`13` for `13`, `13.0` or `13-custom`; "" for a name that does not start with a number."""
    m = re.match(r"\d+", str(version))
    return m.group() if m else ""


def _version_key(version: str) -> tuple:
    return (0, int(version), "") if version.isdigit() else (1, 0, version)


def _stage_overlay(source: Path, target: Path) -> list[str]:
    """Copy the overlay into the build context. Returns special files that were left out.

    Directories are staged 0755: `COPY` applies a source directory's mode to the directory
    it merges into, and a group-writable /system or /vendor is not something to bake in.
    Symlinks are copied as links, never followed, so a link cannot pull host files in."""
    skipped: list[str] = []
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o755)                       # noqa: S103 - mode Android expects on system folders
    if not source.is_dir():
        return skipped
    for folder, dirs, files in os.walk(source, followlinks=False):
        rel = os.path.relpath(folder, source)
        dest = target if rel == "." else target / rel
        for name in list(dirs):
            src = os.path.join(folder, name)
            if os.path.islink(src):
                os.symlink(os.readlink(src), dest / name)
                dirs.remove(name)
            else:
                (dest / name).mkdir(exist_ok=True)
                os.chmod(dest / name, 0o755)     # noqa: S103
        for name in files:
            src = os.path.join(folder, name)
            st = os.lstat(src)
            if stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(src), dest / name)
            elif stat.S_ISREG(st.st_mode):
                shutil.copy2(src, dest / name)
            else:
                skipped.append(os.path.relpath(src, source))
    return skipped

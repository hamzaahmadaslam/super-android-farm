"""Apps on the fleet: install, remove, start, stop, permissions, "who has which version".

Nothing here knows any particular app. Every operation takes a package name (or a
library app id) and a selector, runs per device through core.tasks.run_on_instances with
`control.concurrency` devices at a time, and returns a Task whose per-device results
stand on their own: a device that is off, full or incompatible is one failed line, never
the end of the rollout.

Installing a split app means choosing files per device (see library.select_splits) and
handing them to `adb install-multiple` in one transaction: installing the base alone
"succeeds" and then crashes at first start for lack of its native-code split.

App data backup/restore is an ADVANCED tool. It copies /data/data/<package> out of (or
into) the Android container as root. It does not capture device-encrypted storage,
shared storage or account tokens kept by the system, and a restored login may still be
rejected by a service that pins sessions to a device. Whole-device backups (core.backup)
are the dependable way to move an Android; this is for moving one app's state.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import subprocess
import tarfile
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.adb import Adb, AdbError
from ..core.docker import Docker
from ..core.models import check_id
from ..core.tasks import Task, TaskManager
from .. import __version__
from ..util.files import inside, read_json, write_json
from ..util.proc import kill_tree, spawn
from . import permissions as perms
from .apkinfo import valid_package
from .library import AppLibrary, DeviceTraits, select_splits
from .shell import fleet, pick, reach, settings, sh

if TYPE_CHECKING:
    from ..core.instances import InstanceManager

_ACTIVITY = re.compile(r"^[A-Za-z0-9_.$]+(/[A-Za-z0-9_.$]+)?\Z")
_BLOCK = re.compile(r"^  Package \[([^\]]+)\] \(")
_DATA_BACKUP_TIMEOUT = 3600      # an app's data is at most a few GB; an hour means something hung
_COPY_CHUNK = 1024 * 1024
_MAX_TAR_MEMBERS = 2_000_000

# What Android's terse install failure codes mean for the person reading the task result.
_INSTALL_HINTS = {
    "INSTALL_FAILED_VERSION_DOWNGRADE": "a newer version is installed; uninstall it first or install with downgrade",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "an app with this package name but a different signature is installed; uninstall it first (its data is lost)",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "the app has no native code for this Android's CPU; use an image with ARM translation or an x86 build",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "the Android's data volume is full",
    "INSTALL_FAILED_MISSING_SPLIT": "the app needs its split APKs; add the complete bundle (.xapk/.apks/.apkm) to the library",
    "INSTALL_FAILED_OLDER_SDK": "the app needs a newer Android version than this instance runs",
    "INSTALL_FAILED_ALREADY_EXISTS": "the app is already installed; install with replace",
    "INSTALL_FAILED_TEST_ONLY": "the APK is marked test-only and cannot be installed normally",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "the APK is not signed (or was modified after signing)",
    "INSTALL_FAILED_INVALID_APK": "Android rejected the APK set; the bundle is incomplete or its pieces do not belong together",
}


def check_package(name: str) -> str:
    """Package names become arguments of device commands and parts of paths."""
    if not isinstance(name, str) or not valid_package(name):
        raise ValueError(f"bad package name {str(name)[:80]!r}")
    return name


def explain_install_failure(message: str) -> str:
    for code, hint in _INSTALL_HINTS.items():
        if code in message:
            return f"{code}: {hint}"
    return message


# --------------------------------------------------------------------------- parsers (pure)

def parse_dumpsys_packages(text: str) -> list[dict]:
    """`dumpsys package packages` -> one dict per package (user 0's view)."""
    out: list[dict] = []
    cur: dict | None = None
    section = "Packages:"
    for line in text.splitlines():
        if line and not line[0].isspace():
            section, cur = line.strip(), None
            continue
        if section != "Packages:":      # "Hidden system packages:" repeats updated system apps
            continue
        m = _BLOCK.match(line)
        if m:
            cur = {"package": m.group(1), "version_code": 0, "version_name": "", "system": False,
                   "enabled": True, "installed": True, "uid": 0, "installer": "", "updated": ""}
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("versionCode="):
            m1 = re.match(r"versionCode=(\d+)", s)
            cur["version_code"] = int(m1.group(1)) if m1 else 0
        elif s.startswith("versionName="):
            cur["version_name"] = s[len("versionName="):]
        elif s.startswith(("userId=", "appId=")) and not cur["uid"]:
            m2 = re.match(r"(?:userId|appId)=(\d+)", s)
            cur["uid"] = int(m2.group(1)) if m2 else 0
        elif s.startswith(("flags=[", "pkgFlags=[")):
            cur["system"] = cur["system"] or " SYSTEM " in s
        elif s.startswith("installerPackageName="):
            cur["installer"] = s.split("=", 1)[1]
        elif s.startswith("lastUpdateTime="):
            cur["updated"] = s.split("=", 1)[1]
        elif s.startswith("User 0:"):
            m3, m4 = re.search(r"\benabled=(\d)", s), re.search(r"\binstalled=(true|false)", s)
            # 0 = default, 1 = enabled; 2-4 are the disabled states
            cur["enabled"] = m3 is None or m3.group(1) in ("0", "1")
            cur["installed"] = m4 is None or m4.group(1) == "true"
    return out


def parse_pm_list(text: str) -> list[dict]:
    """`pm list packages [-f] [--show-versioncode] [-U]` -> dicts. The APK path may itself
    contain `=` (Android's randomised install folders end in `==`)."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^package:(?:(.*)=)?([A-Za-z0-9_.]+)(?:\s+versionCode:(\d+))?(?:\s+uid:(\d+))?\s*$", line.strip())
        if m:
            out.append({"package": m.group(2), "path": m.group(1) or "", "version_code": int(m.group(3) or 0),
                        "uid": int(m.group(4) or 0)})
    return out


def parse_requested_permissions(text: str, package: str) -> list[str] | None:
    """The permissions a package requests, from `dumpsys package <package>`.
    None when the package is not in the dump (not installed)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(f"  Package [{package}] ")), None)
    if start is None:
        return None
    out: list[str] = []
    collecting, indent = False, 0
    for line in lines[start + 1:]:
        if _BLOCK.match(line) or (line and not line[0].isspace()):
            break
        stripped = line.strip()
        depth = len(line) - len(line.lstrip())
        if stripped == "requested permissions:":
            collecting, indent = True, depth
        elif collecting:
            if depth <= indent:
                break
            name = stripped.split(":", 1)[0]
            if name and name not in out:
                out.append(name)
    return out


def parse_traits(text: str) -> DeviceTraits:
    """Output of the fixed probe in read_traits() -> DeviceTraits."""
    kv: dict[str, str] = {}
    density = 0
    for line in text.splitlines():
        m = re.match(r"^(Physical|Override) density:\s*(\d+)", line.strip())
        if m:
            if m.group(1) == "Override" or not density:
                density = int(m.group(2))
        elif "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    locales = [t for t in kv.get("syslocales", "").split(",") if t and t != "null"]
    locales = locales or [t for t in (kv.get("locale"), kv.get("rolocale")) if t] or ["en-US"]
    sdk = kv.get("sdk", "")
    return DeviceTraits(abis=[a for a in kv.get("abilist", "").split(",") if a], density=density or 160,
                        locales=locales, sdk=int(sdk) if sdk.isdigit() else 0,
                        native_bridge=kv.get("bridge", "") not in ("", "0"))


_TRAITS_PROBE = ('echo "abilist=$(getprop ro.product.cpu.abilist)"; echo "bridge=$(getprop ro.dalvik.vm.native.bridge)"; '
                 'echo "sdk=$(getprop ro.build.version.sdk)"; echo "locale=$(getprop persist.sys.locale)"; '
                 'echo "rolocale=$(getprop ro.product.locale)"; echo "syslocales=$(settings get system system_locales 2>/dev/null)"; '
                 'wm density')


# --------------------------------------------------------------------------- one device

def read_traits(adb: Adb) -> DeviceTraits:
    return parse_traits(adb.shell(_TRAITS_PROBE, timeout=20, check=False))


def installed_version(adb: Adb, package: str) -> int | None:
    """versionCode of an installed package, or None. One cheap command, for `ensure`."""
    out = adb.shell(["pm", "list", "packages", "--show-versioncode", check_package(package)], timeout=20, check=False)
    return next((p["version_code"] for p in parse_pm_list(out) if p["package"] == package), None)


def list_installed(adb: Adb, *, third_party: bool = True) -> list[dict]:
    """Packages on one device with version, enabled state and whether they came with the image."""
    rows = [p for p in parse_dumpsys_packages(adb.shell("dumpsys package packages", timeout=45, check=False))
            if p["installed"]]
    if not rows:
        # Some builds restrict dumpsys; `pm list` still answers, without version names.
        disabled = {p["package"] for p in parse_pm_list(adb.shell("pm list packages -d", timeout=20, check=False))}
        third = {p["package"] for p in parse_pm_list(adb.shell("pm list packages -3", timeout=20, check=False))}
        listing = parse_pm_list(adb.shell("pm list packages --show-versioncode -U", timeout=20))
        rows = [{"package": p["package"], "version_code": p["version_code"], "version_name": "", "uid": p["uid"],
                 "system": p["package"] not in third, "enabled": p["package"] not in disabled, "installed": True,
                 "installer": "", "updated": ""} for p in listing]
    if third_party:
        rows = [r for r in rows if not r["system"]]
    for r in rows:
        r["version"] = r["version_name"] or str(r["version_code"])
    return sorted(rows, key=lambda r: r["package"])


def launcher_activity(adb: Adb, package: str) -> str:
    """`package/activity` the launcher would start, or "" when the package has none."""
    out = adb.shell(["cmd", "package", "resolve-activity", "--brief", "-c", "android.intent.category.LAUNCHER",
                     check_package(package)], timeout=20, check=False)
    line = next((ln.strip() for ln in reversed(out.splitlines()) if "/" in ln), "")
    return line if _ACTIVITY.match(line) and line.startswith(package + "/") else ""


def install_app(adb: Adb, library: AppLibrary, app_id: str, *, grant: bool = True, replace: bool = True,
                downgrade: bool = False, all_locales: bool = False, timeout: float = 900) -> str:
    """Install one library app on one device: pick the splits, install them in one
    transaction, then push expansion (OBB) files. Returns a one-line summary."""
    meta = library.get(app_id)
    selection = select_splits(meta, read_traits(adb), all_locales=all_locales)
    files = [str(library.file(app_id, name)) for name in selection.files]
    try:
        adb.install(files, replace=replace, grant=grant, downgrade=downgrade, timeout=timeout)
    except AdbError as e:
        raise AdbError(explain_install_failure(str(e))) from None
    package = meta["package"]
    if meta.get("obb"):
        # Games look for these beside the APK data; `-g` above already covers storage access.
        folder = f"/sdcard/Android/obb/{package}"
        adb.shell(["mkdir", "-p", folder], timeout=20)
        for obb in meta["obb"]:
            adb.push(str(library.file(app_id, f"obb/{obb['file']}")), f"{folder}/{obb['file']}", timeout=timeout)
    parts = [f"installed {meta.get('version_name') or meta['version_code']}"]
    if len(files) > 1:
        parts.append(f"{len(files)} APKs")
    if meta.get("obb"):
        parts.append(f"{len(meta['obb'])} OBB")
    return ", ".join(parts) + "".join(f"; warning: {w}" for w in selection.warnings)


def launch_app(adb: Adb, package: str, activity: str | None = None) -> str:
    check_package(package)
    if activity:
        if not _ACTIVITY.match(activity):
            raise ValueError(f"bad activity name {activity[:80]!r}")
        component = activity if "/" in activity else f"{package}/{activity}"
    else:
        component = launcher_activity(adb, package)
    if component:
        argv = ["am", "start", "-n", component]
        if not activity:
            argv += ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER"]
        out = sh(adb, argv, timeout=30)
        if not out.ok or "Error" in out.text or "Exception" in out.text:    # `am start` exits 0 on failure
            raise AdbError(f"could not start {component}: {out.last}")
        return f"started {component}"
    # No resolvable launcher entry (some apps only register an alias): let monkey find one.
    # `--pct-syskeys 0`: monkey aborts on devices without hardware keys, which is every container.
    out = sh(adb, ["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "--pct-syskeys", "0", "1"], timeout=30)
    if not out.ok or "Events injected: 1" not in out.stdout:
        raise AdbError(f"{package} is not installed or has nothing the launcher can start")
    return "started (launcher intent)"


def stop_app(adb: Adb, package: str) -> str:
    out = sh(adb, ["am", "force-stop", check_package(package)], timeout=20)
    if not out.ok:
        raise AdbError(f"could not stop {package}: {out.last}")
    return "stopped"


def clear_app(adb: Adb, package: str) -> str:
    out = sh(adb, ["pm", "clear", check_package(package)], timeout=60)
    if "Success" not in out.stdout:
        why = "not installed" if installed_version(adb, package) is None else out.last
        raise AdbError(f"could not clear {package}: {why}")
    return "data cleared"


def set_enabled(adb: Adb, package: str, enabled: bool) -> str:
    argv = ["pm", "enable", check_package(package)] if enabled else ["pm", "disable-user", "--user", "0", package]
    out = sh(adb, argv, timeout=30)
    if "new state:" not in out.stdout:
        raise AdbError(f"could not {'enable' if enabled else 'disable'} {package}: {out.last}")
    return out.stdout.strip().splitlines()[-1].split("new state:", 1)[1].strip()


def uninstall_app(adb: Adb, package: str, *, keep_data: bool = False) -> str:
    check_package(package)
    # Asked first because `pm uninstall` answers a missing package and a protected system
    # package with the same DELETE_FAILED_INTERNAL_ERROR.
    if installed_version(adb, package) is None:
        return "was not installed"      # the goal state already holds; a rollout must be repeatable
    out = sh(adb, ["pm", "uninstall", *(["-k"] if keep_data else []), package], timeout=120)
    if "Success" in out.stdout:
        return "uninstalled" + (" (data kept)" if keep_data else "")
    hint = " (apps that are part of the image cannot be uninstalled; disable them instead)" if "DELETE_FAILED_INTERNAL_ERROR" in out.text else ""
    raise AdbError(f"uninstall failed: {out.last}{hint}")


def requested_permissions(adb: Adb, package: str) -> list[str]:
    found = parse_requested_permissions(sh(adb, ["dumpsys", "package", check_package(package)], timeout=30).stdout, package)
    if found is None:
        raise AdbError(f"{package} is not installed")
    return found


def set_permission(adb: Adb, package: str, permission: str = perms.ALL, *, grant: bool = True) -> str:
    """Grant or revoke one permission, or (`all`) every runtime permission the app
    requests. Revoking a permission from a running app makes Android kill it."""
    check_package(package)
    verb, past = ("grant", "granted") if grant else ("revoke", "revoked")
    # `pm grant` on a package that does not exist exits 0 and says nothing; asking for the
    # requested permissions first is what turns that into an error.
    requested = requested_permissions(adb, package)
    if permission != perms.ALL:
        name = perms.normalize(permission)
        if perms.is_special(name):
            out = sh(adb, ["appops", "set", package, perms.SPECIAL[name], "allow" if grant else "default"], timeout=20)
            if not out.ok:
                raise AdbError(f"could not {verb} {name}: {out.last}")
            return f"{past} {name} (app-op)"
        if name not in requested:
            raise AdbError(f"{package} does not request {name}; Android only grants what the app's manifest asks for")
        out = sh(adb, ["pm", verb, package, name], timeout=20)
        if not out.ok:
            raise AdbError(f"could not {verb} {name}: {out.last}")
        return f"{past} {name}"
    sdk = adb.getprop("ro.build.version.sdk")
    names = perms.dangerous_of(requested, int(sdk) if sdk.isdigit() else None)
    failed = [n for n in names if not sh(adb, ["pm", verb, package, n], timeout=20).ok]
    done = len(names) - len(failed)
    if names and not done:
        raise AdbError(f"could not {verb} any of the {len(names)} runtime permissions {package} requests")
    tail = f"; {len(failed)} refused by Android ({', '.join(f.rsplit('.', 1)[-1] for f in failed[:4])})" if failed else ""
    return f"{past} {done} runtime permission{'s' if done != 1 else ''}{tail}" if names else "requests no runtime permissions"


# --------------------------------------------------------------------------- app data (advanced)

def _manifest_path(archive: Path) -> Path:
    return archive.with_name(archive.name + ".json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_COPY_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_data_archive(archive: Path, package: str) -> dict:
    """Check that an archive holds exactly one app's data folder and nothing that could be
    written elsewhere when it is unpacked as root inside an Android. Returns {members, uid}."""
    members, uid, links = 0, None, set()
    try:
        with tarfile.open(archive, "r|*") as tar:
            for m in tar:
                members += 1
                if members > _MAX_TAR_MEMBERS:
                    raise ValueError("archive has too many entries")
                parts = m.name.split("/")
                if m.name.startswith("/") or ".." in parts or parts[0] != package:
                    raise ValueError(f"archive entry {m.name[:120]!r} is outside {package}/")
                if m.ischr() or m.isblk():
                    raise ValueError(f"archive entry {m.name[:120]!r} is a device node")
                # An entry reached *through* an archived symlink would be written wherever
                # the link points; legitimate app data never nests under its own links.
                if any("/".join(parts[:i]) in links for i in range(1, len(parts))):
                    raise ValueError(f"archive entry {m.name[:120]!r} is placed through a symbolic link")
                if m.issym():
                    links.add(m.name.rstrip("/"))
                if m.islnk() and (m.linkname.startswith("/") or ".." in m.linkname.split("/") or m.linkname.split("/")[0] != package):
                    raise ValueError(f"archive entry {m.name[:120]!r} hard-links outside {package}/")
                if m.name.rstrip("/") == package:
                    uid = m.uid
    except (tarfile.TarError, EOFError, OSError) as e:
        raise ValueError(f"{archive.name} is not a readable app-data archive: {e}") from None
    if not members:
        raise ValueError(f"{archive.name} is empty")
    return {"members": members, "uid": uid}


# --------------------------------------------------------------------------- the fleet API

class Apps:
    def __init__(self, mgr: InstanceManager, library: AppLibrary, tasks: TaskManager, *, docker: Docker | None = None):
        self.mgr, self.library, self.tasks = mgr, library, tasks
        self.docker = docker or Docker(mgr.config.docker.binary)

    # ---------------------------------------------------------------- install / remove
    def install(self, selector: str | int | Iterable[int], app_id: str, *, grant: bool = True, replace: bool = True,
                downgrade: bool = False, all_locales: bool = False) -> Task:
        meta = self.library.get(app_id)     # fail now, not once per device, when the app is unknown
        timeout = settings(self.mgr.config).install_timeout

        def one(_id: int, adb: Adb, _task: Task) -> str:
            return install_app(adb, self.library, app_id, grant=grant, replace=replace, downgrade=downgrade,
                               all_locales=all_locales, timeout=timeout)

        return fleet(self.mgr, self.tasks, "app-install", f"Install {meta['label']} {meta['version_name']}".strip(), selector, one)

    def ensure(self, selector: str | int | Iterable[int], app_id: str, *, grant: bool = True) -> Task:
        """Idempotent rollout: install only where the app is missing or older. Safe to
        repeat after a partial failure; devices that are already right are not touched."""
        meta = self.library.get(app_id)
        want, package = int(meta["version_code"]), meta["package"]
        timeout = settings(self.mgr.config).install_timeout

        def one(_id: int, adb: Adb, _task: Task) -> str:
            have = installed_version(adb, package)
            if have is not None and have == want:
                return f"already installed ({meta.get('version_name') or want})"
            if have is not None and have > want:
                return f"kept newer version (versionCode {have} > {want})"
            done = install_app(adb, self.library, app_id, grant=grant, replace=True, timeout=timeout)
            return done if have is None else f"updated from versionCode {have}: {done}"

        return fleet(self.mgr, self.tasks, "app-ensure", f"Ensure {meta['label']} {meta['version_name']}".strip(), selector, one)

    def uninstall(self, selector: str | int | Iterable[int], package: str, *, keep_data: bool = False) -> Task:
        check_package(package)
        return fleet(self.mgr, self.tasks, "app-uninstall", f"Uninstall {package}", selector,
                     lambda _id, adb, _t: uninstall_app(adb, package, keep_data=keep_data))

    # ---------------------------------------------------------------- run state
    def launch(self, selector: str | int | Iterable[int], package: str, *, activity: str | None = None) -> Task:
        check_package(package)
        if activity and not _ACTIVITY.match(activity):
            raise ValueError(f"bad activity name {activity[:80]!r}")
        return fleet(self.mgr, self.tasks, "app-launch", f"Launch {package}", selector,
                     lambda _id, adb, _t: launch_app(adb, package, activity))

    def stop(self, selector: str | int | Iterable[int], package: str) -> Task:
        check_package(package)
        return fleet(self.mgr, self.tasks, "app-stop", f"Stop {package}", selector, lambda _id, adb, _t: stop_app(adb, package))

    def clear(self, selector: str | int | Iterable[int], package: str) -> Task:
        check_package(package)
        return fleet(self.mgr, self.tasks, "app-clear", f"Clear data of {package}", selector,
                     lambda _id, adb, _t: clear_app(adb, package))

    def enable(self, selector: str | int | Iterable[int], package: str) -> Task:
        check_package(package)
        return fleet(self.mgr, self.tasks, "app-enable", f"Enable {package}", selector,
                     lambda _id, adb, _t: set_enabled(adb, package, True))

    def disable(self, selector: str | int | Iterable[int], package: str) -> Task:
        check_package(package)
        return fleet(self.mgr, self.tasks, "app-disable", f"Disable {package}", selector,
                     lambda _id, adb, _t: set_enabled(adb, package, False))

    # ---------------------------------------------------------------- permissions
    def grant(self, selector: str | int | Iterable[int], package: str, permission: str = perms.ALL) -> Task:
        check_package(package)
        name = permission if permission == perms.ALL else perms.normalize(permission)
        return fleet(self.mgr, self.tasks, "app-grant", f"Grant {name} to {package}", selector,
                     lambda _id, adb, _t: set_permission(adb, package, name, grant=True))

    def revoke(self, selector: str | int | Iterable[int], package: str, permission: str = perms.ALL) -> Task:
        check_package(package)
        name = permission if permission == perms.ALL else perms.normalize(permission)
        return fleet(self.mgr, self.tasks, "app-revoke", f"Revoke {name} from {package}", selector,
                     lambda _id, adb, _t: set_permission(adb, package, name, grant=False))

    def permissions(self, id: int, package: str) -> dict:
        """What one app requests on one device, split by how it has to be given."""
        requested = requested_permissions(reach(self.mgr, id), check_package(package))
        return {"requested": requested, "dangerous": perms.dangerous_of(requested), "special": perms.special_of(requested)}

    # ---------------------------------------------------------------- inventory
    def installed(self, id: int, *, third_party: bool = True) -> list[dict]:
        rows = list_installed(reach(self.mgr, id), third_party=third_party)
        labels = {m["package"]: m.get("label", "") for m in reversed(self.library.list())}
        for r in rows:
            r["label"] = labels.get(r["package"], "")
        return rows

    def matrix_detail(self, selector: str | int | Iterable[int], *, third_party: bool = True) -> dict:
        """The fleet view: {"instances": [...], "packages": {package: {id: {version, version_code,
        enabled}}}, "errors": {id: why}}. Devices are read in parallel and an unreachable
        one is reported, not fatal."""
        ids = pick(self.mgr, selector)
        packages: dict[str, dict[int, dict]] = {}
        errors: dict[int, str] = {}

        def read(instance_id: int) -> tuple[int, list[dict] | None, str]:
            try:
                return instance_id, list_installed(reach(self.mgr, instance_id), third_party=third_party), ""
            except Exception as e:  # noqa: BLE001 - one device's failure is a cell in the matrix
                return instance_id, None, f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(max_workers=settings(self.mgr.config).concurrency, thread_name_prefix="sf-matrix") as pool:
            for instance_id, rows, error in pool.map(read, ids):
                if rows is None:
                    errors[instance_id] = error
                    continue
                for r in rows:
                    packages.setdefault(r["package"], {})[instance_id] = {
                        "version": r["version"], "version_code": r["version_code"], "enabled": r["enabled"]}
        return {"instances": ids, "packages": dict(sorted(packages.items())), "errors": errors}

    def matrix(self, selector: str | int | Iterable[int]) -> dict:
        """package -> {instance id: version}, for third-party apps."""
        detail = self.matrix_detail(selector)
        return {pkg: {i: cell["version"] for i, cell in cells.items()} for pkg, cells in detail["packages"].items()}

    # ---------------------------------------------------------------- app data (advanced)
    def _backup_root(self) -> Path:
        configured = getattr(getattr(self.mgr.config, "backup", None), "dir", "")
        return (Path(configured).expanduser() if configured else self.library.paths.backups) / "apps"

    def _container(self, id: int) -> str:
        name = self.mgr.resolved(check_id(id)).names.android
        if not self.docker.running(name):
            raise RuntimeError(f"Android {id} is not running; app data can only be copied from a running Android")
        return name

    def backup_app_data(self, id: int, package: str, *, stop: bool = True) -> Path:
        """ADVANCED. Archive /data/data/<package> of one Android, owners preserved, to
        <backups>/apps/<package>/. The app is stopped first so files are not caught mid-write."""
        check_package(package)
        container = self._container(id)
        if not self.docker.exec(container, ["test", "-d", f"/data/data/{package}"], timeout=20, check=False).ok:
            raise RuntimeError(f"{package} has no data on Android {id} (not installed?)")
        if stop:
            stop_app(reach(self.mgr, id), package)
        folder = self._backup_root() / package
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{package}_android{int(id)}_{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        tmp = dest.with_name("." + dest.name + ".part")
        errlog = dest.with_name("." + dest.name + ".err")
        deadline = time.monotonic() + _DATA_BACKUP_TIMEOUT
        try:
            with open(errlog, "wb") as err, gzip.open(tmp, "wb", compresslevel=3) as out:
                p = spawn([self.docker.binary, "exec", container, "tar", "-C", "/data/data", "--numeric-owner",
                           "-cf", "-", package], stdout=subprocess.PIPE, stderr=err)
                try:
                    while chunk := p.stdout.read(_COPY_CHUNK):
                        out.write(chunk)
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"backup of {package} exceeded {_DATA_BACKUP_TIMEOUT}s")
                    code = p.wait(timeout=60)
                finally:
                    if p.poll() is None:
                        kill_tree(p.pid)
                    p.stdout.close()
            if code != 0:
                raise RuntimeError(f"tar in the Android failed (exit {code}): {errlog.read_text(errors='replace').strip()[-300:]}")
            found = inspect_data_archive(tmp, package)      # never leave an archive behind that restore would refuse
            adb = self.mgr.adb(check_id(id))
            manifest = {"kind": "app-data", "package": package, "instance": int(id), "archive": dest.name,
                        "created": time.time(), "size": tmp.stat().st_size, "sha256": _sha256(tmp),
                        "members": found["members"], "uid": found["uid"], "version_code": installed_version(adb, package),
                        "android_sdk": adb.getprop("ro.build.version.sdk"), "superfarm": __version__}
            tmp.replace(dest)
            write_json(_manifest_path(dest), manifest)
        finally:
            tmp.unlink(missing_ok=True)
            errlog.unlink(missing_ok=True)
        return dest

    def restore_app_data(self, id: int, package: str, archive: Path | str) -> str:
        """ADVANCED. Replace /data/data/<package> on one Android with an archive made by
        backup_app_data. The app must already be installed there (its user id on that
        device is what the restored files are given to)."""
        check_package(package)
        archive = Path(archive)
        try:
            archive = inside(self._backup_root(), archive)
        except ValueError:
            raise ValueError(f"app-data archives are only restored from {self._backup_root()}") from None
        self.verify_app_data(archive, package)
        found = inspect_data_archive(archive, package)
        container = self._container(id)
        target = f"/data/data/{package}"
        owner = self.docker.exec(container, ["stat", "-c", "%u", target], timeout=20, check=False)
        if not owner.ok or not owner.out.isdigit():
            raise RuntimeError(f"{package} is not installed on Android {id}; install it first, then restore its data")
        uid = int(owner.out)
        stop_app(reach(self.mgr, id), package)
        self.docker.exec(container, ["find", target, "-mindepth", "1", "-delete"], timeout=300)
        deadline = time.monotonic() + _DATA_BACKUP_TIMEOUT
        p = spawn([self.docker.binary, "exec", "-i", container, "tar", "-C", "/data/data", "-xf", "-"], stdin=subprocess.PIPE)
        try:
            with gzip.open(archive, "rb") as src:
                while chunk := src.read(_COPY_CHUNK):
                    p.stdin.write(chunk)
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"restore of {package} exceeded {_DATA_BACKUP_TIMEOUT}s")
            p.stdin.close()
            code = p.wait(timeout=300)
        except BrokenPipeError:
            code = p.wait(timeout=60)
        finally:
            if p.poll() is None:
                kill_tree(p.pid)
        if code != 0:
            raise RuntimeError(f"unpacking in the Android failed (tar exit {code}); the app's data is incomplete, clear it and retry")
        note = ""
        if found["uid"] is not None and found["uid"] != uid:
            # The same app gets a different user id on every device. Files restored with the
            # old owner would be unreadable to the app: hand everything to the local id, and
            # the cache folders to its cache group (uid + 10000, Android's convention).
            self.docker.exec(container, ["chown", "-R", f"{uid}:{uid}", target], timeout=600)
            if 10000 <= uid <= 19999:
                for cache in ("cache", "code_cache"):
                    self.docker.exec(container, ["chown", "-R", f"{uid}:{uid + 10000}", f"{target}/{cache}"], timeout=120, check=False)
            note = f", owner changed from {found['uid']} to {uid}"
        self.docker.exec(container, ["restorecon", "-RF", target], timeout=300, check=False)
        return f"restored {found['members']} entries{note}"

    def verify_app_data(self, archive: Path | str, package: str | None = None) -> dict | None:
        """Check an archive against the manifest written beside it (same idea as whole-Android
        backups: tar + manifest + sha256). Returns the manifest, or None for an archive that
        has none (copied in by hand); raises ValueError when they disagree."""
        archive = Path(archive)
        manifest = read_json(_manifest_path(archive))
        if not isinstance(manifest, dict):
            return None
        if package and manifest.get("package") != package:
            raise ValueError(f"{archive.name} holds data of {manifest.get('package')}, not of {package}")
        if manifest.get("sha256") != _sha256(archive):
            raise ValueError(f"{archive.name} does not match its manifest (sha256): it was changed or damaged after the backup")
        return manifest

    def backup_data(self, selector: str | int | Iterable[int], package: str) -> Task:
        """ADVANCED. backup_app_data on every selected Android; each result is the archive path."""
        check_package(package)
        return fleet(self.mgr, self.tasks, "app-data-backup", f"Back up data of {package}", selector,
                     lambda instance_id, _adb, _t: str(self.backup_app_data(instance_id, package)))

    def restore_data(self, selector: str | int | Iterable[int], package: str, archive: Path | str) -> Task:
        """ADVANCED. restore_app_data on every selected Android (the same archive on each)."""
        check_package(package)
        return fleet(self.mgr, self.tasks, "app-data-restore", f"Restore data of {package}", selector,
                     lambda instance_id, _adb, _t: self.restore_app_data(instance_id, package, archive))

    def data_backups(self, package: str | None = None) -> list[dict]:
        root = self._backup_root()
        folders = [root / check_package(package)] if package else sorted(p for p in root.glob("*") if p.is_dir())
        out = []
        for folder in folders:
            for f in sorted(folder.glob("*.tar.gz"), reverse=True):
                st = f.stat()
                manifest = read_json(_manifest_path(f)) or {}
                out.append({"package": folder.name, "file": f.name, "path": str(f), "size": st.st_size, "created": st.st_mtime,
                            "instance": manifest.get("instance"), "version_code": manifest.get("version_code"),
                            "sha256": manifest.get("sha256", "")})
        return out

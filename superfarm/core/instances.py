"""The life of an Android: create, start, stop, rebuild, clone, delete.

One Android is two containers that live and die together:

    <prefix>-net-N       owns the network namespace (VPN, kill switch, published ADB port)
    <prefix>-android-N   the Android itself, inside that namespace, with volume <prefix>-data-N

Rules this module exists to enforce (each one was an incident on a real farm):

  * The pair is always torn down and brought up together, network first. Android's network
    service rewrites the routing of the namespace it joins; an Android recreated alone next
    to a running tunnel drops that tunnel.
  * Containers are only rebuilt when their *plan* changed (see planner.plan_hash). A plain
    start of an unchanged Android reuses what exists.
  * Two operations on one Android never interleave (per-instance lock, also across processes).
  * Nothing the farm did not create is ever removed: containers are checked for the farm's
    label before they are touched, and an adopted data volume is only ever detached.

Two kinds of refusal can be overridden by the operator with `force=True`, and only those:
going over the capacity estimate while performance.enforce_capacity is on (OverCapacity),
and an operation that puts existing data at risk (NeedsForce). `force` never means
"rebuild the containers"; that is `recreate`.

The GPU, network and camera modules are imported lazily through `Backends`, so this module
loads (and is testable) without them.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib
import logging
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterator, Sequence

from ..config import Config
from ..paths import PACKAGE_DIR, Paths
from ..util.proc import CommandError
from . import device_profile
from .adb import Adb, AdbError
from .docker import Docker
from .images import VERSION_BOUND, ImageError, Images
from .models import DeviceIdentity, InstanceSpec, Names, Resolved, check_id, parse_selector, resolve
from .planner import (
    LABEL_CPUSET,
    LABEL_GPU,
    LABEL_GPU_NODE,
    LABEL_PLAN,
    CpuTopology,
    allocate_cpuset,
    cpuset_from_label,
    cpuset_fits,
    parse_cpu_list,
    plan_android,
    plan_hash,
)
from .store import Store
from .tasks import Cancelled, Task, TaskManager, run_on_instances

log = logging.getLogger("superfarm.instances")

LABEL_FARM = "io.superfarm.farm"
LABEL_INSTANCE = "io.superfarm.instance"
LABEL_ROLE = "io.superfarm.role"

TRIM_MARKER = "/data/local/tmp/.superfarm_trim"      # the ADB shell user cannot write to /data itself
TRIM_DIR = PACKAGE_DIR / "data" / "trim"

NET_READY_TIMEOUT = 90
NET_STOP_GRACE = 5
# What one Android is expected to take once it is up (measured 0.9-1.1 GB). Used to keep
# several simultaneous starts from each seeing the same "free" memory.
ANDROID_FOOTPRINT_MB = 1100

_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_PROP_KEY = re.compile(r"^[A-Za-z0-9_.\-]{1,90}$")
_PROXY = re.compile(r"^[A-Za-z0-9.\-]{1,253}:\d{1,5}$|^\[[0-9A-Fa-f:]{2,45}\]:\d{1,5}$")
_VOLUME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

# Never disabled, whatever a trim list says: without any of these an Android does not boot,
# cannot be reached, or cannot install and run apps.
PROTECTED_PACKAGES = frozenset({
    "android", "com.android.systemui", "com.android.shell", "com.android.settings",
    "com.android.providers.settings", "com.android.providers.media", "com.android.providers.media.module",
    "com.android.providers.downloads", "com.android.providers.contacts", "com.android.providers.telephony",
    "com.android.packageinstaller", "com.android.permissioncontroller", "com.android.inputmethod.latin",
    "com.android.launcher3", "com.android.webview", "com.android.networkstack",
    "com.android.networkstack.tethering", "com.android.phone", "com.android.server.telecom",
    "com.android.externalstorage", "com.android.documentsui", "com.android.keychain",
    "com.android.certinstaller", "com.android.location.fused", "com.android.camera2",
})

# Clears compiled-shader caches when the renderer changed. Fixed text; the renderer name is
# passed as $1, never pasted into the script.
_RENDERER_SCRIPT = r"""
m=/data/.superfarm_renderer
if [ "$(cat "$m" 2>/dev/null)" = "$1" ]; then echo same; exit 0; fi
for d in /data/user_de /data/user /data/data; do
  [ -d "$d" ] && find "$d" -maxdepth 4 -type f -path '*/code_cache/com.android.*.shaders_cache' -delete
done
printf '%s' "$1" > "$m" && echo cleared
"""

_COPY_SCRIPT = (
    "set -o pipefail; "
    "tar -C /from --xattrs --xattrs-include='*' --acls --numeric-owner --sparse -cf - . | "
    "tar -C /to --xattrs --xattrs-include='*' --acls --numeric-owner --sparse -xpf -"
)


class InstanceError(RuntimeError):
    """An operation on an Android could not be carried out; the message says what to do."""


class InstanceBusy(InstanceError):
    pass


class OverCapacity(InstanceError):
    """A start was refused because performance.enforce_capacity is on and the machine is at
    its estimated limit. `capacity` is what InstanceManager.capacity() returned, so a caller
    can show it and offer "run anyway" (the same start with force=True)."""
    code = "over_capacity"

    def __init__(self, message: str, capacity: dict):
        super().__init__(message)
        self.capacity = capacity


class NeedsForce(InstanceError):
    """Refused because it puts an Android's data at risk; repeating it with force=True is
    the operator's explicit "I know"."""
    code = "needs_force"


class StartError(InstanceError):
    pass


# ============================================================================ trim lists

def trim_list(version: str, *, extra: Sequence[str] = (), keep: Sequence[str] = (),
              directory: Path | None = None) -> list[str]:
    """Packages to disable for one Android version: data/trim/<version>.txt (default.txt when
    that version has no list), plus `extra`, minus `keep` and the protected set."""
    directory = directory or TRIM_DIR
    path = directory / f"{version}.txt"
    if not re.fullmatch(r"[A-Za-z0-9._\-]{1,32}", str(version)) or not path.is_file():
        path = directory / "default.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    out: list[str] = []
    skip = set(keep) | PROTECTED_PACKAGES
    for raw in [*lines, *extra]:
        name = raw.split("#", 1)[0].strip()
        if name and _PACKAGE.match(name) and name not in skip and name not in out:
            out.append(name)
    return out


def trim_hash(packages: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(packages).encode()).hexdigest()[:12] if packages else "none"


# ======================================================================== host readings

def mem_available_mb(meminfo: Path = Path("/proc/meminfo")) -> int:
    """MemAvailable: what the kernel says can be handed out without swapping. `free` memory
    would be wrong here; most of an idle machine's RAM is reclaimable page cache."""
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return -1


def _topology() -> CpuTopology:
    from .metrics import read_cpu_topology
    return read_cpu_topology()


# ============================================================ lazily bound neighbours

def _net_function(name: str) -> Callable:
    try:
        package = importlib.import_module("superfarm.net")
    except ImportError as e:
        raise InstanceError(f"the network module (superfarm.net) cannot be loaded: {e}") from None
    found = getattr(package, name, None)
    if callable(found):
        return found
    for sub in ("sidecar", "status", "vpn"):
        try:
            found = getattr(importlib.import_module(f"superfarm.net.{sub}"), name, None)
        except ImportError:
            continue
        if callable(found):
            return found
    raise InstanceError(f"the network module does not provide {name}()")


def _gpu_choose(r: Resolved, config: Config, paths: Paths, *, counts: dict[str, int] | None = None):
    # For an NVIDIA choice this also starts the relay when it is not running (and leaves a
    # running one alone), and its run_args already carry the socket and runtime mounts.
    from ..host import gpu
    return gpu.choose(r, config, paths, counts=counts)


_GPU_LIST_TTL = 60.0
_gpu_list: tuple[float, list] | None = None
_gpu_list_lock = threading.Lock()


def _capacity_estimate(config: Config, paths: Paths, running: int) -> dict:
    """host.capacity.estimate for this machine and this farm home (where the NVIDIA runtime
    is looked for). The card list is kept for a minute: reading it runs lspci and
    nvidia-smi, and a bulk start asks once per Android."""
    global _gpu_list
    from ..host import capacity, gpu
    with _gpu_list_lock:
        if _gpu_list is None or time.monotonic() - _gpu_list[0] > _GPU_LIST_TTL:
            _gpu_list = (time.monotonic(), gpu.nvidia_vram(gpu.detect_gpus()))
        gpus = _gpu_list[1]
    return capacity.estimate(config, gpus, running=running, paths=paths)


@dataclass
class Backends:
    """Everything the manager needs from outside this module. Each entry defaults to the
    real implementation, looked up on first use; tests replace entries with fakes.

    The network module's sidecar_run_args() builds the sidecar image when it is missing and
    brings a shared gateway (and the farm's uplink network) up by itself, so there is no
    separate entry for either."""
    gpu_choose: Callable[..., Any] = _gpu_choose
    capacity_estimate: Callable[[Config, Paths, int], dict] = _capacity_estimate
    sidecar_run_args: Callable[..., list[str]] = field(
        default=lambda r, config, paths, store, docker: _net_function("sidecar_run_args")(r, config, paths, store, docker))
    net_wait_ready: Callable[..., dict] = field(
        default=lambda docker, r, timeout=NET_READY_TIMEOUT: _net_function("wait_ready")(docker, r, timeout))
    net_status: Callable[..., dict] = field(default=lambda docker, r: _net_function("status")(docker, r))
    adb: Callable[[str], Adb] = Adb
    mem_available_mb: Callable[[], int] = mem_available_mb
    cpu_topology: Callable[[], CpuTopology] = _topology
    sleep: Callable[[float], None] = time.sleep


# ============================================================================= guards

class _Booting:
    """Androids that are being started right now and the memory each is expected to take.

    Several starts running side by side would otherwise all read the same "available"
    memory and the same count of running Androids, and each conclude there is room."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reserved: dict[int, int] = {}

    def reserve(self, instance_id: int, need_mb: int) -> tuple[set[int], int]:
        """Register a start. Returns the *other* Androids that are booting and the memory
        (MB) already promised to them."""
        with self._lock:
            others = {k: v for k, v in self._reserved.items() if k != instance_id}
            self._reserved[instance_id] = need_mb
            return set(others), sum(others.values())

    def release(self, instance_id: int) -> None:
        with self._lock:
            self._reserved.pop(instance_id, None)


def version_order(new: str, old: str) -> int | None:
    """-1 when `new` is an older Android release than `old`, 1 when newer, 0 when the same,
    None when it cannot be told (a custom version name)."""
    a, b = re.match(r"\d+", str(new)), re.match(r"\d+", str(old))
    if not a or not b:
        return 0 if str(new) == str(old) else None
    x, y = int(a.group()), int(b.group())
    return (x > y) - (x < y)


def _step(task: Task | None, message: str) -> None:
    if task is not None:
        task.check_cancel()
        task.step(message)


# ============================================================================ manager

class InstanceManager:
    def __init__(self, config: Config, paths: Paths, docker: Docker, store: Store, tasks: TaskManager):
        self.config, self.paths, self.docker, self.store, self.tasks = config, paths, docker, store, tasks
        self.images = Images(docker, config, paths)
        self.backends = Backends()
        # Called after an Android booted, in order. Other modules append (camera attach, ...).
        self.post_boot_hooks: list[Callable[[Resolved, Adb], None]] = []
        # Each returns extra `docker run` arguments for the Android container (mounts, devices).
        self.extra_run_args_providers: list[Callable[[Resolved], list[str]]] = []
        self.lock_wait = 30.0
        self._locks: dict[int, threading.Lock] = {}
        self._guard = threading.Lock()
        self._creating = threading.Lock()          # ids are handed out to one create() at a time
        # A bulk start runs several starts at once. Each one picks its card from how full
        # the cards are, so the picking is done one at a time and a pick counts from the
        # moment it is made, not from the moment its container is up: three Androids
        # started together must not all see the same empty card.
        self._placing = threading.Lock()
        self._placed: dict[int, str] = {}          # Android id -> render node, while its start is under way
        self._activity: dict[int, str] = {}
        self._booted: set[int] = set()
        self._booting = _Booting()
        from .backup import Backups          # backup.py imports this module
        self.backups = Backups(self)

    def reload_config(self, config: Config) -> None:
        """Swap in a new config. Running Androids are untouched; changes reach an Android at
        its next start (which rebuilds its containers if its plan changed)."""
        self.config = config
        self.images.config = config

    # ------------------------------------------------------------------ helpers
    @property
    def prefix(self) -> str:
        return self.config.docker.name_prefix

    def names(self, id: int) -> Names:
        return Names(prefix=self.prefix, id=check_id(id))

    def volume_of(self, spec: InstanceSpec) -> str:
        """The Docker volume holding this Android's /data. Checked every time it is used:
        the name goes into `-v <name>:/data`, where anything with a slash would be read by
        Docker as a host folder to mount."""
        volume = spec.data_volume or self.names(spec.id).volume
        if not _VOLUME.match(volume):
            raise InstanceError(f"Android {spec.id}: data_volume {volume!r} is not a Docker volume name")
        return volume

    def _spec(self, id: int) -> InstanceSpec:
        try:
            return self.store.get(check_id(id))
        except KeyError:
            raise KeyError(f"there is no Android {id}") from None

    @contextmanager
    def locked(self, id: int, what: str, *, wait: float | None = None) -> Iterator[None]:
        """Hold one Android for an operation. A second operation waits `wait` seconds, then
        fails with InstanceBusy rather than queueing behind a start that may take minutes.
        The file lock covers a CLI process working next to the daemon."""
        id = check_id(id)
        wait = self.lock_wait if wait is None else wait
        with self._guard:
            lock = self._locks.setdefault(id, threading.Lock())
        if not lock.acquire(timeout=max(0.0, wait)):
            raise InstanceBusy(f"Android {id} is busy ({self._activity.get(id) or 'another operation'}); "
                               "wait for it to finish or cancel its task")
        handle = None
        try:
            folder = self.paths.instance(id)
            folder.mkdir(parents=True, exist_ok=True)
            handle = open(folder / ".lock", "w")           # noqa: SIM115 - held across the yield
            deadline = time.monotonic() + max(0.0, wait)
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise InstanceBusy(f"Android {id} is being worked on by another farm process; "
                                           "try again when it has finished") from None
                    time.sleep(0.1)
            self._activity[id] = what
            yield
        finally:
            self._activity.pop(id, None)
            if handle is not None:
                handle.close()                              # closing drops the flock
            lock.release()

    def _container_owner(self, name: str) -> str | None:
        """The farm label of a container, "" for one without it, None when there is none."""
        r = self.docker.run(["container", "inspect", "--format", f'{{{{index .Config.Labels "{LABEL_FARM}"}}}}', name],
                            timeout=30, check=False)
        if r.ok:
            return r.out
        if "no such" in r.stderr.lower():
            return None
        # "Docker did not answer" must never be read as "there is no container": a delete
        # would then drop the settings of an Android whose containers still exist.
        raise InstanceError(f"Docker did not answer ({r.stderr.strip().splitlines()[-1][:200] if r.stderr.strip() else 'no output'}). "
                            "Is the daemon running, and is this user in the `docker` group?")

    def _ours(self, name: str) -> bool:
        """True when the container exists and is ours; False when it does not exist.
        A container with our name that this farm did not create is never touched."""
        owner = self._container_owner(name)
        if owner is None:
            return False
        if owner != self.prefix:
            raise InstanceError(
                f"a container named {name} exists that this farm did not create; it is left alone. Remove or "
                f"rename it yourself, or give this farm another docker.name_prefix")
        return True

    def _farm_containers(self) -> list[dict]:
        return self.docker.ps(all=True, labels={LABEL_FARM: self.prefix})

    def _adb_host(self) -> str:
        bind = self.config.network.adb_bind
        if bind in ("", "0.0.0.0", "::"):      # noqa: S104 - published on all interfaces, reached locally
            return "127.0.0.1"
        return f"[{bind}]" if ":" in bind else bind

    def adb(self, id: int) -> Adb:
        check_id(id)
        port = self.config.network.adb_port_base + int(id)
        return self.backends.adb(f"{self._adb_host()}:{port}")

    def resolved(self, id: int) -> Resolved:
        return self._resolve(self._spec(id))

    def _resolve(self, spec: InstanceSpec) -> Resolved:
        r = resolve(spec, self.config)
        if not 1024 <= r.adb_port <= 65535:
            raise ValueError(f"Android {spec.id}: ADB port {r.adb_port} is out of range; lower network.adb_port_base")
        update: dict[str, Any] = {"device": device_profile.effective_identity(r.device, self.config)}
        if not r.cpuset and self.config.resources.cpu_pinning != "none":
            remembered = self.store.state_get(spec.id, "cpuset") or {}
            if remembered.get("policy") == self.config.resources.cpu_pinning:
                update["cpuset"] = str(remembered.get("cpus", ""))
        return r.model_copy(update=update)

    def select(self, selector: str) -> list[int]:
        return parse_selector(selector, self.store.list(), running=self._running_ids())

    def _running_ids(self) -> set[int]:
        out: set[int] = set()
        for c in self.docker.ps(all=False, labels={LABEL_FARM: self.prefix, LABEL_ROLE: "android"}):
            if c["state"] == "running" and c["labels"].get(LABEL_INSTANCE, "").isdigit():
                out.add(int(c["labels"][LABEL_INSTANCE]))
        return out

    # ----------------------------------------------------------------- capacity
    def capacity(self) -> dict:
        """How full this machine is. Advisory: by default the farm only warns.

            estimate             host.capacity.estimate (max by RAM / CPU / VRAM, which one binds)
            running              Androids running now
            headroom             how many more fit before the estimate is reached (never negative)
            over                 True when starting one more now would go past the estimate or
                                 leave less than performance.memory_guard_mb of RAM available
            reasons              why `over` is True, in words
            memory_available_mb  MemAvailable right now (-1 when it cannot be read)
            enforced             performance.enforce_capacity: is a start over capacity refused?
            advice               what would raise the limit, most effective first
        """
        return self._capacity(len(self._running_ids()))

    def _capacity(self, running: int, *, promised_mb: int = 0) -> dict:
        perf = self.config.performance
        try:
            estimate = dict(self.backends.capacity_estimate(self.config, self.paths, running) or {})
        except Exception as e:  # noqa: BLE001 - advisory figure: a broken probe must not stop a start
            log.warning("capacity estimate failed: %s", e)
            estimate = {"error": f"{type(e).__name__}: {e}"[:300]}
        limit = estimate.get("max_instances")
        available = self.backends.mem_available_mb()
        reasons: list[str] = []
        if isinstance(limit, int) and running >= limit:
            reasons.append(f"{running} Android(s) are running and this machine is estimated to hold about {limit} "
                           f"(limited by {estimate.get('binding') or 'its resources'})")
        if perf.memory_guard_mb > 0 and available >= 0 and available - promised_mb < perf.memory_guard_mb:
            booting = f", {promised_mb} MB of it promised to Androids that are booting" if promised_mb else ""
            reasons.append(f"{available} MB of RAM is available{booting}; below performance.memory_guard_mb "
                           f"({perf.memory_guard_mb} MB)")
        return {"estimate": estimate, "running": running,
                "headroom": max(0, limit - running) if isinstance(limit, int) else 0,
                "over": bool(reasons), "reasons": reasons, "memory_available_mb": available,
                "enforced": perf.enforce_capacity, "advice": list(estimate.get("advice") or [])}

    def _check_capacity(self, id: int, need_mb: int, force: bool) -> list[str]:
        """Called before a stopped Android is started. Returns warnings; raises OverCapacity
        only when the operator asked for the estimate to be enforced and did not force."""
        booting, promised = self._booting.reserve(id, need_mb)
        try:
            capacity = self._capacity(len((self._running_ids() | booting) - {id}), promised_mb=promised)
        except BaseException:
            self._booting.release(id)
            raise
        if not capacity["over"]:
            return []
        why = "; ".join(capacity["reasons"])
        if capacity["enforced"] and not force:
            self._booting.release(id)
            raise OverCapacity(
                f"not starting Android {id}: over capacity ({why}). Stop some Androids, or start it anyway with "
                "force (`sf start --force`); set performance.enforce_capacity to false to only be warned", capacity)
        return [f"over capacity: {why}. Started anyway "
                + ("because the start was forced" if capacity["enforced"] else "(performance.enforce_capacity is off)")]

    # ------------------------------------------------------------- create / edit
    def create(self, *, count: int = 1, name: str = "", template: dict | None = None) -> list[InstanceSpec]:
        """Add `count` Androids. Nothing is started and no container is made yet.

        `template` holds InstanceSpec fields shared by all of them (android_version, tags,
        network, ...). Two extras: `data_volume` adopts an existing Docker volume as /data
        (count must be 1; the volume's own identity is kept), and `use_profile_screen: true`
        gives each Android the screen shape of its device preset."""
        with self._creating:        # or two requests at once pick the same free ids and one fails half way through
            return self._create(count, name, template)

    def _create(self, count: int, name: str, template: dict | None) -> list[InstanceSpec]:
        count = int(count)
        if not 1 <= count <= 500:
            raise ValueError("count must be between 1 and 500")
        template = dict(template or {})
        template.pop("id", None)
        template.pop("created", None)
        use_screen = bool(template.pop("use_profile_screen", False))
        device_in = dict(template.pop("device", None) or {})
        adopt = template.get("data_volume") or None
        if adopt:
            if count != 1:
                raise ValueError("one data volume can belong to one Android only: adopt with count=1")
            self._check_adoptable(adopt, None)
        taken = self._volume_names() if not adopt else set()
        specs: list[InstanceSpec] = []
        next_from = 1
        for n in range(count):
            new_id = self.store.next_id(next_from)
            # An id whose default volume still exists (kept at an earlier delete) is skipped:
            # a "new" Android must never silently come up with someone else's old data.
            while not adopt and Names(prefix=self.prefix, id=new_id).volume in taken:
                new_id = self.store.next_id(new_id + 1)
            next_from = new_id + 1
            if adopt and "profile" not in device_in:
                # Keep what the adopted Android already is: no new model, no new android_id.
                identity = DeviceIdentity(profile="none")
            else:
                identity = device_profile.new_identity(self.config, device_in.get("profile"))
            identity = DeviceIdentity.model_validate({
                **identity.model_dump(), **device_in, "profile": identity.profile,
                "props": {**identity.props, **(device_in.get("props") or {})}})
            device_profile.validate_identity(identity)
            data = {**template, "id": new_id, "device": identity.model_dump()}
            if name:
                data["name"] = name if count == 1 else f"{name} {n + 1}"
            if use_screen:
                screen = device_profile.profile_screen(identity.profile, width=self.config.android.width)
                for key, value in (screen or {}).items():
                    data.setdefault(key, value)
            spec = InstanceSpec.model_validate(data)
            self._resolve(spec)                       # reject what could never start (unknown image, gateway)
            with self.locked(new_id, "creating"):
                if self.store.exists(new_id):
                    raise InstanceBusy(f"Android {new_id} was created by someone else just now; try again")
                self.store.save(spec)
            specs.append(spec)
        return specs

    def _volume_names(self) -> set[str]:
        try:
            return set(self.docker.volumes(f"{self.prefix}-data-"))
        except CommandError:
            return set()           # Docker down: creating a spec must still work

    def _check_adoptable(self, volume: str, own_id: int | None) -> None:
        if not _VOLUME.match(str(volume)):
            raise ValueError(f"bad data volume name {volume!r}: expected the name of a Docker volume, not a path")
        for other_id, other in self.store.list().items():
            if other_id != own_id and (other.data_volume or Names(prefix=self.prefix, id=other_id).volume) == volume:
                raise ValueError(f"volume {volume} already belongs to Android {other_id}")
        if not self.docker.volume_exists(volume):
            raise ValueError(f"Docker volume {volume} does not exist; `docker volume ls` shows the ones that do")

    def _has_data(self, spec: InstanceSpec) -> bool:
        if self.store.state_get(spec.id, "data_android_version"):
            return True
        try:
            return self.docker.volume_exists(self.volume_of(spec))
        except (CommandError, InstanceError):
            return False        # Docker down: what the farm recorded above is all there is to go on

    def update(self, id: int, patch: dict, *, force: bool = False) -> InstanceSpec:
        """Change saved settings of one Android. Validated now, applied at its next (re)start.

        `patch` has the shape of an InstanceSpec and holds only what changes:

            a mapping   is merged key by key: `resources`, `network`, `device`, and the
                        free-form maps `extra_props` and `device.props`
            a list      replaces the saved list whole (`tags`, `extra_boot_args`); [] empties it
            null        removes: in a free-form map that one entry
                        (`{"extra_props": {"debug.x": null}}`), anywhere else the override,
                        so the setting follows the farm default again (`{"fps": null}`) or is
                        empty (`{"tags": null}`, `{"extra_props": null}`)

        `id` and `created` are facts, not settings: another id is refused and `created` in a
        patch is ignored. The device identity as a whole cannot be removed (its fields can
        be changed).

        Changing the Android version of an instance that already has data is refused unless
        `force`: an Android cannot boot /data written by a newer release, and an upgrade
        rewrites /data in a way the older release cannot read again. Setting it back to the
        version that wrote the data is always allowed."""
        # Read, merge and save in one hold of the Android's lock: two edits at the same
        # moment both land, instead of the second writing back what it read before the first.
        with self.locked(self._spec(id).id, "updating"):
            return self._update(id, patch, force)

    def _update(self, id: int, patch: dict, force: bool) -> InstanceSpec:
        spec = self._spec(id)
        if not isinstance(patch, dict):
            raise ValueError("patch must be a mapping of fields to new values")
        if "id" in patch and (patch["id"] is None or int(patch["id"]) != spec.id):
            raise ValueError("an Android's id cannot be changed; clone it instead")
        if "device" in patch and patch["device"] is None:
            raise ValueError("an Android's device identity cannot be removed; change its fields, or set "
                             "device.profile to give it another model")
        merged = _apply_patch(spec.model_dump(mode="json"), {k: v for k, v in patch.items() if k not in ("id", "created")})
        new = InstanceSpec.model_validate(merged)
        device_patch = patch.get("device") if isinstance(patch.get("device"), dict) else {}
        if new.device.profile != spec.device.profile and "props" not in device_patch:
            # Asking for another device model means its properties, not just its label.
            fresh = device_profile.new_identity(self.config, new.device.profile)
            new.device = new.device.model_copy(update={
                "profile": fresh.profile, "props": fresh.props,
                "serial": device_patch.get("serial", fresh.serial)})
        if new.data_volume and new.data_volume != spec.data_volume:
            self._check_adoptable(new.data_volume, spec.id)
        device_profile.validate_identity(new.device)
        wanted = self._resolve(new).android_version
        current = spec.android_version or self.config.android.default_version
        # The release that wrote the data when the farm knows it; for data it never booted
        # (an adopted or restored volume) the version the Android is set to now.
        written = str(self.store.state_get(spec.id, "data_android_version") or current)
        if wanted not in (current, written) and not force and self._has_data(spec):
            raise NeedsForce(
                f"Android {spec.id} already has data, written by Android {written}; its version is not changed to "
                f"{wanted}. An Android cannot boot data written by a newer release, and an upgrade cannot be undone. "
                f"To go ahead anyway repeat the change with force (a backup is taken before the first start on the "
                f"new version when backup.auto_before_risky is on), or create a new Android with version {wanted}")
        self.store.save(new)
        return new

    def clone(self, id: int, *, with_data: bool) -> InstanceSpec:
        """A new Android with the same settings. It always gets a new identity: a copy that
        reports the same device as its source, online at the same time, is how accounts get
        linked or flagged. With `with_data` the source's /data is copied too (source stopped)."""
        source = self._spec(id)
        with self.locked(source.id, "cloning"):
            if with_data and self._ours(self.names(source.id).android) and self.docker.running(self.names(source.id).android):
                raise InstanceError(f"Android {source.id} is running; stop it before cloning its data "
                                    "(a copy of a live /data is not consistent)")
            template = source.model_dump(mode="json", exclude={"id", "created", "device", "data_volume", "autostart"})
            template["name"] = f"{source.display_name} (copy)"[:60]
            template["device"] = {"profile": source.device.profile, "locale": source.device.locale,
                                  "timezone": source.device.timezone, "geo": source.device.geo}
            new = self.create(template=template)[0]
            if not with_data:
                return new
            try:
                with self.locked(new.id, "cloning"):
                    src_volume = self.volume_of(source)
                    if not self.docker.volume_exists(src_volume):
                        raise InstanceError(f"Android {source.id} has no data yet (volume {src_volume} does not exist)")
                    dst = self.names(new.id)
                    self.docker.volume_create(dst.volume, dst.labels("data"))
                    self._helper_run(new.id, ["sh", "-c", _COPY_SCRIPT], gnu_tar=True, timeout=7200,
                                     mounts=[f"{src_volume}:/from:ro", f"{dst.volume}:/to"])
            except BaseException:
                self._discard_new(new.id)
                raise
            return new

    def _discard_new(self, id: int) -> None:
        names = self.names(id)
        if self._volume_is_ours(names.volume, id):
            self.docker.volume_rm(names.volume)
        self.store.delete(id)

    # ------------------------------------------------------------------- delete
    def delete(self, id: int, *, keep_data: bool = False, remove_adopted: bool = False, force: bool = False) -> dict:
        """Remove an Android: its containers, its settings and, unless `keep_data`, its data.

        Returns {"id", "data": removed | kept | none, "volume", "backup", "warnings"}.

        With backup.auto_before_risky (the default) the data is backed up before it is
        removed, and `backup` is where. If that backup cannot be made the Android is left
        as it was, only stopped, and NeedsForce is raised; `force=True` deletes regardless.

        A volume the farm did not create (adopted through data_volume, or simply not carrying
        the farm's label) is never deleted here; the Android is only detached from it.
        `remove_adopted=True` is the explicit request that lifts this for an adopted volume."""
        spec = self._spec(id)
        names = self.names(spec.id)
        with self.locked(spec.id, "deleting"):
            spec = self._spec(id)       # again: another delete may have finished while this one waited for the lock
            volume = self.volume_of(spec)
            adopted = bool(spec.data_volume)
            out: dict[str, Any] = {"id": spec.id, "data": "kept", "volume": volume, "backup": "", "warnings": []}
            remove = False
            if keep_data:
                log.info("Android %s deleted, data volume %s kept", spec.id, volume)
            elif not self.docker.volume_exists(volume):
                out["data"] = "none"
            elif adopted and not remove_adopted:
                out["warnings"].append(f"adopted volume {volume} was not created by this farm and is left in place")
            elif not adopted and not self._volume_is_ours(volume, spec.id):
                out["warnings"].append(f"volume {volume} does not carry this farm's label and is left in place")
            else:
                remove = True
            if remove:
                # Stopped, not yet removed: if the backup fails, nothing has been lost.
                self._stop(spec.id)
                out["backup"], note = self.backups.safety(spec, "before-delete", force=force)
                if note:
                    out["warnings"].append(note)
            self._remove_containers(names)
            if remove:
                if not self.docker.volume_rm(volume):
                    raise InstanceError(f"volume {volume} could not be removed (is another container using it?); "
                                        f"Android {spec.id} was not deleted. Check `docker ps -a --filter volume={volume}`")
                out["data"] = "removed"
            for w in out["warnings"]:
                log.warning("Android %s deleted; %s", spec.id, w)
            self._booted.discard(spec.id)
            self.store.delete(spec.id)
        return out

    def _volume_is_ours(self, volume: str, id: int) -> bool:
        r = self.docker.run(["volume", "inspect", "--format",
                             f'{{{{index .Labels "{LABEL_FARM}"}}}}/{{{{index .Labels "{LABEL_INSTANCE}"}}}}', volume],
                            timeout=30, check=False)
        return r.ok and r.out == f"{self.prefix}/{int(id)}"

    def _remove_containers(self, names: Names) -> None:
        """Android and its network container go together, Android first (it lives in the
        other's namespace)."""
        mine = [n for n in (names.android, names.net) if self._ours(n)]
        if mine:
            self.docker.rm(*mine)
        left = [n for n in mine if self.docker.exists(n)]
        if left:
            raise InstanceError(f"could not remove container(s) {', '.join(left)}; is the Docker daemon healthy?")
        self._booted.discard(names.id)
        self._adb_disconnect(names.id)

    def _adb_disconnect(self, id: int) -> None:
        try:
            self.adb(id).disconnect()       # or the host's adb server keeps a dead entry for this port
        except (CommandError, AdbError, OSError):
            pass

    # --------------------------------------------------------------- start/stop
    def start(self, id: int, task: Task | None = None, *, force: bool = False) -> dict:
        """Bring an Android up and wait until it has booted.

        `force` is the operator's "do it anyway" for the two refusals a start can make:
        over capacity while performance.enforce_capacity is on (OverCapacity), and booting
        data written by a newer Android version (NeedsForce). It does not rebuild
        containers; that is recreate()."""
        spec = self._spec(id)
        with self.locked(spec.id, "starting"):
            return self._start(spec.id, task, recreate=False, force=force)

    def stop(self, id: int) -> None:
        spec = self._spec(id)
        with self.locked(spec.id, "stopping"):
            self._stop(spec.id)

    def restart(self, id: int, task: Task | None = None, *, force: bool = False) -> dict:
        """Stop and start an Android, applying settings changed since it was created (the
        containers are rebuilt when their plan changed, reused otherwise).

        Nothing is stopped until every refusal a start can make has had its say: a restart
        that is refused (over capacity, a version downgrade, a GPU mode this machine does
        not have) leaves a running Android running. `force` as for start()."""
        spec = self._spec(id)
        with self.locked(spec.id, "restarting"):
            return self._start(spec.id, task, recreate=False, force=force, restart=True)

    def recreate(self, id: int, task: Task | None = None, *, force: bool = False) -> dict:
        """Rebuild both containers even if the plan is unchanged. /data is kept. As with
        restart(), a refusal comes before anything is stopped. `force` as for start()."""
        spec = self._spec(id)
        with self.locked(spec.id, "recreating"):
            return self._start(spec.id, task, recreate=True, force=force)

    def _stop(self, id: int) -> None:
        names = self.names(id)
        # Android first: it shuts down cleanly while its network still exists.
        if self._ours(names.android):
            self.docker.stop(names.android, grace=self.config.docker.stop_timeout)
            if self.docker.running(names.android):
                raise InstanceError(f"{names.android} did not stop; check `docker ps` and the Docker daemon")
        if self._ours(names.net):
            self.docker.stop(names.net, grace=NET_STOP_GRACE)
        self._booted.discard(id)
        self._adb_disconnect(id)

    def _start(self, id: int, task: Task | None, *, recreate: bool, force: bool = False, restart: bool = False,
               replacing: bool = False) -> dict:
        """The one way an Android comes up. Three separate questions, three flags:

            recreate   rebuild the containers even when their plan is unchanged
            restart    an Android that is already up is stopped and started, not left alone
            force      go ahead past a refusal: over capacity, data from a newer Android version

        `replacing` says the caller itself stopped this Android a moment ago (a backup
        taking a consistent copy): bringing it back adds nothing to the machine, so capacity
        is not asked."""
        spec = self._spec(id)       # outside the try: an Android deleted while this start waited for its lock
        try:                        # has no state left, and none must be written for whoever gets the id next
            result = self._start_inner(spec, task, recreate=recreate, force=force, restart=restart,
                                       replacing=replacing)
        except Cancelled:
            raise
        except Exception as e:
            self._booted.discard(id)
            self.store.state_set(id, "last_error", f"{type(e).__name__}: {e}"[:500])
            raise
        self.store.state_set(id, "last_error", None)
        self.store.state_set(id, "warnings", [w[:400] for w in result["warnings"]] or None)
        return result

    def _start_inner(self, spec: InstanceSpec, task: Task | None, *, recreate: bool, force: bool,
                     restart: bool = False, replacing: bool = False) -> dict:
        id, began = spec.id, time.monotonic()
        r = self._resolve(spec)
        names = r.names
        android_running = self._ours(names.android) and self.docker.running(names.android)
        net_exists = self._ours(names.net)
        warnings: list[str] = []

        if not recreate and not restart and android_running and net_exists and self.docker.running(names.net):
            # Already up: a start never rebuilds a running Android behind the operator's back,
            # even if settings changed since it was created. `restart` applies those.
            _step(task, f"Android {id}: already running")
            adb = self._wait_boot(r, task)
            self._booted.add(id)
            warnings += self._post_boot(spec, r, adb)
            return {"id": id, "state": "booted", "recreated": False, "already_running": True,
                    "gpu": self.docker.label(names.android, LABEL_GPU), "gpu_node": self.docker.label(names.android, LABEL_GPU_NODE),
                    "serial": adb.serial, "boot_seconds": round(time.monotonic() - began, 1), "warnings": warnings,
                    "backup": ""}

        # Nothing is taken down between here and the "containers change" mark below, and
        # every refusal (capacity, data version, image, GPU, network) is raised in that
        # stretch: a refused restart or recreate leaves a running Android as it was.
        # An Android that is running is already counted: restarting or rebuilding it in place
        # adds nothing to the machine and is not a capacity question.
        reserved = False
        if not android_running and not replacing:
            _step(task, f"Android {id}: checking capacity")
            warnings += self._check_capacity(id, min(r.memory_mb, ANDROID_FOOTPRINT_MB), force)
            reserved = True
        try:
            notes, backup_first = self._check_data_version(spec, r, force)
            warnings += notes
            _step(task, f"Android {id}: preparing images")
            try:
                image = (self.images.ensure_ref(spec.image, task=task) if spec.image
                         else self.images.ensure(r.android_version, task=task))
            except ImageError as e:
                raise StartError(f"Android {id}: {e}") from None

            with self._placing:
                containers = self._farm_containers()
                r = r.model_copy(update={"image": image, "cpuset": self._choose_cpuset(spec, r, containers)})
                gpu, notes = self._choose_gpu(r, _gpu_counts(containers, id, self._placed))
                self._placed[id] = gpu.node
            warnings += notes
            try:
                net_args = [str(a) for a in self.backends.sidecar_run_args(r, self.config, self.paths, self.store, self.docker)]
            except (ValueError, RuntimeError) as e:
                raise StartError(f"Android {id}: network ({r.net_mode}): {e}") from None
            extra = self._extra_run_args(r)
            digest = plan_hash(r, gpu, net_args, extra_run_args=extra, image_id=self.docker.image_id(image))

            same = (not recreate and net_exists
                    and self.docker.label(names.android, LABEL_PLAN) == digest
                    and self.docker.label(names.net, LABEL_PLAN) == digest)
            fresh_volume = self._ensure_volume(spec, r)
            # ---- containers change from here on
            safety_backup = ""
            if backup_first:
                # The one refusal that can come after a stop: the copy has to be consistent,
                # so the Android is down before it is known whether the backup succeeds.
                _step(task, f"Android {id}: backing up before the version change")
                self._stop(id)
                safety_backup, note = self.backups.safety(spec, "before-version-change", force=force, task=task)
                warnings += [note] if note else []
            if same:
                # Same containers, but not both up. Bring the pair down and up in order
                # instead of starting the missing half into whatever state the other is in.
                _step(task, f"Android {id}: starting network")
                self._stop(id)
                warnings += self._check_renderer(r, gpu.mode, fresh_volume)
                self.docker.start(names.net)
                self._wait_net(r)
                _step(task, f"Android {id}: starting Android")
                self.docker.start(names.android)
            else:
                _step(task, f"Android {id}: creating containers")
                self._remove_containers(names)
                warnings += self._check_renderer(r, gpu.mode, fresh_volume)
                self.docker.create_and_start(["--label", f"{LABEL_PLAN}={digest}", *net_args])
                self._wait_net(r)
                _step(task, f"Android {id}: starting Android ({gpu.mode})")
                self.docker.create_and_start(plan_android(r, gpu=gpu, extra_run_args=extra, plan_hash=digest))
            # kind and label are the host module's words for the panel ("igpu", "Intel UHD ... (integrated, Mesa)").
            self.store.state_set(id, "gpu", {"mode": gpu.mode, "node": gpu.node, "kind": str(getattr(gpu, "kind", "")),
                                             "label": str(getattr(gpu, "label", ""))})
            _step(task, f"Android {id}: waiting for boot")
            adb = self._wait_boot(r, task)
        finally:
            with self._placing:
                self._placed.pop(id, None)
            if reserved:
                self._booting.release(id)

        self._booted.add(id)
        boot_seconds = round(time.monotonic() - began, 1)
        if self.store.state_get(id, "data_android_version") != r.android_version:
            # The release that last booted this /data: what a later version change is judged against.
            self.store.state_set(id, "data_android_version", r.android_version)
        _step(task, f"Android {id}: applying settings")
        warnings += self._post_boot(spec, r, adb)
        self.store.state_set(id, "last_start", time.time())
        for w in warnings:
            log.warning("Android %s: %s", id, w)
        return {"id": id, "state": "booted", "recreated": not same, "already_running": False, "gpu": gpu.mode,
                "gpu_node": gpu.node, "gpu_label": str(getattr(gpu, "label", "")),
                "gpu_reason": str(getattr(gpu, "reason", "")), "serial": adb.serial,
                "boot_seconds": boot_seconds, "warnings": warnings, "backup": safety_backup}

    def _check_data_version(self, spec: InstanceSpec, r: Resolved, force: bool) -> tuple[list[str], bool]:
        """Compare the Android version about to boot with the one that last booted this /data.

        Newer than the data: goes ahead with a warning (Android migrates /data forward, and
        there is no way back). Older than the data: NeedsForce unless forced, because that
        Android cannot read it. Touches nothing. Returns (warnings, whether the data is to
        be backed up before it boots: a real version change with backup.auto_before_risky on)."""
        written = str(self.store.state_get(spec.id, "data_android_version") or "")
        if not written or written == r.android_version or not self.docker.volume_exists(self.volume_of(spec)):
            return [], False
        order = version_order(r.android_version, written)
        if order == -1 and not force:
            raise NeedsForce(
                f"Android {spec.id}: its data was written by Android {written} and it is now set to Android "
                f"{r.android_version}. An older Android cannot read data from a newer one and usually does not "
                f"boot with it. Set the version back to {written}, restore a backup made on Android "
                f"{r.android_version}, or start with force to try anyway")
        if order == 1:
            notes = [f"data was written by Android {written} and now boots on Android {r.android_version}: "
                     "Android upgrades it in place and it cannot go back to the older version"]
        elif order == -1:
            notes = [f"forced: booting data written by Android {written} on the older Android {r.android_version}"]
        else:
            notes = [f"data was last booted with Android image version {written!r}, now {r.android_version!r}"]
        # The same release under another name (order 0) is not a version change.
        return notes, order != 0 and self.config.backup.auto_before_risky

    def _choose_gpu(self, r: Resolved, counts: dict[str, int]) -> tuple[Any, list[str]]:
        """Ask the host module how this Android draws. The NVIDIA runtime exists for one
        Android version only: on any other, an explicit request is refused with the reason
        and an automatic pick falls back to the next best mode, instead of an Android that
        dies at boot."""
        def choose(resolved: Resolved):
            try:
                return self.backends.gpu_choose(resolved, self.config, self.paths, counts=counts)
            except ValueError as e:
                raise StartError(f"Android {r.id}: {e}") from None

        # Only "is it built for this Android release" is asked here (missing=[]); whether the
        # runtime files are present is the host module's question, answered by choose().
        blocked = self.images.feature(r.android_version, "nvidia", missing=[])["unsupported"]
        if blocked and r.gpu_mode == "nvidia":
            # Before the host module is asked: choosing NVIDIA starts the relay.
            raise StartError(f"Android {r.id}: gpu mode 'nvidia' is {blocked}. Use Android "
                             f"{'/'.join(VERSION_BOUND['nvidia'])} for this instance, or another gpu_mode")
        gpu = choose(r)
        if not blocked or gpu.mode != "nvidia":
            return gpu, []
        for fallback in ("igpu", "software"):
            try:
                gpu = self.backends.gpu_choose(r.model_copy(update={"gpu_mode": fallback, "gpu_node": ""}),
                                               self.config, self.paths, counts=counts)
            except ValueError:
                continue
            if gpu.mode != "nvidia":
                return gpu, [f"NVIDIA rendering is {blocked}; using {gpu.mode} instead"]
        raise StartError(f"Android {r.id}: NVIDIA rendering is {blocked} and no other mode is available")

    def _extra_run_args(self, r: Resolved) -> list[str]:
        out: list[str] = []
        for provider in self.extra_run_args_providers:
            try:
                out += [str(a) for a in provider(r)]
            except Exception as e:  # noqa: BLE001 - foreign code; say whose arguments are missing
                name = getattr(provider, "__qualname__", repr(provider))
                raise StartError(f"Android {r.id}: {name} could not provide its container arguments: {e}") from e
        return out

    def _choose_cpuset(self, spec: InstanceSpec, r: Resolved, containers: list[dict]) -> str:
        """An explicit cpuset wins. Otherwise the pinning chosen at an earlier start is kept
        while it still fits: a fresh draw at every start would change the plan and rebuild
        the containers each time."""
        policy = self.config.resources.cpu_pinning
        if spec.resources.cpuset:
            return spec.resources.cpuset
        if policy == "none":
            if self.store.state_get(spec.id, "cpuset"):
                self.store.state_set(spec.id, "cpuset", None)
            return ""
        topology = self.backends.cpu_topology()
        if not topology.cores:
            return ""
        remembered = self.store.state_get(spec.id, "cpuset") or {}
        if (remembered.get("policy") == policy and remembered.get("for") == r.cpus
                and cpuset_fits(str(remembered.get("cpus", "")), topology, policy)):
            return str(remembered["cpus"])
        used: dict[int, int] = {}
        for c in containers:
            labels = c["labels"]
            if (c["state"] != "running" or labels.get(LABEL_ROLE) != "android"
                    or labels.get(LABEL_INSTANCE) == str(spec.id)):
                continue
            try:
                for cpu in parse_cpu_list(cpuset_from_label(labels.get(LABEL_CPUSET, ""))):
                    used[cpu] = used.get(cpu, 0) + 1
            except ValueError:
                continue
        cpus = allocate_cpuset(topology, policy, r.cpus, used)
        self.store.state_set(spec.id, "cpuset", {"policy": policy, "cpus": cpus, "for": r.cpus})
        return cpus

    def _ensure_volume(self, spec: InstanceSpec, r: Resolved) -> bool:
        """Make sure /data has a volume. Returns True when it was created just now."""
        if self.docker.volume_exists(self.volume_of(spec)):
            return False
        if spec.data_volume:
            raise StartError(f"Android {spec.id} uses the adopted volume {spec.data_volume}, which no longer exists. "
                             "Restore it, or clear data_volume to give this Android a fresh /data")
        self.docker.volume_create(r.data_volume, r.names.labels("data"))
        return True

    def _wait_net(self, r: Resolved) -> None:
        ready = self.backends.net_wait_ready(self.docker, r, NET_READY_TIMEOUT) or {}
        if not ready.get("ready"):
            # Android is deliberately not started: without its network it would boot offline
            # at best. The network container is left up so its state and log can be read.
            raise StartError(f"Android {r.id}: network ({r.net_mode}) is not ready: "
                             f"{ready.get('state') or 'unknown'} {ready.get('detail') or ''}".rstrip()
                             + f". See `docker logs {r.names.net}`; for a VPN check the profile and credentials")

    def _wait_boot(self, r: Resolved, task: Task | None) -> Adb:
        timeout = self.config.performance.boot_timeout
        adb = self.adb(r.id)
        deadline = time.monotonic() + timeout
        while True:
            if task is not None:
                task.check_cancel()
            if not self.docker.running(r.names.android):
                tail = " | ".join(self.docker.logs(r.names.android, tail=8).strip().splitlines()[-8:])
                raise StartError(f"Android {r.id} stopped while booting. Last output: {tail[:600] or 'none'}. "
                                 "The usual causes are a missing binder module (run `sf doctor`) or a GPU mode "
                                 "this machine cannot provide")
            try:
                if adb.connect(attempts=1) and adb.booted():
                    return adb
            except CommandError as e:
                if e.code == 127:
                    raise StartError(f"Android {r.id} was started but cannot be reached: the `adb` program is not "
                                     "installed. Install your distribution's android-tools / adb package "
                                     "(`sf doctor` lists what is missing)") from None
                raise
            if time.monotonic() >= deadline:
                raise StartError(f"Android {r.id} did not finish booting within {timeout} s. It is left running; "
                                 f"see `docker logs {r.names.android}`. On a loaded machine raise "
                                 "performance.boot_timeout or lower performance.start_concurrency")
            self.backends.sleep(2.0)

    # ---------------------------------------------------------- shader caches
    def _helper_run(self, id: int, command: list[str], *, mounts: list[str], gnu_tar: bool = False,
                    timeout: float = 120) -> str:
        """Run a short-lived helper container on data volumes. No network, bounded memory,
        removed afterwards even when it timed out."""
        image = self.images.helper(gnu_tar=gnu_tar)
        name = f"{self.prefix}-helper-{int(id)}-{secrets.token_hex(3)}"
        args = ["run", "--rm", "--name", name, "--network", "none", "--memory", "512m"]
        for key, value in self.names(id).labels("helper").items():
            args += ["--label", f"{key}={value}"]
        for mount in mounts:
            args += ["-v", mount]
        try:
            return self.docker.run([*args, image, *command], timeout=timeout).out
        except CommandError:
            self.docker.rm(name)
            raise

    def _check_renderer(self, r: Resolved, mode: str, fresh_volume: bool) -> list[str]:
        """Apps keep compiled shaders per renderer; started on another renderer they crash
        while drawing. So when the renderer of an Android changes, those caches are deleted
        (Android rebuilds them). The last renderer is remembered in the farm's state and in
        /data itself, so an adopted or restored volume is handled too."""
        if not self.config.gpu.clear_shader_cache_on_change or self.store.state_get(r.id, "renderer") == mode:
            return []
        if not fresh_volume:
            try:
                outcome = self._helper_run(r.id, ["sh", "-c", _RENDERER_SCRIPT, "sh", mode],
                                           mounts=[f"{r.data_volume}:/data"])
            except (CommandError, ImageError) as e:
                return [f"could not check the shader caches after a renderer change ({e}); if apps crash at "
                        "start, clear their data or run the start again"]
            if outcome.endswith("cleared"):
                log.info("Android %s: renderer is now %s, shader caches cleared", r.id, mode)
        self.store.state_set(r.id, "renderer", mode)
        return []

    # ---------------------------------------------------------------- post-boot
    def _post_boot(self, spec: InstanceSpec, r: Resolved, adb: Adb) -> list[str]:
        """Settings that live inside the running Android. Every item is independent: one
        that fails becomes a warning on the start result, never a failed start."""
        warnings: list[str] = []

        def attempt(what: str, fn: Callable[[], Any]) -> None:
            try:
                extra = fn()
                if isinstance(extra, list):
                    warnings.extend(str(w) for w in extra)
            except Exception as e:  # noqa: BLE001 - includes hooks written elsewhere
                warnings.append(f"{what}: {type(e).__name__}: {e}"[:400])

        def identity() -> list[str]:
            def root(argv: Sequence[str]) -> str:
                return self.docker.exec(r.names.android, list(argv), timeout=20).out
            return device_profile.apply_post_boot(adb, r, root_exec=root)["warnings"]

        def props() -> list[str]:
            notes = []
            for key, value in r.props.items():
                if not _PROP_KEY.match(str(key)):
                    notes.append(f"property {key!r} skipped: not a valid property name")
                elif str(key).startswith("ro."):
                    notes.append(f"property {key} skipped: ro.* is read-only after boot, set it in extra_boot_args")
                else:
                    adb.setprop(str(key), str(value))
            return notes

        def animations() -> None:
            if self.config.performance.disable_animations:
                adb.shell("settings put global window_animation_scale 0; "
                          "settings put global transition_animation_scale 0; "
                          "settings put global animator_duration_scale 0", timeout=20)

        def proxy() -> None:
            if r.net_mode == "proxy":
                if not _PROXY.match(r.proxy):
                    raise ValueError(f"proxy {r.proxy!r} is not host:port")
                if adb.setting_get("global", "http_proxy") != r.proxy:
                    adb.setting_put("global", "http_proxy", r.proxy)
                self.store.state_set(r.id, "proxy", r.proxy)
            elif self.store.state_get(r.id, "proxy"):
                # Only a proxy this farm set is cleared; one configured by hand is not ours to remove.
                adb.setting_put("global", "http_proxy", ":0")
                self.store.state_set(r.id, "proxy", None)

        attempt("device identity", identity)
        attempt("properties", props)
        attempt("animations", animations)
        attempt("proxy", proxy)
        attempt("trim packages", lambda: self._trim(r, adb))
        for hook in self.post_boot_hooks:
            attempt(getattr(hook, "__qualname__", "post-boot hook"), lambda hook=hook: hook(r, adb))
        return warnings

    def _trim(self, r: Resolved, adb: Adb) -> None:
        """Disable the system packages on the trim list, once per list.

        "Once" is tracked twice: in the farm's state (the cheap check) and in a marker file
        inside the Android, which also lists what was disabled. The marker is what makes an
        adopted or restored volume, or a farm whose state was lost, do the right thing."""
        perf = self.config.performance
        wanted = trim_list(r.android_version, extra=perf.trim_extra, keep=perf.trim_keep) if perf.trim_packages else []
        digest = trim_hash(wanted)
        state = self.store.state_get(r.id, "trim") or {}
        if state.get("hash") == digest:
            return
        marker = adb.shell(["cat", TRIM_MARKER], timeout=15, check=False).splitlines()
        on_device_hash = marker[0].strip() if marker else ""
        previously = {p for p in (state.get("packages") or marker[1:]) if _PACKAGE.match(p.strip())}
        if on_device_hash == digest and digest != "none":
            self.store.state_set(r.id, "trim", {"hash": digest, "packages": wanted})
            return
        enable = sorted(p.strip() for p in previously if p.strip() not in wanted)
        enabled_now = {line.removeprefix("package:").strip()
                       for line in adb.shell("pm list packages -e", timeout=60).splitlines()}
        disable = [p for p in wanted if p in enabled_now]
        # One shell round trip for the whole list: forty separate ADB calls take most of a minute
        # on a busy host. Names are validated against _PACKAGE above, and passed as arguments.
        if enable:
            adb.shell(["sh", "-c", 'for p in "$@"; do pm enable --user 0 "$p" >/dev/null 2>&1; done', "sh", *enable],
                      timeout=180, check=False)
        if disable:
            adb.shell(["sh", "-c", 'for p in "$@"; do pm disable-user --user 0 "$p" >/dev/null 2>&1; done', "sh", *disable],
                      timeout=180, check=False)
        if wanted:
            adb.shell(["sh", "-c", f'printf "%s\\n" "$@" > {TRIM_MARKER}', "sh", digest, *wanted], timeout=15, check=False)
        elif marker:
            adb.shell(["rm", "-f", TRIM_MARKER], timeout=15, check=False)
        self.store.state_set(r.id, "trim", {"hash": digest, "packages": wanted})
        log.info("Android %s: %d system package(s) disabled, %d re-enabled", r.id, len(disable), len(enable))

    # ------------------------------------------------------------------- status
    def status(self, id: int) -> dict:
        """Everything about one Android, including a live boot check over ADB."""
        spec = self._spec(id)
        rows = {c["name"]: c for c in self.docker.ps(all=True, labels={LABEL_FARM: self.prefix,
                                                                       LABEL_INSTANCE: str(spec.id)})}
        out = self._summary(spec, rows, self.store.state(spec.id))
        names = self.names(spec.id)
        android = rows.get(names.android)
        if android and android["state"] == "running":
            adb = self.adb(spec.id)
            try:
                booted = (adb.state() == "device" or adb.connect(attempts=1)) and adb.booted()
            except (CommandError, AdbError):
                booted = False          # no adb on this machine: the container state still stands
            (self._booted.add if booted else self._booted.discard)(spec.id)
            out["state"] = self._state(spec.id, rows, out["last_error"])
            out["uptime"] = self._uptime(names.android)
        net = rows.get(names.net)
        if net and net["state"] == "running" and "config_error" not in out:
            try:
                live = self.backends.net_status(self.docker, self._resolve(spec)) or {}
                out["net"] = {**out["net"], **{k: v for k, v in live.items() if k not in ("mode",)}}
            except Exception as e:  # noqa: BLE001 - status must not fail because one probe did
                out["net"]["detail"] = f"status unavailable: {e}"[:200]
        return out

    def list_status(self) -> list[dict]:
        """All Androids at a glance. One `docker ps` for the whole farm and no ADB: the
        "booted" state comes from what this process saw at the last start or status()."""
        by_instance: dict[str, dict[str, dict]] = {}
        for c in self._farm_containers():
            by_instance.setdefault(c["labels"].get(LABEL_INSTANCE, ""), {})[c["name"]] = c
        return [self._summary(spec, by_instance.get(str(i), {}), self.store.state(i))
                for i, spec in sorted(self.store.list().items())]

    def _state(self, id: int, rows: dict[str, dict], last_error: str) -> str:
        names = self.names(id)
        android, net = rows.get(names.android), rows.get(names.net)
        activity = self._activity.get(id, "")
        if android and android["state"] == "running":
            if id in self._booted:
                return "booted"
            return "starting" if activity in ("starting", "restarting", "recreating") else "running"
        if activity in ("starting", "restarting", "recreating"):
            return "starting"
        if not android and not net:
            return "missing"
        if last_error:
            return "error"
        if not android or android["state"] == "created":
            return "created"
        return "stopped"

    def _summary(self, spec: InstanceSpec, rows: dict[str, dict], state: dict) -> dict:
        names = self.names(spec.id)
        android, net = rows.get(names.android), rows.get(names.net)
        last_error = str(state.get("last_error") or "")
        if not (android and android["state"] == "running"):
            self._booted.discard(spec.id)
        port = self.config.network.adb_port_base + spec.id
        out: dict[str, Any] = {
            "id": spec.id, "name": spec.display_name, "tags": list(spec.tags), "notes": spec.notes,
            "state": self._state(spec.id, rows, last_error), "busy": self._activity.get(spec.id, ""),
            "containers": {
                "android": {"name": names.android, "state": android["state"] if android else "missing",
                            "status": android["status"] if android else ""},
                "net": {"name": names.net, "state": net["state"] if net else "missing",
                        "status": net["status"] if net else ""},
            },
            "adb_port": port, "serial": f"{self._adb_host()}:{port}",
            "data_volume": spec.data_volume or names.volume, "adopted": bool(spec.data_volume),
            "autostart": spec.autostart, "device_profile": spec.device.profile,
            "uptime": None, "last_error": last_error, "last_start": state.get("last_start"),
            # From the last start: over capacity, a version change, a setting that did not apply.
            "warnings": [str(w) for w in state.get("warnings") or []],
            "data_android_version": str(state.get("data_android_version") or ""),
        }
        labels = android["labels"] if android else {}
        used = state.get("gpu") or {}
        try:
            r = self._resolve(spec)
        except ValueError as e:
            out["config_error"] = str(e)
            out["gpu"] = {"requested": spec.gpu_mode or self.config.gpu.mode, "mode": labels.get(LABEL_GPU, ""),
                          "node": labels.get(LABEL_GPU_NODE, "")}
            out["net"] = {"mode": spec.network.mode or self.config.network.mode,
                          "profile": state.get("vpn_profile") or ""}
            return out
        out.update({"android_version": r.android_version, "image": r.image,
                    "screen": {"width": r.width, "height": r.height, "dpi": r.dpi, "fps": r.fps}})
        out["gpu"] = {"requested": r.gpu_mode, "mode": labels.get(LABEL_GPU) or used.get("mode", ""),
                      "node": labels.get(LABEL_GPU_NODE) or used.get("node", ""),
                      "kind": used.get("kind", ""), "label": used.get("label", "")}
        out["net"] = {"mode": r.net_mode, "profile": r.vpn_profile or state.get("vpn_profile") or "",
                      "gateway": r.gateway, "proxy": r.proxy, "killswitch": r.killswitch}
        return out

    def _uptime(self, container: str) -> float | None:
        r = self.docker.run(["container", "inspect", "--format", "{{.State.StartedAt}}", container],
                            timeout=30, check=False)
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", r.out if r.ok else "")
        if not m:
            return None
        started = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, round(time.time() - started.timestamp(), 1))

    # --------------------------------------------------------------------- bulk
    def bulk(self, action: str, selector: str, *, force: bool = False) -> Task:
        """start | stop | restart | recreate | delete for every Android a selector names.
        Each Android is its own result; one that fails (or is refused: over capacity, data
        at risk) never stops the others. `force` is passed to every one of them.

        Raises TaskBusy, and does nothing, while another such task (or a manual backup, or a
        restore) has not finished with one of these Androids: see core.tasks."""
        actions: dict[str, Callable[[int, Task], str | None]] = {
            "start": lambda i, t: _describe(self.start(i, t, force=force)),
            "restart": lambda i, t: _describe(self.restart(i, t, force=force)),
            "recreate": lambda i, t: _describe(self.recreate(i, t, force=force)),
            "stop": lambda i, t: self.stop(i),
            "delete": lambda i, t: _describe_delete(self.delete(i, force=force)),
        }
        if action not in actions:
            raise ValueError(f"unknown action {action!r}; use one of: {', '.join(actions)}")
        ids = self.select(selector)
        perf = self.config.performance
        boots = action in ("start", "restart", "recreate")
        # Booting is the heaviest moment in an Android's life; the other actions are cheap,
        # except a delete that first copies each Android's data to a safety backup.
        copies = action == "delete" and self.config.backup.auto_before_risky
        return run_on_instances(self.tasks, f"instance.{action}", f"{action} {len(ids)} Android(s)", ids,
                                actions[action], concurrency=perf.start_concurrency if boots else 2 if copies else 8,
                                stagger=perf.start_stagger_seconds if boots else 0.0, exclusive=True)

    def autostart(self) -> Task | None:
        """Start what should be up when the daemon starts: performance.autostart plus every
        Android with autostart set. Returns None when there is nothing to start."""
        ids = set(self.select(self.config.performance.autostart)) if self.config.performance.autostart else set()
        ids |= {i for i, spec in self.store.list().items() if spec.autostart}
        if not ids:
            return None
        return self.bulk("start", ",".join(str(i) for i in sorted(ids)))


class GpuLoad(dict):
    """Running Androids per render node, and the card the Android being started used last
    (`previous`): the GPU module keeps an Android on its card while that card has room."""
    previous: str = ""


def _gpu_counts(containers: list[dict], skip_id: int, placed: dict[int, str] | None = None) -> GpuLoad:
    """Androids per render node, for the GPU module's assignment policy: the running ones,
    and those whose start is under way and has picked its card (`placed`) but whose
    container is not up yet."""
    counts = GpuLoad()
    seen: set[str] = set()
    for c in containers:
        labels = c["labels"]
        node = labels.get(LABEL_GPU_NODE, "")
        if not node or labels.get(LABEL_ROLE) != "android":
            continue
        if labels.get(LABEL_INSTANCE) == str(skip_id):
            counts.previous = node
        elif c["state"] == "running":
            counts[node] = counts.get(node, 0) + 1
            seen.add(labels.get(LABEL_INSTANCE, ""))
    for other, node in (placed or {}).items():
        if node and other != skip_id and str(other) not in seen:
            counts[node] = counts.get(node, 0) + 1
    return counts


def _describe(result: dict) -> str:
    text = f"booted in {result['boot_seconds']} s ({result['gpu']})"
    if result.get("warnings"):
        text += f"; {len(result['warnings'])} warning(s): {result['warnings'][0]}"
    return text


def _describe_delete(result: dict) -> str:
    text = {"removed": "deleted with its data", "kept": "deleted, data kept", "none": "deleted"}[result["data"]]
    if result.get("backup"):
        text += f"; backup: {result['backup']}"
    if result.get("warnings"):
        text += f"; {result['warnings'][0]}"
    return text


def _apply_patch(saved: dict, patch: dict) -> dict:
    """`saved` with `patch` laid over it: mappings merge, anything else replaces, and a key
    set to None is dropped. For a field of the spec a dropped key means its default when the
    result is validated; for a free-form map it means the entry is gone."""
    out = dict(saved)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _apply_patch(out[key], value)
        elif isinstance(value, dict):
            out[key] = _apply_patch({}, value)       # a new map: nulls in it have nothing to remove
        else:
            out[key] = value
    return out

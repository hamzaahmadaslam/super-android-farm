"""From resolved values to `docker run` arguments. Pure: no I/O, no clock, no randomness.

Everything that decides how an Android container is created is computed here from a
Resolved (and the GPU choice made by the host module), so the same inputs always give the
same arguments. That is what makes plan_hash() meaningful: the hash is stored as a label on
the containers, and a start only rebuilds them when the hash no longer matches.

The second half of this file is the CPU pinning allocator, kept here because it is the same
kind of thing: plain data in, a `--cpuset-cpus` value out.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Protocol
from collections.abc import Iterable, Mapping, Sequence

from .device_profile import boot_props
from .models import Resolved

LABEL_PLAN = "io.superfarm.plan"
LABEL_GPU = "io.superfarm.gpu"              # concrete mode this container was created with
LABEL_GPU_NODE = "io.superfarm.gpu-node"    # render node, so running Androids per card can be counted
LABEL_CPUSET = "io.superfarm.cpuset"        # pinned cores, so the allocator sees what is taken
DATA_MOUNT = "/data"

# A *named* volume only. Docker reads `-v /some/path:/data` as a bind mount of a host folder,
# so a volume name with a slash or colon would hand Android (root, privileged) a host directory.
_VOLUME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
# An image reference must not look like an option (`--volume=...`) to `docker run`.
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/:@]{0,254}$")
# Android's init splits its command line on whitespace; one stray space corrupts every
# argument after it. Same shape the instance spec validates, enforced again here because
# farm-wide arguments from the config arrive unvalidated.
_BOOT_ARG = re.compile(r"^[A-Za-z0-9_.\-]+=[^\s]*$")
_CPUSET = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")
_SIZE = re.compile(r"^\d+[bkmg]?$", re.IGNORECASE)


class GpuLike(Protocol):
    """The part of superfarm.host.gpu.GpuChoice the planner reads."""
    mode: str
    node: str
    run_args: list[str]
    boot_args: list[str]


def _check(pattern: re.Pattern, value: str, what: str, hint: str) -> str:
    if not pattern.match(value or ""):
        raise ValueError(f"{what} {value!r} is not usable: {hint}")
    return value


def _mb(value: int) -> str:
    return f"{int(value)}m"


def _cpus(value: float) -> str:
    return f"{float(value):g}"


def boot_arguments(r: Resolved, gpu: GpuLike) -> list[str]:
    """Everything after the image name: what Android's init receives.

    One entry per key. Android keeps the *first* value it sees for a read-only property, so
    simply appending an override would be ignored; instead later sources replace earlier
    ones here: display defaults < GPU < device identity < extra arguments from config/spec."""
    merged: dict[str, str] = {
        "androidboot.redroid_width": str(r.width),
        "androidboot.redroid_height": str(r.height),
        "androidboot.redroid_dpi": str(r.dpi),
        # Stock images read the frame rate from this argument. An image with its own
        # compositor patch may read another property; pass that one via android.extra_boot_args.
        "androidboot.redroid_fps": str(r.fps),
    }
    for source, args in (("GPU", gpu.boot_args), ("device identity", boot_props(r.device)), ("extra", r.boot_args)):
        for arg in args:
            _check(_BOOT_ARG, arg, f"{source} boot argument", "expected key=value without spaces")
            key, _, value = arg.partition("=")
            merged[key] = value
    return [f"{k}={v}" for k, v in merged.items()]


def cpuset_label(cpuset: str) -> str:
    """A cpuset as a label value. `docker ps` prints all labels of a container as one
    comma-separated string, so a value containing a comma cannot be read back from it."""
    return cpuset.replace(",", "+")


def cpuset_from_label(value: str) -> str:
    return value.replace("+", ",")


def plan_android(r: Resolved, *, gpu: GpuLike, extra_run_args: list[str], plan_hash: str) -> list[str]:
    """Arguments after `docker run -d` for one Android container."""
    volume = _check(_VOLUME, r.data_volume, "data volume", "it must be the name of a Docker volume, not a path")
    image = _check(_IMAGE, r.image, "image", "expected a reference like repo/name:tag")
    net = r.names.net
    args: list[str] = [
        "--name", r.names.android,
        # Android's init mounts binderfs, cgroups and its own /dev; nothing short of
        # privileged lets it boot.
        "--privileged",
        # The sidecar owns the network namespace (VPN, kill switch, published ADB port).
        # Android must never get a network of its own, or it would have a way around them.
        "--network", f"container:{net}",
        "--cpus", _cpus(r.cpus),
        "--memory", _mb(r.memory_mb),
        "--memory-swap", _mb(max(r.memory_swap_mb, r.memory_mb)),
        "--pids-limit", str(int(r.pids_limit)),
        "--shm-size", _mb(r.shm_mb),
        "--restart", r.restart_policy,
        "--stop-timeout", str(int(r.stop_timeout)),
        "--log-driver", "json-file",
        "--log-opt", "max-size=" + _check(_SIZE, r.log_max_size, "log size", "expected a size like 10m"),
        "--log-opt", f"max-file={int(r.log_max_files)}",
    ]
    if r.memory_reservation_mb:
        args += ["--memory-reservation", _mb(min(r.memory_reservation_mb, r.memory_mb))]
    if r.cpuset:
        args += ["--cpuset-cpus", _check(_CPUSET, r.cpuset, "cpuset", "expected cores like 0-3 or 8,9")]
    if r.cpu_shares:
        args += ["--cpu-shares", str(int(r.cpu_shares))]
    if r.oom_score_adj:
        args += ["--oom-score-adj", str(int(r.oom_score_adj))]
    labels = {**r.names.labels("android"), LABEL_PLAN: plan_hash, LABEL_GPU: gpu.mode,
              LABEL_GPU_NODE: gpu.node, LABEL_CPUSET: cpuset_label(r.cpuset)}
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    args += ["-v", f"{volume}:{DATA_MOUNT}"]
    args += [str(a) for a in gpu.run_args]
    args += [str(a) for a in extra_run_args]
    args.append(image)
    args += boot_arguments(r, gpu)
    return args


def plan_hash(r: Resolved, gpu: GpuLike, net_args: list[str], *, extra_run_args: Sequence[str] = (),
              image_id: str = "") -> str:
    """Digest of everything the pair of containers is created from.

    `image_id` lets a rebuilt image (same tag, new content) count as a change. The display
    name is left out: renaming an Android must not rebuild it."""
    android = plan_android(r, gpu=gpu, extra_run_args=list(extra_run_args), plan_hash="")
    payload = {"v": 1, "android": android, "net": [str(a) for a in net_args], "image": image_id}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()[:24]


# ============================================================================ CPU pinning

@dataclass(frozen=True)
class Core:
    """One physical core: its hardware threads and, on hybrid CPUs, its class."""
    id: int
    cpus: tuple[int, ...]
    kind: str = ""          # "performance" | "efficiency" | "" (uniform CPU)


@dataclass(frozen=True)
class CpuTopology:
    cores: tuple[Core, ...]

    @property
    def hybrid(self) -> bool:
        return len({c.kind for c in self.cores if c.kind}) > 1

    def cpus(self, kind: str | None = None) -> list[int]:
        return sorted(n for c in self.cores if kind is None or c.kind == kind for n in c.cpus)


def parse_cpu_list(text: str) -> list[int]:
    """`0-3,8,10-11` -> [0, 1, 2, 3, 8, 10, 11]. Empty text -> []."""
    out: set[int] = set()
    for part in str(text).replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"bad CPU list {text!r}")
        low, high = int(m.group(1)), int(m.group(2) or m.group(1))
        if high < low or high - low > 4096:
            raise ValueError(f"bad CPU range {part!r}")
        out.update(range(low, high + 1))
    return sorted(out)


def format_cpu_list(cpus: Iterable[int]) -> str:
    """[0, 1, 2, 3, 8] -> `0-3,8`."""
    ordered = sorted(set(int(c) for c in cpus))
    parts: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(str(ordered[i]) if i == j else f"{ordered[i]}-{ordered[j]}")
        i = j + 1
    return ",".join(parts)


def build_topology(siblings: Mapping[int, Sequence[int]], *, max_freq: Mapping[int, int] | None = None,
                   performance: Iterable[int] | None = None, efficiency: Iterable[int] | None = None) -> CpuTopology:
    """Group logical CPUs into cores and classify them.

    siblings     logical CPU -> the hardware threads of its core (thread_siblings_list)
    performance, efficiency
                 the kernel's own answer on Intel hybrid CPUs (cpu_core / cpu_atom); used when given
    max_freq     logical CPU -> top frequency. Fallback for kernels and architectures without
                 that answer (big.LITTLE): cores well below the fastest one are efficiency cores.
                 The margin is wide on purpose: "favoured" P-cores turbo a few percent higher
                 than their neighbours and must not be mistaken for a class of their own."""
    groups: dict[tuple[int, ...], None] = {}
    for cpu in sorted(siblings):
        group = tuple(sorted({int(c) for c in siblings[cpu]} | {int(cpu)}))
        groups.setdefault(group)
    perf, eff = set(performance or ()), set(efficiency or ())
    freq = dict(max_freq or {})
    top = max(freq.values(), default=0)
    cores = []
    for index, group in enumerate(sorted(groups)):
        kind = ""
        if perf and eff:
            kind = "performance" if perf & set(group) else "efficiency" if eff & set(group) else ""
        elif top and min(freq.values()) < 0.85 * top:
            known = [freq[c] for c in group if c in freq]
            if known:
                kind = "performance" if max(known) >= 0.85 * top else "efficiency"
        cores.append(Core(id=index, cpus=group, kind=kind))
    return CpuTopology(cores=tuple(cores))


def allocate_cpuset(topology: CpuTopology, policy: str, cpus: float, used: Mapping[int, int] | None = None) -> str:
    """Pick the cores one Android is pinned to; "" means "do not pin".

    policy   none | spread | performance | efficiency  (resources.cpu_pinning)
    cpus     the Android's CPU allowance; it gets that many logical CPUs, rounded up
    used     logical CPU -> number of Androids already pinned to it

    Whole cores are handed out, least-loaded first, so two Androids only share a physical
    core (and its cache) once every core has a tenant. On a CPU without the requested class
    (asking for efficiency cores on a uniform CPU) all cores are eligible rather than none."""
    if policy == "none" or not topology.cores:
        return ""
    if policy not in ("spread", "performance", "efficiency"):
        raise ValueError(f"unknown cpu_pinning policy {policy!r}")
    pool = [c for c in topology.cores if c.kind == policy] if policy != "spread" else list(topology.cores)
    if not pool:
        pool = list(topology.cores)
    used = used or {}
    want = max(1, math.ceil(cpus))

    def load(core: Core) -> float:
        return sum(used.get(c, 0) for c in core.cpus) / len(core.cpus)

    picked: list[int] = []
    for core in sorted(pool, key=lambda c: (load(c), c.id)):
        for cpu in core.cpus:
            if len(picked) < want:
                picked.append(cpu)
        if len(picked) >= want:
            break
    return format_cpu_list(picked)


def cpuset_fits(cpuset: str, topology: CpuTopology, policy: str) -> bool:
    """Whether a remembered pinning is still valid for this machine and policy (cores can
    go offline, and the policy can change in the config)."""
    try:
        cpus = set(parse_cpu_list(cpuset))
    except ValueError:
        return False
    if not cpus or policy == "none":
        return False
    pool = set(topology.cpus(policy if policy in ("performance", "efficiency") else None)) or set(topology.cpus())
    return cpus <= pool

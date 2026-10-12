"""Which GPUs this machine has, and how one Android should draw.

What works, and why the modes differ:

* Intel and AMD: Android's graphics stack is Mesa, and Mesa drives these cards directly.
  The container gets the render node (`--device`) and redroid's own `host` GPU mode.
* NVIDIA: Mesa has no driver for NVIDIA's proprietary stack. Pointing the `host` mode at
  an NVIDIA node fails at boot (gbm cannot create a device, then no EGL config). NVIDIA
  Androids therefore draw through a relay (see superfarm.host.venus) and a custom gralloc.
* software: ANGLE on SwiftShader, on the CPU. Always works; costs about five times the
  CPU of a GPU mode while the screen is being drawn.

Detection reads sysfs only (/sys/class/drm/renderD*/device), so it needs no tools and no
privileges, and `sysfs` can be pointed at a fake tree in tests. choose() is deterministic:
the same Android gets the same card at every start unless the configuration changes,
because a changed card means a rebuilt container.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from ..config import Config
from ..core.models import Resolved
from ..paths import Paths
from ..util import proc
from . import venus

VENDOR_IDS = {"0x8086": "intel", "0x1002": "amd", "0x1022": "amd", "0x10de": "nvidia"}
DRIVER_VENDOR = {"i915": "intel", "xe": "intel", "amdgpu": "amd", "radeon": "amd", "nvidia": "nvidia"}
# Drivers Android's Mesa can render with through redroid's `host` mode.
MESA_DRIVERS = {"i915", "xe", "amdgpu", "radeon"}
VENDOR_LABEL = {"intel": "Intel", "amd": "AMD", "nvidia": "NVIDIA", "other": "GPU"}

BOOT_SOFTWARE = "androidboot.redroid_gpu_mode=guest"
BOOT_HOST = "androidboot.redroid_gpu_mode=host"
BOOT_VENUS = "androidboot.redroid_gpu_mode=venus"

_RENDER = re.compile(r"^renderD(\d+)$")


@dataclass
class GpuInfo:
    node: str                        # /dev/dri/renderD128
    driver: str                      # kernel driver bound to the card: i915, xe, amdgpu, radeon, nvidia, ...
    vendor: str                      # intel | amd | nvidia | other
    name: str
    pci: str                         # PCI address, e.g. 0000:01:00.0 ("" for non-PCI devices)
    usable_modes: list[str] = field(default_factory=list)   # gpu modes this card can serve
    discrete: bool = False           # a card with its own memory (heuristic for Intel/AMD)
    vram_mb: int = 0                 # dedicated memory; 0 = unknown or shared with system RAM
    note: str = ""                   # why usable_modes is empty, when it is


@dataclass
class GpuChoice:
    mode: str                        # concrete: software | intel | amd | nvidia
    node: str                        # render node used ("" for software)
    run_args: list[str]              # extra `docker run` arguments for the Android container
    boot_args: list[str]             # androidboot.* arguments
    reason: str                      # one line for the panel: why this mode / card


# ------------------------------------------------------------------ detection

def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _uevent(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in _read(path).splitlines() if "=" in line)


def lspci_name(pci: str) -> str:
    """Model name from the PCI id database, when `lspci` is installed. Bounded and optional:
    a farm must detect its GPUs on a machine without pciutils too."""
    if not pci or not proc.which("lspci"):
        return ""
    try:
        r = proc.run(["lspci", "-mm", "-s", pci], timeout=5, check=False)
    except proc.CommandError:
        return ""
    # -mm output: 01:00.0 "VGA compatible controller" "Vendor" "Device name" ...
    parts = re.findall(r'"([^"]*)"', r.stdout)
    return parts[2].strip() if r.ok and len(parts) >= 3 else ""


def _is_discrete(vendor: str, pci: str, vram_mb: int) -> bool:
    if vendor == "nvidia":
        return True
    if vendor == "intel":
        # Intel's integrated graphics has sat at 00:02.0 on every platform; Arc cards do not.
        return not pci.endswith(":00:02.0")
    if vendor == "amd":
        # No clean flag exists. An APU reports a small carve-out of system RAM as VRAM
        # (typically 512 MB - 2 GB); discrete cards worth preferring have more.
        return vram_mb >= 3072
    return False


def detect_gpus(sysfs: Path | str = "/sys", *, dev: Path | str = "/dev",
                namer: Callable[[str], str] | None = lspci_name) -> list[GpuInfo]:
    """Every DRM render node, in node order. Never raises: a machine without GPUs or
    without sysfs returns an empty list."""
    drm = Path(sysfs) / "class" / "drm"
    try:
        entries = sorted((e for e in os.listdir(drm) if _RENDER.match(e)), key=lambda e: int(e[7:]))
    except OSError:
        return []
    out: list[GpuInfo] = []
    for entry in entries:
        device = drm / entry / "device"
        try:
            driver = os.path.basename(os.readlink(device / "driver"))
        except OSError:
            driver = ""
        ue = _uevent(device / "uevent")
        driver = driver or ue.get("DRIVER", "")
        vendor_id = _read(device / "vendor").lower()
        vendor = DRIVER_VENDOR.get(driver) or VENDOR_IDS.get(vendor_id, "other")
        if driver == "nouveau":
            vendor = "nvidia"
        pci = ue.get("PCI_SLOT_NAME", "")
        if not pci:
            try:
                tail = os.path.basename(os.path.realpath(device))
                pci = tail if re.match(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$", tail) else ""
            except OSError:
                pci = ""
        vram_bytes = _read(device / "mem_info_vram_total")      # amdgpu / radeon publish this
        vram_mb = int(vram_bytes) // (1024 * 1024) if vram_bytes.isdigit() else 0
        pci_id = ue.get("PCI_ID", "").lower() or f"{vendor_id[2:]}:{_read(device / 'device')[2:]}".strip(":")
        name = (namer(pci) if namer else "") or f"{VENDOR_LABEL.get(vendor, 'GPU')} GPU [{pci_id or driver or 'unknown'}]"
        modes: list[str] = []
        note = ""
        if driver in MESA_DRIVERS:
            modes = [vendor]
        elif driver == "nvidia":
            modes = ["nvidia"]
        elif driver == "nouveau":
            note = "the open nouveau driver is in use; the NVIDIA relay needs NVIDIA's own driver"
        elif not driver:
            note = "no kernel driver is bound to this card"
        else:
            note = f"driver {driver} is not one Android's Mesa can render with"
        out.append(GpuInfo(node=str(Path(dev) / "dri" / entry), driver=driver, vendor=vendor, name=name, pci=pci,
                           usable_modes=modes, discrete=_is_discrete(vendor, pci, vram_mb), vram_mb=vram_mb,
                           note=note))
    return out


def nvidia_vram(gpus: list[GpuInfo], runner: Callable[..., proc.Result] | None = None) -> list[GpuInfo]:
    """Fill vram_mb of NVIDIA cards from nvidia-smi (sysfs does not expose it). Best effort."""
    if not any(g.vendor == "nvidia" and not g.vram_mb for g in gpus):
        return gpus
    run = runner or proc.run
    try:
        r = run(["nvidia-smi", "--query-gpu=pci.bus_id,memory.total", "--format=csv,noheader,nounits"],
                timeout=10, check=False)
    except proc.CommandError:
        return gpus
    if not r.ok:
        return gpus
    by_pci: dict[str, int] = {}
    for line in r.stdout.splitlines():
        bus, _, mem = (p.strip() for p in line.partition(","))
        if mem.isdigit():
            # nvidia-smi prints an 8-digit PCI domain (00000000:01:00.0); sysfs uses 4.
            by_pci[bus.lower()[-12:]] = int(mem)
    for g in gpus:
        if g.vendor == "nvidia" and not g.vram_mb:
            g.vram_mb = by_pci.get(g.pci.lower(), 0)
    return gpus


# ------------------------------------------------------------------ choosing

def _pick(candidates: list[GpuInfo], r: Resolved, config: Config, counts: dict[str, int] | None) -> GpuInfo:
    """Apply gpu.assignment among equally good cards."""
    if len(candidates) == 1 or config.gpu.assignment == "first":
        return candidates[0]
    if config.gpu.assignment == "least-instances":
        counts = counts or {}
        return min(candidates, key=lambda g: (counts.get(g.node, 0), candidates.index(g)))
    # round-robin by instance id rather than by a running counter: the same Android lands
    # on the same card at every start, so its container is not rebuilt for nothing.
    return candidates[r.id % len(candidates)]


def _software(reason: str) -> GpuChoice:
    return GpuChoice(mode="software", node="", run_args=[], boot_args=[BOOT_SOFTWARE], reason=reason)


def _mesa(g: GpuInfo, reason: str) -> GpuChoice:
    return GpuChoice(mode=g.vendor, node=g.node, run_args=["--device", g.node],
                     boot_args=[BOOT_HOST, f"androidboot.redroid_gpu_node={g.node}"], reason=reason)


def _nvidia(g: GpuInfo, config: Config, paths: Paths, reason: str) -> GpuChoice:
    boot = [BOOT_VENUS]
    if config.gpu.nvidia.debug:
        boot.append("androidboot.redroid_gpu_debug=1")       # read by gpu_config.sh's venus branch
    return GpuChoice(mode="nvidia", node=g.node, run_args=venus.container_mounts(config, paths, g.node),
                     boot_args=boot, reason=reason)


def _nvidia_blocker(runtime: dict) -> str:
    """Why NVIDIA mode cannot be used right now ("" = it can)."""
    if not runtime.get("ready"):
        return (f"the Venus runtime or relay is not built ({', '.join(runtime.get('missing', [])) or 'files missing'}); "
                f"{venus.BUILD_HINT}")
    if not runtime.get("icd", True):
        return f"the NVIDIA Vulkan driver manifest {runtime.get('icd_path', '')} is missing; install the NVIDIA driver"
    return ""


def choose(r: Resolved, config: Config, paths: Paths, *, counts: dict[str, int] | None = None,
           gpus: list[GpuInfo] | None = None, runtime: dict | None = None,
           ensure_relay: Callable[[Config, Paths, str], str] | None = venus.ensure_relay) -> GpuChoice:
    """Decide how Android `r` draws.

    counts        running Androids per render node, for `least-instances` assignment
    gpus/runtime  detection results, injectable so the function is testable without hardware
    ensure_relay  called for NVIDIA choices so the relay is up before the Android starts;
                  pass None to only plan (no process is started)

    `auto` resolves to the best mode that works *now*; a mode that was asked for by name
    and is unavailable raises ValueError saying why and what to do.
    """
    mode = r.gpu_mode
    if mode == "software":
        return _software("software rendering was requested")
    gpus = detect_gpus() if gpus is None else gpus
    runtime = venus.runtime_status(config, paths) if runtime is None else runtime
    forced = r.gpu_node
    pool = gpus
    if forced:
        pool = [g for g in gpus if g.node == forced]
        if not pool:
            known = ", ".join(g.node for g in gpus) or "none"
            raise ValueError(f"render node {forced} does not exist on this machine (found: {known}); "
                             "fix gpu.render_node / the instance's gpu_node")

    def start_relay(g: GpuInfo) -> str:
        """"" when the relay is up (or planning only), else the reason it is not."""
        if ensure_relay is None:
            return ""
        try:
            ensure_relay(config, paths, g.node)
        except (venus.RelayError, OSError, proc.CommandError) as e:
            return str(e)
        return ""

    if mode in ("intel", "amd"):
        cands = [g for g in pool if mode in g.usable_modes]
        if not cands:
            if forced:
                g = pool[0]
                raise ValueError(f"{forced} is a {VENDOR_LABEL.get(g.vendor, g.vendor)} card (driver "
                                 f"{g.driver or 'none'}), not usable in {mode} mode")
            raise ValueError(f"gpu mode {mode!r} requested but no {VENDOR_LABEL[mode]} GPU with a Mesa driver "
                             f"was found; run `sf doctor`, or set gpu.mode to auto")
        g = _pick(cands, r, config, counts)
        return _mesa(g, f"{g.name} via Mesa ({g.driver})")

    if mode == "nvidia":
        cands = [g for g in pool if "nvidia" in g.usable_modes]
        if not cands:
            note = next((g.note for g in pool if g.vendor == "nvidia" and g.note), "")
            raise ValueError("gpu mode 'nvidia' requested but no NVIDIA card with NVIDIA's driver was found"
                             + (f" ({note})" if note else "") + "; run `sf doctor`")
        blocker = _nvidia_blocker(runtime)
        if blocker:
            raise ValueError(f"gpu mode 'nvidia' is not available: {blocker}")
        g = _pick(cands, r, config, counts)
        failed = start_relay(g)
        if failed:
            raise ValueError(f"gpu mode 'nvidia' is not available: {failed}")
        return _nvidia(g, config, paths, f"{g.name} through the Venus relay")

    # auto: nvidia (only when runtime + relay exist) > discrete AMD/Intel > integrated > software
    skipped: list[str] = []
    nvidia = [g for g in pool if "nvidia" in g.usable_modes]
    if nvidia:
        blocker = _nvidia_blocker(runtime)
        if not blocker:
            g = _pick(nvidia, r, config, counts)
            blocker = start_relay(g)
            if not blocker:
                return _nvidia(g, config, paths, f"auto: {g.name} through the Venus relay")
        skipped.append(f"NVIDIA skipped: {blocker}")
    mesa = [g for g in pool if g.usable_modes and g.vendor in ("intel", "amd")]
    for tier, label in (([g for g in mesa if g.discrete], "discrete"), ([g for g in mesa if not g.discrete], "integrated")):
        if tier:
            g = _pick(tier, r, config, counts)
            return _mesa(g, "; ".join([f"auto: {label} {g.name} via Mesa ({g.driver})", *skipped]))
    if forced:
        g = pool[0]
        why = skipped[0] if skipped else (g.note or "it cannot be used by Android")
        raise ValueError(f"render node {forced} was requested but cannot be used: {why}")
    return _software("; ".join(["auto: no usable GPU, drawing on the CPU", *skipped]))


def summary(config: Config, paths: Paths, gpus: list[GpuInfo] | None = None) -> dict:
    """What the panel's host page shows: cards, modes available now, and the auto pick."""
    gpus = nvidia_vram(detect_gpus()) if gpus is None else gpus
    runtime = venus.runtime_status(config, paths)
    available = ["software"]
    for g in gpus:
        for m in g.usable_modes:
            if m == "nvidia" and _nvidia_blocker(runtime):
                continue
            if m not in available:
                available.append(m)
    return {"gpus": [g.__dict__ for g in gpus], "available_modes": available,
            "nvidia_runtime": runtime, "nvidia_blocker": _nvidia_blocker(runtime) if any(
                g.vendor == "nvidia" for g in gpus) else ""}

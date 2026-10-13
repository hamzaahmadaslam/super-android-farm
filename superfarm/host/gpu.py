"""Which GPUs this machine has, and how one Android should draw.

What works, and why the modes differ:

* Intel and AMD: Android's graphics stack is Mesa, and Mesa drives these cards directly.
  The container gets the render node (`--device`) and redroid's own `host` GPU mode.
* NVIDIA: Mesa has no driver for NVIDIA's proprietary stack. Pointing the `host` mode at
  an NVIDIA node fails at boot (gbm cannot create a device, then no EGL config). NVIDIA
  Androids therefore draw through a relay (see superfarm.host.venus) and a custom gralloc.
* software: ANGLE on SwiftShader, on the CPU. Always works; costs about five times the
  CPU of a GPU mode while the screen is being drawn.

What an Android can be told to use (gpu.mode for the farm, gpu_mode per Android):

    auto       the best that works now; gpu.prefer says whether a discrete card or the
               integrated GPU is tried first
    balanced   shared across every usable card, the emptiest first, and across the CPU
               once the cards are full; for machines with several GPUs
    software   the CPU
    igpu       the integrated GPU, whoever made it
    dgpu       a discrete card, whoever made it (NVIDIA through the relay, AMD/Intel through Mesa)
    intel / amd / nvidia   a GPU of that vendor
    gpu_node   pins one exact card (a render node or a PCI address); it must be a card
               the mode allows

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
from ..core.models import InstanceSpec, Resolved, resolve
from ..paths import Paths
from ..util import proc
from . import venus

VENDOR_IDS = {"0x8086": "intel", "0x1002": "amd", "0x1022": "amd", "0x10de": "nvidia"}
DRIVER_VENDOR = {"i915": "intel", "xe": "intel", "amdgpu": "amd", "radeon": "amd", "nvidia": "nvidia"}
# Drivers Android's Mesa can render with through redroid's `host` mode.
MESA_DRIVERS = {"i915", "xe", "amdgpu", "radeon"}
VENDOR_LABEL = {"intel": "Intel", "amd": "AMD", "nvidia": "NVIDIA", "other": "GPU"}
VENDOR_MODES = ("intel", "amd", "nvidia")
KIND_MODES = ("igpu", "dgpu")
KIND_LABEL = {"igpu": "integrated", "dgpu": "discrete"}
SOFTWARE_LABEL = "CPU only (software rendering)"
# How a GpuChoice.reason starts the part about an NVIDIA card that was passed over.
NVIDIA_SKIPPED = "NVIDIA skipped: "
# AMD's processor-side vendor id. The graphics part of an AMD processor (0x1002) shares its
# PCI slot with functions that carry this id; a graphics card never does.
AMD_CPU_VENDOR = "0x1022"
# A card on PCI bus 00 that reports this much memory of its own is a discrete card handed
# to a virtual machine, not processor graphics (whose carve-out of system RAM is smaller).
PASSTHROUGH_VRAM_MB = 3072

# Video memory one Android holds on a card, for counting how many a card takes. The
# measurement behind the figure is described in superfarm.host.capacity.
VRAM_PER_ANDROID_MB = 360

BOOT_SOFTWARE = "androidboot.redroid_gpu_mode=guest"
BOOT_HOST = "androidboot.redroid_gpu_mode=host"
BOOT_VENUS = "androidboot.redroid_gpu_mode=venus"

_RENDER = re.compile(r"^renderD(\d+)$")
# The domain is four hex digits on a PC and longer on some servers (Intel VMD uses five).
_PCI = re.compile(r"^[0-9a-f]{4,8}:([0-9a-f]{2}):[0-9a-f]{2}\.[0-9a-f]$")


def pci_key(address: str) -> tuple[int, str] | None:
    """A PCI address in a form that compares equal however its domain is padded:
    sysfs writes 0000:01:00.0 and nvidia-smi 00000000:01:00.0 for the same card."""
    domain, sep, rest = address.strip().lower().partition(":")
    if not sep or not _PCI.match(f"{domain[-8:].rjust(4, '0')}:{rest}"):
        return None
    try:
        return int(domain, 16), rest
    except ValueError:
        return None


@dataclass
class GpuInfo:
    node: str                        # /dev/dri/renderD128
    driver: str                      # kernel driver bound to the card: i915, xe, amdgpu, radeon, nvidia, ...
    vendor: str                      # intel | amd | nvidia | other
    name: str
    pci: str                         # PCI address, e.g. 0000:01:00.0 ("" for non-PCI devices)
    usable_modes: list[str] = field(default_factory=list)   # vendor modes this card can serve
    discrete: bool = False           # a card of its own (always for NVIDIA; by PCI position for Intel/AMD)
    vram_mb: int | None = None       # the card's own memory; None = unknown or shared with system RAM
    note: str = ""                   # why usable_modes is empty, when it is
    # Graphics inside the processor. Derived, so the two flags cannot disagree: an Intel, AMD
    # or NVIDIA GPU is one or the other; neither flag is set for anything else (a virtual GPU).
    integrated: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.integrated = self.vendor in VENDOR_MODES and not self.discrete

    @property
    def kind(self) -> str:
        """igpu | dgpu | "" (a device that is neither, e.g. a virtual GPU)."""
        return "igpu" if self.integrated else "dgpu" if self.discrete else ""


@dataclass
class GpuChoice:
    mode: str                        # concrete backend: software | intel | amd | nvidia
    node: str                        # render node used ("" for software)
    run_args: list[str]              # extra `docker run` arguments for the Android container
    boot_args: list[str]             # androidboot.* arguments
    reason: str                      # one line for the panel: why this mode / card
    kind: str = "cpu"                # cpu | igpu | dgpu
    label: str = SOFTWARE_LABEL      # for the panel, e.g. "NVIDIA GeForce RTX 4080 SUPER (discrete, relay)"


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


def _amd_integrated(device: Path, pci: str, vram_mb: int | None) -> bool:
    """Processor graphics or a Radeon card? No sysfs flag says so; PCI position does.

    Rules in order (from the PCI layout of AMD systems; not exercised on AMD hardware here):
    1. Since Zen, processor graphics is function 0 of a slot whose other functions are parts
       of the processor (security processor, USB, audio) with AMD's processor vendor id.
       A card's extra functions (HDMI audio, USB-C) all carry the graphics vendor id.
    2. Older processor graphics sits directly on PCI bus 00, where no card can be plugged
       in. The exception is a virtual machine, which shows a passed-through card on bus 00:
       that one reports several GB of memory of its own.
    3. Anything behind a PCIe port is a card.
    Model names are deliberately not used: the same family name ("Vega") was sold as both."""
    m = _PCI.match(pci)
    if not m:
        return False
    try:
        slot_dir = Path(os.path.realpath(device)).parent
        siblings = [e for e in os.listdir(slot_dir) if e.startswith(pci.rsplit(".", 1)[0] + ".") and e != pci]
    except OSError:
        slot_dir, siblings = Path(), []
    if any(_read(slot_dir / s / "vendor").lower() == AMD_CPU_VENDOR for s in siblings):
        return True
    if m.group(1) == "00":
        return not (vram_mb is not None and vram_mb >= PASSTHROUGH_VRAM_MB)
    return False


def _is_integrated(vendor: str, device: Path, pci: str, vram_mb: int | None) -> bool:
    if vendor == "intel":
        # Intel's processor graphics has been PCI device 00:02.0 on every platform; Arc cards
        # sit behind a PCIe port.
        return pci.endswith(":00:02.0")
    if vendor == "amd":
        return _amd_integrated(device, pci, vram_mb)
    return False                     # NVIDIA sells no processor graphics for PCs; others are neither


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
        pci = ue.get("PCI_SLOT_NAME", "").lower()
        if not pci:
            try:
                tail = os.path.basename(os.path.realpath(device))
                pci = tail if _PCI.match(tail) else ""
            except OSError:
                pci = ""
        vram_bytes = _read(device / "mem_info_vram_total")      # amdgpu publishes this
        vram_mb = int(vram_bytes) // (1024 * 1024) if vram_bytes.isdigit() else None
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
        integrated = _is_integrated(vendor, device, pci, vram_mb)
        out.append(GpuInfo(node=str(Path(dev) / "dri" / entry), driver=driver, vendor=vendor, name=name, pci=pci,
                           usable_modes=modes, discrete=vendor in VENDOR_MODES and not integrated,
                           vram_mb=vram_mb, note=note))
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
    by_pci: dict[tuple[int, str], int] = {}
    for line in r.stdout.splitlines():
        bus, _, mem = (p.strip() for p in line.partition(","))
        key = pci_key(bus)
        if mem.isdigit() and key:
            # nvidia-smi prints an 8-digit PCI domain (00000000:01:00.0); sysfs uses 4.
            by_pci[key] = int(mem)
    for g in gpus:
        if g.vendor == "nvidia" and not g.vram_mb:
            g.vram_mb = by_pci.get(pci_key(g.pci) or (-1, "")) or None
    return gpus


# ------------------------------------------------------------------ naming

def display_name(g: GpuInfo) -> str:
    """Vendor + marketing name: "AD103 [GeForce RTX 4080 SUPER]" -> "NVIDIA GeForce RTX 4080 SUPER"."""
    vendor = VENDOR_LABEL.get(g.vendor, "GPU")
    if g.name.startswith(vendor):
        return g.name                # our own fallback name, or a name that already says it
    m = re.search(r"\[([^\]]+)\]\s*$", g.name)
    return f"{vendor} {m.group(1) if m else g.name}".strip()


def card_label(g: GpuInfo) -> str:
    kind = KIND_LABEL.get(g.kind, "")
    how = "relay" if g.vendor == "nvidia" else "Mesa"
    return f"{display_name(g)} ({', '.join(p for p in (kind, how) if p)})"


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
                     boot_args=[BOOT_HOST, f"androidboot.redroid_gpu_node={g.node}"], reason=reason,
                     kind=g.kind, label=card_label(g))


def _nvidia(g: GpuInfo, config: Config, paths: Paths, reason: str) -> GpuChoice:
    boot = [BOOT_VENUS]
    if config.gpu.nvidia.debug:
        boot.append("androidboot.redroid_gpu_debug=1")       # read by gpu_config.sh's venus branch
    return GpuChoice(mode="nvidia", node=g.node, run_args=venus.container_mounts(config, paths, g.node),
                     boot_args=boot, reason=reason, kind="dgpu", label=card_label(g))


def _nvidia_blocker(runtime: dict) -> str:
    """Why NVIDIA mode cannot be used right now ("" = it can)."""
    if not runtime.get("ready"):
        # Names only, and only a few: this line is shown in the panel's picker. `sf doctor`
        # lists the full paths.
        missing = [os.path.basename(m) for m in runtime.get("missing", [])]
        listed = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
        return f"the Venus runtime or relay is not built ({listed or 'files'} missing); {venus.BUILD_HINT}"
    if not runtime.get("icd", True):
        return f"the NVIDIA Vulkan driver manifest {runtime.get('icd_path', '')} is missing; install the NVIDIA driver"
    return ""


# ------------------------------------------------------------------ balanced

def card_slots(g: GpuInfo, config: Config) -> int | None:
    """The most Androids `balanced` puts on this card; None = no limit of its own.

    A discrete card is limited by its own memory, where that is known. An integrated GPU
    is not: the "video memory" it reports is a carve-out of system RAM that it grows past,
    so only the operator's figure limits it."""
    cap = config.gpu.balance.per_igpu if g.integrated else config.gpu.balance.per_dgpu
    if cap:
        return cap
    if g.discrete and g.vram_mb:
        return max(0, (g.vram_mb - config.gpu.vram_reserve_mb) // VRAM_PER_ANDROID_MB)
    return None


def balanced_order(cards: list[GpuInfo], config: Config, counts: dict[str, int] | None = None,
                   previous: str = "") -> tuple[list[GpuInfo], list[GpuInfo]]:
    """(cards with room, in the order to try them; cards that are full).

    The emptiest card comes first, measured as running Androids over what the card takes,
    so a large card gets proportionally more than a small one. A card without a limit of
    its own is measured against the largest limit among the others (against nothing when
    no card has one: then it is a plain count). Ties go to gpu.prefer, then node order.
    `previous`, the card this Android used last, stays first while it has room: moving an
    Android to another card rebuilds its container."""
    counts = counts or {}
    slots = {g.node: card_slots(g, config) for g in cards}
    nominal = max((n for n in slots.values() if n), default=1)
    free = [g for g in cards if slots[g.node] is None or counts.get(g.node, 0) < slots[g.node]]
    full = [g for g in cards if g not in free]
    free.sort(key=lambda g: (g.node != previous, counts.get(g.node, 0) / (slots[g.node] or nominal),
                             g.kind != config.gpu.prefer, cards.index(g)))
    return free, full


def _room(g: GpuInfo, config: Config, counts: dict[str, int]) -> str:
    limit = card_slots(g, config)
    return f"{counts.get(g.node, 0)} of {limit}" if limit is not None else f"{counts.get(g.node, 0)}, no limit"


def _balanced(r: Resolved, config: Config, paths: Paths, gpus: list[GpuInfo], runtime: dict,
              counts: dict[str, int] | None, start_relay: Callable[[GpuInfo], str]) -> GpuChoice:
    counts = counts or {}
    skipped: list[str] = []
    cards = [g for g in gpus if g.usable_modes]
    if any(g.vendor == "nvidia" for g in cards):
        blocker = _nvidia_blocker(runtime)
        if blocker:
            skipped.append(NVIDIA_SKIPPED + blocker)
            cards = [g for g in cards if g.vendor != "nvidia"]
    free, full = balanced_order(cards, config, counts, getattr(counts, "previous", ""))
    for g in free:
        if g.vendor == "nvidia":
            why = start_relay(g)
            if why:
                skipped.append(NVIDIA_SKIPPED + why)
                continue
        how = "through the Venus relay" if g.vendor == "nvidia" else f"via Mesa ({g.driver})"
        reason = "; ".join([f"balanced: {g.name} {how}, the emptiest card ({_room(g, config, counts)} in use)", *skipped])
        return _nvidia(g, config, paths, reason) if g.vendor == "nvidia" else _mesa(g, reason)
    if not full:
        return _software("; ".join(["balanced: no usable GPU, drawing on the CPU", *skipped]))
    taken = ", ".join(f"{display_name(g)} {_room(g, config, counts)}" for g in full)
    if config.gpu.balance.overflow == "refuse":
        raise ValueError(f"gpu mode 'balanced': every card is full ({taken}) and gpu.balance.overflow is 'refuse'. "
                         "Stop some Androids, raise gpu.balance.per_dgpu / per_igpu, or set gpu.balance.overflow "
                         "to software")
    return _software("; ".join([f"balanced: every card is full ({taken}), drawing on the CPU", *skipped]))


def resolve_pin(pin: str, gpus: list[GpuInfo]) -> str:
    """The render node a pinned card has now. A pin is a node, a PCI address or a
    /dev/dri/by-path link; the last two keep naming the same card when the kernel hands
    out the node numbers in another order at the next boot. Unknown pins come back as
    they are, for the caller's error message."""
    m = re.match(r"^/dev/dri/by-path/pci-([0-9a-f:.]+)-render$", pin)
    wanted = pci_key(m.group(1) if m else pin)
    for g in gpus:
        if pin == g.node or (wanted and pci_key(g.pci) == wanted):
            return g.node
    return pin


def _fits(g: GpuInfo, mode: str) -> bool:
    """Whether a usable card may serve a requested mode."""
    if not g.usable_modes:
        return False
    if mode in KIND_MODES:
        return g.kind == mode
    return mode in g.usable_modes


def _tiers(mode: str, prefer: str) -> tuple[str, ...]:
    """The requested mode as an ordered list of modes to try."""
    if mode == "auto":
        return ("dgpu", "igpu") if prefer == "dgpu" else ("igpu", "dgpu")
    return (mode,)


def _serve(mode: str, cards: list[GpuInfo], prefer: str, runtime: dict,
           pick: Callable[[list[GpuInfo]], GpuInfo],
           start_relay: Callable[[GpuInfo], str]) -> tuple[GpuInfo | None, str, list[str]]:
    """The card that serves `mode` now: (card, tier it came from, why others were passed over).

    Within a tier NVIDIA cards are tried first (they are the fastest when the relay is
    there); a card whose relay is not built or does not start is passed over with the
    reason, never silently."""
    skipped: list[str] = []
    for tier in _tiers(mode, prefer):
        fitting = [g for g in cards if _fits(g, tier)]
        nvidia = [g for g in fitting if g.vendor == "nvidia"]
        if nvidia:
            blocker = _nvidia_blocker(runtime)
            if not blocker:
                g = pick(nvidia)
                blocker = start_relay(g)
                if not blocker:
                    return g, tier, skipped
            skipped.append(NVIDIA_SKIPPED + blocker)
        mesa = [g for g in fitting if g.vendor != "nvidia"]
        if mesa:
            return pick(mesa), tier, skipped
    return None, "", skipped


def available_modes(gpus: list[GpuInfo], runtime: dict, prefer: str = "dgpu") -> list[str]:
    """Modes that would work right now, in the order the panel lists them."""
    def first(c: list[GpuInfo]) -> GpuInfo:
        return c[0]
    return ["auto", "balanced", "software"] + [m for m in (*KIND_MODES, *VENDOR_MODES)
                                   if _serve(m, gpus, prefer, runtime, first, lambda g: "")[0] is not None]


def _what_is_missing(mode: str, pool: list[GpuInfo], forced: str) -> str:
    what = {"igpu": "an integrated GPU", "dgpu": "a discrete GPU", "intel": "an Intel GPU with a Mesa driver",
            "amd": "an AMD GPU with a Mesa driver", "nvidia": "an NVIDIA card with NVIDIA's own driver"}[mode]
    if forced:
        g = pool[0]
        kind = f"{KIND_LABEL[g.kind]} " if g.kind else ""
        return (f"{forced} is {display_name(g)} ({kind}{VENDOR_LABEL.get(g.vendor, g.vendor)} card, driver "
                f"{g.driver or 'none'}), not {what}: it cannot be used in {mode!r} mode"
                + (f" ({g.note})" if g.note else ""))
    notes = "; ".join(dict.fromkeys(g.note for g in pool if g.note))
    return f"gpu mode {mode!r} needs {what}, and this machine has none" + (f" ({notes})" if notes else "")


def choose(r: Resolved, config: Config, paths: Paths, *, counts: dict[str, int] | None = None,
           gpus: list[GpuInfo] | None = None, runtime: dict | None = None,
           ensure_relay: Callable[[Config, Paths, str], str] | None = venus.ensure_relay) -> GpuChoice:
    """Decide how Android `r` draws.

    counts        running Androids per render node, for `least-instances` assignment
    gpus/runtime  detection results, injectable so the function is testable without hardware
    ensure_relay  called for NVIDIA choices so the relay is up before the Android starts;
                  pass None to only plan (no process is started)

    `auto` resolves to the best mode that works *now*, falling back to the CPU. A mode that
    was asked for by name and is unavailable raises ValueError saying what is missing and
    which choices this machine does offer.
    """
    mode = r.gpu_mode
    if mode == "software":
        return _software("software rendering was requested")
    if gpus is None:
        gpus = detect_gpus()
        if mode == "balanced":
            gpus = nvidia_vram(gpus)         # how many a card takes comes from its memory
    runtime = venus.runtime_status(config, paths) if runtime is None else runtime
    valid = f"Valid choices on this machine: {', '.join(available_modes(gpus, runtime, config.gpu.prefer))}."
    forced = resolve_pin(r.gpu_node, gpus) if r.gpu_node else ""
    pool = gpus
    if forced:
        if mode == "balanced":
            mode = "auto"                    # a pinned card leaves nothing to share
        pool = [g for g in gpus if g.node == forced]
        if not pool:
            known = ", ".join(g.node for g in gpus) or "none"
            raise ValueError(f"the pinned card {forced} does not exist on this machine (found: {known}); "
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

    if mode == "balanced":
        return _balanced(r, config, paths, pool, runtime, counts, start_relay)
    card, tier, skipped = _serve(mode, pool, config.gpu.prefer, runtime,
                                 lambda cands: _pick(cands, r, config, counts), start_relay)
    if card is None:
        if mode == "auto":
            if forced:
                why = skipped[0] if skipped else (pool[0].note or "it cannot be used by Android")
                raise ValueError(f"render node {forced} was requested but cannot be used: {why}. {valid}")
            return _software("; ".join(["auto: no usable GPU, drawing on the CPU", *skipped]))
        if skipped:
            # The card is there; what serves it is not.
            reasons = "; ".join(s.removeprefix(NVIDIA_SKIPPED) for s in skipped)
            raise ValueError(f"gpu mode {mode!r} is not available: {reasons}. {valid}")
        raise ValueError(f"{_what_is_missing(mode, pool, forced)}. {valid}")

    how = "through the Venus relay" if card.vendor == "nvidia" else f"via Mesa ({card.driver})"
    lead = f"auto: {KIND_LABEL[tier]} " if mode == "auto" else ""
    reason = "; ".join([f"{lead}{card.name} {how}", *skipped])
    return _nvidia(card, config, paths, reason) if card.vendor == "nvidia" else _mesa(card, reason)


def default_choice(config: Config, paths: Paths, *, gpus: list[GpuInfo] | None = None,
                   runtime: dict | None = None) -> tuple[GpuChoice | None, str]:
    """How an Android with the farm's default settings (gpu.mode, gpu.prefer,
    gpu.render_node) would draw if it were started now: (choice, "") or (None, why it
    could not start). This is choose() itself, in planning mode, so the capacity estimate
    and a real start cannot disagree. Starts nothing."""
    try:
        return choose(resolve(InstanceSpec(id=1), config), config, paths, gpus=gpus, runtime=runtime,
                      ensure_relay=None), ""
    except ValueError as e:
        return None, str(e)


def options(config: Config, paths: Paths, *, gpus: list[GpuInfo] | None = None,
            runtime: dict | None = None) -> list[dict]:
    """Every rendering choice for this machine, for the per-Android and bulk pickers.

    Each entry is {value, label, available, reason, node}. `value` is a gpu mode. Entries
    with an empty `node` are the modes; they are followed by one entry per card (`node` set,
    `value` the card's vendor mode): choosing one means gpu_mode=value and gpu_node=node,
    which pins that exact card. `reason` says why an entry is unavailable. Starts nothing."""
    gpus = detect_gpus() if gpus is None else gpus
    runtime = venus.runtime_status(config, paths) if runtime is None else runtime
    prefer = config.gpu.prefer

    def first(cands: list[GpuInfo]) -> GpuInfo:
        return cands[0]

    def entry(value: str, title: str, cards: list[GpuInfo], node: str = "") -> dict:
        card, _, skipped = _serve(value, cards, prefer, runtime, first, lambda g: "")
        named = [display_name(g) for g in cards if _fits(g, value)]
        if card is not None:
            reason = ""
        elif skipped:
            reason = "; ".join(s.removeprefix(NVIDIA_SKIPPED) for s in skipped)
        else:
            reason = _what_is_missing(value, cards, node)
        label = title + (f": {', '.join(dict.fromkeys(named))}" if named and not node else "")
        return {"value": value, "label": label, "available": card is not None, "reason": reason, "node": node}

    auto, _, auto_skipped = _serve("auto", gpus, prefer, runtime, first, lambda g: "")
    out = [
        {"value": "auto", "label": f"Automatic (now: {card_label(auto) if auto else SOFTWARE_LABEL})",
         "available": True, "reason": "; ".join(auto_skipped), "node": ""},
        {"value": "balanced", "label": _balanced_label(gpus, runtime), "available": True, "reason": "", "node": ""},
        {"value": "software", "label": SOFTWARE_LABEL, "available": True, "reason": "", "node": ""},
        entry("igpu", "Integrated GPU", gpus),
        entry("dgpu", "Discrete GPU", gpus),
        entry("intel", "Intel GPU (Mesa)", gpus),
        entry("amd", "AMD GPU (Mesa)", gpus),
        entry("nvidia", "NVIDIA GPU (relay)", gpus),
    ]
    for g in gpus:
        if g.vendor not in VENDOR_MODES:
            continue                 # no mode can name it (a virtual GPU, for instance)
        out.append(entry(g.vendor, f"{card_label(g)} on {os.path.basename(g.node)}", [g], g.node))
    return out


def _balanced_label(gpus: list[GpuInfo], runtime: dict) -> str:
    blocked = _nvidia_blocker(runtime)
    cards = [g for g in gpus if g.usable_modes and not (g.vendor == "nvidia" and blocked)]
    if not cards:
        return "Balanced (no usable GPU now: the CPU)"
    return f"Balanced: shared across {len(cards)} GPU{'s' if len(cards) != 1 else ''}, then the CPU"


def summary(config: Config, paths: Paths, gpus: list[GpuInfo] | None = None) -> dict:
    """What the panel's host page shows: cards, modes available now, and the auto pick."""
    gpus = nvidia_vram(detect_gpus()) if gpus is None else gpus
    runtime = venus.runtime_status(config, paths)
    # `auto` and `balanced` always work and are no statement about the hardware, so they
    # are left out here.
    available = [m for m in available_modes(gpus, runtime, config.gpu.prefer) if m not in ("auto", "balanced")]
    return {"gpus": [g.__dict__ for g in gpus], "available_modes": available,
            "nvidia_runtime": runtime, "nvidia_blocker": _nvidia_blocker(runtime) if any(
                g.vendor == "nvidia" for g in gpus) else ""}

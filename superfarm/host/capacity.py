"""How many Androids fit on this machine, and what runs out first.

Every number below was measured on one reference machine (a 32-thread hybrid desktop CPU,
32 GB RAM, one 16 GB NVIDIA card plus integrated Intel graphics; Android 13 at 480x854,
`--cpus 2 --memory 1536m`). They are honest starting points, not guarantees: a different
app mix moves them a lot. Where something was *not* measured it says so.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from ..config import Config
from .gpu import GpuInfo, planned_mode

# ----------------------------------------------------------------- measured: memory
# System RAM one booted Android costs. Source: relay memory test, 927-944 MB per Android
# with the stock package set; live fleets settled between 0.9 and 1.1 GB per Android in use.
RAM_PER_ANDROID_MB = 1000
# Extra system RAM per Android in NVIDIA mode, mostly inside the NVIDIA driver.
# Source: 13 Androids in the same live video, 27 GB used on NVIDIA against 23 GB on Intel.
RAM_NVIDIA_EXTRA_MB = 300
# Saved per Android by disabling the 38 unused system packages (1,079 -> 908 MB).
RAM_TRIM_SAVING_MB = 170
# What the host needs for itself (kernel, Docker, the daemon, a desktop) is a setting,
# performance.reserve_host_mb, not a measurement: the reference machine's full desktop
# session with browsers used close to 10 GB before any Android started, which is why
# "13 in a live video" was its ceiling on 32 GB.

# ----------------------------------------------------------------- measured: CPU
# Percent of one CPU thread per Android. "light" = scrolling a settings screen (13 Androids);
# "video" = a live video stream playing (13 Androids, ARM app translated to x86).
# In video, what remains on a GPU mode is decoding and ARM translation, which no GPU removes.
CPU_PERCENT = {
    "software": {"light": 78, "video": 148},
    "intel": {"light": 13, "video": 55},
    "nvidia": {"light": 15, "video": 66},
    # Not measured (no AMD card on the reference machine). Mesa path like Intel: assumed equal.
    "amd": {"light": 13, "video": 55},
}
# Share of all threads the farm should plan to use. Above this, frame times and ADB latency
# degrade for every Android at once (the video benchmark ran the CPU at 85-90 C at 63%).
CPU_TARGET_UTILISATION = 0.80
# 25 -> 15 fps saved only about 4 points of CPU in video (91% -> 87%): fps is a weak lever
# for video, a strong one only for UI-heavy screens.
FPS_VIDEO_SAVING_PERCENT = 4

# ----------------------------------------------------------------- measured once: VRAM
# 17 NVIDIA Androids idling on an app's home screen had 7.6 GB of video memory in use on a
# 16 GB card; the desktop alone held about 1.5 GB of that card before any Android started.
# (7.6 - 1.5) / 17 = about 360 MB per Android at 480x854. One measurement, one app, one
# resolution: treat it as a planning figure and watch `nvidia-smi` on your own fleet.
VRAM_PER_ANDROID_MB = 360
VRAM_RESERVE_MB = 1536          # what a desktop session (compositor, browsers) holds on the card
VRAM_MEASURED = True

# ----------------------------------------------------------------- measured once: zram
# Compressed swap in RAM (16 GB, zstd, swap priority 100, vm.swappiness=150) on the 32 GB
# reference desktop, Androids idle on an app's home screen, NVIDIA mode:
#     10 Androids  1.9 GB swapped   ratio 4.2 : 1
#     14 Androids  7.2 GB swapped   ratio 4.5 : 1
#     17 Androids 11.7 GB swapped   ratio 5.1 : 1   (10.3 GB of it held in 2.0 GB of zram),
#                                                   all healthy, 3.4 GB still available
# Without zram about 13 was that machine's ceiling. Live video was NOT measured with zram and
# needs more memory per Android than a home screen.
ZRAM_RATIO = 4.2                        # the lowest ratio seen, to stay on the safe side
ZRAM_SWAPPED_PER_ANDROID_MB = 500       # idle memory per Android that ended up in swap under pressure (514 MB at 14)
ZRAM_MEASURED_ANDROIDS = 17             # the largest fleet actually run this way
ZRAM_SWAPPINESS = 150                   # the vm.swappiness the measurement ran with

WORKLOADS = ("light", "video")


def meminfo(root: Path | str = "/") -> dict[str, int]:
    """MemTotal, MemAvailable, SwapTotal ... in MiB."""
    out: dict[str, int] = {}
    try:
        text = (Path(root) / "proc/meminfo").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0]) // 1024
    return out


def zram_size_mb(root: Path | str = "/") -> int:
    """Total size of the zram devices that are in use as swap (from /proc/swaps, in MiB)."""
    total = 0
    try:
        lines = (Path(root) / "proc/swaps").read_text().splitlines()
    except OSError:
        return 0
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("/dev/zram") and parts[2].isdigit():
            total += int(parts[2]) // 1024
    return total


def zram_active(root: Path | str = "/") -> bool:
    try:
        return any(line.startswith("/dev/zram") for line in (Path(root) / "proc/swaps").read_text().splitlines())
    except OSError:
        return False


def _swappiness(root: Path | str = "/") -> int:
    try:
        return int((Path(root) / "proc/sys/vm/swappiness").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return -1


def per_android_ram_mb(mode: str, *, trimmed: bool, limit_mb: int = 0) -> int:
    ram = RAM_PER_ANDROID_MB + (RAM_NVIDIA_EXTRA_MB if mode == "nvidia" else 0)
    if trimmed:
        ram -= RAM_TRIM_SAVING_MB
    # NVIDIA's share lives in the relay's helper processes on the host, outside the
    # container's memory limit, so the limit only caps the Android part.
    return ram if not limit_mb else min(ram, limit_mb + (RAM_NVIDIA_EXTRA_MB if mode == "nvidia" else 0))


def _best_mode(config: Config, gpus: list[GpuInfo]) -> str:
    """The concrete backend the farm's default mode aims for, judged from the cards alone
    (no file checks: this is a planning figure, the doctor reports whether the NVIDIA
    runtime is actually built). auto / igpu / dgpu are resolved to a vendor here."""
    return planned_mode(config, gpus)


def estimate(config: Config, gpus: list[GpuInfo], *, workload: str = "video", root: Path | str = "/",
             cpu_threads: int | None = None, running: int | None = None) -> dict:
    """Max Androids by RAM, CPU and VRAM for the configured GPU mode, the binding
    constraint, and advice ordered by how much it buys.

    workload   "video" (worst case measured) or "light" (UI only)
    running    Androids running right now, when the caller knows. Lets the estimate tell
               farm memory apart from everything else the machine is doing (a desktop
               session with browsers easily holds 10 GB) and adds `max_now`: what fits
               while that other use stays as it is.
    """
    if workload not in WORKLOADS:
        raise ValueError(f"workload must be one of {WORKLOADS}")
    mem = meminfo(root)
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    threads = cpu_threads or os.cpu_count() or 1
    mode = _best_mode(config, gpus)
    trimmed = config.performance.trim_packages
    per_ram = per_android_ram_mb(mode, trimmed=trimmed, limit_mb=config.resources.memory_mb)
    reserve = config.performance.reserve_host_mb
    zram = zram_active(root)

    by_ram = max(0, (total - reserve) // per_ram) if total else 0
    more_now = max(0, (available - config.performance.memory_guard_mb) // per_ram) if available else 0

    cpu_pct = CPU_PERCENT.get(mode, CPU_PERCENT["software"])[workload]
    # An Android cannot use more than its --cpus cap; software video sat exactly on it.
    cpu_pct = min(cpu_pct, int(config.resources.cpus * 100))
    by_cpu = int(threads * 100 * CPU_TARGET_UTILISATION // cpu_pct)

    ram: dict = {"max": int(by_ram), "more_now": int(more_now), "per_android_mb": per_ram,
                 "total_mb": total, "available_mb": available, "host_reserve_mb": reserve}
    # With compressed swap the idle part of each Android costs 1/ZRAM_RATIO of its size. Two
    # bounds, the lower one wins: how much an idle Android gives up, and how big the zram is.
    zram_mb = zram_size_mb(root) or (total * config.performance.zram.size_percent // 100
                                     if config.performance.zram.enabled else 0)
    if zram_mb and total:
        saved = 1 - 1 / ZRAM_RATIO
        usable = max(0, total - reserve)
        by_idle = usable / max(1.0, per_ram - ZRAM_SWAPPED_PER_ANDROID_MB * saved)
        by_size = (usable + zram_mb * saved) / per_ram
        ram["max_idle_with_zram"] = int(min(by_idle, by_size))
        ram["zram_mb"] = int(zram_mb)
        # Anything above the fleet that was really run is arithmetic, not experience.
        ram["zram_extrapolated"] = ram["max_idle_with_zram"] > ZRAM_MEASURED_ANDROIDS
    if running is not None and total and available:
        ram["max_now"] = int(running + more_now)
        ram["other_use_mb"] = max(0, total - available - running * per_ram)
    limits: dict[str, dict] = {
        "ram": ram,
        "cpu": {"max": by_cpu, "percent_per_android": cpu_pct, "threads": threads,
                "target_utilisation": CPU_TARGET_UTILISATION, "workload": workload},
    }
    if mode == "nvidia":
        cards = [g for g in gpus if "nvidia" in g.usable_modes]
        known = [g for g in cards if g.vram_mb]
        if known:
            by_vram = sum(max(0, (g.vram_mb - VRAM_RESERVE_MB) // VRAM_PER_ANDROID_MB) for g in known)
            limits["vram"] = {"max": int(by_vram), "per_android_mb": VRAM_PER_ANDROID_MB,
                              "total_mb": sum(g.vram_mb for g in known), "measured": VRAM_MEASURED,
                              "cards": len(cards)}

    candidates = {k: v["max"] for k, v in limits.items()}
    binding = min(candidates, key=lambda k: (candidates[k], ("ram", "cpu", "vram").index(k)))
    result = {
        "mode": mode, "workload": workload, "limits": limits, "max_instances": candidates[binding],
        "binding": binding, "zram_active": zram, "swappiness": _swappiness(root),
        "assumptions": {
            "ram_per_android_mb": RAM_PER_ANDROID_MB, "ram_nvidia_extra_mb": RAM_NVIDIA_EXTRA_MB,
            "ram_trim_saving_mb": RAM_TRIM_SAVING_MB, "cpu_percent": CPU_PERCENT[mode] if mode in CPU_PERCENT else {},
            "vram_per_android_mb": VRAM_PER_ANDROID_MB, "vram_measured": VRAM_MEASURED,
            "zram_ratio": ZRAM_RATIO, "zram_measured_androids": ZRAM_MEASURED_ANDROIDS,
            "reference": "Android 13, 480x854, 2 CPUs / 1536 MB per Android, one live-video app",
        },
    }
    result["advice"] = _advice(config, gpus, result)
    return result


def _advice(config: Config, gpus: list[GpuInfo], est: dict) -> list[str]:
    mode, binding, limits = est["mode"], est["binding"], est["limits"]
    out: list[str] = []
    per_ram = limits["ram"]["per_android_mb"]
    if binding == "ram":
        if not config.performance.trim_packages:
            gain = _gain(limits["ram"], per_ram, per_ram - RAM_TRIM_SAVING_MB)
            out.append(f"Turn on performance.trim_packages: about {RAM_TRIM_SAVING_MB} MB less per Android "
                       f"(+{gain} Androids here) and no Bluetooth crash loop at boot.")
        if not est["zram_active"] and not config.performance.zram.enabled:
            out.append("Enable compressed swap (performance.zram.enabled: true, then `sf host apply`). "
                       f"Measured on a 32 GB desktop: {ZRAM_MEASURED_ANDROIDS} idle Androids ran healthy where "
                       f"about 13 fit before, with idle memory compressing {ZRAM_RATIO}-5.1 : 1. That run used "
                       f"vm.swappiness={ZRAM_SWAPPINESS} (performance.swappiness). Live video was not measured "
                       "with it and needs more memory per Android.")
        elif "max_idle_with_zram" in limits["ram"]:
            r = limits["ram"]
            out.append(f"With compressed swap about {r['max_idle_with_zram']} mostly idle Androids fit"
                       + (f" (extrapolated: the largest fleet actually run this way was {ZRAM_MEASURED_ANDROIDS})"
                          if r.get("zram_extrapolated") else "")
                       + f"; Androids playing video stay close to the plain figure of {r['max']}."
                       + ("" if max(config.performance.swappiness, est.get("swappiness", -1)) >= 100 else
                          f" The measured setup ran with vm.swappiness={ZRAM_SWAPPINESS}; set performance.swappiness "
                          "to get the same behaviour."))
        if mode == "nvidia" and any(m in g.usable_modes for g in gpus for m in ("intel", "amd")):
            gain = _gain(limits["ram"], per_ram, per_ram - RAM_NVIDIA_EXTRA_MB)
            out.append(f"Mix modes: NVIDIA costs about {RAM_NVIDIA_EXTRA_MB} MB more RAM per Android than Intel/AMD "
                       f"for the same CPU load. Keeping idle or background Androids on the integrated GPU "
                       f"(gpu_mode per instance) fits about {gain} more.")
        out.append("More RAM is the only lever with no trade-off: budget about "
                   f"{per_ram} MB per Android on top of what the host itself uses.")
    if binding == "cpu":
        if mode == "software" and gpus:
            usable = sorted({m for g in gpus for m in g.usable_modes})
            if usable:
                out.append(f"Use a GPU mode ({', '.join(usable)}): drawing on the CPU costs roughly five times "
                           "the CPU of a GPU mode on UI screens and more than twice in video.")
        elif mode == "software":
            out.append("No usable GPU was found; software rendering is the most expensive mode. Any Intel or "
                       "AMD GPU (integrated is enough) removes more than half the CPU load.")
        out.append(f"Lowering android.fps helps UI-heavy screens; in video it saved only about "
                   f"{FPS_VIDEO_SAVING_PERCENT} points of CPU (25 -> 15 fps), so do not expect much there.")
        out.append("Keep performance.disable_animations on and stagger bulk starts "
                   "(performance.start_concurrency): booting is the heaviest moment for the CPU.")
        if config.resources.cpu_pinning == "none":
            out.append("On hybrid CPUs try resources.cpu_pinning: performance|efficiency so background Androids "
                       "stay off the fast cores. Not benchmarked.")
    if binding == "vram":
        out.append(f"Video memory is the limit: about {VRAM_PER_ANDROID_MB} MB per Android (one measurement, "
                   "480x854, idle app). Watch `nvidia-smi` with a few Androids running; a second card, or "
                   "putting part of the fleet on the integrated GPU, raises it.")
    if mode == "nvidia":
        out.append("NVIDIA mode saves CPU, not power: 13 Androids on the NVIDIA card drew about what 9 drew "
                   "in software mode. The integrated GPU was the cheapest per Android.")
    other = limits["ram"].get("other_use_mb", 0)
    if other > limits["ram"]["host_reserve_mb"] + 2048:
        out.append(f"About {other // 1024} GB is in use by things other than Androids (desktop session, browsers, "
                   f"other services). As long as that stays, about {limits['ram']['max_now']} Androids fit, not "
                   f"{limits['ram']['max']}; a headless or lean host gets the higher figure.")
    if limits["ram"]["more_now"] < limits["ram"]["max"] // 4 and limits["ram"]["max"]:
        out.append(f"Right now only about {limits['ram']['more_now']} more would start: "
                   f"{limits['ram']['available_mb']} MB is available and performance.memory_guard_mb keeps "
                   f"{config.performance.memory_guard_mb} MB free.")
    return out


def _gain(ram: dict, per_now: int, per_then: int) -> int:
    usable = max(0, ram["total_mb"] - ram["host_reserve_mb"])
    return max(0, math.floor(usable / max(per_then, 1)) - math.floor(usable / max(per_now, 1)))

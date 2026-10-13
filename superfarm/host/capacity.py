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
from ..paths import Paths, get_paths
from . import gpu as gpu_mod
from . import venus
from .gpu import GpuInfo

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
VRAM_PER_ANDROID_MB = gpu_mod.VRAM_PER_ANDROID_MB       # 360
VRAM_RESERVE_MB = 1536          # what a desktop session (compositor, browsers) holds on the card;
                                # the default of gpu.vram_reserve_mb, which a headless server sets to 0
VRAM_MEASURED = True
# How far the balanced estimate counts before it calls the answer "more than this".
MIX_CEILING = 4096

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


def _default_mode(config: Config, paths: Paths, gpus: list[GpuInfo], runtime: dict | None) -> dict:
    """The backend an Android with the farm's default settings would get if started now.

    The decision is gpu.choose()'s own (see gpu.default_choice), so an NVIDIA card whose
    runtime is not built counts as what `auto` really falls back to, not as NVIDIA. When
    the configured mode cannot be served at all, such an Android would not start: the
    figures are then those of the mode that was asked for (the CPU for igpu / dgpu, which
    name no backend) and `mode_available` is False."""
    choice, blocked = gpu_mod.default_choice(config, paths, gpus=gpus, runtime=runtime)
    if choice is not None:
        return {"mode": choice.mode, "mode_available": True, "mode_reason": choice.reason}
    wanted = config.gpu.mode
    return {"mode": wanted if wanted in gpu_mod.VENDOR_MODES else "software", "mode_available": False,
            "mode_reason": blocked}


def _balanced_mix(config: Config, gpus: list[GpuInfo], runtime: dict, *, workload: str, threads: int,
                  usable_ram_mb: int, trimmed: bool) -> dict:
    """What gpu.mode `balanced` does with a growing fleet: one Android after another is
    placed the way a start would place it (gpu.balanced_order), each with the CPU and RAM
    of the renderer it lands on, until the CPU and the RAM have both run out. Nothing is
    started. Returns the two ceilings, the averages at the lower one, and who got what."""
    blocked = gpu_mod._nvidia_blocker(runtime)                                   # noqa: SLF001 - same package
    cards = [g for g in gpus if g.usable_modes and not (g.vendor == "nvidia" and blocked)]
    cpu_budget = threads * 100 * CPU_TARGET_UTILISATION
    cpu_cap = int(config.resources.cpus * 100)
    counts: dict[str, int] = {}
    placed: list[tuple[str, str, int, int]] = []           # mode, node, cpu percent, RAM MB
    cpu = ram = 0
    by_cpu: int | None = None
    by_ram: int | None = None if usable_ram_mb else 0
    by_cards: int | None = None
    for n in range(MIX_CEILING):
        free, full = gpu_mod.balanced_order(cards, config, counts)
        if free:
            mode, node = free[0].vendor, free[0].node
        elif full and config.gpu.balance.overflow == "refuse":
            by_cards = n
            break
        else:
            mode, node = "software", ""
        one_cpu = min(CPU_PERCENT.get(mode, CPU_PERCENT["software"])[workload], cpu_cap)
        one_ram = per_android_ram_mb(mode, trimmed=trimmed, limit_mb=config.resources.memory_mb)
        cpu += one_cpu
        ram += one_ram
        if by_cpu is None and cpu > cpu_budget:
            by_cpu = n
        if by_ram is None and ram > usable_ram_mb:
            by_ram = n
        if by_cpu is not None and by_ram is not None:
            break
        placed.append((mode, node, one_cpu, one_ram))
        if node:
            counts[node] = counts.get(node, 0) + 1
    # A ceiling the count never reached (the cards refused first, or the count gave up):
    # what the fleet placed so far costs on average says where it would have been.
    if by_cpu is None:
        by_cpu = int(cpu_budget * len(placed) // max(1, sum(x[2] for x in placed))) if placed else 0
    if by_ram is None:
        by_ram = int(usable_ram_mb * len(placed) // max(1, sum(x[3] for x in placed))) if placed else 0
    fits = placed[:min(by_cpu, by_ram, *([by_cards] if by_cards is not None else []))]
    by_node = {g.node: g for g in cards}
    shares: dict[tuple[str, str], dict] = {}
    for mode, node, _cpu, _ram in fits:
        g = by_node.get(node)
        entry = shares.setdefault((mode, node), {
            "mode": mode, "node": node, "label": gpu_mod.card_label(g) if g else gpu_mod.SOFTWARE_LABEL,
            "slots": gpu_mod.card_slots(g, config) if g else None, "count": 0})
        entry["count"] += 1
    first = placed[0] if placed else ("software", "", min(CPU_PERCENT["software"][workload], cpu_cap),
                                      per_android_ram_mb("software", trimmed=trimmed, limit_mb=config.resources.memory_mb))
    return {
        "by_cpu": by_cpu, "by_ram": by_ram, "by_cards": by_cards, "shares": list(shares.values()),
        # Averages over the fleet that fits; of the first Android when none does.
        "cpu_percent": round(sum(x[2] for x in fits) / len(fits)) if fits else first[2],
        "ram_mb": round(sum(x[3] for x in fits) / len(fits)) if fits else first[3],
    }


def estimate(config: Config, gpus: list[GpuInfo], *, workload: str = "video", root: Path | str = "/",
             cpu_threads: int | None = None, running: int | None = None, paths: Paths | None = None,
             runtime: dict | None = None) -> dict:
    """Max Androids by RAM, CPU and VRAM, the binding constraint, and advice ordered by how
    much it buys. The figures are for the rendering mode an Android with the farm's default
    settings would really get now (`mode`, with `mode_reason` saying why that one).

    workload   "video" (worst case measured) or "light" (UI only)
    running    Androids running right now, when the caller knows. Lets the estimate tell
               farm memory apart from everything else the machine is doing (a desktop
               session with browsers easily holds 10 GB) and adds `max_now`: what fits
               while that other use stays as it is.
    paths      the farm home, where the NVIDIA runtime is looked for; default: the home
               from $SUPERFARM_HOME, as everywhere else
    runtime    venus.runtime_status(), when the caller already has it (tests)
    """
    if workload not in WORKLOADS:
        raise ValueError(f"workload must be one of {WORKLOADS}")
    mem = meminfo(root)
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    threads = cpu_threads or os.cpu_count() or 1
    paths = paths or get_paths()
    runtime = venus.runtime_status(config, paths) if runtime is None else runtime
    drawing = _default_mode(config, paths, gpus, runtime)
    mode = drawing["mode"]
    trimmed = config.performance.trim_packages
    per_ram = per_android_ram_mb(mode, trimmed=trimmed, limit_mb=config.resources.memory_mb)
    reserve = config.performance.reserve_host_mb
    zram = zram_active(root)

    by_ram = max(0, (total - reserve) // per_ram) if total else 0

    cpu_pct = CPU_PERCENT.get(mode, CPU_PERCENT["software"])[workload]
    # An Android cannot use more than its --cpus cap; software video sat exactly on it.
    cpu_pct = min(cpu_pct, int(config.resources.cpus * 100))
    by_cpu = int(threads * 100 * CPU_TARGET_UTILISATION // cpu_pct)

    # balanced has no single renderer: the fleet is a mix, counted Android by Android.
    mix = None
    if config.gpu.mode == "balanced" and drawing["mode_available"]:
        mix = _balanced_mix(config, gpus, runtime, workload=workload, threads=threads,
                            usable_ram_mb=max(0, total - reserve), trimmed=trimmed)
        mode, per_ram, cpu_pct, by_ram, by_cpu = "balanced", mix["ram_mb"], mix["cpu_percent"], mix["by_ram"], mix["by_cpu"]
    more_now = max(0, (available - config.performance.memory_guard_mb) // per_ram) if available else 0

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
            by_vram = sum(max(0, (g.vram_mb - config.gpu.vram_reserve_mb) // VRAM_PER_ANDROID_MB) for g in known)
            limits["vram"] = {"max": int(by_vram), "per_android_mb": VRAM_PER_ANDROID_MB,
                              "total_mb": sum(g.vram_mb for g in known), "measured": VRAM_MEASURED,
                              "cards": len(cards), "reserve_mb": config.gpu.vram_reserve_mb}
    if mix and mix["by_cards"] is not None:
        # Only with gpu.balance.overflow: refuse; otherwise the CPU takes what the cards cannot.
        limits["cards"] = {"max": int(mix["by_cards"]), "cards": len(mix["shares"])}

    candidates = {k: v["max"] for k, v in limits.items()}
    binding = min(candidates, key=lambda k: (candidates[k], ("ram", "cpu", "vram", "cards").index(k)))
    result = {
        "mode": mode, "requested_mode": config.gpu.mode, "mode_available": drawing["mode_available"],
        "mode_reason": drawing["mode_reason"], "workload": workload, "limits": limits, "max_instances": candidates[binding],
        "binding": binding, "zram_active": zram, "swappiness": _swappiness(root),
        "assumptions": {
            "ram_per_android_mb": RAM_PER_ANDROID_MB, "ram_nvidia_extra_mb": RAM_NVIDIA_EXTRA_MB,
            "ram_trim_saving_mb": RAM_TRIM_SAVING_MB, "cpu_percent": CPU_PERCENT[mode] if mode in CPU_PERCENT else {},
            "vram_per_android_mb": VRAM_PER_ANDROID_MB, "vram_measured": VRAM_MEASURED,
            "zram_ratio": ZRAM_RATIO, "zram_measured_androids": ZRAM_MEASURED_ANDROIDS,
            "reference": "Android 13, 480x854, 2 CPUs / 1536 MB per Android, one live-video app",
        },
    }
    if mix:
        result["mix"] = mix["shares"]
    result["advice"] = _advice(config, gpus, result)
    return result


def _advice(config: Config, gpus: list[GpuInfo], est: dict) -> list[str]:
    mode, binding, limits = est["mode"], est["binding"], est["limits"]
    out: list[str] = []
    per_ram = limits["ram"]["per_android_mb"]
    reason = est["mode_reason"]
    if not est["mode_available"]:
        out.append(f"No Android can start with the farm's rendering mode ({est['requested_mode']}) right now: "
                   f"{reason} The figures here are for {mode} mode.")
    elif est.get("mix") is not None and gpu_mod.NVIDIA_SKIPPED in reason:
        why = reason.split(gpu_mod.NVIDIA_SKIPPED, 1)[1]
        out.append(f"The NVIDIA card is not part of the mix yet ({why}). Once it is, it takes its share, and each "
                   f"Android on it needs about {RAM_NVIDIA_EXTRA_MB} MB more RAM.")
    elif gpu_mod.NVIDIA_SKIPPED in reason:
        # Said first and in full: the operator sees an NVIDIA card in the machine and would
        # otherwise read these figures as that card's.
        out.append(f"The figures are for {mode} mode, because that is what an Android started now would get "
                   f"({reason}). Once NVIDIA mode works, each Android needs about {RAM_NVIDIA_EXTRA_MB} MB more "
                   "RAM and video memory becomes a limit too.")
    if est.get("mix"):
        parts = [f"{share['count']} on {share['label']}" for share in est["mix"]]
        on_cpu = sum(share["count"] for share in est["mix"] if share["mode"] == "software")
        out.append("Balanced mode shares them: " + ", ".join(parts) + "."
                   + (f" The {on_cpu} on the CPU cost the most: {CPU_PERCENT['software'][est['workload']]}% of a thread "
                      f"each in this workload, against {CPU_PERCENT['intel'][est['workload']]} to "
                      f"{CPU_PERCENT['nvidia'][est['workload']]} on a card." if on_cpu else ""))
    if binding == "cards":
        out.append("Every card is full and gpu.balance.overflow is `refuse`: set it to `software` to let further "
                   "Androids draw on the CPU, or raise gpu.balance.per_dgpu / per_igpu if the cards have room.")
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
        usable = sorted({m for g in gpus for m in g.usable_modes})
        if mode == "software" and usable and est["requested_mode"] == "software":
            out.append(f"Use a GPU mode ({', '.join(usable)}): drawing on the CPU costs roughly five times "
                       "the CPU of a GPU mode on UI screens and more than twice in video.")
        elif mode == "software" and not usable:
            out.append("No usable GPU was found; software rendering is the most expensive mode. Any Intel or "
                       "AMD GPU (integrated is enough) removes more than half the CPU load.")
        # Otherwise a card is there and could not be used: the first line of the advice says why.
        out.append(f"Lowering android.fps helps UI-heavy screens; in video it saved only about "
                   f"{FPS_VIDEO_SAVING_PERCENT} points of CPU (25 -> 15 fps), so do not expect much there.")
        out.append("Keep performance.disable_animations on and stagger bulk starts "
                   "(performance.start_concurrency): booting is the heaviest moment for the CPU.")
        if config.resources.cpu_pinning == "none":
            out.append("On hybrid CPUs try resources.cpu_pinning: performance|efficiency so background Androids "
                       "stay off the fast cores. Not benchmarked.")
    if binding == "vram":
        out.append(f"Video memory is the limit: about {VRAM_PER_ANDROID_MB} MB per Android (one measurement, "
                   "480x854, idle app). Watch `nvidia-smi` with a few Androids running. A second card raises it, and "
                   "so does gpu.mode `balanced`, which puts what the card cannot hold on the other GPUs and the CPU."
                   + ("" if config.gpu.vram_reserve_mb == 0 else
                      f" {config.gpu.vram_reserve_mb} MB per card is kept for a desktop session "
                      "(gpu.vram_reserve_mb); a headless server can set it to 0."))
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

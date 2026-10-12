"""What the machine and the farm are using right now.

farm_metrics() is what the panel's dashboard polls: host CPU, memory, load and temperatures,
every GPU's load and video memory, and one `docker stats` sample for the running Androids.
Everything is read from /proc and /sys (no extra dependency); the readers take the root
folder as an argument so they can be tested against a made-up tree.

This file also holds the one piece of hardware discovery the planner needs but, being pure,
cannot do itself: reading the CPU topology for core pinning.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..util.proc import CommandError, run, which
from .planner import CpuTopology, build_topology, parse_cpu_list

if TYPE_CHECKING:
    from .instances import InstanceManager

_VENDORS = {"0x8086": "intel", "0x1002": "amd", "0x10de": "nvidia"}
# Sensors that describe the processor; a farm's first thermal limit is always the CPU.
_CPU_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "x86_pkg_temp", "acpitz")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _int(text: str) -> int | None:
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return None


# ------------------------------------------------------------------------------- CPU

def read_cpu_topology(sys_devices: Path = Path("/sys/devices")) -> CpuTopology:
    """Cores, their hardware threads and (on hybrid CPUs) which are performance or efficiency.

    The kernel names the two classes directly on Intel hybrid parts (cpu_core / cpu_atom);
    elsewhere the cores' top frequencies tell them apart. An unreadable tree gives an empty
    topology, which the allocator treats as "do not pin"."""
    cpu_root = sys_devices / "system" / "cpu"
    try:
        online = parse_cpu_list(_read(cpu_root / "online"))
    except ValueError:
        online = []
    if not online:
        online = sorted(int(p.name[3:]) for p in cpu_root.glob("cpu[0-9]*") if p.name[3:].isdigit())
    siblings: dict[int, list[int]] = {}
    max_freq: dict[int, int] = {}
    for cpu in online:
        base = cpu_root / f"cpu{cpu}"
        text = _read(base / "topology" / "thread_siblings_list") or _read(base / "topology" / "core_cpus_list")
        try:
            group = [c for c in parse_cpu_list(text) if c in online] or [cpu]
        except ValueError:
            group = [cpu]
        siblings[cpu] = group
        freq = _int(_read(base / "cpufreq" / "cpuinfo_max_freq"))
        if freq:
            max_freq[cpu] = freq

    def kernel_class(name: str) -> list[int]:
        try:
            return [c for c in parse_cpu_list(_read(sys_devices / name / "cpus")) if c in online]
        except ValueError:
            return []

    return build_topology(siblings, max_freq=max_freq, performance=kernel_class("cpu_core"),
                          efficiency=kernel_class("cpu_atom"))


def parse_cpu_times(stat_text: str) -> tuple[int, int]:
    """(idle, total) jiffies from the first line of /proc/stat."""
    for line in stat_text.splitlines():
        if line.startswith("cpu "):
            fields = [int(x) for x in line.split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)       # idle + iowait
            return idle, sum(fields[:8])                                   # guest time is already inside user
    return 0, 0


def cpu_percent(previous: tuple[int, int], current: tuple[int, int]) -> float:
    """Busy share of the whole machine between two samples, 0-100."""
    idle, total = current[0] - previous[0], current[1] - previous[1]
    if total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 1)


class _CpuSampler:
    """CPU load is a difference between two readings. The reading from the previous call is
    kept, so a dashboard polling every few seconds gets the average over its own interval
    and no call has to sleep (except the very first)."""

    def __init__(self, stat: Path = Path("/proc/stat")):
        self.stat = stat
        self._lock = threading.Lock()
        self._last: tuple[int, int] | None = None
        self._last_at = 0.0
        self._value = 0.0

    def sample(self) -> float:
        with self._lock:
            now = time.monotonic()
            if self._last is None:
                self._last = parse_cpu_times(_read(self.stat))
                time.sleep(0.15)
                now = time.monotonic()
            elif now - self._last_at < 0.5:
                return self._value          # too close together to be meaningful; reuse
            current = parse_cpu_times(_read(self.stat))
            self._value = cpu_percent(self._last, current)
            self._last, self._last_at = current, now
            return self._value


_sampler = _CpuSampler()


# ---------------------------------------------------------------------------- memory

def parse_meminfo(text: str) -> dict:
    """/proc/meminfo -> MiB figures. `used` is total minus *available* (not minus free):
    page cache the kernel can drop at once is not memory an Android has to fight for."""
    kb: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        value = _int(rest.split()[0]) if rest.split() else None
        if value is not None:
            kb[key.strip()] = value
    total, available = kb.get("MemTotal", 0), kb.get("MemAvailable", kb.get("MemFree", 0))
    return {
        "total_mb": total // 1024, "available_mb": available // 1024, "used_mb": max(0, total - available) // 1024,
        "swap_total_mb": kb.get("SwapTotal", 0) // 1024, "swap_free_mb": kb.get("SwapFree", 0) // 1024,
        "used_percent": round(100.0 * (total - available) / total, 1) if total else 0.0,
    }


# ----------------------------------------------------------------------- temperature

def read_temps(hwmon: Path = Path("/sys/class/hwmon")) -> list[dict]:
    """The hottest reading of each processor sensor, in degrees Celsius."""
    out: list[dict] = []
    try:
        chips = sorted(hwmon.iterdir())
    except OSError:
        return out
    for chip in chips:
        name = _read(chip / "name")
        if name not in _CPU_SENSORS:
            continue
        best: tuple[float, str] | None = None
        for entry in sorted(chip.glob("temp*_input")):
            milli = _int(_read(entry))
            if milli is None or not -40_000 < milli < 150_000:
                continue
            label = _read(entry.with_name(entry.name.replace("_input", "_label")))
            if best is None or milli / 1000 > best[0]:
                best = (milli / 1000, label)
        if best:
            out.append({"sensor": name, "label": best[1], "celsius": round(best[0], 1)})
    return out


# ------------------------------------------------------------------------------- GPU

def parse_nvidia_smi(text: str) -> list[dict]:
    """Rows of `nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,
    temperature.gpu,power.draw --format=csv,noheader,nounits`."""
    def number(cell: str) -> float | None:
        try:
            return float(cell)
        except ValueError:
            return None            # "[N/A]" on cards that do not report a value

    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 7 or not cells[0].isdigit():
            continue
        out.append({"index": int(cells[0]), "vendor": "nvidia", "driver": "nvidia", "name": cells[1],
                    "util_percent": number(cells[2]), "vram_used_mb": number(cells[3]),
                    "vram_total_mb": number(cells[4]), "temp_c": number(cells[5]), "power_w": number(cells[6])})
    return out


def read_nvidia() -> list[dict]:
    if not which("nvidia-smi"):
        return []
    try:
        r = run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits"], timeout=8, check=False)
    except CommandError:
        return []          # a hung driver must not hang the dashboard
    return parse_nvidia_smi(r.stdout) if r.ok else []


def read_sysfs_gpus(drm: Path = Path("/sys/class/drm"), *, skip_vendors: tuple[str, ...] = ()) -> list[dict]:
    """Intel and AMD cards from sysfs. AMD reports load and video memory; Intel exposes no
    load figure without a profiler, so its current/maximum frequency is given instead."""
    out: list[dict] = []
    try:
        cards = sorted(p for p in drm.iterdir() if p.name.startswith("card") and p.name[4:].isdigit())
    except OSError:
        return out
    for card in cards:
        device = card / "device"
        vendor = _VENDORS.get(_read(device / "vendor").lower(), "")
        if not vendor or vendor in skip_vendors:
            continue
        try:
            driver = os.path.basename(os.readlink(device / "driver"))
        except OSError:
            driver = ""
        node = next((p.name for p in sorted(device.glob("drm/renderD*"))), "")
        row: dict = {"index": int(card.name[4:]), "vendor": vendor, "driver": driver, "name": f"{vendor} {driver}".strip(),
                     "node": f"/dev/dri/{node}" if node else "", "util_percent": None, "vram_used_mb": None,
                     "vram_total_mb": None, "temp_c": None, "power_w": None}
        busy = _int(_read(device / "gpu_busy_percent"))
        if busy is not None:
            row["util_percent"] = float(busy)
        used, total = _int(_read(device / "mem_info_vram_used")), _int(_read(device / "mem_info_vram_total"))
        if used is not None and total:
            row["vram_used_mb"], row["vram_total_mb"] = round(used / 1048576, 1), round(total / 1048576, 1)
        for name in ("gt_act_freq_mhz", "gt_cur_freq_mhz"):
            mhz = _int(_read(card / name))
            if mhz is not None:
                row["freq_mhz"] = mhz
                row["freq_max_mhz"] = _int(_read(card / "gt_max_freq_mhz"))
                break
        for entry in sorted(device.glob("hwmon/hwmon*/temp1_input")):
            milli = _int(_read(entry))
            if milli is not None:
                row["temp_c"] = round(milli / 1000, 1)
                break
        out.append(row)
    return out


def read_gpus() -> list[dict]:
    nvidia = read_nvidia()
    return [*nvidia, *read_sysfs_gpus(skip_vendors=("nvidia",) if nvidia else ())]


# ------------------------------------------------------------------------------ farm

def host_metrics() -> dict:
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = []
    return {"cpu_percent": _sampler.sample(), "cpu_count": os.cpu_count() or 0, "load": load,
            "memory": parse_meminfo(_read(Path("/proc/meminfo"))), "temps": read_temps()}


def farm_metrics(mgr: InstanceManager) -> dict:
    """One snapshot for the dashboard. Costs one `docker ps` and one `docker stats` (about
    two seconds for any number of Androids, so callers should cache it for a few seconds)."""
    statuses = mgr.list_status()
    running = {s["containers"]["android"]["name"]: s["id"] for s in statuses
               if s["containers"]["android"]["state"] == "running"}
    try:
        stats = mgr.docker.stats(running) if running else {}
    except CommandError:
        stats = {}
    instances = {running[name]: sample for name, sample in stats.items() if name in running}
    return {
        "time": time.time(),
        "host": host_metrics(),
        "gpus": read_gpus(),
        "instances": instances,
        "totals": {
            "defined": len(statuses), "running": len(running),
            "cpu_percent": round(sum(s["cpu"] for s in instances.values()), 1),
            "mem_mb": round(sum(s["mem_mb"] for s in instances.values()), 1),
        },
    }

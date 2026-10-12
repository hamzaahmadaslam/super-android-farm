"""Host readings, tested against made-up /proc and /sys trees."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))

from superfarm.core import metrics as m  # noqa: E402

MEMINFO = """MemTotal:       32038784 kB
MemFree:         1000000 kB
MemAvailable:   21448176 kB
Buffers:          500000 kB
SwapTotal:       8388604 kB
SwapFree:        7602080 kB
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_cpus(root, layout: dict[int, tuple[str, int]], online: str):
    write(root / "system/cpu/online", online + "\n")
    for cpu, (siblings, freq) in layout.items():
        write(root / f"system/cpu/cpu{cpu}/topology/thread_siblings_list", siblings + "\n")
        if freq:
            write(root / f"system/cpu/cpu{cpu}/cpufreq/cpuinfo_max_freq", f"{freq}\n")


def test_topology_of_a_hybrid_cpu_from_kernel_classes(tmp_path):
    make_cpus(tmp_path, {0: ("0-1", 6000000), 1: ("0-1", 6000000), 2: ("2-3", 5700000), 3: ("2-3", 5700000),
                         4: ("4", 4400000), 5: ("5", 4400000)}, "0-5")
    write(tmp_path / "cpu_core/cpus", "0-3\n")
    write(tmp_path / "cpu_atom/cpus", "4-5\n")
    t = m.read_cpu_topology(tmp_path)
    assert [c.cpus for c in t.cores] == [(0, 1), (2, 3), (4,), (5,)]
    assert t.cpus("performance") == [0, 1, 2, 3] and t.cpus("efficiency") == [4, 5]


def test_topology_falls_back_to_frequencies(tmp_path):
    make_cpus(tmp_path, {0: ("0", 2800000), 1: ("1", 2800000), 2: ("2", 1800000), 3: ("3", 1800000)}, "0-3")
    t = m.read_cpu_topology(tmp_path)
    assert t.hybrid and t.cpus("performance") == [0, 1] and t.cpus("efficiency") == [2, 3]


def test_offline_cpus_are_left_out(tmp_path):
    make_cpus(tmp_path, {0: ("0-1", 0), 1: ("0-1", 0), 2: ("2-3", 0), 3: ("2-3", 0)}, "0-2")
    t = m.read_cpu_topology(tmp_path)
    assert t.cpus() == [0, 1, 2] and not t.hybrid


def test_unreadable_tree_gives_an_empty_topology(tmp_path):
    assert m.read_cpu_topology(tmp_path / "missing").cores == ()


def test_cpu_percent_between_two_samples():
    a = m.parse_cpu_times("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3 4\n")
    b = m.parse_cpu_times("cpu  200 0 200 1300 300 0 0 0 0 0\n")
    assert a == (800, 1000) and b == (1600, 2000)
    assert m.cpu_percent(a, b) == 20.0
    assert m.cpu_percent(b, b) == 0.0
    assert m.parse_cpu_times("garbage") == (0, 0)


def test_sampler_reuses_the_previous_reading(tmp_path):
    stat = tmp_path / "stat"
    stat.write_text("cpu  100 0 100 800 0 0 0 0\n")
    sampler = m._CpuSampler(stat)
    first = sampler.sample()
    stat.write_text("cpu  900 0 100 800 0 0 0 0\n")
    assert sampler.sample() == first          # called again at once: too close to measure, cached


def test_meminfo_uses_available_not_free():
    mem = m.parse_meminfo(MEMINFO)
    assert mem["total_mb"] == 31287 and mem["available_mb"] == 20945
    assert mem["used_mb"] == 10342 and mem["used_percent"] == 33.1
    assert mem["swap_total_mb"] == 8191 and mem["swap_free_mb"] == 7423
    assert m.parse_meminfo("")["total_mb"] == 0


def test_temps_report_the_hottest_cpu_reading(tmp_path):
    write(tmp_path / "hwmon0/name", "coretemp\n")
    write(tmp_path / "hwmon0/temp1_input", "61000\n")
    write(tmp_path / "hwmon0/temp1_label", "Package id 0\n")
    write(tmp_path / "hwmon0/temp2_input", "85000\n")
    write(tmp_path / "hwmon0/temp2_label", "Core 4\n")
    write(tmp_path / "hwmon1/name", "nvme\n")
    write(tmp_path / "hwmon1/temp1_input", "99000\n")
    write(tmp_path / "hwmon2/name", "k10temp\n")
    write(tmp_path / "hwmon2/temp1_input", "not a number\n")
    assert m.read_temps(tmp_path) == [{"sensor": "coretemp", "label": "Core 4", "celsius": 85.0}]
    assert m.read_temps(tmp_path / "missing") == []


def test_nvidia_smi_rows_are_parsed():
    text = "0, NVIDIA GeForce RTX 4080 SUPER, 37, 5120, 16376, 61, 142.50\n1, Tesla T4, [N/A], 10, 15360, 40, [N/A]\nnoise\n"
    rows = m.parse_nvidia_smi(text)
    assert rows[0] == {"index": 0, "vendor": "nvidia", "driver": "nvidia", "name": "NVIDIA GeForce RTX 4080 SUPER",
                       "util_percent": 37.0, "vram_used_mb": 5120.0, "vram_total_mb": 16376.0, "temp_c": 61.0,
                       "power_w": 142.5}
    assert rows[1]["util_percent"] is None and rows[1]["power_w"] is None and len(rows) == 2


def test_sysfs_gpus_amd_and_intel(tmp_path):
    amd = tmp_path / "card0"
    write(amd / "device/vendor", "0x1002\n")
    write(amd / "device/gpu_busy_percent", "42\n")
    write(amd / "device/mem_info_vram_used", str(512 * 1048576))
    write(amd / "device/mem_info_vram_total", str(8192 * 1048576))
    write(amd / "device/hwmon/hwmon3/temp1_input", "55000\n")
    (amd / "device/drm/renderD128").mkdir(parents=True)
    (tmp_path / "drivers/amdgpu").mkdir(parents=True)
    os.symlink(tmp_path / "drivers/amdgpu", amd / "device/driver")
    intel = tmp_path / "card1"
    write(intel / "device/vendor", "0x8086\n")
    write(intel / "gt_act_freq_mhz", "700\n")
    write(intel / "gt_max_freq_mhz", "1650\n")
    write(tmp_path / "card1-HDMI-A-1/status", "connected\n")          # connectors are not cards
    nv = tmp_path / "card2"
    write(nv / "device/vendor", "0x10de\n")

    rows = m.read_sysfs_gpus(tmp_path, skip_vendors=("nvidia",))
    assert [r["vendor"] for r in rows] == ["amd", "intel"]
    assert rows[0]["driver"] == "amdgpu" and rows[0]["node"] == "/dev/dri/renderD128"
    assert rows[0]["util_percent"] == 42.0 and rows[0]["vram_used_mb"] == 512.0 and rows[0]["vram_total_mb"] == 8192.0
    assert rows[0]["temp_c"] == 55.0
    assert rows[1]["util_percent"] is None and rows[1]["freq_mhz"] == 700 and rows[1]["freq_max_mhz"] == 1650
    assert len(m.read_sysfs_gpus(tmp_path)) == 3


def test_farm_metrics_samples_only_running_androids(monkeypatch):
    monkeypatch.setattr(m, "read_gpus", lambda: [{"vendor": "intel"}])
    monkeypatch.setattr(m, "host_metrics", lambda: {"cpu_percent": 5.0})
    asked = []

    def stats(names):
        asked.extend(names)
        return {n: {"cpu": 50.0, "mem_mb": 900.0, "mem_limit_mb": 1536.0, "pids": 80} for n in names}

    statuses = [
        {"id": 1, "containers": {"android": {"name": "sf-android-1", "state": "running"}}},
        {"id": 2, "containers": {"android": {"name": "sf-android-2", "state": "exited"}}},
        {"id": 3, "containers": {"android": {"name": "sf-android-3", "state": "running"}}},
    ]
    mgr = SimpleNamespace(list_status=lambda: statuses, docker=SimpleNamespace(stats=stats))
    out = m.farm_metrics(mgr)
    assert sorted(asked) == ["sf-android-1", "sf-android-3"]
    assert set(out["instances"]) == {1, 3} and out["instances"][1]["mem_mb"] == 900.0
    assert out["totals"] == {"defined": 3, "running": 2, "cpu_percent": 100.0, "mem_mb": 1800.0}
    assert out["gpus"] == [{"vendor": "intel"}] and out["host"] == {"cpu_percent": 5.0}

"""Capacity estimates: the measured numbers, the binding constraint and the advice."""
import pytest

from superfarm.config import Config
from superfarm.host import capacity
from superfarm.host.gpu import GpuInfo


def fake_mem(tmp_path, total_gb, available_gb=None, zram=False, swap_gb=8):
    (tmp_path / "proc").mkdir(exist_ok=True)
    avail = total_gb if available_gb is None else available_gb
    (tmp_path / "proc/meminfo").write_text(
        f"MemTotal:       {int(total_gb * 1024 * 1024)} kB\nMemFree:  1 kB\nMemAvailable:   {int(avail * 1024 * 1024)} kB\n"
        f"SwapTotal:      {swap_gb * 1024 * 1024} kB\nHugePages_Total:       0\n")
    (tmp_path / "proc/swaps").write_text(
        "Filename\tType\tSize\tUsed\tPriority\n" + ("/dev/zram0 partition 8388604 0 100\n" if zram else "/swap.img file 8388604 0 -1\n"))
    return tmp_path


NVIDIA = GpuInfo("/dev/dri/renderD129", "nvidia", "nvidia", "Big card", "0000:01:00.0", ["nvidia"], True, 16376)
INTEL = GpuInfo("/dev/dri/renderD128", "i915", "intel", "Integrated", "0000:00:02.0", ["intel"], False, None)
RESERVE = 3072          # performance.reserve_host_mb: what the estimate leaves to the host by default


def test_constants_are_the_measured_numbers():
    assert capacity.RAM_PER_ANDROID_MB == 1000 and capacity.RAM_NVIDIA_EXTRA_MB == 300
    assert capacity.RAM_TRIM_SAVING_MB == 170
    assert capacity.CPU_PERCENT["software"] == {"light": 78, "video": 148}
    assert capacity.CPU_PERCENT["intel"] == {"light": 13, "video": 55}
    assert capacity.CPU_PERCENT["nvidia"] == {"light": 15, "video": 66}
    assert capacity.VRAM_PER_ANDROID_MB == 360 and capacity.VRAM_MEASURED is True     # (7.6 - 1.5) GB / 17 Androids
    assert capacity.ZRAM_RATIO == 4.2 and capacity.ZRAM_MEASURED_ANDROIDS == 17 and capacity.ZRAM_SWAPPINESS == 150


def test_meminfo_and_zram_detection(tmp_path):
    root = fake_mem(tmp_path, 32, 20, zram=True)
    m = capacity.meminfo(root)
    assert m["MemTotal"] == 32 * 1024 and m["MemAvailable"] == 20 * 1024 and m["SwapTotal"] == 8 * 1024
    assert capacity.zram_active(root) and not capacity.zram_active(tmp_path / "missing")
    assert capacity.meminfo(tmp_path / "missing") == {}


def test_per_android_ram_by_mode_and_trim():
    assert capacity.per_android_ram_mb("intel", trimmed=False) == 1000
    assert capacity.per_android_ram_mb("intel", trimmed=True) == 830
    assert capacity.per_android_ram_mb("nvidia", trimmed=False) == 1300
    assert capacity.per_android_ram_mb("intel", trimmed=False, limit_mb=768) == 768       # the container limit caps it
    assert capacity.per_android_ram_mb("nvidia", trimmed=False, limit_mb=768) == 1068     # NVIDIA's share is outside the limit


def test_ram_binds_on_a_32gb_nvidia_machine(tmp_path):
    root = fake_mem(tmp_path, 32, 30)
    e = capacity.estimate(Config(), [INTEL, NVIDIA], root=root, cpu_threads=32)
    assert e["mode"] == "nvidia" and e["binding"] == "ram"
    ram = e["limits"]["ram"]
    assert ram["per_android_mb"] == 1130 and ram["host_reserve_mb"] == RESERVE
    assert ram["max"] == (32 * 1024 - RESERVE) // 1130 == e["max_instances"]
    assert e["limits"]["cpu"]["max"] == int(32 * 100 * 0.8 // 66)
    assert e["limits"]["vram"]["max"] == (16376 - 1536) // 360 and e["limits"]["vram"]["measured"] is True
    assert "max_idle_with_zram" not in ram            # no zram on this fake machine
    text = " ".join(e["advice"])
    assert "zram" in text and "Mix modes" in text and "saves CPU, not power" in text


def test_cpu_binds_in_software_mode_and_says_use_a_gpu(tmp_path):
    cfg = Config.model_validate({"gpu": {"mode": "software"}})
    e = capacity.estimate(cfg, [INTEL], root=fake_mem(tmp_path, 128), cpu_threads=16)
    assert e["mode"] == "software" and e["binding"] == "cpu"
    assert e["limits"]["cpu"]["percent_per_android"] == 148 and e["max_instances"] == int(16 * 100 * 0.8 // 148)
    assert "vram" not in e["limits"]
    assert any("Use a GPU mode (intel)" in a for a in e["advice"])
    light = capacity.estimate(cfg, [INTEL], root=tmp_path, cpu_threads=16, workload="light")
    assert light["limits"]["cpu"]["max"] == int(16 * 100 * 0.8 // 78) > e["limits"]["cpu"]["max"]


def test_cpu_figure_never_exceeds_the_per_android_cap(tmp_path):
    cfg = Config.model_validate({"gpu": {"mode": "software"}, "resources": {"cpus": 1}})
    e = capacity.estimate(cfg, [], root=fake_mem(tmp_path, 64), cpu_threads=8)
    assert e["limits"]["cpu"]["percent_per_android"] == 100


def test_auto_mode_picks_the_best_card_present(tmp_path):
    root = fake_mem(tmp_path, 64)
    assert capacity.estimate(Config(), [INTEL], root=root, cpu_threads=8)["mode"] == "intel"
    assert capacity.estimate(Config(), [], root=root, cpu_threads=8)["mode"] == "software"
    amd = GpuInfo("/dev/dri/renderD128", "amdgpu", "amd", "Card", "0000:03:00.0", ["amd"], True, 8192)
    assert capacity.estimate(Config(), [INTEL, amd], root=root, cpu_threads=8)["mode"] == "amd"


def test_host_reserve_is_the_configured_value(tmp_path):
    root = fake_mem(tmp_path, 32, 30)
    lean = Config.model_validate({"performance": {"reserve_host_mb": 1024}})
    desktop = Config.model_validate({"performance": {"reserve_host_mb": 10240}})
    a = capacity.estimate(lean, [INTEL], root=root, cpu_threads=256)
    b = capacity.estimate(desktop, [INTEL], root=root, cpu_threads=256)
    assert a["limits"]["ram"]["host_reserve_mb"] == 1024 and a["limits"]["ram"]["max"] == (32 * 1024 - 1024) // 830
    assert b["limits"]["ram"]["host_reserve_mb"] == 10240 and b["limits"]["ram"]["max"] == (32 * 1024 - 10240) // 830


def test_farm_default_by_kind_resolves_to_a_vendor(tmp_path):
    root = fake_mem(tmp_path, 64)
    for gpu_cfg, mode in (({"mode": "igpu"}, "intel"), ({"mode": "dgpu"}, "nvidia"), ({"prefer": "igpu"}, "intel"),
                          ({}, "nvidia")):
        cfg = Config.model_validate({"gpu": gpu_cfg})
        assert capacity.estimate(cfg, [INTEL, NVIDIA], root=root, cpu_threads=8)["mode"] == mode
    only_intel = capacity.estimate(Config.model_validate({"gpu": {"mode": "dgpu"}}), [INTEL], root=root, cpu_threads=8)
    assert only_intel["mode"] == "software"          # nothing can serve it; the estimate must still be a number


def test_vram_binds_on_a_small_card(tmp_path):
    small = GpuInfo("/dev/dri/renderD129", "nvidia", "nvidia", "Small", "0000:01:00.0", ["nvidia"], True, 2048)
    e = capacity.estimate(Config(), [small], root=fake_mem(tmp_path, 256), cpu_threads=128)
    assert e["binding"] == "vram" and e["max_instances"] == (2048 - 1536) // 360
    assert any("Video memory is the limit" in a for a in e["advice"])
    unknown = GpuInfo("/dev/dri/renderD129", "nvidia", "nvidia", "Unknown", "0000:01:00.0", ["nvidia"], True, None)
    assert "vram" not in capacity.estimate(Config(), [unknown], root=tmp_path, cpu_threads=8)["limits"]


def test_running_count_separates_other_memory_use(tmp_path):
    # 32 GB machine, 21 GB available, nothing running: a desktop holds about 11 GB.
    e = capacity.estimate(Config(), [INTEL, NVIDIA], root=fake_mem(tmp_path, 32, 21), cpu_threads=32, running=0)
    ram = e["limits"]["ram"]
    assert ram["more_now"] == (21 * 1024 - 1500) // 1130 == ram["max_now"]
    assert ram["other_use_mb"] == 11 * 1024 and ram["max_now"] < ram["max"]
    assert any("other than Androids" in a for a in e["advice"])
    e10 = capacity.estimate(Config(), [NVIDIA], root=fake_mem(tmp_path, 32, 9), cpu_threads=32, running=10)
    assert e10["limits"]["ram"]["max_now"] == 10 + (9 * 1024 - 1500) // 1130
    assert "max_now" not in capacity.estimate(Config(), [NVIDIA], root=tmp_path, cpu_threads=32)["limits"]["ram"]


def test_trim_advice_only_when_trim_is_off(tmp_path):
    root = fake_mem(tmp_path, 16)
    off = Config.model_validate({"performance": {"trim_packages": False}})
    assert any("trim_packages" in a for a in capacity.estimate(off, [INTEL], root=root, cpu_threads=64)["advice"])
    assert not any("trim_packages" in a for a in capacity.estimate(Config(), [INTEL], root=root, cpu_threads=64)["advice"])


def test_zram_advice_changes_once_zram_is_active(tmp_path):
    e = capacity.estimate(Config(), [INTEL], root=fake_mem(tmp_path, 16, zram=True), cpu_threads=64)
    assert e["zram_active"] and not any("Enable compressed swap" in a for a in e["advice"])
    assert any("With compressed swap about" in a and "vm.swappiness=150" in a for a in e["advice"])
    tuned = Config.model_validate({"performance": {"swappiness": 150}})
    e2 = capacity.estimate(tuned, [INTEL], root=tmp_path, cpu_threads=64)
    assert not any("vm.swappiness" in a for a in e2["advice"])
    (tmp_path / "proc/sys/vm").mkdir(parents=True)
    (tmp_path / "proc/sys/vm/swappiness").write_text("150\n")           # already set outside the farm's config
    e3 = capacity.estimate(Config(), [INTEL], root=tmp_path, cpu_threads=64)
    assert e3["swappiness"] == 150 and not any("vm.swappiness" in a for a in e3["advice"])


def test_zram_raises_the_idle_figure_but_not_the_binding_one(tmp_path):
    # The reference machine: 32 GB, 16 GB of zram, NVIDIA mode.
    root = fake_mem(tmp_path, 32, 30)
    (root / "proc/swaps").write_text("Filename Type Size Used Priority\n/dev/zram0 partition 16777212 0 100\n/swap.img file 8388604 0 -1\n")
    assert capacity.zram_size_mb(root) == 16383
    e = capacity.estimate(Config(), [NVIDIA], root=root, cpu_threads=32)
    ram = e["limits"]["ram"]
    plain = (32 * 1024 - RESERVE) // 1130
    assert ram["max"] == plain == e["max_instances"]              # video was not measured with zram: unchanged
    assert ram["max_idle_with_zram"] > plain and ram["zram_mb"] == 16383
    assert ram["max_idle_with_zram"] >= capacity.ZRAM_MEASURED_ANDROIDS      # 17 really ran on this configuration
    assert ram["zram_extrapolated"] is True
    assert any("extrapolated" in a for a in e["advice"])


def test_zram_enabled_in_config_is_planned_before_it_exists(tmp_path):
    cfg = Config.model_validate({"performance": {"zram": {"enabled": True, "size_percent": 50}}})
    ram = capacity.estimate(cfg, [INTEL], root=fake_mem(tmp_path, 16), cpu_threads=64)["limits"]["ram"]
    assert ram["zram_mb"] == 8 * 1024 and ram["max_idle_with_zram"] > ram["max"]


def test_unknown_workload_and_unreadable_meminfo(tmp_path):
    with pytest.raises(ValueError):
        capacity.estimate(Config(), [], workload="gaming")
    e = capacity.estimate(Config(), [], root=tmp_path / "nothing", cpu_threads=4)
    assert e["limits"]["ram"]["max"] == 0 and e["max_instances"] == 0

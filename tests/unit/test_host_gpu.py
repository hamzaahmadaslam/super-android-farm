"""GPU detection against a fake sysfs tree, and the mode / card choice."""
import os

import pytest

from superfarm.config import Config
from superfarm.core.models import InstanceSpec, resolve
from superfarm.host import gpu, venus
from superfarm.paths import get_paths

INTEL_IGPU = dict(driver="i915", vendor="0x8086", device="0xa780", slot="0000:00:02.0")
INTEL_ARC = dict(driver="xe", vendor="0x8086", device="0x56a0", slot="0000:03:00.0")
AMD_APU = dict(driver="amdgpu", vendor="0x1002", device="0x15bf", slot="0000:c5:00.0", vram=512 << 20)
AMD_CARD = dict(driver="amdgpu", vendor="0x1002", device="0x744c", slot="0000:03:00.0", vram=24 << 30)
NVIDIA = dict(driver="nvidia", vendor="0x10de", device="0x2702", slot="0000:01:00.0")
NOUVEAU = dict(driver="nouveau", vendor="0x10de", device="0x1c82", slot="0000:01:00.0")
VIRTIO = dict(driver="virtio-pci", vendor="0x1af4", device="0x1050", slot="0000:00:01.0")


def fake_sysfs(tmp_path, cards):
    """cards: {render node number: spec}. Builds /sys/class/drm/renderDN/device like the kernel does."""
    sysfs = tmp_path / "sys"
    for n, c in cards.items():
        dev = sysfs / "devices" / "pci0000:00" / c["slot"]
        dev.mkdir(parents=True, exist_ok=True)
        (dev / "vendor").write_text(c["vendor"] + "\n")
        (dev / "device").write_text(c["device"] + "\n")
        uevent = f"PCI_CLASS=30000\nPCI_ID={c['vendor'][2:].upper()}:{c['device'][2:].upper()}\nPCI_SLOT_NAME={c['slot']}\n"
        if c.get("driver"):
            drv = sysfs / "bus" / "pci" / "drivers" / c["driver"]
            drv.mkdir(parents=True, exist_ok=True)
            os.symlink(drv, dev / "driver")
            uevent = f"DRIVER={c['driver']}\n" + uevent
        (dev / "uevent").write_text(uevent)
        if "vram" in c:
            (dev / "mem_info_vram_total").write_text(str(c["vram"]) + "\n")
        node = sysfs / "class" / "drm" / f"renderD{n}"
        node.mkdir(parents=True)
        os.symlink(dev, node / "device")
        (sysfs / "class" / "drm" / f"card{n - 128}").mkdir()        # non-render entries must be ignored
    return sysfs


def detect(tmp_path, cards):
    return gpu.detect_gpus(fake_sysfs(tmp_path, cards), namer=None)


def resolved(config=None, **spec):
    return resolve(InstanceSpec(id=spec.pop("id", 1), **spec), config or Config())


READY = {"ready": True, "icd": True, "missing": []}
NOT_BUILT = {"ready": False, "icd": True, "missing": ["/x/overlays/venus/vulkan.virtio.so"]}


@pytest.fixture
def paths(tmp_path):
    return get_paths(tmp_path / "home")


def choose(r, paths, gpus, config=None, runtime=READY, relay=None, counts=None):
    return gpu.choose(r, config or Config(), paths, gpus=gpus, runtime=runtime, ensure_relay=relay, counts=counts)


# ------------------------------------------------------------------ detection

def test_detects_vendor_driver_node_and_pci(tmp_path):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    assert [(g.node, g.driver, g.vendor, g.pci, g.usable_modes) for g in gs] == [
        ("/dev/dri/renderD128", "i915", "intel", "0000:00:02.0", ["intel"]),
        ("/dev/dri/renderD129", "nvidia", "nvidia", "0000:01:00.0", ["nvidia"]),
    ]
    assert gs[0].discrete is False and gs[1].discrete is True
    assert "8086:a780" in gs[0].name and gs[0].name.startswith("Intel")


@pytest.mark.parametrize("card,vendor,modes,discrete", [
    (INTEL_ARC, "intel", ["intel"], True), (AMD_APU, "amd", ["amd"], False), (AMD_CARD, "amd", ["amd"], True),
    (dict(AMD_CARD, driver="radeon"), "amd", ["amd"], True),
])
def test_mesa_cards(tmp_path, card, vendor, modes, discrete):
    g = detect(tmp_path, {128: card})[0]
    assert (g.vendor, g.usable_modes, g.discrete) == (vendor, modes, discrete)


def test_amd_vram_is_read_from_sysfs(tmp_path):
    assert detect(tmp_path, {128: AMD_CARD})[0].vram_mb == 24 * 1024


def test_cards_android_cannot_use_say_why(tmp_path):
    gs = detect(tmp_path, {128: NOUVEAU, 129: VIRTIO, 130: dict(NVIDIA, driver="", slot="0000:02:00.0")})
    assert [g.usable_modes for g in gs] == [[], [], []]
    assert gs[0].vendor == "nvidia" and "nouveau" in gs[0].note
    assert gs[1].vendor == "other" and "virtio-pci" in gs[1].note
    assert gs[2].vendor == "nvidia" and "no kernel driver" in gs[2].note     # vendor from the PCI id alone


def test_nodes_come_back_in_numeric_order_and_missing_sysfs_is_empty(tmp_path):
    gs = detect(tmp_path, {130: AMD_CARD, 128: INTEL_IGPU, 129: NVIDIA})
    assert [g.node for g in gs] == ["/dev/dri/renderD128", "/dev/dri/renderD129", "/dev/dri/renderD130"]
    assert gpu.detect_gpus(tmp_path / "no-sysfs") == []


def test_namer_supplies_the_model_name(tmp_path):
    gs = gpu.detect_gpus(fake_sysfs(tmp_path, {128: INTEL_IGPU}), namer=lambda pci: f"Model at {pci}")
    assert gs[0].name == "Model at 0000:00:02.0"


def test_nvidia_vram_filled_from_nvidia_smi_output(tmp_path):
    from superfarm.util.proc import Result
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return Result(0, "00000000:01:00.0, 16376\n", "")
    gpu.nvidia_vram(gs, runner)
    assert gs[1].vram_mb == 16376 and gs[0].vram_mb == 0 and calls[0][0] == "nvidia-smi"
    gpu.nvidia_vram(gs, lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called again")))


# ------------------------------------------------------------------ choose: explicit modes

def test_software_needs_nothing(paths):
    c = choose(resolved(gpu_mode="software"), paths, [])
    assert (c.mode, c.node, c.run_args, c.boot_args) == ("software", "", [], ["androidboot.redroid_gpu_mode=guest"])


def test_intel_gets_the_device_and_host_mode(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    c = choose(resolved(gpu_mode="intel"), paths, gs)
    assert c.mode == "intel" and c.node == "/dev/dri/renderD128"
    assert c.run_args == ["--device", "/dev/dri/renderD128"]
    assert c.boot_args == ["androidboot.redroid_gpu_mode=host", "androidboot.redroid_gpu_node=/dev/dri/renderD128"]


def test_amd_mode(tmp_path, paths):
    c = choose(resolved(gpu_mode="amd"), paths, detect(tmp_path, {128: AMD_CARD}))
    assert c.mode == "amd" and c.run_args == ["--device", "/dev/dri/renderD128"]


def test_nvidia_mounts_runtime_and_socket_and_starts_the_relay(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    started = []
    c = choose(resolved(gpu_mode="nvidia"), paths, gs, relay=lambda cfg, p, node: started.append(node) or "dir")
    assert started == ["/dev/dri/renderD129"]
    assert c.mode == "nvidia" and c.node == "/dev/dri/renderD129" and c.boot_args == ["androidboot.redroid_gpu_mode=venus"]
    rt = paths.overlays / "venus"
    pairs = [c.run_args[i + 1] for i in range(0, len(c.run_args), 2)]
    assert all(flag == "-v" for flag in c.run_args[::2])
    assert pairs == [
        "/run/superfarm/venus/renderD129:/venus",
        f"{rt}/gpu_config.sh:/vendor/bin/gpu_config.sh:ro",
        f"{rt}/vulkan.virtio.so:/vendor/lib64/hw/vulkan.virtio.so:ro",
        f"{rt}/gralloc.venus.so:/vendor/lib64/hw/gralloc.venus.so:ro",
        f"{rt}/gralloc.venus.x86.so:/vendor/lib/hw/gralloc.venus.so:ro",
    ]


def test_nvidia_debug_flag_and_custom_dirs(tmp_path, paths):
    cfg = Config.model_validate({"gpu": {"nvidia": {"debug": True, "runtime_dir": "/opt/rt", "socket_dir": "/tmp/s"}}})
    c = choose(resolved(cfg, gpu_mode="nvidia"), paths, detect(tmp_path, {129: NVIDIA}), config=cfg)
    assert "androidboot.redroid_gpu_debug=1" in c.boot_args
    assert c.run_args[1] == "/tmp/s/renderD129:/venus" and c.run_args[3].startswith("/opt/rt/gpu_config.sh:")


def test_requested_modes_that_cannot_work_raise_with_the_reason(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    with pytest.raises(ValueError, match="no AMD GPU"):
        choose(resolved(gpu_mode="amd"), paths, gs)
    with pytest.raises(ValueError, match="not built.*build.sh"):
        choose(resolved(gpu_mode="nvidia"), paths, gs, runtime=NOT_BUILT)
    with pytest.raises(ValueError, match="no NVIDIA card"):
        choose(resolved(gpu_mode="nvidia"), paths, gs[:1])
    with pytest.raises(ValueError, match="nouveau"):
        choose(resolved(gpu_mode="nvidia"), paths, gpu.detect_gpus(fake_sysfs(tmp_path / "b", {128: NOUVEAU}), namer=None))

    def broken(cfg, p, node):
        raise venus.RelayError("cannot create /run/superfarm/venus")
    with pytest.raises(ValueError, match="cannot create"):
        choose(resolved(gpu_mode="nvidia"), paths, gs, relay=broken)
    with pytest.raises(ValueError, match="manifest"):
        choose(resolved(gpu_mode="nvidia"), paths, gs, runtime={"ready": True, "icd": False, "icd_path": "/x.json"})


def test_forced_node_must_exist_and_match_the_mode(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    with pytest.raises(ValueError, match="renderD140 does not exist"):
        choose(resolved(gpu_mode="intel", gpu_node="/dev/dri/renderD140"), paths, gs)
    with pytest.raises(ValueError, match="NVIDIA card"):
        choose(resolved(gpu_mode="intel", gpu_node="/dev/dri/renderD129"), paths, gs)
    cfg = Config.model_validate({"gpu": {"render_node": "/dev/dri/renderD128"}})
    assert choose(resolved(cfg), paths, gs, config=cfg).mode == "intel"      # auto + forced node follows the node


# ------------------------------------------------------------------ choose: auto

def test_auto_prefers_nvidia_only_when_runtime_and_relay_exist(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    assert choose(resolved(), paths, gs).mode == "nvidia"
    fallback = choose(resolved(), paths, gs, runtime=NOT_BUILT)
    assert fallback.mode == "intel" and "NVIDIA skipped" in fallback.reason


def test_auto_falls_back_when_the_relay_cannot_start(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})

    def broken(cfg, p, node):
        raise venus.RelayError("relay exited with code 1")
    c = choose(resolved(), paths, gs, relay=broken)
    assert c.mode == "intel" and "relay exited" in c.reason
    only_nvidia = choose(resolved(), paths, gs[1:], relay=broken)
    assert only_nvidia.mode == "software" and "relay exited" in only_nvidia.reason


def test_auto_order_discrete_before_integrated_before_software(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: AMD_CARD})
    c = choose(resolved(), paths, gs)
    assert (c.mode, c.node) == ("amd", "/dev/dri/renderD129") and "discrete" in c.reason
    gs2 = gpu.detect_gpus(fake_sysfs(tmp_path / "b", {128: AMD_APU}), namer=None)
    assert choose(resolved(), paths, gs2).mode == "amd"
    none = choose(resolved(), paths, [])
    assert none.mode == "software" and none.boot_args == ["androidboot.redroid_gpu_mode=guest"]
    unusable = gpu.detect_gpus(fake_sysfs(tmp_path / "c", {128: VIRTIO}), namer=None)
    assert choose(resolved(), paths, unusable).mode == "software"


def test_planning_only_never_starts_a_relay(tmp_path, paths):
    gs = detect(tmp_path, {129: NVIDIA})
    assert choose(resolved(), paths, gs, relay=None).mode == "nvidia"


# ------------------------------------------------------------------ choose: several cards

def two_nvidia(tmp_path):
    return detect(tmp_path, {128: NVIDIA, 129: dict(NVIDIA, slot="0000:02:00.0")})


def test_round_robin_is_stable_per_instance(tmp_path, paths):
    gs = two_nvidia(tmp_path)
    nodes = [choose(resolved(id=i, gpu_mode="nvidia"), paths, gs).node for i in range(1, 7)]
    assert nodes == ["/dev/dri/renderD129", "/dev/dri/renderD128"] * 3
    again = [choose(resolved(id=i, gpu_mode="nvidia"), paths, gs, counts={"/dev/dri/renderD128": 50}).node for i in range(1, 7)]
    assert again == nodes        # a rebuilt container must not hop cards because others started


def test_first_and_least_instances(tmp_path, paths):
    gs = two_nvidia(tmp_path)
    first = Config.model_validate({"gpu": {"assignment": "first"}})
    assert {choose(resolved(first, id=i, gpu_mode="nvidia"), paths, gs, config=first).node for i in range(1, 5)} == {"/dev/dri/renderD128"}
    least = Config.model_validate({"gpu": {"assignment": "least-instances"}})
    pick = lambda counts: choose(resolved(least, gpu_mode="nvidia"), paths, gs, config=least, counts=counts).node  # noqa: E731
    assert pick({"/dev/dri/renderD128": 3, "/dev/dri/renderD129": 1}) == "/dev/dri/renderD129"
    assert pick({"/dev/dri/renderD128": 1, "/dev/dri/renderD129": 1}) == "/dev/dri/renderD128"
    assert pick(None) == "/dev/dri/renderD128"


def test_instance_pinned_to_one_of_several_cards(tmp_path, paths):
    c = choose(resolved(gpu_mode="nvidia", gpu_node="/dev/dri/renderD129"), paths, two_nvidia(tmp_path))
    assert c.node == "/dev/dri/renderD129" and c.run_args[1].startswith("/run/superfarm/venus/renderD129:")


def test_summary_lists_modes_available_now(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    s = gpu.summary(Config(), paths, gs)
    assert s["available_modes"] == ["software", "intel"] and "not built" in s["nvidia_blocker"]
    assert len(s["gpus"]) == 2 and s["gpus"][1]["vendor"] == "nvidia"

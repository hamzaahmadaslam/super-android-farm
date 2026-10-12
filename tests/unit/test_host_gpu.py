"""GPU detection against fake sysfs trees, and the mode / card choice.

The fake trees follow the PCI layout of real machines (the Intel + NVIDIA one is this
project's reference desktop; the AMD ones are built from AMD's PCI topology and were not
compared against AMD hardware)."""
import os

import pytest

from superfarm.config import Config
from superfarm.core.models import InstanceSpec, resolve
from superfarm.host import gpu, venus
from superfarm.paths import get_paths

AUDIO = ("0x1002",)                                   # a card's own HDMI audio function
APU_PARTS = ("0x1002", "0x1022", "0x1022", "0x1022")  # HDMI audio, security processor, USB, audio co-processor

# path: PCI devices from the root bus down to the GPU. `bridges`: vendor id per bridge on the way.
INTEL_IGPU = dict(driver="i915", vendor="0x8086", device="0xa780", path=["0000:00:02.0"])
INTEL_ARC = dict(driver="xe", vendor="0x8086", device="0x56a0", bridges=["0x8086", "0x8086", "0x8086"],
                 path=["0000:00:01.0", "0000:01:00.0", "0000:02:00.0", "0000:03:00.0"], siblings=("0x8086",))
# A Zen processor's graphics: behind the internal bridge 00:08.1, next to other processor parts.
AMD_APU = dict(driver="amdgpu", vendor="0x1002", device="0x15bf", bridges=["0x1022"],
               path=["0000:00:08.1", "0000:c5:00.0"], siblings=APU_PARTS, vram=512 << 20)
# The same with 8 GB of system RAM assigned to graphics in the firmware (handhelds, mini PCs).
AMD_APU_BIG = dict(AMD_APU, vram=8 << 30)
# A pre-Zen processor's graphics: directly on bus 00, old driver, no VRAM figure in sysfs.
AMD_APU_OLD = dict(driver="radeon", vendor="0x1002", device="0x130f", path=["0000:00:01.0"], siblings=AUDIO)
# A current Radeon card: behind its own PCIe switch (vendor 0x1002 ports).
AMD_CARD = dict(driver="amdgpu", vendor="0x1002", device="0x744c", bridges=["0x1022", "0x1002", "0x1002"],
                path=["0000:00:01.1", "0000:01:00.0", "0000:02:00.0", "0000:03:00.0"], siblings=AUDIO, vram=24 << 30)
# An older, small Radeon card: straight behind a root port, 2 GB.
AMD_CARD_SMALL = dict(driver="amdgpu", vendor="0x1002", device="0x699f", bridges=["0x8086"],
                      path=["0000:00:01.0", "0000:01:00.0"], siblings=AUDIO, vram=2 << 30)
# A Radeon card handed to a virtual machine shows up on the VM's bus 00.
AMD_CARD_IN_VM = dict(driver="amdgpu", vendor="0x1002", device="0x73bf", path=["0000:00:05.0"], vram=16 << 30)
NVIDIA = dict(driver="nvidia", vendor="0x10de", device="0x2702", bridges=["0x8086"],
              path=["0000:00:01.0", "0000:01:00.0"], siblings=("0x10de",))
NVIDIA_2 = dict(NVIDIA, path=["0000:00:01.1", "0000:02:00.0"])
NOUVEAU = dict(NVIDIA, driver="nouveau", device="0x1c82")
VIRTIO = dict(driver="virtio-pci", vendor="0x1af4", device="0x1050", path=["0000:00:01.0"])


def fake_sysfs(tmp_path, cards):
    """cards: {render node number: spec}. Builds /sys/class/drm/renderDN/device like the kernel does."""
    sysfs = tmp_path / "sys"
    for n, c in cards.items():
        *bridges, slot = c["path"]
        parent = sysfs / "devices" / "pci0000:00"
        for bridge, vendor in zip(bridges, c.get("bridges", []), strict=True):
            parent = parent / bridge
            parent.mkdir(parents=True, exist_ok=True)
            (parent / "vendor").write_text(vendor + "\n")
        dev = parent / slot
        dev.mkdir(parents=True, exist_ok=True)
        (dev / "vendor").write_text(c["vendor"] + "\n")
        (dev / "device").write_text(c["device"] + "\n")
        for i, vendor in enumerate(c.get("siblings", ()), start=1):
            other = parent / f"{slot[:-1]}{i}"
            other.mkdir(exist_ok=True)
            (other / "vendor").write_text(vendor + "\n")
        uevent = f"PCI_CLASS=30000\nPCI_ID={c['vendor'][2:].upper()}:{c['device'][2:].upper()}\nPCI_SLOT_NAME={slot}\n"
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
D128, D129 = "/dev/dri/renderD128", "/dev/dri/renderD129"


@pytest.fixture
def paths(tmp_path):
    return get_paths(tmp_path / "home")


def choose(r, paths, gpus, config=None, runtime=READY, relay=None, counts=None):
    return gpu.choose(r, config or Config(), paths, gpus=gpus, runtime=runtime, ensure_relay=relay, counts=counts)


def pick(mode, paths, gpus, **kw):
    c = choose(resolved(kw.pop("config", None), gpu_mode=mode), paths, gpus, **kw)
    return c.mode, c.kind, c.node


# ------------------------------------------------------------------ detection

def test_detects_vendor_driver_node_and_pci(tmp_path):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    assert [(g.node, g.driver, g.vendor, g.pci, g.usable_modes) for g in gs] == [
        (D128, "i915", "intel", "0000:00:02.0", ["intel"]),
        (D129, "nvidia", "nvidia", "0000:01:00.0", ["nvidia"]),
    ]
    assert (gs[0].integrated, gs[0].discrete, gs[0].kind) == (True, False, "igpu")
    assert (gs[1].integrated, gs[1].discrete, gs[1].kind) == (False, True, "dgpu")
    assert "8086:a780" in gs[0].name and gs[0].name.startswith("Intel")
    assert gs[0].vram_mb is None and gs[1].vram_mb is None      # unknown until nvidia-smi is asked


@pytest.mark.parametrize("card,vendor,integrated", [
    (INTEL_IGPU, "intel", True), (INTEL_ARC, "intel", False),
    (AMD_APU, "amd", True), (AMD_APU_BIG, "amd", True), (AMD_APU_OLD, "amd", True),
    (AMD_CARD, "amd", False), (AMD_CARD_SMALL, "amd", False), (AMD_CARD_IN_VM, "amd", False),
    (dict(AMD_CARD, driver="radeon"), "amd", False),
    (NVIDIA, "nvidia", False), (NOUVEAU, "nvidia", False),
], ids=["intel-igpu", "intel-arc", "amd-apu", "amd-apu-8gb-carve-out", "amd-apu-pre-zen", "amd-card",
        "amd-card-2gb", "amd-card-in-vm", "amd-card-radeon-driver", "nvidia", "nvidia-nouveau"])
def test_integrated_or_discrete(tmp_path, card, vendor, integrated):
    g = detect(tmp_path, {128: card})[0]
    assert (g.vendor, g.integrated, g.discrete) == (vendor, integrated, not integrated)
    assert g.kind == ("igpu" if integrated else "dgpu")


def test_a_virtual_gpu_is_neither_kind(tmp_path):
    g = detect(tmp_path, {128: VIRTIO})[0]
    assert (g.vendor, g.integrated, g.discrete, g.kind, g.usable_modes) == ("other", False, False, "", [])


def test_amd_vram_is_read_from_sysfs(tmp_path):
    assert detect(tmp_path, {128: AMD_CARD})[0].vram_mb == 24 * 1024


def test_cards_android_cannot_use_say_why(tmp_path):
    gs = detect(tmp_path, {128: NOUVEAU, 129: VIRTIO, 130: dict(NVIDIA_2, driver="")})
    assert [g.usable_modes for g in gs] == [[], [], []]
    assert gs[0].vendor == "nvidia" and "nouveau" in gs[0].note
    assert gs[1].vendor == "other" and "virtio-pci" in gs[1].note
    assert gs[2].vendor == "nvidia" and "no kernel driver" in gs[2].note     # vendor from the PCI id alone


def test_nodes_come_back_in_numeric_order_and_missing_sysfs_is_empty(tmp_path):
    gs = detect(tmp_path, {130: AMD_CARD, 128: INTEL_IGPU, 129: NVIDIA})
    assert [g.node for g in gs] == [D128, D129, "/dev/dri/renderD130"]
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
    assert gs[1].vram_mb == 16376 and gs[0].vram_mb is None and calls[0][0] == "nvidia-smi"
    gpu.nvidia_vram(gs, lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called again")))


@pytest.mark.parametrize("name,vendor,flags,label", [
    ("AD103 [GeForce RTX 4080 SUPER]", "nvidia", dict(discrete=True), "NVIDIA GeForce RTX 4080 SUPER (discrete, relay)"),
    ("Raptor Lake-S GT1 [UHD Graphics 770]", "intel", {}, "Intel UHD Graphics 770 (integrated, Mesa)"),
    ("Intel GPU [8086:a780]", "intel", {}, "Intel GPU [8086:a780] (integrated, Mesa)"),
    ("Some Card", "amd", dict(discrete=True), "AMD Some Card (discrete, Mesa)"),
])
def test_labels_for_the_panel(name, vendor, flags, label):
    assert gpu.card_label(gpu.GpuInfo("/dev/dri/renderD128", "x", vendor, name, "", [vendor], **flags)) == label


# ------------------------------------------------------------------ the seven machines

def machine(tmp_path, cards, paths, *, runtime=READY, config=None):
    """(modes available, what each selectable mode resolves to) on one fake machine."""
    gs = detect(tmp_path, cards)
    config = config or Config()
    got = {}
    for mode in ("auto", "software", "igpu", "dgpu", "intel", "amd", "nvidia"):
        try:
            got[mode] = pick(mode, paths, gs, runtime=runtime, config=config)
        except ValueError:
            got[mode] = None
    return gpu.available_modes(gs, runtime, config.gpu.prefer), got


def test_machine_intel_igpu_only(tmp_path, paths):
    available, got = machine(tmp_path, {128: INTEL_IGPU}, paths)
    assert available == ["auto", "software", "igpu", "intel"]
    assert got == {"auto": ("intel", "igpu", D128), "software": ("software", "cpu", ""),
                   "igpu": ("intel", "igpu", D128), "intel": ("intel", "igpu", D128),
                   "dgpu": None, "amd": None, "nvidia": None}


def test_machine_intel_igpu_plus_nvidia(tmp_path, paths):
    available, got = machine(tmp_path, {128: INTEL_IGPU, 129: NVIDIA}, paths)
    assert available == ["auto", "software", "igpu", "dgpu", "intel", "nvidia"]
    assert got == {"auto": ("nvidia", "dgpu", D129), "software": ("software", "cpu", ""),
                   "igpu": ("intel", "igpu", D128), "intel": ("intel", "igpu", D128),
                   "dgpu": ("nvidia", "dgpu", D129), "nvidia": ("nvidia", "dgpu", D129), "amd": None}
    # Without the relay the card is there but cannot be used; auto falls back to the integrated GPU.
    available, got = machine(tmp_path / "b", {128: INTEL_IGPU, 129: NVIDIA}, paths, runtime=NOT_BUILT)
    assert available == ["auto", "software", "igpu", "intel"]
    assert got["auto"] == ("intel", "igpu", D128) and got["dgpu"] is None and got["nvidia"] is None


def test_machine_amd_igpu_plus_nvidia(tmp_path, paths):
    available, got = machine(tmp_path, {128: AMD_APU, 129: NVIDIA}, paths)
    assert available == ["auto", "software", "igpu", "dgpu", "amd", "nvidia"]
    assert got == {"auto": ("nvidia", "dgpu", D129), "software": ("software", "cpu", ""),
                   "igpu": ("amd", "igpu", D128), "amd": ("amd", "igpu", D128),
                   "dgpu": ("nvidia", "dgpu", D129), "nvidia": ("nvidia", "dgpu", D129), "intel": None}


def test_machine_amd_igpu_plus_amd_dgpu(tmp_path, paths):
    available, got = machine(tmp_path, {128: AMD_APU, 129: AMD_CARD}, paths)
    assert available == ["auto", "software", "igpu", "dgpu", "amd"]
    assert got["auto"] == ("amd", "dgpu", D129) and got["dgpu"] == ("amd", "dgpu", D129)
    assert got["igpu"] == ("amd", "igpu", D128)
    assert got["intel"] is None and got["nvidia"] is None
    # `amd` alone does not say which of the two: both qualify and gpu.assignment spreads Androids over them.
    gs = detect(tmp_path / "b", {128: AMD_APU, 129: AMD_CARD})
    assert {choose(resolved(id=i, gpu_mode="amd"), paths, gs).node for i in (1, 2)} == {D128, D129}
    # Pinning the node settles it.
    assert choose(resolved(gpu_mode="amd", gpu_node=D128), paths, gs).kind == "igpu"


def test_machine_intel_igpu_plus_intel_arc(tmp_path, paths):
    available, got = machine(tmp_path, {128: INTEL_IGPU, 129: INTEL_ARC}, paths)
    assert available == ["auto", "software", "igpu", "dgpu", "intel"]
    assert got["auto"] == ("intel", "dgpu", D129) and got["dgpu"] == ("intel", "dgpu", D129)
    assert got["igpu"] == ("intel", "igpu", D128)
    assert got["amd"] is None and got["nvidia"] is None
    c = choose(resolved(gpu_mode="dgpu"), paths, detect(tmp_path / "b", {128: INTEL_IGPU, 129: INTEL_ARC}))
    assert c.run_args == ["--device", D129] and c.boot_args[0] == "androidboot.redroid_gpu_mode=host"


def test_machine_two_nvidia_cards(tmp_path, paths):
    available, got = machine(tmp_path, {128: NVIDIA, 129: NVIDIA_2}, paths)
    assert available == ["auto", "software", "dgpu", "nvidia"]
    assert got["auto"][:2] == got["dgpu"][:2] == got["nvidia"][:2] == ("nvidia", "dgpu")
    assert got["igpu"] is None and got["intel"] is None and got["amd"] is None
    available, got = machine(tmp_path / "b", {128: NVIDIA, 129: NVIDIA_2}, paths, runtime=NOT_BUILT)
    assert available == ["auto", "software"] and got["auto"] == ("software", "cpu", "")


def test_machine_without_a_gpu(tmp_path, paths):
    available, got = machine(tmp_path, {}, paths)
    assert available == ["auto", "software"]
    assert got == {"auto": ("software", "cpu", ""), "software": ("software", "cpu", ""),
                   "igpu": None, "dgpu": None, "intel": None, "amd": None, "nvidia": None}


# ------------------------------------------------------------------ choose: explicit modes

def test_software_needs_nothing(paths):
    c = choose(resolved(gpu_mode="software"), paths, [])
    assert (c.mode, c.node, c.run_args, c.boot_args) == ("software", "", [], ["androidboot.redroid_gpu_mode=guest"])
    assert (c.kind, c.label) == ("cpu", "CPU only (software rendering)")


def test_intel_gets_the_device_and_host_mode(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    c = choose(resolved(gpu_mode="intel"), paths, gs)
    assert c.mode == "intel" and c.node == D128
    assert c.run_args == ["--device", D128]
    assert c.boot_args == ["androidboot.redroid_gpu_mode=host", f"androidboot.redroid_gpu_node={D128}"]
    assert c.kind == "igpu" and c.label == "Intel GPU [8086:a780] (integrated, Mesa)"


def test_amd_mode(tmp_path, paths):
    c = choose(resolved(gpu_mode="amd"), paths, detect(tmp_path, {128: AMD_CARD}))
    assert c.mode == "amd" and c.run_args == ["--device", D128] and c.kind == "dgpu"


def test_nvidia_mounts_runtime_and_socket_and_starts_the_relay(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    started = []
    c = choose(resolved(gpu_mode="nvidia"), paths, gs, relay=lambda cfg, p, node: started.append(node) or "dir")
    assert started == [D129]
    assert c.mode == "nvidia" and c.node == D129 and c.boot_args == ["androidboot.redroid_gpu_mode=venus"]
    assert c.kind == "dgpu" and c.label.endswith("(discrete, relay)")
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


def test_dgpu_that_is_nvidia_goes_through_the_relay_and_starts_it(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    started = []
    c = choose(resolved(gpu_mode="dgpu"), paths, gs, relay=lambda cfg, p, node: started.append(node) or "dir")
    assert started == [D129] and c.mode == "nvidia" and c.boot_args == ["androidboot.redroid_gpu_mode=venus"]
    started.clear()
    assert choose(resolved(gpu_mode="igpu"), paths, gs, relay=lambda *a: started.append(a)).mode == "intel"
    assert started == []          # an integrated GPU never needs the relay


def test_nvidia_debug_flag_and_custom_dirs(tmp_path, paths):
    cfg = Config.model_validate({"gpu": {"nvidia": {"debug": True, "runtime_dir": "/opt/rt", "socket_dir": "/tmp/s"}}})
    c = choose(resolved(cfg, gpu_mode="nvidia"), paths, detect(tmp_path, {129: NVIDIA}), config=cfg)
    assert "androidboot.redroid_gpu_debug=1" in c.boot_args
    assert c.run_args[1] == "/tmp/s/renderD129:/venus" and c.run_args[3].startswith("/opt/rt/gpu_config.sh:")


def test_requested_modes_that_cannot_work_raise_with_the_reason(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    with pytest.raises(ValueError, match="needs an AMD GPU with a Mesa driver, and this machine has none"):
        choose(resolved(gpu_mode="amd"), paths, gs)
    with pytest.raises(ValueError, match="not built.*build.sh"):
        choose(resolved(gpu_mode="nvidia"), paths, gs, runtime=NOT_BUILT)
    with pytest.raises(ValueError, match="not built.*build.sh"):
        choose(resolved(gpu_mode="dgpu"), paths, gs, runtime=NOT_BUILT)
    with pytest.raises(ValueError, match="needs an NVIDIA card"):
        choose(resolved(gpu_mode="nvidia"), paths, gs[:1])
    with pytest.raises(ValueError, match="needs a discrete GPU"):
        choose(resolved(gpu_mode="dgpu"), paths, gs[:1])
    with pytest.raises(ValueError, match="needs an integrated GPU"):
        choose(resolved(gpu_mode="igpu"), paths, gs[1:])
    nouveau = gpu.detect_gpus(fake_sysfs(tmp_path / "b", {128: NOUVEAU}), namer=None)
    for mode in ("nvidia", "dgpu"):
        with pytest.raises(ValueError, match="nouveau"):
            choose(resolved(gpu_mode=mode), paths, nouveau)

    def broken(cfg, p, node):
        raise venus.RelayError("cannot create /run/superfarm/venus")
    with pytest.raises(ValueError, match="cannot create"):
        choose(resolved(gpu_mode="nvidia"), paths, gs, relay=broken)
    with pytest.raises(ValueError, match="manifest"):
        choose(resolved(gpu_mode="nvidia"), paths, gs, runtime={"ready": True, "icd": False, "icd_path": "/x.json"})


def test_errors_name_the_choices_this_machine_does_have(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    with pytest.raises(ValueError, match=r"Valid choices on this machine: auto, software, igpu, dgpu, intel, nvidia\.$"):
        choose(resolved(gpu_mode="amd"), paths, gs)
    with pytest.raises(ValueError, match=r"Valid choices on this machine: auto, software, igpu, intel\.$"):
        choose(resolved(gpu_mode="nvidia"), paths, gs, runtime=NOT_BUILT)
    with pytest.raises(ValueError, match=r"Valid choices on this machine: auto, software\.$"):
        choose(resolved(gpu_mode="igpu"), paths, [])


def test_forced_node_must_exist_and_match_the_mode(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    with pytest.raises(ValueError, match="renderD140 does not exist"):
        choose(resolved(gpu_mode="intel", gpu_node="/dev/dri/renderD140"), paths, gs)
    with pytest.raises(ValueError, match="renderD129 is .*discrete NVIDIA card.* cannot be used in 'intel' mode"):
        choose(resolved(gpu_mode="intel", gpu_node=D129), paths, gs)
    with pytest.raises(ValueError, match="renderD129 is .* not an integrated GPU"):
        choose(resolved(gpu_mode="igpu", gpu_node=D129), paths, gs)
    with pytest.raises(ValueError, match="renderD128 is .*integrated Intel card.* not a discrete GPU"):
        choose(resolved(gpu_mode="dgpu", gpu_node=D128), paths, gs)
    assert choose(resolved(gpu_mode="igpu", gpu_node=D128), paths, gs).node == D128
    assert choose(resolved(gpu_mode="dgpu", gpu_node=D129), paths, gs).node == D129
    cfg = Config.model_validate({"gpu": {"render_node": D128}})
    assert choose(resolved(cfg), paths, gs, config=cfg).mode == "intel"      # auto + forced node follows the node
    with pytest.raises(ValueError, match="renderD129 was requested but cannot be used: .*not built"):
        choose(resolved(gpu_node=D129), paths, gs, runtime=NOT_BUILT)
    assert choose(resolved(gpu_mode="software", gpu_node=D129), paths, gs).mode == "software"   # the CPU has no card


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
    assert (c.mode, c.node) == ("amd", D129) and "discrete" in c.reason
    gs2 = gpu.detect_gpus(fake_sysfs(tmp_path / "b", {128: AMD_APU}), namer=None)
    c2 = choose(resolved(), paths, gs2)
    assert c2.mode == "amd" and "integrated" in c2.reason
    none = choose(resolved(), paths, [])
    assert none.mode == "software" and none.boot_args == ["androidboot.redroid_gpu_mode=guest"]
    unusable = gpu.detect_gpus(fake_sysfs(tmp_path / "c", {128: VIRTIO}), namer=None)
    assert choose(resolved(), paths, unusable).mode == "software"


def test_prefer_igpu_turns_the_auto_order_around(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    igpu_first = Config.model_validate({"gpu": {"prefer": "igpu"}})
    started = []
    c = choose(resolved(igpu_first), paths, gs, config=igpu_first, relay=lambda *a: started.append(a))
    assert (c.mode, c.kind, c.node) == ("intel", "igpu", D128) and started == []    # the card is left alone
    # With no integrated GPU the discrete card is still used.
    assert choose(resolved(igpu_first), paths, gs[1:], config=igpu_first).mode == "nvidia"
    # prefer only steers auto: an explicit request is not touched by it.
    assert choose(resolved(igpu_first, gpu_mode="dgpu"), paths, gs, config=igpu_first).mode == "nvidia"


def test_an_android_overrides_the_farm_default(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    farm = Config.model_validate({"gpu": {"mode": "igpu"}})
    assert choose(resolved(farm), paths, gs, config=farm).mode == "intel"
    assert choose(resolved(farm, gpu_mode="dgpu"), paths, gs, config=farm).mode == "nvidia"
    assert choose(resolved(farm, gpu_mode="software"), paths, gs, config=farm).mode == "software"


def test_planning_only_never_starts_a_relay(tmp_path, paths):
    gs = detect(tmp_path, {129: NVIDIA})
    assert choose(resolved(), paths, gs, relay=None).mode == "nvidia"


# ------------------------------------------------------------------ choose: several cards

def two_nvidia(tmp_path):
    return detect(tmp_path, {128: NVIDIA, 129: NVIDIA_2})


def test_round_robin_is_stable_per_instance(tmp_path, paths):
    gs = two_nvidia(tmp_path)
    nodes = [choose(resolved(id=i, gpu_mode="nvidia"), paths, gs).node for i in range(1, 7)]
    assert nodes == [D129, D128] * 3
    again = [choose(resolved(id=i, gpu_mode="nvidia"), paths, gs, counts={D128: 50}).node for i in range(1, 7)]
    assert again == nodes        # a rebuilt container must not hop cards because others started
    assert [choose(resolved(id=i, gpu_mode="dgpu"), paths, gs).node for i in range(1, 7)] == nodes


def test_first_and_least_instances(tmp_path, paths):
    gs = two_nvidia(tmp_path)
    first = Config.model_validate({"gpu": {"assignment": "first"}})
    assert {choose(resolved(first, id=i, gpu_mode="nvidia"), paths, gs, config=first).node for i in range(1, 5)} == {D128}
    least = Config.model_validate({"gpu": {"assignment": "least-instances"}})
    pick = lambda counts: choose(resolved(least, gpu_mode="nvidia"), paths, gs, config=least, counts=counts).node  # noqa: E731
    assert pick({D128: 3, D129: 1}) == D129
    assert pick({D128: 1, D129: 1}) == D128
    assert pick(None) == D128


def test_instance_pinned_to_one_of_several_cards(tmp_path, paths):
    c = choose(resolved(gpu_mode="nvidia", gpu_node=D129), paths, two_nvidia(tmp_path))
    assert c.node == D129 and c.run_args[1].startswith("/run/superfarm/venus/renderD129:")


# ------------------------------------------------------------------ options for the panel

def by_key(rows):
    return {(o["value"], o["node"]): o for o in rows}


def test_options_shape_and_order(tmp_path, paths):
    rows = gpu.options(Config(), paths, gpus=detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA}), runtime=READY)
    assert all(set(o) == {"value", "label", "available", "reason", "node"} for o in rows)
    assert [(o["value"], o["node"]) for o in rows] == [
        ("auto", ""), ("software", ""), ("igpu", ""), ("dgpu", ""), ("intel", ""), ("amd", ""), ("nvidia", ""),
        ("intel", D128), ("nvidia", D129)]
    o = by_key(rows)
    assert all(o[k]["available"] for k in o if k != ("amd", ""))
    assert not o[("amd", "")]["available"] and "this machine has none" in o[("amd", "")]["reason"]
    assert o[("auto", "")]["label"] == "Automatic (now: NVIDIA GPU [10de:2702] (discrete, relay))"
    assert o[("igpu", "")]["label"] == "Integrated GPU: Intel GPU [8086:a780]"
    assert o[("dgpu", "")]["label"] == "Discrete GPU: NVIDIA GPU [10de:2702]"
    assert o[("nvidia", D129)]["label"] == "NVIDIA GPU [10de:2702] (discrete, relay) on renderD129"
    assert o[("software", "")]["label"] == "CPU only (software rendering)"


def test_options_say_why_the_nvidia_card_cannot_be_chosen_yet(tmp_path, paths):
    rows = gpu.options(Config(), paths, gpus=detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA}), runtime=NOT_BUILT)
    o = by_key(rows)
    for key in (("dgpu", ""), ("nvidia", ""), ("nvidia", D129)):
        assert not o[key]["available"] and "not built" in o[key]["reason"] and "build.sh" in o[key]["reason"]
    assert o[("igpu", "")]["available"] and o[("intel", D128)]["available"]
    assert o[("auto", "")]["available"] and "Intel" in o[("auto", "")]["label"] and "not built" in o[("auto", "")]["reason"]


def test_options_on_other_machines(tmp_path, paths):
    none = by_key(gpu.options(Config(), paths, gpus=[], runtime=READY))
    assert [k for k, o in none.items() if o["available"]] == [("auto", ""), ("software", "")]
    assert none[("auto", "")]["label"] == "Automatic (now: CPU only (software rendering))"
    assert "needs an integrated GPU" in none[("igpu", "")]["reason"]

    two = gpu.options(Config(), paths, gpus=two_nvidia(tmp_path), runtime=READY)
    assert [(o["value"], o["node"]) for o in two][-2:] == [("nvidia", D128), ("nvidia", D129)]   # one entry per card

    odd = by_key(gpu.options(Config(), paths, runtime=READY,
                             gpus=detect(tmp_path / "b", {128: NOUVEAU, 129: VIRTIO})))
    assert not odd[("nvidia", D128)]["available"] and "nouveau" in odd[("nvidia", D128)]["reason"]
    assert not any(node == D129 for _, node in odd)          # no mode can name a virtual GPU

    prefer = Config.model_validate({"gpu": {"prefer": "igpu"}})
    rows = by_key(gpu.options(prefer, paths, gpus=detect(tmp_path / "c", {128: INTEL_IGPU, 129: NVIDIA}), runtime=READY))
    assert "Intel" in rows[("auto", "")]["label"]


def test_options_start_nothing(tmp_path, paths, monkeypatch):
    monkeypatch.setattr(venus, "ensure_relay", lambda *a, **k: (_ for _ in ()).throw(AssertionError("relay started")))
    found = detect(tmp_path, {129: NVIDIA})
    monkeypatch.setattr(gpu, "detect_gpus", lambda *a, **k: found)
    rows = gpu.options(Config(), paths)          # detection and the runtime check are the real code paths
    assert by_key(rows)[("nvidia", "")]["available"] is False       # no runtime was built in the temporary home


def test_planned_mode_for_capacity(tmp_path):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    cfg = lambda **g: Config.model_validate({"gpu": g})  # noqa: E731
    assert gpu.planned_mode(cfg(), gs) == "nvidia"
    assert gpu.planned_mode(cfg(prefer="igpu"), gs) == "intel"
    assert gpu.planned_mode(cfg(mode="igpu"), gs) == "intel"
    assert gpu.planned_mode(cfg(mode="dgpu"), gs) == "nvidia"
    assert gpu.planned_mode(cfg(mode="dgpu"), gs[:1]) == "software"
    assert gpu.planned_mode(cfg(mode="amd"), gs) == "amd"            # an explicit vendor is taken at its word
    assert gpu.planned_mode(cfg(), []) == "software"


def test_summary_lists_modes_available_now(tmp_path, paths):
    gs = detect(tmp_path, {128: INTEL_IGPU, 129: NVIDIA})
    s = gpu.summary(Config(), paths, gs)
    assert s["available_modes"] == ["software", "igpu", "intel"] and "not built" in s["nvidia_blocker"]
    assert len(s["gpus"]) == 2 and s["gpus"][1]["vendor"] == "nvidia"
    assert s["gpus"][0]["integrated"] is True and s["gpus"][1]["discrete"] is True

"""gpu.mode `balanced`: one farm shared across every usable card and the CPU, the pin by
PCI address, and one relay per NVIDIA card.

Every machine here is a fake sysfs tree or a list of GpuInfo. Nothing in this file ran on a
machine with more than one discrete card."""
import pytest

from superfarm.config import Config
from superfarm.core.instances import GpuLoad, _gpu_counts
from superfarm.core.models import InstanceSpec, resolve
from superfarm.host import capacity, gpu, venus
from superfarm.host.gpu import GpuInfo
from superfarm.paths import get_paths

READY = {"ready": True, "icd": True, "missing": []}
NOT_BUILT = {"ready": False, "icd": True, "missing": ["/x/overlays/venus/vulkan.virtio.so"]}


def card(number, vendor="nvidia", *, vram=None, discrete=True, pci=None):
    driver = {"nvidia": "nvidia", "intel": "i915", "amd": "amdgpu"}[vendor]
    return GpuInfo(f"/dev/dri/renderD{number}", driver, vendor, f"{vendor} card {number}", pci or f"0000:{number - 127:02x}:00.0",
                   [vendor], discrete, vram)


IGPU = card(128, "intel", discrete=False, pci="0000:00:02.0")


@pytest.fixture
def paths(tmp_path):
    return get_paths(tmp_path / "home")


def config(**gpu_settings):
    return Config.model_validate({"gpu": {"mode": "balanced", **gpu_settings}})


def start(gpus, cfg, paths, counts, *, id=1, runtime=READY, relay=None, **spec):
    r = resolve(InstanceSpec(id=id, **spec), cfg)
    return gpu.choose(r, cfg, paths, gpus=gpus, runtime=runtime, ensure_relay=relay, counts=counts)


def fill(gpus, cfg, paths, how_many, **kw):
    """Start Androids one after another, each seeing the ones before it. Returns where each landed."""
    counts, landed = {}, []
    for n in range(1, how_many + 1):
        choice = start(gpus, cfg, paths, dict(counts), id=n, **kw)
        landed.append(choice.node or "cpu")
        if choice.node:
            counts[choice.node] = counts.get(choice.node, 0) + 1
    return landed


def tally(landed):
    return {where: landed.count(where) for where in dict.fromkeys(landed)}


# ------------------------------------------------------------------ sharing

def test_four_equal_cards_share_equally(paths):
    gpus = [card(n, vram=24576) for n in (128, 129, 130, 131)]
    landed = fill(gpus, config(vram_reserve_mb=0), paths, 40)
    assert tally(landed) == {g.node: 10 for g in gpus}
    assert landed[:4] == [g.node for g in gpus]          # the first four take one card each


def test_a_large_card_takes_proportionally_more_than_a_small_one(paths):
    small, large = card(128, vram=4096), card(129, vram=49152)
    cfg = config(vram_reserve_mb=0)
    assert gpu.card_slots(small, cfg) == 11 and gpu.card_slots(large, cfg) == 136
    counts = tally(fill([small, large], cfg, paths, 49))
    assert counts[large.node] == 45 and counts[small.node] == 4
    # Filled to the brim, each holds what its memory allows and the rest draws on the CPU.
    assert tally(fill([small, large], cfg, paths, 150)) == {large.node: 136, small.node: 11, "cpu": 3}


def test_a_small_card_under_the_desktop_reserve_takes_few_or_none(paths):
    two_gb = card(129, "amd", vram=2048)
    assert gpu.card_slots(two_gb, config()) == 1                       # (2048 - 1536) // 360
    assert gpu.card_slots(two_gb, config(vram_reserve_mb=0)) == 5
    assert gpu.card_slots(card(129, "amd", vram=1024), config()) == 0
    # A card with no room at all is passed over from the first Android on.
    assert tally(fill([IGPU, card(129, "amd", vram=1024)], config(), paths, 3)) == {IGPU.node: 3}


def test_integrated_and_discrete_then_the_cpu(paths):
    dgpu = card(129, vram=16376)                                       # (16376 - 1536) // 360 = 41
    cfg = config()
    assert gpu.card_slots(dgpu, cfg) == 41 and gpu.card_slots(IGPU, cfg) is None
    first = fill([IGPU, dgpu], cfg, paths, 10)
    assert first[0] == dgpu.node and tally(first) == {dgpu.node: 5, IGPU.node: 5}     # gpu.prefer breaks the tie
    # The integrated GPU has no limit of its own, so it takes everything the card cannot.
    assert tally(fill([IGPU, dgpu], cfg, paths, 100)) == {dgpu.node: 41, IGPU.node: 59}
    # With a figure for it, the CPU takes what is left.
    capped = config(balance={"per_igpu": 8})
    assert tally(fill([IGPU, dgpu], capped, paths, 60)) == {dgpu.node: 41, IGPU.node: 8, "cpu": 11}
    last = start([IGPU, dgpu], capped, paths, {dgpu.node: 41, IGPU.node: 8})
    assert last.mode == "software" and "every card is full" in last.reason and "41 of 41" in last.reason


def test_prefer_igpu_reverses_the_tie(paths):
    dgpu = card(129, vram=16376)
    assert fill([IGPU, dgpu], config(prefer="igpu"), paths, 2) == [IGPU.node, dgpu.node]


def test_overflow_refuse_fails_the_start_and_says_how_to_change_it(paths):
    cfg = config(balance={"per_dgpu": 2, "overflow": "refuse"})
    gpus = [card(128), card(129)]
    assert tally(fill(gpus, cfg, paths, 4)) == {gpus[0].node: 2, gpus[1].node: 2}
    with pytest.raises(ValueError, match="every card is full.*gpu.balance.overflow"):
        start(gpus, cfg, paths, {gpus[0].node: 2, gpus[1].node: 2})


def test_an_android_keeps_its_card_while_that_card_has_room(paths):
    a, b = card(128, vram=8192), card(129, vram=8192)
    cfg = config(vram_reserve_mb=0)
    load = GpuLoad({a.node: 3, b.node: 20})
    load.previous = b.node
    assert start([a, b], cfg, paths, load).node == b.node               # not rebuilt for a better balance
    load = GpuLoad({a.node: 3, b.node: 22})                             # 8192 // 360 = 22: b is full now
    load.previous = b.node
    assert start([a, b], cfg, paths, load).node == a.node


def test_running_androids_are_counted_and_the_own_card_is_remembered():
    def container(instance, node, state="running", role="android"):
        return {"state": state, "labels": {"io.superfarm.instance": str(instance), "io.superfarm.role": role,
                                           "io.superfarm.gpu-node": node}}
    from superfarm.core import instances
    rows = [container(1, "/dev/dri/renderD128"), container(2, "/dev/dri/renderD128"), container(3, "/dev/dri/renderD129"),
            container(4, "/dev/dri/renderD129", state="exited"), container(5, "/dev/dri/renderD128", role="net")]
    for row in rows:                                                     # the real label names, whatever they are
        row["labels"] = {instances.LABEL_INSTANCE: row["labels"]["io.superfarm.instance"],
                         instances.LABEL_ROLE: row["labels"]["io.superfarm.role"],
                         instances.LABEL_GPU_NODE: row["labels"]["io.superfarm.gpu-node"]}
    load = _gpu_counts(rows, 3)
    assert load == {"/dev/dri/renderD128": 2} and load.previous == "/dev/dri/renderD129"
    stopped = _gpu_counts(rows, 4)                                       # a stopped Android remembers its card too
    assert stopped == {"/dev/dri/renderD128": 2, "/dev/dri/renderD129": 1} and stopped.previous == "/dev/dri/renderD129"
    # Starts that are under way count from the moment they picked a card: Android 6 and 7
    # are being started and have no running container yet; Android 1 has one and is not
    # counted twice; an Android drawing on the CPU ("") takes no card.
    under_way = _gpu_counts(rows, 9, {6: "/dev/dri/renderD129", 7: "/dev/dri/renderD129", 1: "/dev/dri/renderD128", 8: "", 9: "/dev/dri/renderD128"})
    assert under_way == {"/dev/dri/renderD128": 2, "/dev/dri/renderD129": 3}


# ------------------------------------------------------------------ what is not there

def test_nvidia_that_is_not_built_is_passed_over_with_the_reason(paths):
    dgpu = card(129, vram=16376)
    c = start([IGPU, dgpu], config(), paths, {}, runtime=NOT_BUILT)
    assert c.node == IGPU.node and gpu.NVIDIA_SKIPPED in c.reason and "not built" in c.reason
    alone = start([dgpu], config(), paths, {}, runtime=NOT_BUILT)
    assert alone.mode == "software" and "no usable GPU" in alone.reason


def test_a_relay_that_does_not_start_moves_on_to_the_next_card(paths):
    a, b = card(128, vram=8192), card(129, vram=8192)
    asked = []

    def relay(cfg, paths_, node):
        asked.append(node)
        if node == a.node:
            raise venus.RelayError("the relay for renderD128 did not start")
    c = start([a, b], config(vram_reserve_mb=0), paths, {}, relay=relay)
    assert c.node == b.node and asked == [a.node, b.node] and "did not start" in c.reason


def test_no_gpu_at_all_draws_on_the_cpu_and_cards_android_cannot_use_do_not_count(paths):
    assert start([], config(), paths, {}).mode == "software"
    virtual = GpuInfo("/dev/dri/renderD128", "virtio_gpu", "other", "Virtual GPU", "0000:00:01.0", [], False, None, "not usable")
    assert start([virtual], config(), paths, {}).mode == "software"


def test_processor_graphics_is_not_limited_by_the_memory_it_reports(paths):
    apu = card(128, "amd", vram=512, discrete=False, pci="0000:c5:00.0")
    assert gpu.card_slots(apu, config()) is None                         # a carve-out of system RAM, not a ceiling
    assert tally(fill([apu], config(), paths, 30)) == {apu.node: 30}


def test_balanced_is_always_offered_and_says_what_it_shares(paths):
    gpus = [IGPU, card(129, vram=16376)]
    assert gpu.available_modes(gpus, READY)[:3] == ["auto", "balanced", "software"]
    rows = {o["value"]: o for o in gpu.options(Config(), paths, gpus=gpus, runtime=READY) if not o["node"]}
    assert rows["balanced"]["available"] and rows["balanced"]["label"] == "Balanced: shared across 2 GPUs, then the CPU"
    rows = {o["value"]: o for o in gpu.options(Config(), paths, gpus=gpus, runtime=NOT_BUILT) if not o["node"]}
    assert rows["balanced"]["label"] == "Balanced: shared across 1 GPU, then the CPU"
    rows = {o["value"]: o for o in gpu.options(Config(), paths, gpus=[], runtime=READY) if not o["node"]}
    assert "CPU" in rows["balanced"]["label"]


# ------------------------------------------------------------------ pinning a card

def test_a_card_is_pinned_by_node_pci_address_or_by_path_link(paths):
    a, b = card(128, vram=8192, pci="0000:41:00.0"), card(129, vram=8192, pci="0000:81:00.0")
    for pin in (b.node, "0000:81:00.0", "/dev/dri/by-path/pci-0000:81:00.0-render"):
        assert start([a, b], Config(), paths, {}, gpu_mode="nvidia", gpu_node=pin).node == b.node
    # The same pin after a boot that numbered the cards the other way round.
    swapped = [card(128, vram=8192, pci="0000:81:00.0"), card(129, vram=8192, pci="0000:41:00.0")]
    assert start(swapped, Config(), paths, {}, gpu_mode="nvidia", gpu_node="0000:81:00.0").node == "/dev/dri/renderD128"
    # A pin leaves nothing to share: balanced with a pinned card is that card.
    assert start([a, b], config(), paths, {a.node: 0, b.node: 20}, gpu_node="0000:81:00.0").node == b.node
    with pytest.raises(ValueError, match="pinned card 0000:99:00.0 does not exist"):
        start([a, b], Config(), paths, {}, gpu_mode="nvidia", gpu_node="0000:99:00.0")
    farm_wide = Config.model_validate({"gpu": {"mode": "nvidia", "render_node": "0000:41:00.0"}})
    assert start([a, b], farm_wide, paths, {}).node == a.node


def test_pin_formats_are_validated():
    for good in ("/dev/dri/renderD129", "0000:81:00.0", "10000:01:00.0", "/dev/dri/by-path/pci-0000:81:00.0-render", ""):
        assert InstanceSpec(id=1, gpu_node=good).gpu_node == good
    for bad in ("renderD129", "/dev/sda", "81:00.0", "0000:81:00", "$(reboot)"):
        with pytest.raises(ValueError):
            InstanceSpec(id=1, gpu_node=bad)


def test_pci_addresses_compare_equal_however_the_domain_is_written():
    assert gpu.pci_key("0000:01:00.0") == gpu.pci_key("00000000:01:00.0") == (0, "01:00.0")
    assert gpu.pci_key("0001:01:00.0") == gpu.pci_key("00000001:01:00.0") == (1, "01:00.0")
    assert gpu.pci_key("10000:e1:00.0") == (0x10000, "e1:00.0")          # behind Intel VMD
    assert gpu.pci_key("01:00.0") is None and gpu.pci_key("") is None and gpu.pci_key("nonsense") is None


def test_video_memory_is_matched_to_cards_in_any_pci_domain():
    from superfarm.util.proc import Result
    cards = [card(128, pci="0000:01:00.0"), card(129, pci="0001:01:00.0"), card(130, pci="10000:01:00.0")]
    gpu.nvidia_vram(cards, lambda argv, **kw: Result(0, "00000000:01:00.0, 24576\n00000001:01:00.0, 49152\n00010000:01:00.0, 81559\n", ""))
    assert [g.vram_mb for g in cards] == [24576, 49152, 81559]


# ------------------------------------------------------------------ one relay per card

def nvidia_sysfs(tmp_path, addresses):
    sysfs = tmp_path / "sys"
    driver = sysfs / "bus/pci/drivers/nvidia"
    driver.mkdir(parents=True)
    for index, address in enumerate(addresses):
        device = sysfs / "devices/pci0000:00" / address
        device.mkdir(parents=True)
        (device / "driver").symlink_to(driver)
        node = sysfs / "class/drm" / f"renderD{128 + index}"
        node.mkdir(parents=True)
        (node / "device").symlink_to(device)
    return sysfs


def test_each_relay_is_limited_to_its_card_when_there_are_several(tmp_path, paths):
    two = nvidia_sysfs(tmp_path / "two", ["0000:41:00.0", "0000:81:00.0"])
    assert venus.nvidia_cards(two) == {"renderD128": "0000:41:00.0", "renderD129": "0000:81:00.0"}
    assert venus.card_filter(Config(), "/dev/dri/renderD129", two) == {
        "DRI_PRIME": "pci-0000_81_00_0!", "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE": "1"}
    _argv, env = venus.relay_command(Config(), paths, "/dev/dri/renderD128", sysfs=two)
    assert env["DRI_PRIME"] == "pci-0000_41_00_0!" and "VK_ICD_FILENAMES" in env
    # One card: nothing changes for the machine this was measured on.
    one = nvidia_sysfs(tmp_path / "one", ["0000:01:00.0"])
    assert venus.card_filter(Config(), "/dev/dri/renderD128", one) == {}
    assert "DRI_PRIME" not in venus.relay_command(Config(), paths, "/dev/dri/renderD128", sysfs=one)[1]
    # And it can be switched off.
    off = Config.model_validate({"gpu": {"nvidia": {"isolate_cards": False}}})
    assert venus.card_filter(off, "/dev/dri/renderD129", two) == {}
    assert venus.nvidia_cards(tmp_path / "no-sysfs") == {}


def test_the_device_select_layer_is_looked_for_where_distributions_put_it(tmp_path):
    assert not venus.device_select_layer(tmp_path)
    folder = tmp_path / "usr/share/vulkan/implicit_layer.d"
    folder.mkdir(parents=True)
    (folder / "VkLayer_MESA_device_select.json").write_text("{}")
    assert venus.device_select_layer(tmp_path)


# ------------------------------------------------------------------ the capacity estimate

def machine(tmp_path, ram_gb):
    (tmp_path / "proc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "proc/meminfo").write_text(f"MemTotal: {ram_gb * 1024 * 1024} kB\nMemAvailable: {ram_gb * 1024 * 1024} kB\nSwapTotal: 0 kB\n")
    (tmp_path / "proc/swaps").write_text("Filename\tType\tSize\tUsed\tPriority\n")
    return tmp_path


def estimate(cfg, gpus, tmp_path, *, ram_gb=512, threads=128, runtime=READY):
    return capacity.estimate(cfg, gpus, root=machine(tmp_path, ram_gb), cpu_threads=threads, runtime=runtime, paths=get_paths("/farm"))


def test_the_estimate_counts_the_mix_android_by_android(tmp_path):
    gpus = [card(n, vram=24576) for n in (128, 129, 130, 131)]            # 68 each with no reserve
    est = estimate(config(vram_reserve_mb=0), gpus, tmp_path)
    assert est["mode"] == "balanced" and est["requested_mode"] == "balanced" and est["mode_available"]
    # 128 threads at 80% = 10240 points of CPU: 155 Androids at 66 each fit, and 272 slots are there.
    assert est["binding"] == "cpu" and est["max_instances"] == 155
    assert [(s["mode"], s["count"], s["slots"]) for s in est["mix"]] == [("nvidia", 39, 68), ("nvidia", 39, 68), ("nvidia", 39, 68), ("nvidia", 38, 68)]
    assert sum(s["count"] for s in est["mix"]) == est["max_instances"]
    assert est["limits"]["cpu"]["percent_per_android"] == 66 and est["limits"]["ram"]["per_android_mb"] == 1130      # 1000 + 300 for NVIDIA - 170 trimmed
    assert any(line.startswith("Balanced mode shares them") for line in est["advice"])


def test_the_estimate_shows_what_ends_up_on_the_cpu(tmp_path):
    small = [card(128, vram=4096), card(129, vram=4096)]                  # 11 each
    est = estimate(config(vram_reserve_mb=0), small, tmp_path, threads=64)
    on = {s["label"]: s["count"] for s in est["mix"]}
    assert on[gpu.SOFTWARE_LABEL] > 0 and sum(n for label, n in on.items() if label != gpu.SOFTWARE_LABEL) == 22
    # 64 threads at 80% = 5120; 22 x 66 = 1452 on the cards leaves room for 24 at 148 on the CPU.
    assert est["max_instances"] == 22 + 24 and est["binding"] == "cpu"
    assert "on the CPU cost the most" in " ".join(est["advice"])
    # The same machine in plain nvidia mode stops at its video memory.
    plain = estimate(Config.model_validate({"gpu": {"mode": "nvidia", "vram_reserve_mb": 0}}), small, tmp_path / "b", threads=64)
    assert plain["binding"] == "vram" and plain["max_instances"] == 22 and "balanced" in " ".join(plain["advice"])


def test_refuse_makes_the_cards_the_limit(tmp_path):
    cfg = config(balance={"per_dgpu": 5, "overflow": "refuse"})
    est = estimate(cfg, [card(128), card(129)], tmp_path)
    assert est["binding"] == "cards" and est["max_instances"] == 10 and est["limits"]["cards"]["max"] == 10
    assert "gpu.balance.overflow" in " ".join(est["advice"])


def test_memory_can_be_what_stops_a_balanced_fleet(tmp_path):
    est = estimate(config(vram_reserve_mb=0), [card(128, vram=49152)], tmp_path, ram_gb=32)
    # (32768 - 3072) MB at 1130 MB per NVIDIA Android.
    assert est["binding"] == "ram" and est["max_instances"] == (32 * 1024 - 3072) // 1130


def test_the_video_memory_reserve_is_a_setting(tmp_path):
    one = [card(129, vram=16376)]
    desktop = estimate(Config.model_validate({"gpu": {"mode": "nvidia"}}), one, tmp_path)
    server = estimate(Config.model_validate({"gpu": {"mode": "nvidia", "vram_reserve_mb": 0}}), one, tmp_path / "b")
    assert desktop["limits"]["vram"]["max"] == 41 and desktop["limits"]["vram"]["reserve_mb"] == 1536
    assert server["limits"]["vram"]["max"] == 45 and server["limits"]["vram"]["reserve_mb"] == 0


def test_balanced_without_a_gpu_is_the_cpu(tmp_path):
    est = estimate(config(), [], tmp_path, threads=32)
    assert est["mode"] == "balanced" and [s["mode"] for s in est["mix"]] == ["software"]
    assert est["max_instances"] == int(32 * 100 * 0.8 // 148)

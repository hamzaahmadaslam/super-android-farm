"""The container planner: pure functions, so every field can be pinned down exactly."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core_fakes import FakeGpu, labels_of, option, options  # noqa: E402

from superfarm.config import Config  # noqa: E402
from superfarm.core.models import DeviceIdentity, InstanceSpec, resolve  # noqa: E402
from superfarm.core.planner import (  # noqa: E402
    Core,
    CpuTopology,
    allocate_cpuset,
    boot_arguments,
    build_topology,
    cpuset_fits,
    cpuset_from_label,
    cpuset_label,
    format_cpu_list,
    parse_cpu_list,
    plan_android,
    plan_hash,
)


def make(spec: dict | None = None, config: dict | None = None):
    cfg = Config.model_validate(config or {})
    return resolve(InstanceSpec.model_validate({"id": 7, **(spec or {})}), cfg)


def plan(r, gpu=None, extra=None, digest="abc123"):
    return plan_android(r, gpu=gpu or FakeGpu(), extra_run_args=extra or [], plan_hash=digest)


def split(args: list[str], image: str) -> tuple[list[str], list[str]]:
    i = args.index(image)
    return args[:i], args[i + 1:]


# ------------------------------------------------------------------ run arguments

def test_defaults_give_the_tuned_container():
    r = make()
    opts, boot = split(plan(r), r.image)
    assert option(opts, "--name") == "sf-android-7"
    assert "--privileged" in opts
    assert option(opts, "--network") == "container:sf-net-7"
    assert option(opts, "--cpus") == "2"
    assert option(opts, "--memory") == "1536m"
    assert option(opts, "--memory-swap") == "2560m"
    assert option(opts, "--pids-limit") == "4096"
    assert option(opts, "--shm-size") == "64m"
    assert option(opts, "--restart") == "no"
    assert option(opts, "--stop-timeout") == "10"
    assert option(opts, "--log-driver") == "json-file"
    assert options(opts, "--log-opt") == ["max-size=10m", "max-file=2"]
    assert options(opts, "-v") == ["sf-data-7:/data"]
    assert boot[:4] == ["androidboot.redroid_width=480", "androidboot.redroid_height=854",
                        "androidboot.redroid_dpi=160", "androidboot.redroid_fps=20"]
    assert "androidboot.redroid_gpu_mode=guest" in boot


def test_optional_limits_are_absent_until_configured():
    opts, _ = split(plan(make()), make().image)
    for flag in ("--memory-reservation", "--cpuset-cpus", "--cpu-shares", "--oom-score-adj"):
        assert flag not in opts


def test_every_resource_setting_reaches_the_arguments():
    r = make(
        {"resources": {"cpus": 1.5, "memory_mb": 2048, "memory_swap_mb": 3072, "cpuset": "8,9"}},
        {"resources": {"memory_reservation_mb": 1024, "cpu_shares": 512, "pids_limit": 2000, "shm_mb": 128,
                       "oom_score_adj": 300},
         "performance": {"restart_policy": "unless-stopped"},
         "docker": {"name_prefix": "lab", "stop_timeout": 25, "log_max_size": "5m", "log_max_files": 4}})
    opts, _ = split(plan(r), r.image)
    assert option(opts, "--name") == "lab-android-7"
    assert option(opts, "--network") == "container:lab-net-7"
    assert option(opts, "--cpus") == "1.5"
    assert option(opts, "--memory") == "2048m"
    assert option(opts, "--memory-swap") == "3072m"
    assert option(opts, "--memory-reservation") == "1024m"
    assert option(opts, "--cpuset-cpus") == "8,9"
    assert option(opts, "--cpu-shares") == "512"
    assert option(opts, "--pids-limit") == "2000"
    assert option(opts, "--shm-size") == "128m"
    assert option(opts, "--oom-score-adj") == "300"
    assert option(opts, "--restart") == "unless-stopped"
    assert option(opts, "--stop-timeout") == "25"
    assert options(opts, "--log-opt") == ["max-size=5m", "max-file=4"]
    assert options(opts, "-v") == ["lab-data-7:/data"]


def test_no_swap_means_swap_limit_equals_memory():
    r = make(config={"resources": {"memory_mb": 1024, "memory_swap_mb": 0}})
    assert option(plan(r), "--memory-swap") == "1024m"


def test_screen_and_fps_become_boot_arguments():
    r = make({"width": 720, "height": 1280, "dpi": 320, "fps": 30})
    _, boot = split(plan(r), r.image)
    assert {"androidboot.redroid_width=720", "androidboot.redroid_height=1280", "androidboot.redroid_dpi=320",
            "androidboot.redroid_fps=30"} <= set(boot)


def test_labels_identify_farm_instance_role_plan_gpu_and_cpuset():
    r = make({"resources": {"cpuset": "0-1,16"}})
    gpu = FakeGpu(mode="intel", node="/dev/dri/renderD128")
    labels = labels_of(plan(r, gpu=gpu, digest="deadbeef"))
    assert labels == {
        "io.superfarm.farm": "sf", "io.superfarm.instance": "7", "io.superfarm.role": "android",
        "io.superfarm.plan": "deadbeef", "io.superfarm.gpu": "intel",
        "io.superfarm.gpu-node": "/dev/dri/renderD128", "io.superfarm.cpuset": "0-1+16",
    }
    # `docker ps` joins labels with commas, so no label value may contain one.
    assert all("," not in v for v in labels.values())
    assert cpuset_from_label(cpuset_label("0-1,16")) == "0-1,16"


def test_gpu_and_extra_arguments_sit_before_the_image():
    r = make()
    gpu = FakeGpu(mode="intel", node="/dev/dri/renderD128", run_args=["--device", "/dev/dri/renderD128"],
                  boot_args=["androidboot.redroid_gpu_mode=host", "androidboot.redroid_gpu_node=/dev/dri/renderD128"])
    args = plan(r, gpu=gpu, extra=["-v", "/run/mic/7:/dev/mic"])
    opts, boot = split(args, r.image)
    assert option(opts, "--device") == "/dev/dri/renderD128"
    assert "/run/mic/7:/dev/mic" in options(opts, "-v")
    assert "androidboot.redroid_gpu_mode=host" in boot
    assert "androidboot.redroid_gpu_node=/dev/dri/renderD128" in boot


def test_adopted_volume_and_custom_image_are_used():
    r = make({"data_volume": "old_android_data_3", "image": "registry.example/custom:1"})
    args = plan(r)
    assert options(args, "-v") == ["old_android_data_3:/data"]
    assert "registry.example/custom:1" in args


@pytest.mark.parametrize("volume", ["/etc", "../x", "a:b", "data vol", "-v"])
def test_a_host_path_can_never_become_the_data_volume(volume):
    r = make().model_copy(update={"data_volume": volume})
    with pytest.raises(ValueError, match="volume"):
        plan(r)


@pytest.mark.parametrize("image", ["--privileged", "-v=/:/host", "", "bad image"])
def test_an_image_name_cannot_be_an_option(image):
    with pytest.raises(ValueError, match="image"):
        plan(make().model_copy(update={"image": image}))


def test_bad_cpuset_and_log_size_are_refused():
    with pytest.raises(ValueError, match="cpuset"):
        plan(make().model_copy(update={"cpuset": "0;rm"}))
    with pytest.raises(ValueError, match="log size"):
        plan(make().model_copy(update={"log_max_size": "10m --privileged"}))


# ------------------------------------------------------------------ boot arguments

def test_device_identity_is_passed_at_boot():
    ident = DeviceIdentity(profile="x", serial="R58N123ABCD", locale="de-DE",
                           props={"ro.product.model": "SM-S911B", "ro.product.brand": "samsung"})
    r = make({"device": ident.model_dump()})
    boot = boot_arguments(r, FakeGpu())
    assert "ro.product.model=SM-S911B" in boot
    assert "ro.product.brand=samsung" in boot
    assert "androidboot.serialno=R58N123ABCD" in boot
    assert "ro.product.locale=de-DE" in boot


def test_extra_boot_arguments_override_instead_of_duplicating():
    # Android keeps the first value of a read-only property, so an override must replace.
    r = make({"extra_boot_args": ["androidboot.redroid_fps=60", "ro.secure=0"]},
             {"android": {"extra_boot_args": ["androidboot.use_memfd=true"]}})
    boot = boot_arguments(r, FakeGpu())
    assert boot.count("androidboot.redroid_fps=60") == 1
    assert not any(a == "androidboot.redroid_fps=20" for a in boot)
    assert "ro.secure=0" in boot and "androidboot.use_memfd=true" in boot
    assert len({a.split("=")[0] for a in boot}) == len(boot)


def test_a_boot_argument_with_a_space_is_refused():
    r = make(config={"android": {"extra_boot_args": ["ro.product.model=Pixel 7"]}})
    with pytest.raises(ValueError, match="spaces"):
        plan(r)
    r = make().model_copy(update={"device": DeviceIdentity(props={"ro.product.model": "Pixel 7"})})
    with pytest.raises(ValueError, match="spaces"):
        plan(r)


# ----------------------------------------------------------------------- plan hash

def test_hash_is_stable_and_embedded():
    r = make()
    net = ["--name", "sf-net-7", "superfarm/net"]
    h = plan_hash(r, FakeGpu(), net)
    assert h == plan_hash(make(), FakeGpu(), list(net))
    assert len(h) == 24 and all(c in "0123456789abcdef" for c in h)
    assert labels_of(plan(r, digest=h))["io.superfarm.plan"] == h


def test_hash_changes_with_anything_that_shapes_the_containers():
    r, gpu, net = make(), FakeGpu(), ["--name", "sf-net-7", "superfarm/net"]
    base = plan_hash(r, gpu, net)
    assert plan_hash(make({"fps": 30}), gpu, net) != base
    assert plan_hash(make({"resources": {"memory_mb": 2048}}), gpu, net) != base
    assert plan_hash(r, FakeGpu(mode="intel", node="/dev/dri/renderD128"), net) != base
    assert plan_hash(r, gpu, [*net, "vpn"]) != base
    assert plan_hash(r, gpu, net, extra_run_args=["-v", "/a:/b"]) != base
    assert plan_hash(r, gpu, net, image_id="sha256:new") != base


def test_renaming_does_not_change_the_hash():
    net = ["--name", "sf-net-7", "superfarm/net"]
    assert plan_hash(make({"name": "A"}), FakeGpu(), net) == plan_hash(make({"name": "B", "notes": "x"}), FakeGpu(), net)


# ---------------------------------------------------------------------- cpu lists

def test_cpu_lists_round_trip():
    assert parse_cpu_list("0-3,8,10-11") == [0, 1, 2, 3, 8, 10, 11]
    assert parse_cpu_list("") == [] and parse_cpu_list(" 5 \n") == [5]
    assert format_cpu_list([3, 1, 0, 2, 8, 10, 11]) == "0-3,8,10-11"
    assert format_cpu_list([]) == ""
    for bad in ("a", "3-1", "1-", "0-99999"):
        with pytest.raises(ValueError):
            parse_cpu_list(bad)


def hybrid() -> CpuTopology:
    # 2 P-cores with two threads each (0-3), 4 E-cores (4-7): the shape of an Intel hybrid CPU.
    siblings = {0: [0, 1], 1: [0, 1], 2: [2, 3], 3: [2, 3], 4: [4], 5: [5], 6: [6], 7: [7]}
    return build_topology(siblings, performance=[0, 1, 2, 3], efficiency=[4, 5, 6, 7])


def test_topology_from_kernel_classes():
    t = hybrid()
    assert t.hybrid and len(t.cores) == 6
    assert t.cpus("performance") == [0, 1, 2, 3] and t.cpus("efficiency") == [4, 5, 6, 7]
    assert t.cores[0] == Core(id=0, cpus=(0, 1), kind="performance")


def test_topology_from_frequencies_ignores_favoured_cores():
    siblings = {i: [i] for i in range(6)}
    # cores 0-3 fast (one "favoured" a little faster), 4-5 clearly slower
    freq = {0: 5_700_000, 1: 6_000_000, 2: 5_700_000, 3: 5_700_000, 4: 4_400_000, 5: 4_400_000}
    t = build_topology(siblings, max_freq=freq)
    assert t.cpus("performance") == [0, 1, 2, 3] and t.cpus("efficiency") == [4, 5]


def test_uniform_cpu_has_no_classes():
    t = build_topology({i: [i] for i in range(4)}, max_freq={i: 3_000_000 for i in range(4)})
    assert not t.hybrid and all(c.kind == "" for c in t.cores)


def test_pinning_none_or_unknown_machine_means_no_pinning():
    assert allocate_cpuset(hybrid(), "none", 2) == ""
    assert allocate_cpuset(CpuTopology(cores=()), "spread", 2) == ""
    with pytest.raises(ValueError):
        allocate_cpuset(hybrid(), "fastest", 2)


def test_performance_and_efficiency_use_their_own_cores():
    t = hybrid()
    assert allocate_cpuset(t, "performance", 2) == "0-1"          # one whole P-core, both threads
    assert allocate_cpuset(t, "efficiency", 2) == "4-5"
    assert allocate_cpuset(t, "efficiency", 0.5) == "4"           # a fraction still needs one CPU


def test_allocation_avoids_cores_already_in_use():
    t = hybrid()
    assert allocate_cpuset(t, "performance", 2, {0: 1, 1: 1}) == "2-3"
    assert allocate_cpuset(t, "efficiency", 2, {4: 1, 5: 1}) == "6-7"
    # everything taken once: start again from the least loaded, lowest core
    assert allocate_cpuset(t, "performance", 2, {0: 1, 1: 1, 2: 1, 3: 1}) == "0-1"
    assert allocate_cpuset(t, "performance", 2, {0: 2, 1: 2, 2: 1, 3: 1}) == "2-3"


def test_spread_walks_all_cores_round_robin():
    t, used, seen = hybrid(), {}, []
    for _ in range(4):
        cpus = allocate_cpuset(t, "spread", 2, used)
        seen.append(cpus)
        for c in parse_cpu_list(cpus):
            used[c] = used.get(c, 0) + 1
    assert seen == ["0-1", "2-3", "4-5", "6-7"]


def test_request_larger_than_the_class_gets_the_whole_class():
    assert allocate_cpuset(hybrid(), "performance", 16) == "0-3"


def test_class_missing_on_uniform_cpu_falls_back_to_all_cores():
    t = build_topology({i: [i] for i in range(4)})
    assert allocate_cpuset(t, "efficiency", 2) == "0-1"


def test_remembered_pinning_is_checked_against_machine_and_policy():
    t = hybrid()
    assert cpuset_fits("0-1", t, "performance")
    assert not cpuset_fits("4-5", t, "performance")        # those are efficiency cores
    assert cpuset_fits("4-5", t, "spread")
    assert not cpuset_fits("30-31", t, "spread")           # cores this machine does not have
    assert not cpuset_fits("", t, "spread") and not cpuset_fits("0-1", t, "none")
    assert not cpuset_fits("zz", t, "spread")


def test_an_image_with_its_own_frame_rate_property_gets_the_rate_under_that_name_too():
    """Measured on an image with a patched compositor: `androidboot.redroid_fps=15` alone
    left the screen at the image's built-in 25 Hz; the image's own property made it 15 Hz."""
    config = Config.model_validate({"android": {"fps": 20, "images": {
        "13": {"base": "example/android:13", "fps_property": "ro.vendor.example_fps"}}}})

    def args(**spec):
        return boot_arguments(resolve(InstanceSpec(id=1, **spec), config), FakeGpu())

    assert "ro.vendor.example_fps=20" in args() and "androidboot.redroid_fps=20" in args()
    per_android = args(fps=15)
    assert "ro.vendor.example_fps=15" in per_android and "androidboot.redroid_fps=15" in per_android
    # set by hand for one Android, the same key still wins
    assert "ro.vendor.example_fps=30" in args(fps=15, extra_boot_args=["ro.vendor.example_fps=30"])
    assert "ro.vendor.example_fps=15" not in args(fps=15, extra_boot_args=["ro.vendor.example_fps=30"])
    # an exact image named on the Android bypasses android.images, and with it this setting
    assert not [a for a in args(image="other/android:13") if a.startswith("ro.vendor.")]
    stock = Config.model_validate({"android": {"images": {"13": {"base": "example/android:13"}}}})
    assert not [a for a in boot_arguments(resolve(InstanceSpec(id=1), stock), FakeGpu()) if a.startswith("ro.vendor.")]
    with pytest.raises(ValueError):
        Config.model_validate({"android": {"images": {"13": {"base": "x/y:13", "fps_property": "bad name=1"}}}})

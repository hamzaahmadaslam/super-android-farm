"""The doctor against a fake machine: every check must find its problem and name the fix."""
import json
import os

import pytest

from superfarm.config import Config
from superfarm.host import doctor, setup
from superfarm.paths import get_paths
from superfarm.util.proc import CommandError, Result

REL = "6.8.0-test"
ALL_MODULES = ["binder_linux", "veth", "bridge", "overlay", "nf_conntrack", "nf_nat", "xt_tcpudp", "xt_nat",
               "xt_MASQUERADE", "xt_conntrack", "xt_addrtype"]
DOCKER_INFO = {"ServerVersion": "27.0.1", "Driver": "overlay2", "CgroupVersion": "2", "CgroupDriver": "systemd",
               "DockerRootDir": "/var/lib/docker", "DefaultAddressPools": None}
UBUNTU = 'PRETTY_NAME="Ubuntu 24.04"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n'
ARCH = 'NAME="Arch Linux"\nID=arch\n'


def w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def add_gpu(root, n, driver, vendor, slot):
    dev = root / "sys/devices/pci" / slot
    dev.mkdir(parents=True)
    w(root, f"sys/devices/pci/{slot}/vendor", vendor + "\n")
    w(root, f"sys/devices/pci/{slot}/device", "0x1234\n")
    w(root, f"sys/devices/pci/{slot}/uevent", f"DRIVER={driver}\nPCI_SLOT_NAME={slot}\nPCI_ID={vendor[2:]}:1234\n")
    drv = root / "sys/bus/pci/drivers" / driver
    drv.mkdir(parents=True, exist_ok=True)
    os.symlink(drv, dev / "driver")
    node = root / "sys/class/drm" / f"renderD{n}"
    node.mkdir(parents=True)
    os.symlink(dev, node / "device")
    w(root, f"dev/dri/renderD{n}", "")


def healthy(tmp_path, *, os_release=UBUNTU, loaded=ALL_MODULES, on_disk=ALL_MODULES, builtin=("tun",), modprobe="/sbin/modprobe\n",
            filesystems=("binder",), home_fs="ext4", sysctl=None, total_gb=64, secure_boot=None, gpus=True):
    root = tmp_path / "root"
    w(root, "etc/os-release", os_release)
    w(root, "proc/cpuinfo", "processor\t: 0\nflags\t\t: fpu sse ssse3 sse4_1 sse4_2 vmx avx2\n")
    w(root, "proc/sys/kernel/osrelease", REL + "\n")
    w(root, "proc/sys/kernel/modprobe", modprobe)
    w(root, "proc/modules", "".join(f"{m} 1 0 - Live 0x0\n" for m in loaded))
    w(root, "proc/filesystems", "\text4\n" + "".join(f"nodev\t{f}\n" for f in filesystems))
    w(root, f"lib/modules/{REL}/modules.dep", "".join(f"kernel/{m}.ko.zst:\n" for m in on_disk))
    w(root, f"lib/modules/{REL}/modules.builtin", "".join(f"kernel/{m}.ko\n" for m in builtin))
    w(root, "dev/net/tun", "")
    w(root, "proc/meminfo", f"MemTotal: {total_gb * 1048576} kB\nMemAvailable: {total_gb * 1048576 - 4000000} kB\nSwapTotal: 8388604 kB\n")
    w(root, "proc/swaps", "Filename Type Size Used Priority\n/swap.img file 8388604 0 -1\n")
    values = {s.key: s.minimum for s in setup.SYSCTLS}
    values.update(sysctl or {})
    for key, v in values.items():
        w(root, "proc/sys/" + key.replace(".", "/"), f"{v}\n")
    w(root, "proc/sys/net/ipv4/ip_local_port_range", "32768\t60999\n")
    w(root, "sys/fs/cgroup/cgroup.controllers", "cpuset cpu io memory pids\n")
    w(root, "sys/kernel/security/lsm", "lockdown,capability,yama,apparmor\n")
    home = tmp_path / "farm"
    w(root, "proc/mounts", f"/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 {home} {home_fs} rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n")
    w(root, "run/systemd/timesync/synchronized", "")
    if secure_boot is not None:
        p = root / "sys/firmware/efi/efivars" / doctor.SECUREBOOT_VAR
        p.parent.mkdir(parents=True)
        p.write_bytes(bytes([6, 0, 0, 0, 1 if secure_boot else 0]))
    if gpus:
        add_gpu(root, 128, "i915", "0x8086", "0000:00:02.0")
    return root, get_paths(home).ensure()


def make_runner(info=DOCKER_INFO, *, docker_error="", networks=3, bridge="172.17.0.0/16", ps="", tools=None):
    tools = {"adb": "Android Debug Bridge version 1.0.41\nVersion 34.0.5\n", "scrcpy": "scrcpy 3.1 <https://example.invalid>\n",
             "ffmpeg": "ffmpeg version 6.1.1 Copyright\n", **(tools or {})}

    def runner(argv, timeout=10, check=False, **kw):
        a = list(argv)
        if a[0] == "docker":
            if a[1] == "info":
                if docker_error:
                    return Result(1, json.dumps({"ServerErrors": [docker_error]}), "")
                return Result(0, json.dumps(info), "")
            if a[1:3] == ["network", "ls"]:
                return Result(0, "\n".join(f"net{i}" for i in range(networks)) + "\n", "")
            if a[1:3] == ["network", "inspect"]:
                return Result(0, bridge + " \n", "")
            if a[1] == "ps":
                return Result(0, ps, "")
        if a[0] in tools:
            return Result(0, tools[a[0]], "")
        raise CommandError(a, 127, "", f"{a[0]}: command not found")
    return runner


def run(root, paths, config=None, *, runner=None, which=None, env=None, port_free=True):
    present = {"docker", "adb", "scrcpy", "ffmpeg"}
    return {c.id: c for c in doctor.run_checks(
        config or Config(), paths, root=root, runner=runner or make_runner(),
        which=which or (lambda n: f"/usr/bin/{n}" if n in present else None),
        env=env if env is not None else {"USER": "farmer", "XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"},
        cache={"port_probe": lambda host, port: port_free})}


# ------------------------------------------------------------------ the whole run

def test_healthy_machine_has_no_failures_and_enough_checks(tmp_path):
    root, paths = healthy(tmp_path)
    checks = doctor.run_checks(Config(), paths, root=root, runner=make_runner(), which=lambda n: f"/usr/bin/{n}",
                               env={"USER": "farmer", "WAYLAND_DISPLAY": "wayland-0"}, cache={"port_probe": lambda h, p: True})
    assert len(checks) >= 30 and len({c.id for c in checks}) == len(checks)
    fails = [c for c in checks if c.status == "fail"]
    assert not fails, [(c.id, c.detail) for c in fails]
    assert all(c.status in ("ok", "warn", "fail", "skip") and c.title for c in checks)
    assert all(c.fix for c in checks if c.status in ("warn", "fail")), [c.id for c in checks if c.status == "warn" and not c.fix]
    assert doctor.summarize(checks)["ready"] is True


def test_a_crashing_check_becomes_skip_and_the_rest_still_run(tmp_path, monkeypatch):
    root, paths = healthy(tmp_path)

    def boom(ctx):
        raise RuntimeError("bug in a check")
    monkeypatch.setattr(doctor, "CHECKS", (boom, doctor.check_arch))
    checks = doctor.run_checks(Config(), paths, root=root, runner=make_runner())
    assert [c.status for c in checks][0] == "skip" and "bug in a check" in checks[0].detail and len(checks) == 2


def test_summarize_counts():
    s = doctor.summarize([doctor.Check("a", "A", "ok"), doctor.Check("b", "B", "fail"), doctor.Check("c", "C", "warn")])
    assert s == {"counts": {"ok": 1, "warn": 1, "fail": 1, "skip": 0}, "ready": False, "worst": "fail"}


# ------------------------------------------------------------------ CPU / kernel

def test_wrong_architecture_fails(tmp_path):
    root, paths = healthy(tmp_path)
    c = run(root, paths, env={"SF_DOCTOR_MACHINE": "aarch64"})["cpu.arch"]
    assert c.status == "fail" and "aarch64" in c.detail


def test_missing_sse42_and_virtualization_warn(tmp_path):
    root, paths = healthy(tmp_path)
    w(root, "proc/cpuinfo", "flags\t: fpu sse sse2 hypervisor\n")
    c = run(root, paths)["cpu.flags"]
    assert c.status == "warn" and "SSE4.2" in c.detail and "virtual machine" in c.detail


@pytest.mark.parametrize("rel,status", [("4.19.0-x", "fail"), ("5.4.0-x", "warn"), ("6.8.0-x", "ok"), ("7.0.0-38-generic", "ok")])
def test_kernel_version(tmp_path, rel, status):
    root, paths = healthy(tmp_path)
    w(root, "proc/sys/kernel/osrelease", rel + "\n")
    assert doctor.check_kernel(doctor.Ctx(Config(), paths, root, doctor.distro_mod.detect(root), make_runner(), lambda n: None, {})).status == status


def test_binder_not_loaded_gives_modprobe_line(tmp_path):
    root, paths = healthy(tmp_path, loaded=[m for m in ALL_MODULES if m != "binder_linux"], filesystems=())
    c = run(root, paths)["kernel.binder"]
    assert c.status == "fail" and "sudo modprobe binder_linux devices=binder,hwbinder,vndbinder" in c.fix


def test_binder_unsupported_fix_depends_on_the_distribution(tmp_path):
    no_binder = [m for m in ALL_MODULES if m != "binder_linux"]
    root, paths = healthy(tmp_path / "a", loaded=no_binder, on_disk=no_binder, filesystems=())
    deb = run(root, paths)["kernel.binder"]
    assert deb.status == "fail" and f"linux-modules-extra-{os.uname().release}" in deb.fix
    root, paths = healthy(tmp_path / "b", os_release=ARCH, loaded=no_binder, on_disk=no_binder, filesystems=())
    assert "linux-zen" in run(root, paths)["kernel.binder"].fix


def test_autoload_off_makes_idle_modules_a_failure(tmp_path):
    loaded = [m for m in ALL_MODULES if m not in ("veth", "xt_nat")]
    root, paths = healthy(tmp_path, loaded=loaded, modprobe="\n")
    r = run(root, paths)
    assert r["kernel.veth"].status == "fail" and "sudo modprobe -a veth" in r["kernel.veth"].fix
    assert r["kernel.netfilter"].status == "fail" and "xt_nat" in r["kernel.netfilter"].detail
    assert r["kernel.autoload"].status == "warn" and "veth" in r["kernel.autoload"].detail
    # Same modules idle but auto-loading on: they will load when Docker asks.
    root2, paths2 = healthy(tmp_path / "second", loaded=loaded)
    r2 = run(root2, paths2)
    assert r2["kernel.veth"].status == "ok" and r2["kernel.autoload"].status == "ok"


def test_autoload_off_with_everything_loaded_only_asks_to_make_it_permanent(tmp_path):
    root, paths = healthy(tmp_path, modprobe="\n")
    c = run(root, paths)["kernel.autoload"]
    assert c.status == "ok" and "not yet made permanent" in c.detail and c.fix == "sf host apply"


def test_module_missing_for_this_kernel_fails(tmp_path):
    some = [m for m in ALL_MODULES if m != "veth"]
    root, paths = healthy(tmp_path, loaded=some, on_disk=some)
    c = run(root, paths)["kernel.veth"]
    assert c.status == "fail" and "not available for this kernel: veth" in c.detail


def test_ip_forward_off_and_cgroup_problems(tmp_path):
    root, paths = healthy(tmp_path, sysctl={"net.ipv4.ip_forward": 0})
    w(root, "sys/fs/cgroup/cgroup.controllers", "cpuset cpu io\n")
    r = run(root, paths)
    assert r["kernel.ip_forward"].status == "fail" and "sysctl -w net.ipv4.ip_forward=1" in r["kernel.ip_forward"].fix
    assert r["kernel.cgroup"].status == "fail" and "memory" in r["kernel.cgroup"].detail
    (root / "sys/fs/cgroup/cgroup.controllers").unlink()
    assert run(root, paths)["kernel.cgroup"].status == "warn"


# ------------------------------------------------------------------ Docker

def test_docker_missing_gives_the_install_line_for_this_distribution(tmp_path):
    root, paths = healthy(tmp_path)
    r = run(root, paths, which=lambda n: None)
    assert r["docker.installed"].status == "fail"
    assert r["docker.installed"].fix == "sudo apt-get install -y --no-install-recommends docker.io"
    assert r["docker.running"].status == "skip" and r["docker.storage"].status == "skip"
    root, paths = healthy(tmp_path / "arch", os_release=ARCH)
    assert run(root, paths, which=lambda n: None)["docker.installed"].fix == "sudo pacman -S --needed --noconfirm docker"


def test_docker_permission_denied_names_the_group_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.os, "geteuid", lambda: 1000)     # the suite also runs as root in the distro containers
    root, paths = healthy(tmp_path)
    r = run(root, paths, runner=make_runner(docker_error="permission denied while trying to connect to the Docker daemon socket"))
    assert r["docker.access"].status == "fail" and r["docker.access"].fix.startswith("sudo usermod -aG docker farmer")
    assert r["docker.running"].status == "skip"


def test_docker_daemon_down(tmp_path):
    root, paths = healthy(tmp_path)
    r = run(root, paths, runner=make_runner(docker_error="Cannot connect to the Docker daemon"))
    assert r["docker.running"].status == "fail" and "systemctl enable --now docker" in r["docker.running"].fix


def test_docker_storage_and_cgroup(tmp_path):
    root, paths = healthy(tmp_path)
    r = run(root, paths, runner=make_runner({**DOCKER_INFO, "Driver": "vfs", "CgroupVersion": "1"}))
    assert r["docker.storage"].status == "fail" and "overlay2" in r["docker.storage"].fix
    assert r["docker.cgroup"].status == "warn"
    assert run(root, paths, runner=make_runner({**DOCKER_INFO, "Driver": "overlayfs"}))["docker.storage"].status == "ok"
    assert run(root, paths, runner=make_runner({**DOCKER_INFO, "Driver": "btrfs"}))["docker.storage"].status == "warn"


def test_address_space_small_default_bridge_fails_when_farm_is_bigger(tmp_path):
    root, paths = healthy(tmp_path)
    for i in range(1, 301):
        w(paths.instances, f"{i}/instance.yaml", f"id: {i}\n")
    c = run(root, paths, runner=make_runner(bridge="172.17.0.0/24"))["docker.address_pools"]
    assert c.status == "fail" and "300 Androids" in c.detail and '"bip"' in c.fix
    assert run(root, paths)["docker.address_pools"].status == "ok"


def test_address_pools_nearly_exhausted_warns(tmp_path):
    root, paths = healthy(tmp_path)
    c = run(root, paths, runner=make_runner(networks=28))["docker.address_pools"]
    assert c.status == "warn" and "default-address-pools" in c.fix and "28 of about 31" in c.detail
    custom = {**DOCKER_INFO, "DefaultAddressPools": [{"Base": "10.200.0.0/14", "Size": 24}]}
    ok = run(root, paths, runner=make_runner(custom, networks=28))["docker.address_pools"]
    assert ok.status == "ok" and "about 1024" in ok.detail


def test_adb_published_on_all_interfaces_is_spotted(tmp_path):
    root, paths = healthy(tmp_path)
    ps = "sf-net-1 127.0.0.1:5556->5555/tcp\nsf-net-2 0.0.0.0:5557->5555/tcp, [::]:5557->5555/tcp\n"
    c = run(root, paths, runner=make_runner(ps=ps))["net.adb_published"]
    assert c.status == "warn" and "sf-net-2" in c.detail and "sf-net-1" not in c.detail
    assert run(root, paths, runner=make_runner(ps="sf-net-1 127.0.0.1:5556->5555/tcp\n"))["net.adb_published"].status == "ok"


# ------------------------------------------------------------------ limits, memory, disk

def test_low_limits_warn_with_exact_sysctl_commands(tmp_path):
    root, paths = healthy(tmp_path, sysctl={"fs.inotify.max_user_instances": 128, "kernel.pid_max": 32768,
                                            "net.ipv4.neigh.default.gc_thresh3": 1024})
    r = run(root, paths)
    assert r["limits.inotify"].status == "warn" and "sudo sysctl -w fs.inotify.max_user_instances=8192" in r["limits.inotify"].fix
    assert r["limits.pid_max"].status == "warn" and "30 Androids" in r["limits.pid_max"].detail
    assert r["limits.neigh"].status == "warn" and "gc_thresh3=16384" in r["limits.neigh"].fix
    assert r["limits.file_max"].status == "ok"


def test_memory_checks(tmp_path):
    root, paths = healthy(tmp_path, total_gb=2)
    assert run(root, paths)["mem.ram"].status == "fail"
    root, paths = healthy(tmp_path / "b", total_gb=6)
    assert run(root, paths)["mem.ram"].status == "warn"
    root, paths = healthy(tmp_path / "c")
    w(root, "proc/meminfo", "MemTotal: 67108864 kB\nMemAvailable: 60000000 kB\nSwapTotal: 0 kB\n")
    w(root, "proc/swaps", "Filename Type Size Used Priority\n")
    swap = run(root, paths)["mem.swap"]
    assert swap.status == "warn" and "zram" in swap.fix
    w(root, "proc/meminfo", "MemTotal: 67108864 kB\nMemAvailable: 60000000 kB\nSwapTotal: 16777212 kB\n")
    w(root, "proc/swaps", "Filename Type Size Used Priority\n/dev/zram0 partition 16777212 0 100\n")
    w(root, "proc/sys/vm/swappiness", "60\n")
    z = run(root, paths)["mem.swap"]
    assert z.status == "ok" and "16.0 GB of it zram" in z.detail and "performance.swappiness 150" in z.detail
    w(root, "proc/sys/vm/swappiness", "150\n")
    assert "swappiness" not in run(root, paths)["mem.swap"].detail


@pytest.mark.parametrize("fs,status", [("ext4", "ok"), ("btrfs", "ok"), ("fuseblk", "fail"), ("ntfs3", "fail"), ("exfat", "fail"),
                                       ("vfat", "fail"), ("nfs4", "warn"), ("cifs", "warn"), ("fuse.sshfs", "warn")])
def test_farm_home_filesystem(tmp_path, fs, status):
    root, paths = healthy(tmp_path, home_fs=fs)
    c = run(root, paths)["disk.home_fs"]
    assert c.status == status and fs in c.detail
    if status != "ok":
        assert "SUPERFARM_HOME" in c.fix


def test_docker_data_filesystem_is_checked_too(tmp_path):
    root, paths = healthy(tmp_path)
    w(root, "proc/mounts", "/dev/sda1 / ext4 rw 0 0\n/dev/sdc1 /mnt/win fuseblk rw 0 0\n")
    c = run(root, paths, runner=make_runner({**DOCKER_INFO, "DockerRootDir": "/mnt/win/docker"}))["disk.docker_fs"]
    assert c.status == "fail" and "data-root" in c.fix


# ------------------------------------------------------------------ Secure Boot, camera, GPU

def test_secure_boot_states(tmp_path):
    root, paths = healthy(tmp_path / "on", secure_boot=True)
    on = run(root, paths)["boot.secureboot"]
    assert on.status == "ok" and "refuses unsigned modules" in on.detail and on.fix.startswith("sf host sign-module -- v4l2loopback")
    root, paths = healthy(tmp_path / "off", secure_boot=False)
    assert "off" in run(root, paths)["boot.secureboot"].detail
    root, paths = healthy(tmp_path / "bios")
    assert "off" in run(root, paths)["boot.secureboot"].detail          # no /sys/firmware/efi at all


def test_camera_module_states(tmp_path):
    cam = Config.model_validate({"media": {"camera": {"enabled": True, "slots": 4}}})
    root, paths = healthy(tmp_path / "a", secure_boot=True)
    assert run(root, paths)["camera.module"].status == "skip"                    # cameras off, module absent
    missing = run(root, paths, cam)["camera.module"]
    assert missing.status == "fail" and "native/v4l2loopback/build.sh install" in missing.fix
    # The build script is named by its real location, the CLI commands as the CLI spells them.
    lines = missing.fix.splitlines()
    assert lines[1].startswith("sudo /") and lines[1].endswith("/native/v4l2loopback/build.sh install")
    assert lines[2].startswith("sf host sign-module -- v4l2loopback") and lines[3] == "sf host apply"
    assert "linux-headers-" in missing.fix
    root, paths = healthy(tmp_path / "b", secure_boot=True, on_disk=[*ALL_MODULES, "v4l2loopback"])
    unsigned = run(root, paths, cam)["camera.module"]
    assert unsigned.status == "fail" and "unsigned" in unsigned.detail
    root, paths = healthy(tmp_path / "c", loaded=[*ALL_MODULES, "v4l2loopback"])
    w(root, "dev/video101", "")
    w(root, "dev/video102", "")
    loaded = run(root, paths, cam)["camera.module"]
    assert loaded.status == "ok" and "2 of 4" in loaded.detail


def test_no_gpu_warns_and_nvidia_checks_are_skipped(tmp_path):
    root, paths = healthy(tmp_path, gpus=False)
    r = run(root, paths)
    assert r["gpu.detected"].status == "warn" and "CPU" in r["gpu.detected"].detail
    assert {r[k].status for k in ("gpu.access", "nvidia.driver", "nvidia.icd", "nvidia.runtime", "nvidia.relay")} == {"skip"}


def test_nvidia_card_states(tmp_path):
    root, paths = healthy(tmp_path)
    add_gpu(root, 129, "nvidia", "0x10de", "0000:01:00.0")
    r = run(root, paths)
    assert "nvidia" in r["gpu.detected"].detail and r["gpu.access"].status == "ok"
    assert r["nvidia.driver"].status == "fail"                  # no /proc/driver/nvidia/version
    assert r["nvidia.icd"].status == "fail" and "nvidia_icd.json" in r["nvidia.icd"].fix
    assert r["nvidia.runtime"].status == "warn" and "native/venus/build.sh all" in r["nvidia.runtime"].fix
    assert r["nvidia.relay"].status == "skip"
    w(root, "proc/driver/nvidia/version", "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  595.99.02  Release Build\n")
    w(root, "usr/share/vulkan/icd.d/nvidia_icd.json", "{}")
    r = run(root, paths)
    assert r["nvidia.driver"].status == "ok" and "595.99.02" in r["nvidia.driver"].detail and "open" in r["nvidia.driver"].detail
    assert r["nvidia.icd"].status == "ok"


def test_nouveau_card_is_reported_as_wrong_driver(tmp_path):
    root, paths = healthy(tmp_path, gpus=False)
    add_gpu(root, 128, "nouveau", "0x10de", "0000:01:00.0")
    r = run(root, paths)
    assert r["gpu.detected"].status == "warn" and "nouveau" in r["gpu.detected"].detail
    assert r["nvidia.driver"].status == "fail" and "ubuntu-drivers" in r["nvidia.driver"].fix


# ------------------------------------------------------------------ tools, exposure, session

def test_missing_and_old_tools(tmp_path):
    root, paths = healthy(tmp_path)
    r = run(root, paths, which=lambda n: "/usr/bin/docker" if n == "docker" else None)
    assert r["tools.adb"].status == "fail" and r["tools.adb"].fix == "sudo apt-get install -y --no-install-recommends adb"
    assert r["tools.scrcpy"].status == "warn" and "scrcpy" in r["tools.scrcpy"].fix
    assert r["tools.ffmpeg"].status == "warn"                    # only required when cameras are on
    cam = Config.model_validate({"media": {"camera": {"enabled": True}}})
    assert run(root, paths, cam, which=lambda n: None)["tools.ffmpeg"].status == "fail"
    old = run(root, paths, runner=make_runner(tools={"scrcpy": "scrcpy 1.25 <x>\n"}))["tools.scrcpy"]
    assert old.status == "warn" and "2.1" in old.detail
    assert "1.0.41" in run(root, paths)["tools.adb"].detail


def test_exposure_warnings(tmp_path):
    root, paths = healthy(tmp_path)
    cfg = Config.model_validate({"network": {"adb_bind": "0.0.0.0"}, "server": {"host": "0.0.0.0"}})
    r = run(root, paths, cfg)
    assert r["net.adb_bind"].status == "warn" and "root shell" in r["net.adb_bind"].detail and "127.0.0.1" in r["net.adb_bind"].fix
    assert r["net.server_host"].status == "warn" and "without TLS" in r["net.server_host"].detail
    tls = Config.model_validate({"server": {"host": "192.168.1.5", "tls_cert": "/c.pem", "tls_key": "/k.pem"}})
    assert "with TLS" in run(root, paths, tls)["net.server_host"].detail
    ok = run(root, paths)
    assert ok["net.adb_bind"].status == "ok" and ok["net.server_host"].status == "ok"


def test_ports(tmp_path):
    root, paths = healthy(tmp_path)
    assert run(root, paths)["net.ports"].status == "ok"
    busy = run(root, paths, port_free=False)["net.ports"]
    assert busy.status == "warn" and "7700" in busy.detail
    eph = Config.model_validate({"network": {"adb_port_base": 40000}})
    c = run(root, paths, eph)["net.ports"]
    assert c.status == "warn" and "32768-60999" in c.detail


def test_selinux_enforcing_warns_about_sidecar_mounts(tmp_path):
    root, paths = healthy(tmp_path)
    w(root, "sys/kernel/security/lsm", "lockdown,capability,selinux\n")
    w(root, "sys/fs/selinux/enforce", "1\n")
    c = run(root, paths)["security.lsm"]
    assert c.status == "warn" and "chcon" in c.fix and str(paths.vpn) in c.fix
    assert "AppArmor" in run(*healthy(tmp_path / "b"))["security.lsm"].detail


def test_session_and_host_setup(tmp_path):
    root, paths = healthy(tmp_path)
    headless = run(root, paths, env={"USER": "farmer", "XDG_RUNTIME_DIR": "/run/user/1000"})["session.desktop"]
    assert headless.status == "warn" and "display" in headless.detail
    w(root, "run/user/1000/pipewire-0", "")
    assert run(root, paths)["session.desktop"].status == "ok"
    assert run(root, paths)["host.setup"].status == "warn"
    w(root, "etc/modules-load.d/superfarm.conf", "veth\n")
    w(root, "etc/sysctl.d/90-superfarm.conf", "x\n")
    w(root, "etc/systemd/system/superfarm-host.service", "[Unit]\n")
    assert run(root, paths)["host.setup"].status == "ok"

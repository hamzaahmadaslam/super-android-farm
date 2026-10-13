"""`sf doctor`: everything about the machine that can stop a farm, with the fix for each.

Every check answers three questions: is it fine, what exactly was found, and what is the
command that fixes it on *this* distribution. Checks never change anything, never need
root, are each bounded by a timeout, and one that crashes is reported as skipped instead
of taking the others down.

Readers take the filesystem root from the context so the whole doctor can run against a
fake /proc, /sys and /etc in tests.
"""
from __future__ import annotations

import grp
import ipaddress
import os
import platform
import re
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Mapping

from ..config import Config
from ..core.store import Store
from ..paths import Paths
from ..resources import resource_path
from ..util import proc
from . import capacity, distro as distro_mod, gpu as gpu_mod, modules, service, setup, venus

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# Filesystems without Unix permissions, ownership or extended attributes. The farm home
# holds 0600 secrets, FIFOs and sockets; Docker's data holds Android /data with SELinux
# labels and numeric owners. None of that survives on these.
FS_NO_UNIX = {"ntfs", "ntfs3", "fuseblk", "exfat", "vfat", "msdos", "fat", "hfsplus"}
FS_NETWORK = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "fuse.sshfs", "davfs", "ceph", "glusterfs", "afs"}
LOOPBACK = ("127.0.0.1", "::1", "localhost")
SECUREBOOT_VAR = "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
# Docker's built-in local pools: 172.17-172.31 as /16 (15) plus 192.168.0.0/16 cut into /20 (16).
DOCKER_DEFAULT_NETWORKS = 31
# Fix lines name commands exactly as the CLI spells them. `sf host apply` asks for sudo
# itself (a plain `sudo sf` usually fails: sudo does not search a virtual environment).
HOST_APPLY = "sf host apply"
SIGN_MODULE = "sf host sign-module -- v4l2loopback"


@dataclass
class Check:
    id: str
    title: str
    status: str            # ok | warn | fail | skip
    detail: str = ""
    fix: str = ""          # exact command(s) to fix


@dataclass
class Ctx:
    config: Config
    paths: Paths
    root: Path
    distro: distro_mod.Distro
    run: Callable[..., proc.Result]
    which: Callable[[str], str | None]
    env: Mapping[str, str]
    cache: dict = field(default_factory=dict)

    def read(self, rel: str) -> str:
        try:
            return (self.root / rel.lstrip("/")).read_text(errors="replace")
        except OSError:
            return ""

    def tool(self, argv: list[str], timeout: float = 10) -> proc.Result | None:
        """Run a read-only tool; None when it is missing, hangs or cannot start."""
        try:
            return self.run(argv, timeout=timeout, check=False)
        except (proc.CommandError, OSError):
            return None

    def docker_info(self) -> dict | None:
        if "docker_info" not in self.cache and not self.which(self.config.docker.binary):
            self.cache["docker_info"], self.cache["docker_err"] = None, "Docker is not installed"
        if "docker_info" not in self.cache:
            import json
            r = self.tool([self.config.docker.binary, "info", "--format", "{{json .}}"], timeout=20)
            try:
                data = json.loads(r.stdout) if r and r.stdout.strip() else None
            except ValueError:
                data = None
            # `docker info` prints client data even when the daemon is unreachable.
            self.cache["docker_info"] = data if data and data.get("ServerVersion") else None
            self.cache["docker_err"] = ((r.stderr or r.stdout).strip()[:300] if r else "docker did not answer")
            if data and data.get("ServerErrors"):
                self.cache["docker_err"] = "; ".join(data["ServerErrors"])[:300]
        return self.cache["docker_info"]

    def gpus(self) -> list[gpu_mod.GpuInfo]:
        if "gpus" not in self.cache:
            real = self.root == Path("/")
            self.cache["gpus"] = gpu_mod.detect_gpus(self.root / "sys", dev=self.root / "dev",
                                                     namer=gpu_mod.lspci_name if real else None)
        return self.cache["gpus"]


def _version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(x) for x in m.groups(default="0")) if m else ()


# ============================================================ CPU / kernel

def check_arch(c: Ctx) -> Check:
    machine = c.env.get("SF_DOCTOR_MACHINE") or platform.machine()
    if machine in ("x86_64", "amd64"):
        return Check("cpu.arch", "64-bit x86 CPU", OK, machine)
    return Check("cpu.arch", "64-bit x86 CPU", FAIL, f"this machine is {machine}",
                 "The Android images and the ARM translation layer are x86_64 only; use an x86_64 host.")


def check_cpu_flags(c: Ctx) -> Check:
    flags = set()
    for line in c.read("proc/cpuinfo").splitlines():
        if line.startswith("flags"):
            flags = set(line.partition(":")[2].split())
            break
    if not flags:
        return Check("cpu.flags", "CPU features", SKIP, "could not read /proc/cpuinfo")
    notes, status, fix = [], OK, ""
    if "sse4_2" not in flags or "ssse3" not in flags:
        status = WARN
        notes.append("no SSE4.2/SSSE3: ARM-only apps (native bridge) will not run")
    virt = "vmx" if "vmx" in flags else "svm" if "svm" in flags else ""
    if virt:
        notes.append(f"virtualization: {virt}")
    else:
        notes.append("no vmx/svm flag (virtualization off in firmware, or this is a VM)")
        fix = ("Containers do not need hardware virtualization, so the farm works. Inside a VM expect lower "
               "density; on bare metal enable VT-x/AMD-V in the firmware if you also use emulators.")
        status = WARN if status == OK else status
    if "hypervisor" in flags:
        notes.append("running inside a virtual machine")
    return Check("cpu.flags", "CPU features", status, "; ".join(notes), fix)


def check_cpu_count(c: Ctx) -> Check:
    n = os.cpu_count() or 1
    cpus = c.config.resources.cpus
    if n < 4:
        return Check("cpu.count", "CPU threads", WARN, f"{n} threads; each Android may use {cpus:g}",
                     "Lower resources.cpus or expect one or two Androids at most.")
    return Check("cpu.count", "CPU threads", OK, f"{n} threads")


def check_kernel(c: Ctx) -> Check:
    rel = modules.kernel_release(c.root)
    v = _version(rel)
    if not v:
        return Check("kernel.version", "Kernel version", SKIP, f"cannot parse {rel!r}")
    if v < (5, 0):
        return Check("kernel.version", "Kernel version", FAIL, f"{rel}: binderfs needs 5.0 or newer",
                     "Upgrade the distribution or install a newer kernel.")
    if v < (5, 10):
        return Check("kernel.version", "Kernel version", WARN, f"{rel}: works, but 5.10+ is what the images are tested on",
                     "Install a newer kernel when convenient.")
    return Check("kernel.version", "Kernel version", OK, rel)


def check_binder(c: Ctx) -> Check:
    b = modules.binder_status(c.root)
    title = "Android binder"
    if b["ok"]:
        how = "binderfs" if b["binderfs"] else "devices " + ", ".join(b["devices"])
        return Check("kernel.binder", title, OK, f"{how} (binder_linux: {b['module']})")
    load = f"sudo modprobe binder_linux devices={modules.BINDER_DEVICES}"
    if b["how"] == "module-not-loaded":
        return Check("kernel.binder", title, FAIL, "binder_linux exists for this kernel but is not loaded",
                     f"{load}\nsf host apply    # makes it permanent")
    if b["how"] == "binderfs-not-registered":
        return Check("kernel.binder", title, WARN, "binder is built into the kernel but binderfs is not registered",
                     "sudo mkdir -p /dev/binderfs && sudo mount -t binder binder /dev/binderfs")
    fixes = {
        "debian": f"sudo apt-get install -y linux-modules-extra-{c.distro.kernel}   # only on releases that still split it out\n{load}",
        "fedora": "The running kernel was built without binder (common on RHEL-family kernels). Boot a kernel "
                  "built with CONFIG_ANDROID_BINDER_IPC and CONFIG_ANDROID_BINDERFS, then run `sf doctor` again.",
        "arch": "sudo pacman -S linux-zen linux-zen-headers   # a kernel flavour that ships binder; reboot into it\n" + load,
        "suse": "The running kernel was built without binder; boot a kernel built with "
                "CONFIG_ANDROID_BINDER_IPC and CONFIG_ANDROID_BINDERFS, then run `sf doctor` again.",
    }
    return Check("kernel.binder", title, FAIL, "this kernel has no binder support (no module, not built in)",
                 fixes.get(c.distro.family, "Use a kernel built with CONFIG_ANDROID_BINDER_IPC and CONFIG_ANDROID_BINDERFS."))


def _module_check(c: Ctx, cid: str, title: str, names: list[str], fix_extra: str = "") -> Check:
    cache = c.cache.setdefault("modstate", {})
    states = {n: modules.state(n, root=c.root, _cache=cache) for n in names}
    absent = [n for n, s in states.items() if s == "missing"]
    idle = [n for n, s in states.items() if s == "available"]
    if absent:
        return Check(cid, title, FAIL, f"not available for this kernel: {', '.join(absent)}",
                     fix_extra or "Install the kernel's full module package, or use the distribution's standard kernel.")
    if idle:
        status = OK if modules.autoload_enabled(c.root) else FAIL
        detail = f"installed but not loaded: {', '.join(idle)}" + (
            " (will load on demand)" if status == OK else "; auto-loading is off, so they never will be")
        return Check(cid, title, status, detail,
                     "" if status == OK else f"sudo modprobe -a {' '.join(idle)}\nsf host apply    # makes it permanent")
    return Check(cid, title, OK, ", ".join(f"{n} ({s})" for n, s in states.items()))


def check_tun(c: Ctx) -> Check:
    chk = _module_check(c, "kernel.tun", "TUN device (VPN)", ["tun"])
    if chk.status == OK and not (c.root / "dev/net/tun").exists():
        return Check("kernel.tun", "TUN device (VPN)", WARN, "/dev/net/tun does not exist",
                     "sudo mkdir -p /dev/net && sudo mknod /dev/net/tun c 10 200 && sudo chmod 666 /dev/net/tun")
    return chk


def check_veth(c: Ctx) -> Check:
    return _module_check(c, "kernel.veth", "Container networking (veth, bridge)", ["veth", "bridge"])


def check_netfilter(c: Ctx) -> Check:
    names = [m.name for m in modules.MODULES if m.group == "netfilter" and m.required]
    return _module_check(c, "kernel.netfilter", "Netfilter (NAT, published ports, kill switch)", names)


def check_autoload(c: Ctx) -> Check:
    title = "Kernel module auto-loading"
    if modules.autoload_enabled(c.root):
        return Check("kernel.autoload", title, OK, "on")
    features = modules.features_of(c.config)
    idle = [m.name for m in modules.missing(features, root=c.root, required_only=False)
            if modules.state(m.name, root=c.root) == "available"]
    conf = bool(c.read(modules.MODULES_LOAD_FILE))
    if not idle:
        return Check("kernel.autoload", title, OK,
                     "off (/proc/sys/kernel/modprobe is empty), but every module the farm needs is loaded"
                     + ("" if conf else "; not yet made permanent"),
                     "" if conf else "sf host apply")
    return Check("kernel.autoload", title, WARN,
                 "off (/proc/sys/kernel/modprobe is empty): nothing loads on demand. Present but not loaded "
                 f"(mostly what Android's own network service asks for): {', '.join(idle)}",
                 f"sudo modprobe -a {' '.join(idle)}\nsf host apply    # loads them at every boot")


def check_ip_forward(c: Ctx) -> Check:
    v = setup.read_sysctl("net.ipv4.ip_forward", c.root)
    if v is None:
        return Check("kernel.ip_forward", "IP forwarding", SKIP, "not readable")
    if v == 1:
        return Check("kernel.ip_forward", "IP forwarding", OK, "on")
    return Check("kernel.ip_forward", "IP forwarding", FAIL, "off: containers cannot reach the network",
                 "sudo sysctl -w net.ipv4.ip_forward=1\nsf host apply    # makes it permanent")


def check_cgroup(c: Ctx) -> Check:
    title = "Control groups (cgroup v2)"
    controllers = c.read("sys/fs/cgroup/cgroup.controllers").split()
    if not controllers:
        return Check("kernel.cgroup", title, WARN, "cgroup v2 is not mounted at /sys/fs/cgroup (legacy v1 host)",
                     "Boot with systemd.unified_cgroup_hierarchy=1 (kernel command line). Memory and CPU limits "
                     "per Android are only reliable on v2.")
    lacking = [x for x in ("cpu", "memory", "pids") if x not in controllers]
    if lacking:
        return Check("kernel.cgroup", title, FAIL, f"controllers missing: {', '.join(lacking)}",
                     "Enable them on the kernel command line (for memory: cgroup_enable=memory) and reboot; "
                     "without them --memory/--cpus limits are ignored.")
    return Check("kernel.cgroup", title, OK, "v2 with " + ", ".join(controllers))


# ============================================================ Docker

def check_docker_installed(c: Ctx) -> Check:
    path = c.which(c.config.docker.binary)
    if path:
        return Check("docker.installed", "Docker installed", OK, path)
    return Check("docker.installed", "Docker installed", FAIL, f"`{c.config.docker.binary}` is not on PATH",
                 c.distro.install_command("docker"))


def check_docker_running(c: Ctx) -> Check:
    if not c.which(c.config.docker.binary):
        return Check("docker.running", "Docker daemon", SKIP, "Docker is not installed")
    info = c.docker_info()
    if info:
        return Check("docker.running", "Docker daemon", OK, f"version {info.get('ServerVersion', '?')}")
    err = c.cache.get("docker_err", "")
    if "permission denied" in err.lower():
        return Check("docker.running", "Docker daemon", SKIP, "cannot tell: no permission to ask (see next check)")
    return Check("docker.running", "Docker daemon", FAIL, err or "not reachable",
                 "sudo systemctl enable --now docker.service")


def check_docker_access(c: Ctx) -> Check:
    title = "Docker usable by this user"
    if not c.which(c.config.docker.binary):
        return Check("docker.access", title, SKIP, "Docker is not installed")
    user = c.env.get("USER") or str(os.getuid())
    if os.geteuid() == 0:
        return Check("docker.access", title, WARN, "running as root",
                     "Run the farm as your desktop user (member of the docker group); root has no display or sound.")
    if c.docker_info():
        return Check("docker.access", title, OK, f"{user} can reach the Docker socket")
    err = c.cache.get("docker_err", "")
    if "permission denied" in err.lower():
        return Check("docker.access", title, FAIL, f"{user} may not use the Docker socket",
                     f"sudo usermod -aG docker {user}    # then log out and in again (or: newgrp docker)")
    return Check("docker.access", title, SKIP, "Docker daemon is not reachable")


def check_docker_cgroup(c: Ctx) -> Check:
    info = c.docker_info()
    if not info:
        return Check("docker.cgroup", "Docker cgroup setup", SKIP, "Docker daemon is not reachable")
    ver, drv = str(info.get("CgroupVersion", "")), info.get("CgroupDriver", "")
    if ver != "2":
        return Check("docker.cgroup", "Docker cgroup setup", WARN, f"cgroup v{ver or '?'} ({drv})",
                     "Switch the host to cgroup v2 (systemd.unified_cgroup_hierarchy=1); swap limits and "
                     "memory pressure handling per Android depend on it.")
    return Check("docker.cgroup", "Docker cgroup setup", OK, f"cgroup v2, driver {drv}")


def check_docker_storage(c: Ctx) -> Check:
    info = c.docker_info()
    if not info:
        return Check("docker.storage", "Docker storage driver", SKIP, "Docker daemon is not reachable")
    drv = info.get("Driver", "")
    root_dir = info.get("DockerRootDir", "/var/lib/docker")
    if drv in ("overlay2", "overlayfs"):
        return Check("docker.storage", "Docker storage driver", OK, f"{drv} in {root_dir}")
    if drv == "vfs":
        return Check("docker.storage", "Docker storage driver", FAIL,
                     "vfs copies the whole image for every container (several GB per Android)",
                     'Put Docker\'s data on ext4 or xfs and set {"storage-driver": "overlay2"} in /etc/docker/daemon.json, '
                     "then: sudo systemctl restart docker")
    return Check("docker.storage", "Docker storage driver", WARN, f"{drv}: untested with Android images",
                 "overlay2 on ext4/xfs is the tested combination.")


def check_docker_pools(c: Ctx) -> Check:
    """Room for containers and networks at scale."""
    title = "Docker address space at scale"
    info = c.docker_info()
    if not info:
        return Check("docker.address_pools", title, SKIP, "Docker daemon is not reachable")
    docker = c.config.docker.binary
    pools = info.get("DefaultAddressPools") or []
    capacity_nets = 0
    for p in pools:
        try:
            net = ipaddress.ip_network(p.get("Base", ""), strict=False)
            capacity_nets += 2 ** max(0, int(p.get("Size", net.prefixlen)) - net.prefixlen)
        except (ValueError, TypeError):
            continue
    capacity_nets = capacity_nets or DOCKER_DEFAULT_NETWORKS
    r = c.tool([docker, "network", "ls", "--filter", "driver=bridge", "-q"], timeout=15)
    used = len(r.stdout.split()) if r and r.ok else 0
    r = c.tool([docker, "network", "inspect", "bridge", "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}"], timeout=15)
    bridge_hosts = 0
    if r and r.ok and r.stdout.split():
        try:
            bridge_hosts = max(0, ipaddress.ip_network(r.stdout.split()[0], strict=False).num_addresses - 3)
        except ValueError:
            pass
    try:
        instances = len(Store(c.paths).ids())
    except OSError:
        instances = 0
    detail = (f"{used} of about {capacity_nets} bridge networks in use; default bridge has room for "
              f"{bridge_hosts or '?'} containers; {instances} Androids defined")
    daemon_fix = ('Add larger pools to /etc/docker/daemon.json, e.g. '
                  '{"default-address-pools":[{"base":"10.200.0.0/14","size":24}]} (1024 networks), '
                  "then: sudo systemctl restart docker")
    if bridge_hosts and instances > bridge_hosts:
        return Check("docker.address_pools", title, FAIL, detail + ": more Androids than addresses on the default bridge",
                     'Widen the default bridge in /etc/docker/daemon.json ("bip": "172.17.0.1/16"), then: sudo systemctl restart docker')
    if used >= capacity_nets * 0.8:
        return Check("docker.address_pools", title, WARN, detail + ": close to the limit, new networks will soon fail",
                     daemon_fix + f"\n{docker} network prune    # only if you know every unused network is disposable")
    return Check("docker.address_pools", title, OK, detail)


def check_adb_published(c: Ctx) -> Check:
    """Catch ADB ports that are already published on every interface."""
    title = "Published ADB ports"
    if not c.docker_info():
        return Check("net.adb_published", title, SKIP, "Docker daemon is not reachable")
    prefix = c.config.docker.name_prefix
    r = c.tool([c.config.docker.binary, "ps", "--filter", f"label=io.superfarm.farm={prefix}",
                "--format", "{{.Names}} {{.Ports}}"], timeout=15)
    if not r or not r.ok:
        return Check("net.adb_published", title, SKIP, "could not list containers")
    exposed = sorted({line.split()[0] for line in r.stdout.splitlines()
                      if re.search(r"(0\.0\.0\.0|\[?::\]?):\d+->5555/tcp", line)})
    if exposed:
        return Check("net.adb_published", title, WARN,
                     f"ADB of {len(exposed)} running Android(s) is reachable from the network: {', '.join(exposed[:5])}",
                     "Set network.adb_bind to 127.0.0.1 and recreate them (`sf recreate all`). ADB has no password.")
    return Check("net.adb_published", title, OK, "no running Android publishes ADB on all interfaces")


# ============================================================ limits

def _sysctl_check(c: Ctx, cid: str, title: str, keys: list[str]) -> Check:
    floors = {s.key: s for s in setup.SYSCTLS}
    low, seen = [], []
    for k in keys:
        v = setup.read_sysctl(k, c.root)
        if v is None:
            continue
        seen.append(f"{k}={v}")
        if v < floors[k].minimum:
            low.append(f"{k}={v} (want {floors[k].minimum})")
    if not seen:
        return Check(cid, title, SKIP, "not readable")
    if low:
        cmds = "\n".join(f"sudo sysctl -w {k.split('=')[0]}={floors[k.split('=')[0]].minimum}" for k in low)
        return Check(cid, title, WARN, "; ".join(low) + ". " + floors[keys[0]].why,
                     cmds + "\nsf host apply    # writes them to " + setup.SYSCTL_FILE)
    return Check(cid, title, OK, ", ".join(seen))


def check_inotify(c: Ctx) -> Check:
    return _sysctl_check(c, "limits.inotify", "inotify limits",
                         ["fs.inotify.max_user_instances", "fs.inotify.max_user_watches"])


def check_pid_max(c: Ctx) -> Check:
    return _sysctl_check(c, "limits.pid_max", "Process id limit", ["kernel.pid_max"])


def check_file_max(c: Ctx) -> Check:
    return _sysctl_check(c, "limits.file_max", "Open file limit", ["fs.file-max"])


def check_neigh(c: Ctx) -> Check:
    return _sysctl_check(c, "limits.neigh", "ARP / neighbour table size",
                         [f"net.ipv4.neigh.default.gc_thresh{i}" for i in (1, 2, 3)])


# ============================================================ memory / disk

def check_ram(c: Ctx) -> Check:
    mem = capacity.meminfo(c.root)
    total = mem.get("MemTotal", 0)
    if not total:
        return Check("mem.ram", "Memory", SKIP, "could not read /proc/meminfo")
    est = capacity.estimate(c.config, c.gpus(), root=c.root, paths=c.paths)
    detail = (f"{total / 1024:.1f} GB total, {mem.get('MemAvailable', 0) / 1024:.1f} GB available; room for about "
              f"{est['limits']['ram']['max']} Androids by memory ({est['limits']['ram']['per_android_mb']} MB each "
              f"in {est['mode']} mode)")
    need = c.config.resources.memory_mb + c.config.performance.reserve_host_mb // 2
    if total < need:
        return Check("mem.ram", "Memory", FAIL, detail,
                     f"At least {need} MB ({-(-need // 1024)} GB) of RAM is needed for one Android: its limit "
                     "(resources.memory_mb) plus half of performance.reserve_host_mb.")
    if total < 8192:
        return Check("mem.ram", "Memory", WARN, detail, "Expect a few Androids at most; see `sf host capacity`.")
    return Check("mem.ram", "Memory", OK, detail)


def check_swap(c: Ctx) -> Check:
    mem = capacity.meminfo(c.root)
    swap = mem.get("SwapTotal", 0)
    zram = capacity.zram_active(c.root)
    if not mem:
        return Check("mem.swap", "Swap / zram", SKIP, "could not read /proc/meminfo")
    if zram:
        swappiness = setup.read_sysctl("vm.swappiness", c.root)
        note = ("" if swappiness is None or swappiness >= 100 else
                f"; vm.swappiness is {swappiness}, the measured zram setup ran with {capacity.ZRAM_SWAPPINESS} "
                f"(sf config set performance.swappiness {capacity.ZRAM_SWAPPINESS} && sf host apply)")
        return Check("mem.swap", "Swap / zram", OK,
                     f"{swap / 1024:.1f} GB swap, {capacity.zram_size_mb(c.root) / 1024:.1f} GB of it zram" + note)
    hint = "sf config set performance.zram.enabled true && sf host apply    # compressed swap in RAM"
    if not swap:
        return Check("mem.swap", "Swap / zram", WARN,
                     "no swap at all: when memory runs out the kernel kills Androids instead of slowing down", hint)
    return Check("mem.swap", "Swap / zram", OK, f"{swap / 1024:.1f} GB disk swap, no zram (optional: {hint})")


def _mount_of(c: Ctx, path: Path) -> tuple[str, str]:
    """(mount point, filesystem type) holding `path`, from /proc/mounts."""
    try:
        target = str(path.resolve())
    except OSError:
        target = str(path)
    best = ("", "")
    for line in c.read("proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = parts[1].replace("\\040", " ")
        if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best[0]):
            best = (mnt, parts[2])
    return best


def _fs_check(c: Ctx, cid: str, title: str, path: Path, move_hint: str) -> Check:
    mnt, fstype = _mount_of(c, path)
    if not fstype:
        return Check(cid, title, SKIP, f"could not find the filesystem of {path}")
    where = f"{path} is on {fstype} ({mnt})"
    if fstype in FS_NO_UNIX:
        return Check(cid, title, FAIL,
                     where + ": no Unix permissions, owners, FIFOs or extended attributes. Secrets cannot be "
                     "made private, sockets cannot be created and Android data would be corrupted.", move_hint)
    if fstype in FS_NETWORK or fstype.startswith("fuse."):
        return Check(cid, title, WARN, where + ": network/FUSE filesystems are slow for Android data and may not "
                     "support sockets, locks or extended attributes.", move_hint)
    return Check(cid, title, OK, where)


def check_home_fs(c: Ctx) -> Check:
    return _fs_check(c, "disk.home_fs", "Farm home filesystem", c.paths.home,
                     "Move the farm home to a native Linux filesystem (ext4, xfs, btrfs): "
                     "export SUPERFARM_HOME=~/.local/share/superfarm")


def check_docker_fs(c: Ctx) -> Check:
    info = c.docker_info()
    if not info:
        return Check("disk.docker_fs", "Docker data filesystem", SKIP, "Docker daemon is not reachable")
    return _fs_check(c, "disk.docker_fs", "Docker data filesystem", Path(info.get("DockerRootDir", "/var/lib/docker")),
                     'Point Docker at a native Linux filesystem: {"data-root": "/path/on/ext4"} in '
                     "/etc/docker/daemon.json, then: sudo systemctl restart docker")


def check_disk_space(c: Ctx) -> Check:
    notes, status = [], OK
    targets = [("farm home", c.paths.home, 5, 1)]
    info = c.docker_info()
    if info:
        # An Android's /data grows to several GB once apps are installed.
        targets.append(("Docker data", Path(info.get("DockerRootDir", "/var/lib/docker")), 20, 5))
    for label, path, warn_gb, fail_gb in targets:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free / 1024 ** 3
        except OSError:
            continue
        notes.append(f"{label}: {free:.0f} GB free")
        if free < fail_gb:
            status = FAIL
        elif free < warn_gb and status == OK:
            status = WARN
    if not notes:
        return Check("disk.space", "Disk space", SKIP, "could not read disk usage")
    return Check("disk.space", "Disk space", status, "; ".join(notes),
                 "" if status == OK else "Free space or move the data: each Android needs 2-6 GB once apps are "
                 "installed (`docker system df` shows what uses it; `sf backup prune` trims old backups).")


# ============================================================ Secure Boot / camera

def _secure_boot(c: Ctx) -> bool | None:
    if "sb" in c.cache:
        return c.cache["sb"]
    state: bool | None = None
    try:
        data = (c.root / "sys/firmware/efi/efivars" / SECUREBOOT_VAR).read_bytes()
        state = bool(data and data[-1] == 1)       # 4 attribute bytes, then the value
    except OSError:
        if not (c.root / "sys/firmware/efi").exists() and (c.root / "sys").exists():
            state = False                           # legacy BIOS boot: no Secure Boot
    if state is None and c.root == Path("/"):
        r = c.tool(["mokutil", "--sb-state"], timeout=5)
        if r and r.ok:
            state = "enabled" in r.stdout.lower()
    c.cache["sb"] = state
    return state


def check_secure_boot(c: Ctx) -> Check:
    sb = _secure_boot(c)
    if sb is None:
        return Check("boot.secureboot", "Secure Boot", SKIP, "state unknown (no efivars access, mokutil missing)",
                     c.distro.install_command("mokutil"))
    if not sb:
        return Check("boot.secureboot", "Secure Boot", OK, "off: out-of-tree modules load without signing")
    return Check("boot.secureboot", "Secure Boot", OK,
                 "on: the kernel refuses unsigned modules. Only the virtual camera module (v4l2loopback) is "
                 "affected; it must be signed with an enrolled key after every kernel update",
                 f"{SIGN_MODULE}    # prints enrolment steps when no key is enrolled")


def check_camera_module(c: Ctx) -> Check:
    title = "Virtual camera module (v4l2loopback)"
    enabled = c.config.media.camera.enabled
    st = modules.state("v4l2loopback", root=c.root, _cache=c.cache.setdefault("modstate", {}))
    sb = _secure_boot(c)
    if st == "loaded":
        first = c.config.media.camera.device_base + 1
        present = sum(1 for n in range(first, first + c.config.media.camera.slots)
                      if (c.root / f"dev/video{n}").exists())
        return Check("camera.module", title, OK,
                     f"loaded; {present} of {c.config.media.camera.slots} camera devices exist"
                     + ("" if present or not enabled else " (run `sf host apply` to create them)"))
    if not enabled:
        return Check("camera.module", title, SKIP, f"cameras are off (media.camera.enabled); module state: {st}")
    if st == "available":
        signer = ""
        if c.root == Path("/"):
            r = c.tool(["modinfo", "-F", "signer", "v4l2loopback"], timeout=5)
            signer = r.stdout.strip() if r and r.ok else ""
        if sb and not signer:
            return Check("camera.module", title, FAIL, "installed but unsigned, and Secure Boot is on: it will not load",
                         f"{SIGN_MODULE}\n{HOST_APPLY}")
        return Check("camera.module", title, WARN, "installed but not loaded" + (f" (signed by {signer})" if signer else ""),
                     "sf host apply")
    return Check("camera.module", title, FAIL, "not installed for this kernel",
                 c.distro.install_command("build-tools", "kernel-headers")
                 + f"\nsudo {resource_path('native', 'v4l2loopback', 'build.sh')} install"
                 + (f"\n{SIGN_MODULE}    # Secure Boot is on" if sb else "")
                 + f"\n{HOST_APPLY}")


# ============================================================ GPU

def check_gpus(c: Ctx) -> Check:
    gpus = c.gpus()
    if not gpus:
        return Check("gpu.detected", "GPUs", WARN, "no DRM render node found: Androids will draw on the CPU "
                     "(about five times the CPU of a GPU mode)",
                     "Install the GPU's kernel driver; any Intel or AMD GPU works out of the box.")
    parts = [f"{g.node} {g.name} [{gpu_mod.KIND_LABEL.get(g.kind, 'other')}, {g.driver or 'no driver'}] -> "
             f"{', '.join(m for m in (g.kind, *g.usable_modes) if m) if g.usable_modes else 'unusable: ' + g.note}"
             for g in gpus]
    status = OK if any(g.usable_modes for g in gpus) else WARN
    several = [g for g in gpus if g.vendor == "nvidia" and g.usable_modes]
    if len(several) > 1:
        # One relay per card, each limited to its card by a Vulkan layer of the host.
        parts.append(f"{len(several)} NVIDIA cards: each relay is limited to its own card through Mesa's device-select "
                     "layer, " + ("which is installed" if venus.device_select_layer(c.root) else
                                  "which is NOT installed: every relay would offer all cards and Androids would draw "
                                  "on the first (install the distribution's Mesa Vulkan layers package)")
                     + "; several NVIDIA cards have not been verified")
    return Check("gpu.detected", "GPUs", status, "; ".join(parts),
                 "" if status == OK else "No card can be used by Android; software rendering will be used.")


def _groups() -> set[str]:
    """Names of the groups this process is in."""
    mine = set()
    for gid in os.getgroups():
        try:
            mine.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return mine


def check_gpu_access(c: Ctx) -> Check:
    title = "Render node permissions"
    gpus = [g for g in c.gpus() if g.usable_modes]
    if not gpus:
        return Check("gpu.access", title, SKIP, "no usable GPU")
    mine = _groups()
    denied = [g for g in gpus if not os.access(g.node, os.R_OK | os.W_OK)]
    user = c.env.get("USER") or "$USER"
    if not denied:
        in_group = mine & {"video", "render"}
        opened = "this user can open " + ", ".join(g.node for g in gpus)
        if in_group:
            return Check("gpu.access", title, OK, opened + " (through groups " + ", ".join(sorted(in_group)) + ")")
        # The ACL belongs to whoever sits at the machine: it is not there over SSH, and it
        # goes away when that person logs out. Intel and AMD cards do not care (Docker
        # passes the node itself), the NVIDIA relay does: it opens the card as this user.
        nvidia = [g for g in gpus if g.vendor == "nvidia"]
        if nvidia:
            owners = set()
            for g in nvidia:
                try:
                    owners.add(grp.getgrgid(os.stat(g.node).st_gid).gr_name)
                except (OSError, KeyError):
                    owners.add("render")
            return Check("gpu.access", title, WARN,
                         opened + ", but only through the login session's device ACL: over SSH, on a server without "
                         "a desktop, or once you log out while the user service keeps running, the NVIDIA relay "
                         "loses the card",
                         f"sudo usermod -aG {','.join(sorted(owners))} {user}    # then log out and in again")
        return Check("gpu.access", title, OK, opened + " (through the login session's device ACL)")
    owners = set()
    for g in denied:
        try:
            owners.add(grp.getgrgid(os.stat(g.node).st_gid).gr_name)
        except (OSError, KeyError):
            owners.update(("render", "video"))
    needs_relay = any(g.vendor == "nvidia" for g in denied)
    return Check("gpu.access", title, FAIL if needs_relay else WARN,
                 f"no access to {', '.join(g.node for g in denied)}"
                 + (": the NVIDIA relay runs as this user and must open the card"
                    if needs_relay else ": Docker passes Intel/AMD nodes to Androids itself, so they still work; "
                    "only host-side tools are affected"),
                 f"sudo usermod -aG {','.join(sorted(owners))} {user}    # then log out and in again")


def _nvidia_gpus(c: Ctx) -> list[gpu_mod.GpuInfo]:
    return [g for g in c.gpus() if g.vendor == "nvidia"]


def check_nvidia_driver(c: Ctx) -> Check:
    title = "NVIDIA driver"
    cards = _nvidia_gpus(c)
    if not cards:
        return Check("nvidia.driver", title, SKIP, "no NVIDIA card")
    version = c.read("proc/driver/nvidia/version")
    m = re.search(r"Kernel Module(?: for \S+)?\s+(\d+\.\d+(?:\.\d+)?)", version)
    if cards[0].driver != "nvidia" or not m:
        return Check("nvidia.driver", title, FAIL,
                     f"card uses driver {cards[0].driver or 'none'}; the relay needs NVIDIA's own driver",
                     {"debian": "sudo ubuntu-drivers install    # Ubuntu; on Debian: sudo apt-get install nvidia-driver",
                      "fedora": "sudo dnf install akmod-nvidia    # from RPM Fusion",
                      "arch": "sudo pacman -S nvidia-open nvidia-utils",
                      "suse": "sudo zypper install nvidia-video-G06 nvidia-gl-G06    # from the NVIDIA repository",
                      }.get(c.distro.family, "Install NVIDIA's proprietary or open kernel driver."))
    return Check("nvidia.driver", title, OK, f"version {m.group(1)}" + (" (open kernel module)" if "Open Kernel" in version else ""))


def check_nvidia_icd(c: Ctx) -> Check:
    if not _nvidia_gpus(c):
        return Check("nvidia.icd", "NVIDIA Vulkan driver", SKIP, "no NVIDIA card")
    icd = c.config.gpu.nvidia.icd
    if (c.root / icd.lstrip("/")).is_file():
        return Check("nvidia.icd", "NVIDIA Vulkan driver", OK, icd)
    return Check("nvidia.icd", "NVIDIA Vulkan driver", FAIL, f"{icd} not found",
                 "Install the NVIDIA userspace driver (it ships nvidia_icd.json), or find the file with "
                 "`find /usr/share /etc -name nvidia_icd.json` and set gpu.nvidia.icd to it.")


def check_nvidia_runtime(c: Ctx) -> Check:
    title = "NVIDIA runtime for Android (Venus)"
    if not _nvidia_gpus(c):
        return Check("nvidia.runtime", title, SKIP, "no NVIDIA card")
    st = venus.runtime_status(c.config, c.paths)
    if st["ready"]:
        return Check("nvidia.runtime", title, OK,
                     f"runtime in {st['runtime_dir']}, relay in {st['relay_dir']} ({st['built_for']} only)")
    return Check("nvidia.runtime", title, WARN,
                 f"not built, NVIDIA mode is unavailable and `auto` falls back. Missing: {', '.join(st['missing'][:4])}"
                 + (" ..." if len(st["missing"]) > 4 else ""),
                 f"{resource_path('native', 'venus', 'build.sh')} all    # about 15 minutes; needs Docker and 6 GB of disk")


def check_nvidia_relay(c: Ctx) -> Check:
    title = "NVIDIA relay"
    cards = _nvidia_gpus(c)
    if not cards:
        return Check("nvidia.relay", title, SKIP, "no NVIDIA card")
    if not venus.runtime_status(c.config, c.paths)["relay_ready"]:
        return Check("nvidia.relay", title, SKIP, "relay is not built")
    sock_root = Path(c.config.gpu.nvidia.socket_dir)
    rows = venus.relay_status(c.config, nodes=[g.node for g in cards], proc_root=c.root / "proc")
    running = [r for r in rows if r["running"]]
    if running:
        return Check("nvidia.relay", title, OK, "; ".join(f"{r['node']} pid {r['pid']}" for r in running))
    probe = sock_root if sock_root.exists() else sock_root.parent
    if not os.access(probe, os.W_OK):
        return Check("nvidia.relay", title, WARN, f"not running, and {probe} is not writable by this user",
                     "sf host apply    # creates the socket folder; or set gpu.nvidia.socket_dir to a folder you own")
    return Check("nvidia.relay", title, OK, "not running; it is started with the first NVIDIA Android")


# ============================================================ tools

def _tool_check(c: Ctx, cid: str, title: str, name: str, version_argv: list[str], pkg: str,
                minimum: tuple[int, ...] = (), why_min: str = "", required: bool = True, newer: str = "") -> Check:
    """`newer`: where a recent enough version comes from. Installing the package again is no
    fix when the installed one is the distribution's and too old."""
    if not c.which(name):
        return Check(cid, title, FAIL if required else WARN, f"`{name}` is not on PATH", c.distro.install_command(pkg))
    r = c.tool(version_argv, timeout=10)
    text = (r.stdout + r.stderr) if r else ""
    line = next((ln.strip() for ln in text.splitlines() if re.search(r"\d+\.\d+", ln)), "")
    v = _version(line)
    if minimum and v and v < minimum:
        return Check(cid, title, WARN, f"{line[:80]}: {why_min}", newer or c.distro.install_command(pkg))
    return Check(cid, title, OK, line[:80] or "installed")


def check_adb(c: Ctx) -> Check:
    return _tool_check(c, "tools.adb", "adb", "adb", ["adb", "version"], "adb")


SCRCPY_OLD = ("This distribution's scrcpy package is older than that. Use the upstream build: download "
              "scrcpy-linux-x86_64-*.tar.gz from https://github.com/Genymobile/scrcpy/releases, unpack "
              "it and put its folder on PATH ahead of the packaged one.")      # seen on Ubuntu 24.04 (scrcpy 1.25)


def check_scrcpy(c: Ctx) -> Check:
    """What each feature that uses scrcpy needs, as the code enforces it: native windows
    (stream/scrcpy.py) and listening (media/audio.py) only need a working `scrcpy`; the version
    just picks option names for windows. The H.264 live view (stream/h264.py) has a range."""
    from ..stream import h264
    title = "scrcpy"
    if not c.which("scrcpy"):
        return Check("tools.scrcpy", title, WARN,
                     "`scrcpy` is not on PATH: no native windows, no listening, no H.264 live view",
                     c.distro.install_command("scrcpy"))
    r = c.tool(["scrcpy", "--version"], timeout=10)
    text = (r.stdout + r.stderr) if r else ""
    line = next((ln.strip() for ln in text.splitlines() if re.search(r"\d+\.\d+", ln)), "")
    v = _version(line)
    if not v:
        return Check("tools.scrcpy", title, OK, line[:80] or "installed")
    low, high = ".".join(map(str, h264.MIN_VERSION)), h264.MAX_MAJOR
    tested = ", ".join(f"{a}.{b}.x" for a, b in h264.VERIFIED)
    if v < h264.MIN_VERSION or v[0] > high:
        live, ok = f"live H.264 view not supported (needs {low} up to {high}.x)", False
    elif v[:2] in h264.VERIFIED:
        live, ok = "live H.264 view supported", True
    else:
        live, ok = f"live H.264 view untested (tested: {tested}; stream.h264 `on` uses it anyway)", True
    detail = f"{line[:60]}: windows ok; {live}; listening ok (no version check)"
    if ok:
        return Check("tools.scrcpy", title, OK, detail)
    return Check("tools.scrcpy", title, WARN, detail,
                 SCRCPY_OLD if v < h264.MIN_VERSION else "install a scrcpy release the H.264 view supports")


def check_ffmpeg(c: Ctx) -> Check:
    return _tool_check(c, "tools.ffmpeg", "ffmpeg", "ffmpeg", ["ffmpeg", "-hide_banner", "-version"], "ffmpeg",
                       required=c.config.media.camera.enabled)


# ============================================================ security / environment

def check_lsm(c: Ctx) -> Check:
    lsm = c.read("sys/kernel/security/lsm").strip().split(",")
    notes, status, fix = [], OK, ""
    if c.read("sys/fs/selinux/enforce").strip() == "1":
        status = WARN
        notes.append("SELinux is enforcing: Androids run privileged and are unaffected, but the network sidecars "
                     "are confined and may be denied their bind-mounted VPN profiles")
        fix = f"sudo chcon -R -t container_file_t {c.paths.vpn} {c.paths.secrets}    # if a VPN sidecar cannot read its profile"
    elif "selinux" in lsm:
        notes.append("SELinux present, not enforcing")
    if "apparmor" in lsm:
        notes.append("AppArmor active: privileged Android containers run unconfined, sidecars under docker-default")
    if "lockdown" in lsm:
        notes.append("kernel lockdown available (with Secure Boot it is what rejects unsigned modules)")
    if not notes:
        notes.append("no SELinux or AppArmor confinement detected")
    return Check("security.lsm", "SELinux / AppArmor", status, "; ".join(notes), fix)


def check_time(c: Ctx) -> Check:
    title = "Clock synchronised"
    if (c.root / "run/systemd/timesync/synchronized").exists():
        return Check("time.sync", title, OK, "systemd-timesyncd reports synchronised")
    if c.root == Path("/"):
        r = c.tool(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=5)
        if r and r.ok and r.stdout.strip() == "yes":
            return Check("time.sync", title, OK, "NTP synchronised")
        r2 = c.tool(["chronyc", "-n", "tracking"], timeout=5)
        if r2 and r2.ok and "Leap status     : Normal" in r2.stdout:
            return Check("time.sync", title, OK, "chrony reports synchronised")
        if r is None and r2 is None:
            return Check("time.sync", title, SKIP, "neither timedatectl nor chronyc available")
    else:
        return Check("time.sync", title, SKIP, "not determinable")
    return Check("time.sync", title, WARN, "the clock is not synchronised: VPN TLS handshakes and app logins fail "
                 "with a wrong clock, and Androids take their time from the host",
                 "sudo timedatectl set-ntp true")


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def check_ports(c: Ctx) -> Check:
    cfg = c.config
    notes, status, fix = [], OK, ""
    host = "127.0.0.1" if cfg.server.host == "localhost" else cfg.server.host
    probe = c.cache.get("port_probe", _port_free)
    if not probe(host, cfg.server.port):
        status = WARN
        notes.append(f"panel port {cfg.server.port} is in use (fine if the farm daemon is already running)")
        fix = (f"ss -ltnp 'sport = :{cfg.server.port}'    # shows what holds it\n"
               f"sf config set server.port {cfg.server.port + 1}    # or move the panel to a free port")
    else:
        notes.append(f"panel port {cfg.server.port} is free")
    rng = c.read("proc/sys/net/ipv4/ip_local_port_range").split()
    base = cfg.network.adb_port_base
    if len(rng) == 2 and rng[0].isdigit() and int(rng[0]) <= base + 1000 and base <= int(rng[1]):
        status = WARN
        notes.append(f"ADB ports start at {base}, inside the kernel's outgoing port range {rng[0]}-{rng[1]}: "
                     "an Android's port may be taken by an unrelated connection")
        fix = (fix + "\n" if fix else "") + "sf config set network.adb_port_base 5555"
    else:
        notes.append(f"ADB ports from {base + 1}")
    cam = cfg.media.camera
    if cam.enabled and not probe(cam.rtmp_bind, cam.rtmp_port):
        status = WARN
        notes.append(f"RTMP ingest port {cam.rtmp_port} is in use")
    return Check("net.ports", "Ports", status, "; ".join(notes), fix)


def check_adb_bind(c: Ctx) -> Check:
    bind = c.config.network.adb_bind
    if bind in LOOPBACK:
        return Check("net.adb_bind", "ADB exposure", OK, f"ADB is published on {bind} only")
    everywhere = bind in ("0.0.0.0", "::", "")   # noqa: S104 - compared, not bound
    return Check("net.adb_bind", "ADB exposure", WARN,
                 f"network.adb_bind is {bind or '(empty)'}: ADB has no authentication, so "
                 + ("every machine that can reach this host" if everywhere else f"everything that can reach {bind}")
                 + " gets a root shell on every Android",
                 "sf config set network.adb_bind 127.0.0.1    # then `sf recreate all`; use an SSH tunnel for remote ADB")


def check_server_host(c: Ctx) -> Check:
    s = c.config.server
    if s.host in LOOPBACK:
        return Check("net.server_host", "Panel exposure", OK, f"panel and API listen on {s.host} only")
    tls = bool(s.tls_cert and s.tls_key)
    if tls:
        return Check("net.server_host", "Panel exposure", WARN,
                     f"panel listens on {s.host} with TLS; whoever has the API token has root on this machine "
                     "(Androids are privileged containers and the daemon controls Docker)",
                     "Keep the token secret and firewall the port to the addresses that need it.")
    return Check("net.server_host", "Panel exposure", WARN,
                 f"panel listens on {s.host} without TLS: the API token and every screen travel in clear text",
                 "sf config set server.host 127.0.0.1    # and reach it through an SSH tunnel or a TLS reverse proxy; "
                 "or set server.tls_cert / server.tls_key")


def check_session(c: Ctx) -> Check:
    """scrcpy windows and 'listen to a device' need the desktop user's display and sound."""
    title = "Desktop session (windows and sound)"
    runtime = c.env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    rt = c.root / runtime.lstrip("/")
    display = bool(c.env.get("WAYLAND_DISPLAY") or c.env.get("DISPLAY")) or any(rt.glob("wayland-*"))
    sound = (rt / "pulse/native").exists() or (rt / "pipewire-0").exists()
    # The daemon normally runs as a user service, which gets its environment from the user
    # manager rather than from this terminal: say what a service started now would have.
    note = ""
    if c.root == Path("/"):
        names = service.manager_variables(c.run)
        if names is not None:
            known = [v for v in service.DISPLAY_VARS if v in names]
            note = ("; a user service started now gets " + ", ".join(known) if known else
                    "; the user manager has no display variables, so a user service started now has no display")
    if display and sound:
        return Check("session.desktop", title, OK, "display and sound server reachable" + note)
    # A server: nothing to fix. No desktop is installed, so there is no session to run in.
    installed = any(any((c.root / folder).glob("*.desktop")) for folder in ("usr/share/xsessions", "usr/share/wayland-sessions"))
    if not display and not installed:
        return Check("session.desktop", title, SKIP,
                     "no desktop is installed (a server): native device windows and listening on the host's speakers "
                     "are not available; the web panel, live views and everything else work")
    lacking = [n for n, ok in (("display", display), ("sound server", sound)) if not ok]
    return Check("session.desktop", title, WARN,
                 f"no {' or '.join(lacking)} for this process: native device windows / listening will not work "
                 "(the web panel still does)" + note,
                 "Run the daemon inside your desktop login (`sf serve`), or as your user service: sf service install\n"
                 "systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY    # from a desktop "
                 "terminal, when the session does not publish them itself; then: systemctl --user restart superfarm")


def check_user(c: Ctx) -> Check:
    if os.geteuid() == 0:
        return Check("user.daemon", "Run as the desktop user", WARN,
                     "running as root: no display, no sound, and every file the farm writes becomes root's",
                     "Run `sf` as your normal user; only `sf host apply` needs root, and it asks for sudo itself.")
    return Check("user.daemon", "Run as the desktop user", OK, c.env.get("USER") or str(os.getuid()))


def check_host_setup(c: Ctx) -> Check:
    title = "Host setup applied"
    conf = bool(c.read(modules.MODULES_LOAD_FILE))
    sysctl = bool(c.read(setup.SYSCTL_FILE))
    unit = (c.root / setup.UNIT_DIR.lstrip("/") / "superfarm-host.service").exists()
    if conf and sysctl and unit:
        return Check("host.setup", title, OK, "module list, kernel limits and the boot service are installed")
    lacking = [n for n, ok in (("module list", conf), ("kernel limits", sysctl), ("boot service", unit)) if not ok]
    return Check("host.setup", title, WARN, f"not installed: {', '.join(lacking)}. The farm may work now and "
                 "stop after the next reboot", "sf host apply    # add --dry-run to see every command first")


CHECKS: tuple[Callable[[Ctx], Check], ...] = (
    check_arch, check_cpu_flags, check_cpu_count, check_kernel, check_binder, check_tun, check_veth,
    check_netfilter, check_autoload, check_ip_forward, check_cgroup,
    check_docker_installed, check_docker_running, check_docker_access, check_docker_cgroup, check_docker_storage,
    check_docker_pools, check_docker_fs,
    check_inotify, check_pid_max, check_file_max, check_neigh,
    check_ram, check_swap, check_disk_space, check_home_fs,
    check_secure_boot, check_camera_module,
    check_gpus, check_gpu_access, check_nvidia_driver, check_nvidia_icd, check_nvidia_runtime, check_nvidia_relay,
    check_adb, check_scrcpy, check_ffmpeg,
    check_lsm, check_time, check_ports, check_adb_bind, check_adb_published, check_server_host,
    check_session, check_user, check_host_setup,
)


def run_checks(config: Config, paths: Paths, *, root: Path | str = "/",
               runner: Callable[..., proc.Result] | None = None,
               which: Callable[[str], str | None] | None = None,
               env: Mapping[str, str] | None = None, distro: distro_mod.Distro | None = None,
               cache: dict | None = None) -> list[Check]:
    """Run every check. Never raises; a check that breaks is returned as `skip`."""
    root = Path(root)
    ctx = Ctx(config=config, paths=paths, root=root, distro=distro or distro_mod.detect(root),
              run=runner or proc.run, which=which or proc.which, env=os.environ if env is None else env,
              cache=cache if cache is not None else {})
    out: list[Check] = []
    for fn in CHECKS:
        try:
            out.append(fn(ctx))
        except Exception as e:  # noqa: BLE001 - a diagnostic tool must survive its own bugs
            name = fn.__name__.removeprefix("check_")
            out.append(Check(f"internal.{name}", name.replace("_", " "), SKIP, f"check failed to run: {e!r}"[:300]))
    return out


def summarize(checks: list[Check]) -> dict:
    counts = {s: sum(1 for c in checks if c.status == s) for s in (OK, WARN, FAIL, SKIP)}
    return {"counts": counts, "ready": counts[FAIL] == 0,
            "worst": FAIL if counts[FAIL] else WARN if counts[WARN] else OK}

"""Kernel modules the farm depends on, and how to tell whether they are there.

Why every module is named here instead of being left to the kernel: on Ubuntu 26.04
module auto-loading turned out to be switched off (/proc/sys/kernel/modprobe was empty).
Docker then could not create a network or publish a port until `veth`, `xt_tcpudp` and
`xt_nat` were loaded by name. So the farm never relies on auto-loading: everything it
needs is listed, loaded explicitly at setup and at every boot, and checked by the doctor.

All readers take a `root` so tests can run against a fake /proc, /sys and /lib/modules.
Nothing in this module loads or unloads anything by itself: load_argv() only *builds*
the modprobe commands, which superfarm.host.setup runs as root.
"""
from __future__ import annotations

import gzip
import os
import re
from dataclasses import dataclass
from pathlib import Path

BINDER_DEVICES = "binder,hwbinder,vndbinder"
MODULES_LOAD_FILE = "/etc/modules-load.d/superfarm.conf"
MODPROBE_FILE = "/etc/modprobe.d/superfarm.conf"


@dataclass(frozen=True)
class Module:
    name: str
    why: str
    group: str                    # binder | vpn | docker | netfilter | android-net | camera | memory
    required: bool = True         # False = loaded when present, absence is not an error
    params: str = ""              # options written to modprobe.d and passed to modprobe
    feature: str = ""             # "" = always; else only when that feature is on: camera | zram | wireguard


# Order matters only for readability; modprobe resolves dependencies itself.
MODULES: tuple[Module, ...] = (
    Module("binder_linux", "Android's IPC. Without it no Android boots.", "binder",
           params=f"devices={BINDER_DEVICES}"),
    Module("tun", "Tunnel device for the per-Android VPN clients.", "vpn"),
    Module("veth", "Virtual cable between a container and its bridge; Docker cannot create networks without it.",
           "docker"),
    Module("bridge", "Docker's bridge networks.", "docker"),
    Module("overlay", "Docker's image layers.", "docker"),
    Module("br_netfilter", "Lets firewall rules see bridged container traffic.", "docker", required=False),
    Module("nf_conntrack", "Connection tracking for NAT and the VPN kill switch.", "netfilter"),
    Module("nf_nat", "Address translation for containers.", "netfilter"),
    Module("xt_tcpudp", "Port matching; Docker cannot publish a port (ADB) without it.", "netfilter"),
    Module("xt_nat", "DNAT/SNAT targets behind published ports.", "netfilter"),
    Module("xt_MASQUERADE", "Outbound NAT for containers and for shared VPN gateways.", "netfilter"),
    Module("xt_conntrack", "State matching (ESTABLISHED,RELATED) used by Docker and the kill switch.", "netfilter"),
    Module("xt_addrtype", "Address-type matching in Docker's own rules.", "netfilter"),
    # The two iptables back ends. A host uses one of them; the other is simply absent or idle.
    Module("nf_tables", "nftables back end of iptables.", "netfilter", required=False),
    Module("nft_compat", "Lets iptables-nft use xt_* matches.", "netfilter", required=False),
    Module("nft_chain_nat", "NAT chains for iptables-nft.", "netfilter", required=False),
    Module("ip_tables", "Legacy iptables; Android's own network service (netd) only speaks this.", "android-net",
           required=False),
    Module("iptable_filter", "Legacy filter table for netd.", "android-net", required=False),
    Module("iptable_nat", "Legacy nat table for netd.", "android-net", required=False),
    Module("iptable_mangle", "Legacy mangle table: netd marks packets per network here.", "android-net",
           required=False),
    Module("iptable_raw", "Legacy raw table for netd.", "android-net", required=False),
    Module("ip6_tables", "Legacy IPv6 tables for netd.", "android-net", required=False),
    Module("ip6table_filter", "Legacy IPv6 filter table for netd.", "android-net", required=False),
    Module("ip6table_mangle", "Legacy IPv6 mangle table for netd.", "android-net", required=False),
    Module("ip6table_raw", "Legacy IPv6 raw table for netd.", "android-net", required=False),
    Module("xt_mark", "Packet marks: netd's per-network routing and the kill switch.", "android-net", required=False),
    Module("xt_connmark", "Connection marks used by netd.", "android-net", required=False),
    Module("xt_owner", "Per-app (uid) firewall rules inside Android.", "android-net", required=False),
    Module("xt_comment", "Rule comments; some iptables front ends add them to every rule.", "android-net",
           required=False),
    # Reject targets, so blocked traffic fails at once instead of hanging until a timeout.
    Module("ipt_REJECT", "Reject target (legacy iptables) for the kill switch.", "netfilter", required=False),
    Module("ip6t_REJECT", "Reject target (legacy ip6tables) for the kill switch.", "netfilter", required=False),
    Module("nft_reject_inet", "Reject target (nftables) for the kill switch.", "netfilter", required=False),
    Module("wireguard", "WireGuard tunnels.", "vpn", required=False, feature="wireguard"),
    Module("videodev", "Video4Linux core for virtual cameras.", "camera", feature="camera"),
    Module("v4l2loopback", "Virtual camera devices (out-of-tree; see native/v4l2loopback).", "camera",
           params="devices=0 max_buffers=8", feature="camera"),
    Module("zram", "Compressed swap in RAM.", "memory", feature="zram"),
)


def wanted(features: set[str] | frozenset[str] = frozenset()) -> list[Module]:
    """Modules for this configuration: the unconditional ones plus enabled features."""
    return [m for m in MODULES if not m.feature or m.feature in features]


def features_of(config) -> set[str]:
    """Which optional module groups a farm config switches on."""
    out: set[str] = set()
    if config.media.camera.enabled:
        out.add("camera")
    if config.performance.zram.enabled:
        out.add("zram")
    providers = {config.network.vpn.provider, *(g.provider for g in config.network.gateways.values())}
    if "wireguard" in providers:
        out.add("wireguard")
    return out


# ------------------------------------------------------------------ kernel state

def _norm(name: str) -> str:
    # The kernel treats - and _ in module names as the same character.
    return name.replace("-", "_")


def kernel_release(root: Path | str = "/") -> str:
    try:
        return (Path(root) / "proc/sys/kernel/osrelease").read_text().strip()
    except OSError:
        return os.uname().release


def loaded(root: Path | str = "/") -> set[str]:
    try:
        text = (Path(root) / "proc/modules").read_text()
    except OSError:
        return set()
    return {_norm(line.split()[0]) for line in text.splitlines() if line.strip()}


def _names_in(index: Path) -> set[str]:
    """Module names from modules.dep / modules.builtin (one path per line, any compression suffix)."""
    names: set[str] = set()
    try:
        with open(index, encoding="utf-8", errors="replace") as f:
            for line in f:
                path = line.split(":", 1)[0].strip()
                if not path:
                    continue
                base = os.path.basename(path)
                m = re.match(r"^(.+?)\.ko(\.(gz|xz|zst))?$", base)
                if m:
                    names.add(_norm(m.group(1)))
    except OSError:
        pass
    return names


def builtin(root: Path | str = "/", release: str | None = None) -> set[str]:
    root = Path(root)
    release = release or kernel_release(root)
    return _names_in(root / "lib/modules" / release / "modules.builtin")


def installed(root: Path | str = "/", release: str | None = None) -> set[str]:
    """Loadable modules that exist for the running kernel."""
    root = Path(root)
    release = release or kernel_release(root)
    return _names_in(root / "lib/modules" / release / "modules.dep")


def state(name: str, *, root: Path | str = "/", _cache: dict | None = None) -> str:
    """loaded | builtin | available (on disk, not loaded) | missing."""
    root = Path(root)
    c = _cache if _cache is not None else {}
    if "loaded" not in c:
        c["loaded"], c["builtin"], c["installed"] = loaded(root), builtin(root), installed(root)
    n = _norm(name)
    if n in c["loaded"]:
        return "loaded"
    # /sys/module/<name> also exists for built-in code that takes parameters.
    if n in c["builtin"] or (root / "sys/module" / n).is_dir():
        return "builtin"
    return "available" if n in c["installed"] else "missing"


def status(features: set[str] | frozenset[str] = frozenset(), *, root: Path | str = "/") -> list[dict]:
    cache: dict = {}
    return [{"name": m.name, "group": m.group, "required": m.required, "why": m.why,
             "state": state(m.name, root=root, _cache=cache)} for m in wanted(features)]


def autoload_enabled(root: Path | str = "/") -> bool:
    """False when the kernel has no helper to load modules on demand.

    /proc/sys/kernel/modprobe holds the helper's path; empty means requests such as
    "this iptables rule needs xt_nat" are silently dropped."""
    try:
        return bool((Path(root) / "proc/sys/kernel/modprobe").read_text().strip())
    except OSError:
        return True      # unreadable (old kernel, container): assume the default


def kernel_config(root: Path | str = "/", release: str | None = None) -> dict[str, str]:
    """CONFIG_* values of the running kernel, from /boot/config-<release>, the module tree
    or /proc/config.gz, whichever exists. Empty when the distribution ships none."""
    root = Path(root)
    release = release or kernel_release(root)
    text = ""
    for p in (root / "boot" / f"config-{release}", root / "lib/modules" / release / "config",
              root / "usr/lib/modules" / release / "config"):
        try:
            text = p.read_text(errors="replace")
            break
        except OSError:
            continue
    if not text:
        try:
            with gzip.open(root / "proc/config.gz", "rt", errors="replace") as f:
                text = f.read()
        except OSError:
            return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip('"')
    return out


def filesystems(root: Path | str = "/") -> set[str]:
    try:
        text = (Path(root) / "proc/filesystems").read_text()
    except OSError:
        return set()
    return {line.split()[-1] for line in text.splitlines() if line.strip()}


def binder_status(root: Path | str = "/") -> dict:
    """How (and whether) this kernel can give Android its binder devices.

    Two working arrangements exist: binderfs (each Android mounts its own private set of
    devices; preferred, isolates Androids from each other) or the classic fixed devices
    /dev/binder, /dev/hwbinder, /dev/vndbinder created by the module's `devices=` option."""
    root = Path(root)
    st = state("binder_linux", root=root)
    cfg = kernel_config(root)
    fs = filesystems(root)
    devices = [d for d in BINDER_DEVICES.split(",") if (root / "dev" / d).exists()]
    binderfs = "binder" in fs
    ipc = cfg.get("CONFIG_ANDROID_BINDER_IPC", "")
    ok = binderfs or len(devices) == 3
    if ok:
        how = "binderfs" if binderfs else "devices"
    elif st == "available" or ipc == "m":
        how = "module-not-loaded"
    elif ipc == "y" and cfg.get("CONFIG_ANDROID_BINDERFS", "") in ("y", "m"):
        how = "binderfs-not-registered"
    else:
        how = "unsupported"
    return {"ok": ok, "how": how, "module": st, "binderfs": binderfs, "devices": devices,
            "config_ipc": ipc, "config_binderfs": cfg.get("CONFIG_ANDROID_BINDERFS", "")}


# ------------------------------------------------------------------ what setup writes / runs

def modules_load_conf(features: set[str] | frozenset[str] = frozenset(), *, root: Path | str = "/") -> str:
    """Content of /etc/modules-load.d/superfarm.conf.

    Only modules that exist for the running kernel are listed: systemd-modules-load marks
    the whole unit failed when one name cannot be found, and the optional ones differ
    between kernels. The boot service loads the optional list itself, tolerating gaps."""
    cache: dict = {}
    lines = ["# Written by `sf host apply`. Kernel modules the Android farm needs at boot.",
             "# Listed explicitly because module auto-loading cannot be relied on."]
    for m in wanted(features):
        # v4l2loopback is created with devices=0 and filled per Android by the boot service;
        # loading it here is harmless, the options come from modprobe.d.
        st = state(m.name, root=root, _cache=cache)
        if st == "builtin":
            continue
        if st == "missing" and not m.required:
            continue
        lines.append(m.name)
    return "\n".join(lines) + "\n"


def modprobe_conf(features: set[str] | frozenset[str] = frozenset()) -> str:
    """Content of /etc/modprobe.d/superfarm.conf (module options)."""
    lines = ["# Written by `sf host apply`. Options for modules the Android farm loads."]
    lines += [f"options {m.name} {m.params}" for m in wanted(features) if m.params]
    return "\n".join(lines) + "\n"


def load_argv(module: Module) -> list[str]:
    """`modprobe` argv for one module (run as root). Parameters come from the table above,
    never from user input."""
    return ["modprobe", module.name, *module.params.split()]


def missing(features: set[str] | frozenset[str] = frozenset(), *, root: Path | str = "/",
            required_only: bool = True) -> list[Module]:
    """Modules that are neither loaded nor built in."""
    cache: dict = {}
    return [m for m in wanted(features)
            if (m.required or not required_only) and state(m.name, root=root, _cache=cache) in ("available", "missing")]

"""Which Linux this is, and what its packages are called.

The farm targets four package families. Nothing here assumes one of them: the family is
read from /etc/os-release, and every "how do I fix this" message in the doctor and the
installer is built from the tables below so it is the exact command for *this* machine.

    family    distributions                                  package manager
    debian    Debian, Ubuntu, Mint, Pop!_OS, Raspberry Pi OS  apt-get
    fedora    Fedora, RHEL, CentOS Stream, Rocky, Alma        dnf
    arch      Arch, Manjaro, EndeavourOS                      pacman
    suse      openSUSE Leap / Tumbleweed, SLES                zypper

Every name in the tables below is checked against the real package index of one release
per family by tests/distro/run.sh (Ubuntu 24.04, Debian 12, Fedora 44, Arch, openSUSE
Tumbleweed, each in a throwaway container): the required and optional sets are really
installed there, the rest is resolved without installing. Not covered by that check: the
exact kernel header package (named after the running kernel, which in a container is the
host's; only the package of the distribution's own default kernel is resolved) and the
RHEL-family hints.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

FAMILIES = ("debian", "fedora", "arch", "suse")

# os-release ID / ID_LIKE word -> family. ID_LIKE is consulted in order, so a derivative
# that names its parent ("ubuntu debian") lands in the right column without being listed.
_FAMILY_OF = {
    "debian": "debian", "ubuntu": "debian", "linuxmint": "debian", "pop": "debian", "raspbian": "debian",
    "kali": "debian", "elementary": "debian", "zorin": "debian", "neon": "debian", "devuan": "debian",
    "fedora": "fedora", "rhel": "fedora", "centos": "fedora", "rocky": "fedora", "almalinux": "fedora",
    "ol": "fedora", "amzn": "fedora", "nobara": "fedora",
    "arch": "arch", "manjaro": "arch", "endeavouros": "arch", "garuda": "arch", "cachyos": "arch",
    "artix": "arch",
    "suse": "suse", "opensuse": "suse", "opensuse-leap": "suse", "opensuse-tumbleweed": "suse",
    "sles": "suse", "sled": "suse",
}
# RHEL and its rebuilds share Fedora's package manager but not its repositories: Docker,
# adb and ffmpeg are not in the base repos there. They get their own hints.
_RHEL_LIKE = {"rhel", "centos", "rocky", "almalinux", "ol", "amzn"}

_MANAGER = {"debian": "apt", "fedora": "dnf", "arch": "pacman", "suse": "zypper"}

# key -> family -> package names. None = not packaged there (see _HINTS).
# "{kernel}" is replaced with the running kernel release (uname -r).
_PACKAGES: dict[str, dict[str, tuple[str, ...] | None]] = {
    "docker": {"debian": ("docker.io",), "fedora": ("moby-engine",), "arch": ("docker",), "suse": ("docker",)},
    "adb": {"debian": ("adb",), "fedora": ("android-tools",), "arch": ("android-tools",),
            "suse": ("android-tools",)},
    "scrcpy": {"debian": ("scrcpy",), "fedora": None, "arch": ("scrcpy",), "suse": ("scrcpy",)},
    "ffmpeg": {"debian": ("ffmpeg",), "fedora": ("ffmpeg-free",), "arch": ("ffmpeg",), "suse": ("ffmpeg",)},
    "v4l-utils": {f: ("v4l-utils",) for f in FAMILIES},
    # `python3-dev` is needed as soon as one dependency has no wheel for this interpreter;
    # its absence stopped the first native install of the reference farm.
    "python": {"debian": ("python3", "python3-venv", "python3-dev"), "fedora": ("python3", "python3-devel"),
               "arch": ("python",), "suse": ("python3", "python3-devel")},
    "kernel-headers": {"debian": ("linux-headers-{kernel}",), "fedora": ("kernel-devel-{kernel}",),
                       "arch": ("linux-headers",), "suse": ("kernel-default-devel",)},
    "build-tools": {"debian": ("build-essential",), "fedora": ("gcc", "make"), "arch": ("base-devel",),
                    "suse": ("gcc", "make")},
    "patchelf": {f: ("patchelf",) for f in FAMILIES},
    "zstd": {f: ("zstd",) for f in FAMILIES},
    "pigz": {f: ("pigz",) for f in FAMILIES},
    "openvpn": {f: ("openvpn",) for f in FAMILIES},
    "wireguard-tools": {f: ("wireguard-tools",) for f in FAMILIES},
    "mokutil": {f: ("mokutil",) for f in FAMILIES},
    "dkms": {f: ("dkms",) for f in FAMILIES},
    "git": {f: ("git",) for f in FAMILIES},
    "curl": {f: ("curl",) for f in FAMILIES},
    "kmod": {f: ("kmod",) for f in FAMILIES},
    "iproute": {"debian": ("iproute2",), "fedora": ("iproute",), "arch": ("iproute2",), "suse": ("iproute2",)},
    "pciutils": {f: ("pciutils",) for f in FAMILIES},
    "zram-tools": {"debian": ("util-linux",), "fedora": ("util-linux",), "arch": ("util-linux",),
                   "suse": ("util-linux",)},
    # Shared libraries the NVIDIA relay (virgl_test_server) links against at run time.
    "relay-runtime": {
        "debian": ("libepoxy0", "libgbm1", "libdrm2", "libvulkan1", "libegl1", "libva2", "libva-drm2", "libx11-6"),
        "fedora": ("libepoxy", "mesa-libgbm", "libdrm", "vulkan-loader", "libglvnd-egl", "libva", "libX11"),
        "arch": ("libepoxy", "mesa", "libdrm", "vulkan-icd-loader", "libglvnd", "libva", "libx11"),
        "suse": ("libepoxy0", "libgbm1", "libdrm2", "libvulkan1", "libglvnd", "libva2", "libva-drm2", "libX11-6"),
    },
}

# What to tell the operator when a package is missing from the distribution.
_HINTS: dict[tuple[str, str], str] = {
    ("scrcpy", "fedora"): "scrcpy is not in the Fedora/RHEL repositories. Use the upstream static build: "
                          "download scrcpy-linux-x86_64-*.tar.gz from https://github.com/Genymobile/scrcpy/releases, "
                          "unpack it and put its folder on PATH (or `dnf copr enable zeno/scrcpy && dnf install scrcpy`).",
    ("scrcpy", "debian"): "If your release has no scrcpy package (it left Debian testing for a while), use the upstream "
                          "static build from https://github.com/Genymobile/scrcpy/releases and put it on PATH.",
    ("docker", "rhel"): "RHEL-family repositories ship podman, not Docker. Add Docker's own repository: "
                        "`dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo` "
                        "then `dnf install docker-ce docker-ce-cli containerd.io`.",
    ("adb", "rhel"): "android-tools is in EPEL: `dnf install epel-release` first.",
    ("ffmpeg", "rhel"): "ffmpeg is not in RHEL-family base repositories; enable RPM Fusion (https://rpmfusion.org/Configuration) "
                        "and install `ffmpeg`.",
    ("scrcpy", "rhel"): "scrcpy is not packaged for RHEL-family systems; use the upstream static build from "
                        "https://github.com/Genymobile/scrcpy/releases.",
    ("kernel-headers", "arch"): "Install the headers that match your kernel flavour (linux-headers, linux-lts-headers, "
                                "linux-zen-headers; on Manjaro linuxXY-headers).",
}

_VALID_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._:-]*$")
# Every abstract package key Distro.packages() understands.
PACKAGE_KEYS: tuple[str, ...] = tuple(_PACKAGES)


@dataclass(frozen=True)
class PackageSet:
    """Result of translating package keys for one distribution."""
    names: tuple[str, ...] = ()
    hints: dict[str, str] = field(default_factory=dict)   # key -> how to get it when not packaged


@dataclass(frozen=True)
class Distro:
    id: str = "unknown"
    name: str = "Linux"
    version: str = ""
    like: tuple[str, ...] = ()
    family: str = "unknown"          # debian | fedora | arch | suse | unknown
    rhel_like: bool = False
    kernel: str = ""                 # uname -r

    @property
    def pkg_manager(self) -> str:
        return _MANAGER.get(self.family, "")

    @property
    def supported(self) -> bool:
        return self.family in FAMILIES

    # ------------------------------------------------------------ packages
    def packages(self, *keys: str) -> PackageSet:
        """Translate abstract package keys ("docker", "adb", ...) for this distribution."""
        names: list[str] = []
        hints: dict[str, str] = {}
        for key in keys:
            if key not in _PACKAGES:
                raise KeyError(f"unknown package key {key!r}")
            hint = self._hint(key)
            row = _PACKAGES[key].get(self.family)
            # RHEL-likes: a hint means "the Fedora name does not exist in your repositories".
            if row is None or (self.rhel_like and (key, "rhel") in _HINTS):
                hints[key] = hint or f"{key} is not packaged for {self.name}; install it from its upstream project."
                continue
            for n in self._kernel_specific(key, row):
                n = n.replace("{kernel}", self.kernel)
                if n not in names:
                    names.append(n)
            if hint:
                hints[key] = hint
        return PackageSet(tuple(names), hints)

    def _hint(self, key: str) -> str:
        if self.rhel_like and (key, "rhel") in _HINTS:
            return _HINTS[(key, "rhel")]
        return _HINTS.get((key, self.family), "")

    def _kernel_specific(self, key: str, row: tuple[str, ...]) -> tuple[str, ...]:
        """Kernel header packages are named after the kernel flavour on Arch and SUSE."""
        if key != "kernel-headers":
            return row
        k = self.kernel
        if self.family == "arch":
            if self.id == "manjaro":
                m = re.match(r"^(\d+)\.(\d+)\.", k)
                return (f"linux{m.group(1)}{m.group(2)}-headers",) if m else row
            for flavour in ("lts", "zen", "hardened", "rt"):
                if f"-{flavour}" in k:
                    return (f"linux-{flavour}-headers",)
        if self.family == "suse":
            m = re.search(r"-([a-z]+)$", k)
            if m and m.group(1) != "default":
                return (f"kernel-{m.group(1)}-devel",)
        return row

    def install_argv(self, names: tuple[str, ...] | list[str]) -> list[str]:
        """Non-interactive install command (run as root). Package names are validated because
        this argv is also printed for people to paste into a shell."""
        for n in names:
            if not _VALID_PKG.match(n):
                raise ValueError(f"bad package name {n!r}")
        if not names:
            return []
        if self.family == "debian":
            return ["apt-get", "install", "-y", "--no-install-recommends", *names]
        if self.family == "fedora":
            return ["dnf", "install", "-y", *names]
        if self.family == "arch":
            return ["pacman", "-S", "--needed", "--noconfirm", *names]
        if self.family == "suse":
            return ["zypper", "--non-interactive", "install", "--no-recommends", *names]
        raise ValueError(f"no known package manager for {self.name}; install manually: {' '.join(names)}")

    def refresh_argv(self) -> list[str]:
        """Refresh the package index. Only apt needs it before an install can succeed."""
        return ["apt-get", "update"] if self.family == "debian" else []

    def install_command(self, *keys: str) -> str:
        """A paste-able fix line: `sudo apt-get install -y ...`, or the hint when not packaged."""
        if not self.supported:
            return f"install with your package manager: {', '.join(keys)}"
        ps = self.packages(*keys)
        parts = []
        if ps.names:
            parts.append("sudo " + shlex.join(self.install_argv(ps.names)))
        parts += list(ps.hints.values())
        return "\n".join(dict.fromkeys(parts))


def parse_os_release(text: str) -> dict[str, str]:
    """KEY=value lines with optional shell quoting; comments and junk are ignored."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = [raw.strip("\"'")]
        out[key.strip()] = parts[0] if parts else ""
    return out


def family_of(os_id: str, like: tuple[str, ...] = ()) -> str:
    for word in (os_id, *like):
        fam = _FAMILY_OF.get(word.lower())
        if fam:
            return fam
    return "unknown"


def detect(root: Path | str = "/", kernel: str | None = None) -> Distro:
    """Read <root>/etc/os-release (falling back to /usr/lib/os-release, as the spec for
    that file says). `root` exists so tests can point at a fake tree."""
    root = Path(root)
    text = ""
    for rel in ("etc/os-release", "usr/lib/os-release"):
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            break
        except OSError:
            continue
    data = parse_os_release(text)
    os_id = data.get("ID", "unknown").lower()
    like = tuple(data.get("ID_LIKE", "").lower().split())
    return Distro(
        id=os_id, name=data.get("PRETTY_NAME") or data.get("NAME") or "Linux", version=data.get("VERSION_ID", ""),
        like=like, family=family_of(os_id, like),
        rhel_like=os_id in _RHEL_LIKE or (os_id != "fedora" and bool(_RHEL_LIKE & set(like))),
        kernel=kernel if kernel is not None else os.uname().release,
    )

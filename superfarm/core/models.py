"""What an Android instance *is*: its saved spec, and that spec resolved against the farm config.

    InstanceSpec      what is stored in instances/<id>/instance.yaml. Every tunable is
                      optional; None means "follow the farm default, even if it changes".
    Resolved          spec + config -> concrete values. This is the only thing the
                      container planner reads, so the planner stays a pure function.
    Names             every Docker object name for one instance, derived from the prefix.
"""
from __future__ import annotations

import re
import secrets
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import Config, GpuMode, NetMode

MAX_INSTANCE_ID = 9999
NET_IMAGE = "superfarm/net"       # sidecar image: network namespace owner, VPN client, kill switch
_TAG = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")

InstanceState = Literal["missing", "created", "stopped", "starting", "running", "booted", "error"]


def check_id(value: int | str) -> int:
    """Instance ids reach shell-free argv lists, file names and port numbers; keep them plain ints."""
    try:
        n = int(str(value), 10)
    except (TypeError, ValueError):
        raise ValueError(f"bad instance id {value!r}") from None
    if not 0 <= n <= MAX_INSTANCE_ID:
        raise ValueError(f"instance id {n} out of range 0-{MAX_INSTANCE_ID}")
    return n


class ResourceOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpus: float | None = Field(None, gt=0)
    memory_mb: int | None = Field(None, ge=512)
    memory_swap_mb: int | None = Field(None, ge=0)
    cpuset: str | None = Field(None, pattern=r"^[0-9,\-]*$", description="Explicit cores, e.g. `0-3` or `8,9`. Overrides cpu_pinning.")


class NetworkOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: NetMode | None = None
    vpn_profile: str | None = Field(None, description="Pinned VPN profile file name. None = chosen by network.vpn.selection.")
    gateway: str | None = None
    proxy: str | None = None
    killswitch: bool | None = None


class DeviceIdentity(BaseModel):
    """What the Android tells apps about itself. Generated once at creation, then stable:
    apps tie accounts to it, so it must survive every rebuild of the container."""
    model_config = ConfigDict(extra="forbid")
    profile: str = Field("none", description="Device preset this identity was drawn from.")
    android_id: str = Field("", pattern=r"^([0-9a-f]{16})?$")
    serial: str = Field("", pattern=r"^[A-Za-z0-9]{0,20}$")
    props: dict[str, str] = Field(default_factory=dict, description="ro.product.* style properties passed at boot.")
    locale: str | None = None
    timezone: str | None = None
    geo: str | None = None


class InstanceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str = Field("", max_length=60)
    tags: list[str] = Field(default_factory=list)
    notes: str = Field("", max_length=2000)
    created: float = Field(default_factory=time.time)

    android_version: str | None = None
    image: str | None = Field(None, description="Exact image to run, bypassing android.images.")
    data_volume: str | None = Field(None, description="Existing Docker volume to use as /data (adopt an Android created elsewhere). None = <prefix>-data-<id>.")
    width: int | None = Field(None, ge=240, le=3840)
    height: int | None = Field(None, ge=320, le=3840)
    dpi: int | None = Field(None, ge=80, le=640)
    fps: int | None = Field(None, ge=5, le=120)

    gpu_mode: GpuMode | None = None
    gpu_node: str | None = Field(None, pattern=r"^(/dev/dri/renderD\d+|/dev/dri/by-path/pci-[0-9a-f:.]+-render|[0-9a-f]{4,8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])?$")
    resources: ResourceOverrides = Field(default_factory=ResourceOverrides)
    network: NetworkOverrides = Field(default_factory=NetworkOverrides)
    device: DeviceIdentity = Field(default_factory=DeviceIdentity)

    camera: bool | None = Field(None, description="Attach a virtual camera + microphone. None = media.camera.enabled.")
    autostart: bool = False
    extra_boot_args: list[str] = Field(default_factory=list)
    extra_props: dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, v: int) -> int:
        return check_id(v)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = t.strip().lower()
            if not _TAG.match(t):
                raise ValueError(f"bad tag {t!r}: use a-z, 0-9, _ . - (max 32)")
            if t not in out:
                out.append(t)
        return out

    @field_validator("extra_boot_args")
    @classmethod
    def _boot_args(cls, v: list[str]) -> list[str]:
        for a in v:
            if not re.match(r"^[A-Za-z0-9_.]+=[^\s]*$", a):
                raise ValueError(f"bad boot argument {a!r}: expected key=value without spaces")
        return v

    @property
    def display_name(self) -> str:
        return self.name or f"Android {self.id}"


class Names(BaseModel):
    """Docker object names for one instance. Labels let the farm find its own containers
    without trusting names alone."""
    prefix: str
    id: int

    @property
    def android(self) -> str: return f"{self.prefix}-android-{self.id}"
    @property
    def net(self) -> str: return f"{self.prefix}-net-{self.id}"
    @property
    def volume(self) -> str: return f"{self.prefix}-data-{self.id}"

    @staticmethod
    def gateway(prefix: str, name: str) -> str: return f"{prefix}-gw-{name}"
    @staticmethod
    def gateway_network(prefix: str, name: str) -> str: return f"{prefix}-gwnet-{name}"

    def labels(self, role: str) -> dict[str, str]:
        return {"io.superfarm.farm": self.prefix, "io.superfarm.instance": str(self.id), "io.superfarm.role": role}


class Resolved(BaseModel):
    """Concrete values for one instance. Built by resolve(); never stored."""
    id: int
    name: str
    names: Names
    android_version: str
    image: str                    # image reference to run (built tag, custom tag, or base)
    base_image: str
    data_volume: str
    width: int
    height: int
    dpi: int
    fps: int
    gpu_mode: GpuMode             # may still be "auto"; superfarm.host.gpu picks the concrete mode at start
    gpu_node: str
    cpus: float
    memory_mb: int
    memory_swap_mb: int
    memory_reservation_mb: int
    cpuset: str
    cpu_shares: int
    pids_limit: int
    shm_mb: int
    oom_score_adj: int
    restart_policy: str
    net_mode: NetMode
    vpn_profile: str              # "" = not pinned; chosen at start by the VPN module
    gateway: str
    proxy: str
    killswitch: bool
    allow_lan: bool
    dns: list[str]
    ipv6: bool
    adb_bind: str
    adb_port: int
    camera: bool
    device: DeviceIdentity
    boot_args: list[str]
    props: dict[str, str]
    stop_timeout: int
    log_max_size: str = "10m"     # docker.log_max_size / log_max_files: without a cap one chatty Android fills the disk
    log_max_files: int = 2


def image_tag(prefix: str, version: str) -> str:
    """Tag of the locally built image for an Android version (base + overlays)."""
    return f"superfarm/android:{version}"


def resolve(spec: InstanceSpec, config: Config) -> Resolved:
    """Merge one instance's overrides over the farm defaults."""
    version = spec.android_version or config.android.default_version
    fps = spec.fps or config.android.fps
    fps_args: list[str] = []
    if spec.image:
        image = base = spec.image
    else:
        if version not in config.android.images:
            raise ValueError(f"Android {version} has no image configured (android.images)")
        entry = config.android.images[version]
        base = entry.base
        image = entry.tag or image_tag(config.docker.name_prefix, version)
        if entry.fps_property:
            # First in the list: the same key in extra_boot_args, set by hand, still wins.
            fps_args = [f"{entry.fps_property}={fps}"]
    names = Names(prefix=config.docker.name_prefix, id=spec.id)
    res, net = spec.resources, spec.network
    memory = res.memory_mb or config.resources.memory_mb
    swap = res.memory_swap_mb if res.memory_swap_mb is not None else config.resources.memory_swap_mb
    net_mode = net.mode or config.network.mode
    gateway = (net.gateway if net.gateway is not None else config.network.gateway) if net_mode == "gateway" else ""
    if net_mode == "gateway" and gateway not in config.network.gateways:
        raise ValueError(f"instance {spec.id}: gateway {gateway!r} is not defined in network.gateways")
    return Resolved(
        id=spec.id, name=spec.display_name, names=names, android_version=version, image=image, base_image=base,
        data_volume=spec.data_volume or names.volume,
        width=spec.width or config.android.width, height=spec.height or config.android.height,
        dpi=spec.dpi or config.android.dpi, fps=fps,
        gpu_mode=spec.gpu_mode or config.gpu.mode, gpu_node=spec.gpu_node or config.gpu.render_node,
        cpus=res.cpus or config.resources.cpus, memory_mb=memory, memory_swap_mb=max(swap, memory) if swap else memory,
        memory_reservation_mb=config.resources.memory_reservation_mb, cpuset=res.cpuset or "",
        cpu_shares=config.resources.cpu_shares, pids_limit=config.resources.pids_limit, shm_mb=config.resources.shm_mb,
        oom_score_adj=config.resources.oom_score_adj, restart_policy=config.performance.restart_policy,
        net_mode=net_mode, vpn_profile=net.vpn_profile or "", gateway=gateway,
        proxy=(net.proxy if net.proxy is not None else config.network.proxy) if net_mode == "proxy" else "",
        killswitch=config.network.killswitch if net.killswitch is None else net.killswitch,
        allow_lan=config.network.allow_lan, dns=list(config.network.dns), ipv6=config.network.ipv6,
        adb_bind=config.network.adb_bind, adb_port=config.network.adb_port_base + spec.id,
        camera=config.media.camera.enabled if spec.camera is None else spec.camera,
        device=spec.device, boot_args=[*fps_args, *config.android.extra_boot_args, *spec.extra_boot_args],
        props={**config.android.extra_props, **spec.extra_props}, stop_timeout=config.docker.stop_timeout,
        log_max_size=config.docker.log_max_size, log_max_files=config.docker.log_max_files,
    )


def new_android_id() -> str:
    return secrets.token_hex(8)


def parse_selector(selector: str, known: dict[int, InstanceSpec], running: set[int] | None = None) -> list[int]:
    """Turn a selector into instance ids, sorted.

        all | running | stopped | tag:<name> | 5 | 1-10 | 1-10,15,tag:eu   (comma = union)
        a leading `!` on a part removes instead of adds:  all,!tag:spare
    """
    running = running or set()
    picked: set[int] = set()
    for raw in (p.strip() for p in str(selector).split(",") if p.strip()):
        negate, part = (True, raw[1:]) if raw.startswith("!") else (False, raw)
        if part == "all":
            ids = set(known)
        elif part == "running":
            ids = set(known) & running
        elif part == "stopped":
            ids = set(known) - running
        elif part.startswith("tag:"):
            tag = part[4:].lower()
            ids = {i for i, s in known.items() if tag in s.tags}
        elif re.fullmatch(r"\d+-\d+", part):
            a, b = (check_id(x) for x in part.split("-"))
            ids = {i for i in known if min(a, b) <= i <= max(a, b)}
        elif re.fullmatch(r"\d+", part):
            ids = {check_id(part)} & set(known)
        else:
            raise ValueError(f"bad selector part {raw!r}")
        picked = picked - ids if negate else picked | ids
    return sorted(picked)

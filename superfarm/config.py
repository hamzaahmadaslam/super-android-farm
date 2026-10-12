"""Every setting of a farm, with its default and its meaning.

`config.yaml` in the farm home holds only what differs from these defaults. Each
field carries a description; `sf config docs` renders this file's models into
docs/CONFIGURATION.md, so the reference can never drift from the code.

Farm-wide values here are *defaults*. Almost all of them can be overridden per
Android in its InstanceSpec (see superfarm.core.models).
"""
from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .paths import Paths, get_paths
from .util.files import read_yaml, write_yaml

# software = CPU only. igpu / dgpu pick by kind (integrated / discrete) whatever the vendor;
# intel / amd / nvidia pick by vendor. A specific card is pinned with render_node.
GpuMode = Literal["auto", "software", "igpu", "dgpu", "intel", "amd", "nvidia"]
NetMode = Literal["direct", "vpn", "gateway", "proxy", "none"]
VpnProvider = Literal["openvpn", "wireguard"]

# Android versions the project knows how to run. `base` is the public upstream image;
# overlays (camera HAL, GPU runtime, extra files) are layered on top at build time.
SUPPORTED_ANDROID = ("9", "10", "11", "12", "13", "14", "15")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _is_loopback(address: str) -> bool:
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False              # a name: where it points is not known here


class ServerConfig(_Model):
    host: str = Field("127.0.0.1", description="Address the panel and API listen on. Keep 127.0.0.1 unless you put TLS in front; anything else exposes farm control to the network. The API token is equivalent to root on this machine (the daemon controls Docker, and Androids are privileged containers): treat it like a root password.")
    port: int = Field(7700, ge=1, le=65535, description="Port of the panel and API.")
    auth_required: bool = Field(True, description="Require the API token for every request. Turning this off is refused when host is not a loopback address; a daemon that is listening on another address keeps asking for the token until it is restarted on loopback.")
    tls_cert: str = Field("", description="PEM certificate file. With tls_key, the server speaks HTTPS.")
    tls_key: str = Field("", description="PEM private key file for tls_cert.")
    session_hours: int = Field(12, ge=1, le=720, description="How long a browser login stays valid.")
    max_upload_mb: int = Field(4096, ge=1, description="Largest file (APK, video) the panel accepts.")
    allowed_origins: list[str] = Field(default_factory=list, description="Extra browser origins allowed to call the API (CORS). Empty = same origin only.")
    audit_log: bool = Field(True, description="Record every state-changing request (who, what, when) in logs/audit.log. Requests nobody authenticated (refused logins, wrong tokens) go to logs/audit-anonymous.log, so they cannot push the real history out.")
    login_attempts_per_minute: int = Field(10, ge=1, description="Wrong tokens allowed per client address per minute; further wrong ones from that address are refused for a minute. The right token is always accepted.")
    trusted_proxies: list[str] = Field(default_factory=list, description="Addresses or networks (e.g. 127.0.0.1, 10.0.0.0/8) of reverse proxies whose X-Forwarded-For and X-Forwarded-Proto headers are believed. Empty = none: the recorded client is always the peer that connected. A proxy listed here must set both headers itself.")

    @field_validator("trusted_proxies")
    @classmethod
    def _proxy_addresses(cls, v: list[str]) -> list[str]:
        for entry in v:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                # No names and no "*": whoever is trusted here chooses the address that is rate-limited and audited.
                raise ValueError(f"server.trusted_proxies: {entry!r} is not an IP address or network") from None
        return v


class DockerConfig(_Model):
    binary: str = Field("docker", description="Docker CLI to use. The daemon runs this program: it is set in config.yaml or with `sf config set`, and cannot be changed through the API.")
    name_prefix: str = Field("sf", pattern=r"^[a-z][a-z0-9]{0,11}$", description="Prefix of every container, volume and network this farm creates. Two farms on one machine need different prefixes.")
    stop_timeout: int = Field(10, ge=0, le=300, description="Seconds an Android gets to shut down before it is killed.")
    subnet_pool: str = Field("10.213.0.0/16", description="Private range that shared-VPN gateway networks are carved from (one /24 each).")
    log_max_size: str = Field("10m", description="Per-container log size cap (json-file driver).")
    log_max_files: int = Field(2, ge=1, description="Rotated log files kept per container.")


class AndroidImage(_Model):
    base: str = Field(..., description="Upstream image this Android version starts from.")
    tag: str = Field("", description="Use this exact local image instead of building one from `base` (for an image you built or imported yourself).")
    arm_translation: bool = Field(True, description="Whether the image is expected to run ARM-only apps (native bridge present).")
    fps_property: str = Field("", pattern=r"^([A-Za-z0-9_.\-]{1,90})?$", description="Extra boot property that carries the frame rate, for an image whose compositor does not read the standard `androidboot.redroid_fps` (a patched build that takes its rate from a property of its own). The frame rate of every Android on this image is then passed under this name as well. Empty = the standard argument only.")


def _default_images() -> dict[str, AndroidImage]:
    return {v: AndroidImage(base=f"redroid/redroid:{v}.0.0-latest") for v in SUPPORTED_ANDROID}


class AndroidConfig(_Model):
    default_version: str = Field("13", description="Android version given to new instances.")
    images: dict[str, AndroidImage] = Field(default_factory=_default_images, description="Android version -> image. Add an entry to support another version or a custom build.")
    width: int = Field(480, ge=240, le=3840, description="Screen width in pixels.")
    height: int = Field(854, ge=320, le=3840, description="Screen height in pixels.")
    dpi: int = Field(160, ge=80, le=640, description="Screen density.")
    fps: int = Field(20, ge=5, le=120, description="Frames per second Android draws at. The single biggest CPU lever after the GPU mode.")
    extra_boot_args: list[str] = Field(default_factory=list, description="Extra `androidboot.*` / `ro.*` arguments appended to every Android's command line.")
    extra_props: dict[str, str] = Field(default_factory=dict, description="System properties set on every Android after boot (`setprop`).")

    @field_validator("default_version")
    @classmethod
    def _known(cls, v: str) -> str:
        return str(v)


class ResourcesConfig(_Model):
    cpus: float = Field(2.0, gt=0, le=256, description="CPU cores one Android may use (Docker --cpus).")
    memory_mb: int = Field(1536, ge=512, description="RAM limit per Android in MiB.")
    memory_swap_mb: int = Field(2560, ge=0, description="RAM + swap limit per Android in MiB. 0 = same as memory (no swap).")
    memory_reservation_mb: int = Field(0, ge=0, description="Soft limit the kernel reclaims toward under pressure. 0 = none.")
    cpu_pinning: Literal["none", "spread", "performance", "efficiency"] = Field("none", description="none: let the scheduler decide. spread: pin each Android to its own cores, round-robin. performance / efficiency: pin to P-cores or E-cores on hybrid CPUs.")
    cpu_shares: int = Field(0, ge=0, description="Relative CPU weight under contention. 0 = Docker default (1024).")
    pids_limit: int = Field(4096, ge=256, description="Maximum processes and threads inside one Android.")
    shm_mb: int = Field(64, ge=16, description="/dev/shm size in MiB.")  # noqa: S108 - a description, not a path we write to
    oom_score_adj: int = Field(0, ge=-1000, le=1000, description="Bias for the kernel's out-of-memory killer; raise it to sacrifice Androids before the desktop.")


class NvidiaConfig(_Model):
    runtime_dir: str = Field("", description="Folder holding the Venus runtime for Android (vulkan.virtio.so, gralloc.venus.so, gralloc.venus.x86.so, gpu_config.sh). Empty = <home>/overlays/venus.")
    relay_dir: str = Field("", description="Install prefix of the patched virgl_test_server relay (contains bin/ and lib/). Empty = <home>/overlays/venus-relay. The daemon runs the relay from here: set in config.yaml or with `sf config set`, not through the API.")
    socket_dir: str = Field("/run/superfarm/venus", description="Where relay sockets live. One socket per NVIDIA card.")
    icd: str = Field("/usr/share/vulkan/icd.d/nvidia_icd.json", description="Vulkan ICD manifest of the NVIDIA driver on the host. It names the driver library the relay loads: set in config.yaml or with `sf config set`, not through the API.")
    debug: bool = Field(False, description="Verbose driver logging inside Android (mesa.log.level=debug). Costs CPU; leave off.")


class GpuConfig(_Model):
    mode: GpuMode = Field("auto", description="How Androids draw (default for the farm; each Android can override it). auto: best available. software: CPU only. igpu: the integrated GPU. dgpu: a discrete card of any vendor. intel / amd: that vendor's GPU through Mesa. nvidia: Vulkan relayed to the card (needs the Venus runtime).")
    prefer: Literal["dgpu", "igpu"] = Field("dgpu", description="Which kind `auto` tries first when the machine has both. igpu keeps the discrete card free for other work and draws the least power.")
    render_node: str = Field("", description="Force one /dev/dri/renderD* node for intel/amd/nvidia. Empty = detect by driver.")
    assignment: Literal["first", "round-robin", "least-instances"] = Field("round-robin", description="With several usable GPUs of the chosen kind: which one a new Android gets.")
    clear_shader_cache_on_change: bool = Field(True, description="Delete apps' compiled-shader caches when an Android's renderer changes. Stale caches crash apps at start.")
    nvidia: NvidiaConfig = Field(default_factory=NvidiaConfig)


class VpnConfig(_Model):
    provider: VpnProvider = Field("openvpn", description="VPN software for per-instance tunnels.")
    selection: Literal["fixed", "random", "round-robin", "unique"] = Field("round-robin", description="How an Android without a pinned profile gets one. unique: never share a profile between two running Androids (fails when profiles run out).")
    profile: str = Field("", description="Profile file name used when selection is `fixed`.")
    credentials: str = Field("default", description="Name of the credentials entry in secrets/ (username + password for OpenVPN auth-user-pass). Managed with `sf vpn credentials`.")
    ping_seconds: int = Field(10, ge=1, description="OpenVPN keepalive interval.")
    ping_restart_seconds: int = Field(120, ge=10, description="Tunnel is restarted after this long without a reply.")
    connect_retry_max: int = Field(5, ge=1, description="Connection attempts per server before giving up (the container then restarts).")
    extra_args: list[str] = Field(default_factory=list, description="Extra arguments passed to openvpn.")
    public_ip_url: str = Field("https://api.ipify.org", description="Service used to read an Android's public address for display and leak checks.")


class GatewayConfig(_Model):
    provider: VpnProvider = Field("openvpn", description="VPN software this gateway runs.")
    profile: str = Field(..., description="VPN profile this gateway connects with.")
    credentials: str = Field("default", description="Name of the credentials entry in secrets/ this gateway logs in with.")
    subnet: str = Field("", description="Private /24 for Androids behind this gateway. Empty = taken from docker.subnet_pool.")


class NetworkConfig(_Model):
    mode: NetMode = Field("direct", description="direct: Androids use the host's connection. vpn: one tunnel per Android. gateway: Androids share a named VPN gateway. proxy: Android's global HTTP proxy. none: no network at all.")
    gateway: str = Field("", description="Gateway name used when mode is `gateway`.")
    proxy: str = Field("", description="host:port used when mode is `proxy`.")
    killswitch: bool = Field(True, description="Block all traffic that would leave outside the tunnel. On from the first packet, before the VPN connects.")
    allow_lan: bool = Field(False, description="Let Androids reach private (RFC 1918) addresses on your LAN.")
    dns: list[str] = Field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"], description="DNS servers Androids use.")
    dns_leak_protection: bool = Field(True, description="With a VPN: resolve the VPN server names once at start, then allow DNS only through the tunnel.")
    ipv6: bool = Field(False, description="Leave IPv6 enabled inside Androids. Off by default: most VPN profiles do not carry IPv6, so it would bypass the tunnel.")
    adb_bind: str = Field("127.0.0.1", description="Host address ADB ports are published on. ADB has no authentication; do not expose it.")
    adb_port_base: int = Field(5555, ge=1024, le=60000, description="Android N is reachable on adb_port_base + N.")
    vpn: VpnConfig = Field(default_factory=VpnConfig)
    gateways: dict[str, GatewayConfig] = Field(default_factory=dict, description="Named shared VPN gateways.")


class DeviceConfig(_Model):
    profile: str = Field("random", description="Device model an Android reports: a preset name (the Create form in the panel lists them), `random`, or `none` to keep the image's own identity.")
    randomize_android_id: bool = Field(True, description="Give each new Android its own random android_id.")
    locale: str = Field("en-US", description="System language.")
    timezone: str = Field("", description="Time zone as an Olson name, e.g. Europe/Berlin. Empty = the host's zone.")
    geo: str = Field("", description="Mock location as `lat,lon`, e.g. `52.52,13.405`. Empty = none.")
    extra_props: dict[str, str] = Field(default_factory=dict, description="Extra build properties set at boot (ro.product.* and friends).")

    # The checks below live with the code that applies these values to an Android, and that
    # package builds on this module; hence the imports inside.
    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str) -> str:
        if v.strip():
            from .core.device_profile import check_timezone
            check_timezone(v.strip())
        return v.strip()

    @field_validator("geo")
    @classmethod
    def _position(cls, v: str) -> str:
        if v.strip():
            from .core.device_profile import parse_geo
            parse_geo(v)
        return v.strip()


class ZramConfig(_Model):
    enabled: bool = Field(False, description="Create a compressed RAM swap device at host setup. Raises how many Androids fit; costs some CPU under pressure.")
    size_percent: int = Field(50, ge=5, le=200, description="zram size as a percentage of RAM.")
    algorithm: str = Field("zstd", description="Compression algorithm.")


class PerformanceConfig(_Model):
    trim_packages: bool = Field(True, description="Disable system apps the farm never needs after first boot (list per Android version in data/trim/). Saves about 170 MB per Android.")
    trim_extra: list[str] = Field(default_factory=list, description="More package names to disable.")
    trim_keep: list[str] = Field(default_factory=list, description="Package names never to disable, even if listed.")
    disable_animations: bool = Field(True, description="Set Android's animation scales to 0. Fewer frames drawn, faster automation.")
    start_concurrency: int = Field(4, ge=1, le=64, description="Androids booted at the same time during bulk start. Booting is the heaviest moment.")
    start_stagger_seconds: float = Field(1.0, ge=0, description="Pause between launches inside a bulk start.")
    boot_timeout: int = Field(180, ge=30, description="Seconds to wait for sys.boot_completed before a start is reported failed.")
    memory_guard_mb: int = Field(1500, ge=0, description="Free-RAM threshold of the capacity guard: starting another Android when less than this much RAM is available counts as over capacity. 0 = no guard.")
    enforce_capacity: bool = Field(False, description="What happens when a start goes over the capacity estimate shown in System Info. false: warn and let the operator run more anyway. true: refuse unless the start is explicitly forced.")
    reserve_host_mb: int = Field(3072, ge=0, description="RAM the capacity estimate leaves for the host itself (desktop, Docker, the panel).")
    zram: ZramConfig = Field(default_factory=ZramConfig)
    ksm: bool = Field(False, description="Enable kernel same-page merging at host setup. Identical pages across Androids are stored once. Experimental.")
    swappiness: int = Field(-1, ge=-1, le=200, description="vm.swappiness to set at host setup. -1 = leave alone.")
    restart_policy: Literal["no", "on-failure", "unless-stopped"] = Field("no", description="Docker restart policy for Androids. `no` means nothing starts by itself after a reboot.")
    autostart: str = Field("", description="Selector of Androids to start when the daemon starts (e.g. `tag:always`, `1-10`, `all`). Empty = none.")


class StreamConfig(_Model):
    thumbnail_fps: float = Field(1.0, gt=0, le=10, description="Refresh rate of the grid's thumbnails.")
    thumbnail_width: int = Field(240, ge=80, le=1080, description="Thumbnail width in pixels.")
    live_fps: float = Field(8.0, gt=0, le=30, description="Refresh rate of the focused live view (screenshot transport).")
    jpeg_quality: int = Field(60, ge=10, le=95, description="JPEG quality of streamed frames.")
    max_viewers: int = Field(64, ge=1, description="Simultaneous live views served.")
    scrcpy_bitrate: str = Field("2M", description="Bitrate of native scrcpy windows.")
    scrcpy_max_size: int = Field(720, ge=0, description="Longest side of native scrcpy windows. 0 = full size.")
    scrcpy_max_fps: int = Field(20, ge=1, description="Frame rate cap of native scrcpy windows.")
    scrcpy_extra_args: list[str] = Field(default_factory=list, description="Extra arguments for native scrcpy windows.")
    h264: Literal["auto", "on", "off"] = Field("auto", description="Experimental H.264 live view in the browser (the device's own video stream, relayed from scrcpy-server; uses scrcpy_bitrate / scrcpy_max_size / scrcpy_max_fps). auto: offered when the installed scrcpy version is one this release was tested with. on: also with untested versions. off: never.")


class CameraConfig(_Model):
    enabled: bool = Field(False, description="Give Androids a virtual front camera and microphone fed from a video file or an RTMP stream. Needs the v4l2loopback module and the camera overlay.")
    slots: int = Field(32, ge=0, le=240, description="Virtual camera devices created at host setup (one per Android that will use a camera).")
    device_base: int = Field(100, ge=10, le=200, description="Android N's camera is /dev/video(device_base + N) on the host, for N from 1 to `slots` (the devices host setup creates). An Android with any other id is given a free device from that pool.")
    width: int = Field(640, description="Sensor frame width (landscape).")
    height: int = Field(360, description="Sensor frame height.")
    fps: int = Field(20, ge=5, le=60, description="Frames per second of the camera feed.")
    quality: int = Field(5, ge=2, le=31, description="MJPEG quantiser; lower is better and larger.")
    mirror: bool = Field(True, description="Flip the picture horizontally. On by default: apps mirror a front camera, and in a live broadcast test viewers (and the broadcaster's preview) saw the source the right way round only with this on. Turn it off if text in your source appears reversed.")
    rtmp_bind: str = Field("127.0.0.1", description="Address the RTMP ingest listens on for live sources (OBS). The ingest has no authentication (the stream key is not checked): whatever can reach this address becomes the camera picture and microphone of the Androids. Keep 127.0.0.1; for OBS on another PC use an SSH tunnel. Another address is refused unless rtmp_allow_remote is on.")
    rtmp_allow_remote: bool = Field(False, description="Accept an rtmp_bind that is not a loopback address. Only on a network where every machine is trusted: the ingest cannot tell your OBS from anyone else's.")
    rtmp_port: int = Field(1935, ge=1024, le=65535, description="Port the RTMP ingest listens on.")
    hal_overlay: str = Field("", description="Folder with the camera/microphone HAL files copied into /vendor. Empty = <home>/overlays/camera/<android version>.")

    @model_validator(mode="after")
    def _ingest(self) -> CameraConfig:
        if not self.rtmp_allow_remote and not _is_loopback(self.rtmp_bind):
            raise ValueError(f"media.camera.rtmp_bind {self.rtmp_bind!r} is not a loopback address and the RTMP ingest has "
                             "no authentication: keep 127.0.0.1 (use an SSH tunnel for OBS on another PC), or set "
                             "media.camera.rtmp_allow_remote to true on a network you trust")
        return self


class AudioConfig(_Model):
    listen_buffer_ms: int = Field(200, ge=20, le=2000, description="Buffer of the `listen to one device` stream. Larger = fewer stutters, more delay.")
    listen_volume: int = Field(15, ge=0, le=15, description="Android media volume applied while listening (capture is scaled by it).")


class MediaConfig(_Model):
    camera: CameraConfig = Field(default_factory=CameraConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)


class LlmConfig(_Model):
    provider: Literal["none", "ollama", "openai"] = Field("none", description="Model used to describe trained steps and to recover a stuck run. none: automation is fully deterministic. openai: any OpenAI-compatible endpoint.")
    base_url: str = Field("http://127.0.0.1:11434", description="Model server address.")
    model: str = Field("", description="Model name. Empty = first one the server lists.")
    api_key_secret: str = Field("llm", description="Name of the secret holding the API key (openai provider).")
    timeout: int = Field(120, ge=5, description="Seconds to wait for one answer from the model server.")
    temperature: float = Field(0.1, ge=0, le=2, description="Sampling temperature sent to the model. Low values keep answers repeatable.")
    num_ctx: int = Field(8192, ge=1024, description="Context window in tokens requested from the model (ollama provider).")


class AutomationConfig(_Model):
    max_parallel: int = Field(8, ge=1, le=256, description="Devices one run works on at the same time; the rest queue.")
    hierarchy_reader: Literal["auto", "uiautomator", "u2"] = Field("auto", description="How the UI tree is read. uiautomator: built in, waits for the screen to go idle. u2: persistent reader that never waits (needs the `u2` extra). auto: u2 when installed.")
    read_timeout: int = Field(30, ge=5, description="Hard limit for one UI-tree read.")
    step_timeout: int = Field(20, ge=1, description="Seconds a step may wait for its element before recovery starts.")
    run_timeout: int = Field(900, ge=10, description="Hard limit for one device's whole run.")
    match_threshold: float = Field(3.0, ge=0.5, description="Score an on-screen element needs to be accepted as a step's target (id 5, text 4, description 3, path 3, class 0.5, position up to 1).")
    settle_seconds: float = Field(0.6, ge=0, description="Pause after each action before the screen is read again.")
    human_jitter: bool = Field(True, description="Vary tap positions and pauses slightly, as a person does.")
    learn_fixes: bool = Field(True, description="Remember recoveries (keyed by app and screen) and replay them without asking the model again.")
    stop_on_unknown_screen: bool = Field(True, description="A device that meets a screen no rule covers stops with `needs attention` instead of guessing. The other devices carry on.")
    forbidden_field_words: list[str] = Field(default_factory=lambda: ["password", "passwd", "pin", "otp", "2fa", "seed", "card", "cvv", "ssn"], description="Values whose name contains one of these are never written to disk or sent to a model.")
    llm: LlmConfig = Field(default_factory=LlmConfig)


class BackupConfig(_Model):
    dir: str = Field("", description="Where backups go. Empty = <home>/backups.")
    compression: Literal["zstd", "gzip", "none"] = Field("zstd", description="How the data archive of a backup is compressed. none: a plain tar.")
    level: int = Field(3, ge=1, le=19, description="Compression level: 1 is fastest, higher is smaller and slower. gzip stops at 9.")
    keep: int = Field(7, ge=0, description="Backups kept per Android by `sf backup prune`. 0 = keep all.")
    stop_for_backup: bool = Field(True, description="Stop a running Android while its data is copied, for a consistent snapshot, then start it again.")
    auto_before_risky: bool = Field(True, description="Take a backup automatically before an operation that can lose data (delete, forced Android version change, restore over an existing Android). If that backup fails, the operation is refused unless forced.")
    schedule: str = Field("", description="Recurring backups as a cron line `minute hour day-of-month month day-of-week` (e.g. `0 4 * * *` = every day at 04:00). Empty = off.")
    schedule_selector: str = Field("all", description="Which Androids the scheduled backup covers.")
    include_secrets: bool = Field(False, description="Include VPN credentials and tokens in farm backups. Off by default so a backup can be stored or shared without leaking them.")

    @field_validator("schedule")
    @classmethod
    def _cron(cls, v: str) -> str:
        if v.strip():
            # Imported here: the parser lives with the automation schedules (one cron dialect
            # for the whole farm), and that package builds on this module.
            from .automation.schedule import parse_cron
            parse_cron(v)          # ValueError says which field is wrong
        return v.strip()


class ControlConfig(_Model):
    concurrency: int = Field(8, ge=1, le=256, description="Androids worked on at the same time by app, file, shell and device-setting operations; the rest queue.")
    install_timeout: int = Field(900, ge=30, description="Seconds one Android may take to install an app before that device is reported failed.")
    shell_timeout: int = Field(60, ge=1, le=3600, description="Default time limit for one shell command on one Android.")
    shell_output_kb: int = Field(256, ge=1, le=16384, description="Output kept per Android from one shell command; a command that prints more is stopped.")
    shell_denylist: bool = Field(True, description="Refuse obviously destructive shell commands (wipes, power-off, firewall changes that would open the VPN kill switch) unless the caller forces them.")
    max_app_mb: int = Field(8192, ge=16, description="Largest app (all split APKs and OBB files together, unpacked) the app library accepts.")
    max_transfer_mb: int = Field(2048, ge=1, description="Largest single file pushed to or pulled from an Android.")


class LoggingConfig(_Model):
    level: Literal["debug", "info", "warning", "error"] = Field("info", description="Least severe kind of message the daemon writes to its log.")
    max_mb: int = Field(20, ge=1, description="Size at which a log file rotates.")
    keep: int = Field(5, ge=1, description="Rotated files kept.")


class McpConfig(_Model):
    enabled: bool = Field(True, description="Let MCP clients (desktop assistants, IDE agents) connect through `sf mcp`. Off: `sf mcp` refuses to start. It needs the farm's API token either way, so this is a switch for the operator, not a security boundary.")
    http_port: int = Field(7711, ge=1024, le=65535, description="Port `sf mcp --http` listens on (loopback only, bearer token required) when no --port is given.")
    elicitation: bool = Field(False, description="When an approval is needed and the MCP client can ask its user questions (MCP elicitation), also show the Yes/No there. Off by default: a client may answer such a question by itself without showing it to anyone, and the decision is meant to be a person's. The request is in the farm's own queue either way and the first answer counts.")


class ModelProfile(_Model):
    provider: Literal["ollama", "openai"] = Field(..., description="Wire format of the model server. ollama: an Ollama server's own API. openai: any server that speaks the OpenAI chat-completions API (most hosted services and local servers do).")
    base_url: str = Field("http://127.0.0.1:11434", description="Address of the model server. For `openai`, with or without a trailing /v1.")
    model: str = Field("", max_length=200, description="Model name as the server knows it. Empty = the first one the server lists.")
    api_key_secret: str = Field("", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$|^$", description="Name of the secret that holds the API key (a file in <home>/secrets, written with `sf ai models key <profile>`). Never the key itself. Empty = the server needs no key.")
    tools: bool = Field(True, description="The model can call tools (function calling). The Assistant needs it. Off: the Pilot asks for plain JSON instead.")
    vision: bool = Field(False, description="The model can read pictures. On: the Pilot also sends a downscaled screenshot with every step.")
    timeout: int = Field(120, ge=5, le=1800, description="Seconds to wait for one answer.")
    temperature: float | None = Field(0.1, ge=0, le=2, description="Sampling temperature. Low values keep answers repeatable. Leave it out (null) for a server that does not accept the setting.")
    num_ctx: int = Field(16384, ge=1024, description="Context window in tokens requested from the model (ollama provider). The Assistant's tool list alone is about 6,500 tokens, so less than 12288 leaves it little room; the Pilot needs about 2,000 per step.")
    max_tokens: int = Field(1024, ge=0, le=65536, description="Longest answer in tokens. 0 = do not send a limit.")
    headers: dict[str, str] = Field(default_factory=dict, description="Extra HTTP headers sent with every request (a gateway's routing header, for example). Not for keys: those go in api_key_secret.")
    extra: dict[str, Any] = Field(default_factory=dict, description="Extra fields added to every request body, for options a particular server needs (e.g. `think: false`, `keep_alive: 10m`).")


class AssistantConfig(_Model):
    max_steps: int = Field(12, ge=1, le=100, description="Model turns the Assistant may take to answer one message. It stops and says so when the limit is reached.")
    max_tool_calls: int = Field(24, ge=1, le=500, description="Tool calls the Assistant may make to answer one message.")
    max_seconds: int = Field(600, ge=10, le=7200, description="Wall-clock limit for answering one message. Time spent waiting for the operator's Yes or No does not count.")
    max_result_chars: int = Field(6000, ge=500, le=100000, description="Longest tool result handed back to the model; longer ones are cut and marked as cut.")
    keep_conversations: int = Field(20, ge=1, le=500, description="Conversations kept (in memory and as transcripts under <home>/automation/assistant). The oldest finished ones are removed first.")


class PilotConfig(_Model):
    max_parallel: int = Field(2, ge=1, le=64, description="Androids one Pilot session drives at the same time; the rest queue. Each one asks the model once per step, so this is also how many requests the model server sees at once.")
    max_steps: int = Field(25, ge=1, le=200, description="Actions one Pilot run may take on one Android before it stops.")
    max_minutes: float = Field(10, gt=0, le=240, description="Wall-clock limit for one Pilot run on one Android.")
    max_model_calls: int = Field(60, ge=1, le=1000, description="Model requests one Pilot run on one Android may make (rejected answers count too).")
    max_no_progress: int = Field(3, ge=1, le=20, description="Actions in a row that lead to no new screen (nothing changes, or the run is back on a screen it has already seen) before the run stops.")
    max_elements: int = Field(60, ge=10, le=200, description="Screen elements described to the model per step.")
    reply: Literal["auto", "tools", "json"] = Field("auto", description="How the Pilot gets its one action per step from the model. json: one JSON object held to a schema by the server. tools: a tool call. auto: json on an `ollama` server (it enforces the schema, so even a small model cannot answer in the wrong form), tools elsewhere when the profile has `tools`, otherwise json.")
    restart_app: bool = Field(True, description="Force-stop the app before each Pilot run, so every recording starts from the app's first screen.")
    allowed_packages: list[str] = Field(default_factory=lambda: ["com.android.permissioncontroller", "com.google.android.permissioncontroller"], description="Packages a Pilot run may pass through besides the app it was started for (permission dialogs by default). Landing anywhere else ends the run.")


class AiConfig(_Model):
    policy: Literal["read-only", "ask", "allow"] = Field("ask", description="What AI assistants may do through the farm's tools. read-only: look, never change anything. ask: actions that change the farm wait for the operator's Yes or No (see ask_for). allow: every tool that is not denied runs without asking.")
    ask_for: list[Literal["change", "destructive"]] = Field(default_factory=lambda: ["change", "destructive"], description="Under policy `ask`: which kinds of action need the operator's approval. change: start, stop, install, tap, type and the like. destructive: delete, uninstall, clear data, restore, shell. Remove `change` to be asked only before destructive actions.")
    approval_timeout_seconds: int = Field(300, ge=5, le=3600, description="How long an approval request waits for the operator. After that it expires and the assistant is told that nobody answered; nothing runs.")
    auto_approve: list[str] = Field(default_factory=list, description="Tool names that never need approval under policy `ask` (e.g. `tap`, `type_text`). `sf mcp tools` lists the names.")
    deny: list[str] = Field(default_factory=lambda: ["run_shell"], description="Tool names AI assistants can never use, whatever the policy. `sf mcp tools` lists the names.")
    max_screenshot_px: int = Field(768, ge=128, le=2048, description="Longest side, in pixels, of a screenshot handed to an assistant. Larger pictures cost the model more and rarely help.")
    mcp: McpConfig = Field(default_factory=McpConfig)
    models: dict[str, ModelProfile] = Field(default_factory=dict, description="Named model profiles the Assistant and the Pilot can use: profile name -> where the model lives and what it can do. `automation` is reserved for the model set under automation.llm.")
    default_model: str = Field("", max_length=32, description="Profile used when none is named. Empty = the only profile there is.")
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    pilot: PilotConfig = Field(default_factory=PilotConfig)

    @field_validator("ask_for")
    @classmethod
    def _always_destructive(cls, v: list[str]) -> list[str]:
        # "ask" that lets a deletion through unasked would not mean what it says. One tool can
        # still be pre-approved by name (auto_approve), which is a visible, deliberate choice.
        if "destructive" not in v:
            raise ValueError("ask_for must include `destructive`; to run one destructive tool without asking, "
                             "name it in ai.auto_approve, or set ai.policy to allow")
        return [k for k in ("change", "destructive") if k in v]

    @field_validator("models")
    @classmethod
    def _profile_names(cls, v: dict[str, ModelProfile]) -> dict[str, ModelProfile]:
        import re
        for name in v:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
                raise ValueError(f"bad model profile name {name!r}: use lower-case letters, digits, - and _")
            if name == "automation":
                raise ValueError("the profile name `automation` is taken by the model set under automation.llm")
        return v

    @field_validator("auto_approve", "deny")
    @classmethod
    def _tool_names(cls, v: list[str]) -> list[str]:
        # A misspelt name in `deny` would silently leave that tool available: refuse it here.
        # Imported inside: the tool registry builds on this module.
        from .ai.tools import TOOL_NAMES
        unknown = sorted(set(v) - set(TOOL_NAMES))
        if unknown:
            raise ValueError(f"unknown tool name(s): {', '.join(unknown)} (see `sf mcp tools`)")
        return sorted(set(v))


class Config(_Model):
    version: int = Field(1, description="Config format version.")
    farm_name: str = Field("Super Farm", max_length=60, description="Name shown in the panel.")
    server: ServerConfig = Field(default_factory=ServerConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    android: AndroidConfig = Field(default_factory=AndroidConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    gpu: GpuConfig = Field(default_factory=GpuConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ai: AiConfig = Field(default_factory=AiConfig)

    @model_validator(mode="after")
    def _safety(self) -> Config:
        loopback = self.server.host in ("127.0.0.1", "::1", "localhost")
        if not loopback and not self.server.auth_required:
            raise ValueError("server.auth_required cannot be off when server.host is not a loopback address")
        if self.android.default_version not in self.android.images:
            raise ValueError(f"android.default_version {self.android.default_version!r} has no entry in android.images")
        if self.resources.memory_swap_mb and self.resources.memory_swap_mb < self.resources.memory_mb:
            raise ValueError("resources.memory_swap_mb is RAM + swap and cannot be below memory_mb")
        if self.network.mode == "gateway" and self.network.gateway not in self.network.gateways:
            raise ValueError(f"network.gateway {self.network.gateway!r} is not defined in network.gateways")
        return self


# --------------------------------------------------------------------------- load / save

def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _changed(model: BaseModel, default: BaseModel) -> dict:
    """What differs from the defaults, so config.yaml only shows what was changed.

    Only settings *sections* (nested models) are compared field by field. A free-form
    map such as android.images or network.gateways is written whole as soon as anything
    in it differs: writing just the changed field of one entry produced a file that would
    not load again, because a map in the file replaces the default map entirely.
    """
    out: dict = {}
    for name in type(model).model_fields:
        value, base = getattr(model, name), getattr(default, name)
        if isinstance(value, BaseModel) and isinstance(base, BaseModel) and type(value) is type(base):
            inner = _changed(value, base)
            if inner:
                out[name] = inner
        elif value != base:
            out[name] = model.model_dump(mode="json", include={name})[name]
    return out


def load_config(paths: Paths | None = None) -> Config:
    paths = paths or get_paths()
    data = read_yaml(paths.config, {}) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{paths.config}: expected a mapping of sections, got {type(data).__name__}")
    return Config.model_validate(data)


def save_config(config: Config, paths: Paths | None = None) -> Path:
    paths = paths or get_paths()
    changed = _changed(config, Config())
    return write_yaml(paths.config, changed, mode=0o640)


# Settings that name a program the daemon starts on the host, or code that program loads.
# Whoever may change one runs anything as the daemon's user, so they are changed where a
# person with a shell on the machine changes things (config.yaml, `sf config set`) and never
# through the HTTP API.
API_IMMUTABLE_KEYS = ("docker.binary", "gpu.nvidia.relay_dir", "gpu.nvidia.icd")


def _after_patch(patch: dict, dotted: str, current: Any, default: Any) -> Any:
    """The value of one dotted key once a merge patch is applied."""
    node: Any = patch
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return node               # a value where a section belongs: not valid, and not the current value
        if part not in node:
            return current            # the patch does not reach this key
        node = node[part]
        if node is None:
            return default            # null removes the key, or the section holding it
    return node


def check_api_patch(config: Config, patch: Any) -> None:
    """Refuse a settings patch (JSON merge patch, as PATCH /api/config takes it) that would
    change one of API_IMMUTABLE_KEYS. Raises ValueError naming the keys; a patch that repeats
    the current value passes, so a form may send back everything it shows."""
    if not isinstance(patch, dict):
        raise ValueError("a config patch is a mapping of sections to new values")
    defaults = Config()
    refused = [dotted for dotted in API_IMMUTABLE_KEYS
               if _after_patch(patch, dotted, get_value(config, dotted), get_value(defaults, dotted)) != get_value(config, dotted)]
    if refused:
        raise ValueError(f"{', '.join(refused)} cannot be changed through the API: it decides which program the "
                         "daemon runs. Change it in config.yaml or with `sf config set`, then restart the daemon")


def get_value(config: Config, dotted: str) -> Any:
    node: Any = config.model_dump(mode="json")
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def set_value(config: Config, dotted: str, value: Any) -> Config:
    """Return a new validated Config with one dotted key changed (e.g. `android.fps`, 25)."""
    data = config.model_dump(mode="json")
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            raise KeyError(dotted)
        node = node[part]
    node[parts[-1]] = value
    return Config.model_validate(data)


def update_config(config: Config, patch: dict) -> Config:
    """Return a new validated Config with a nested patch applied."""
    return Config.model_validate(_deep_merge(config.model_dump(mode="json"), patch))

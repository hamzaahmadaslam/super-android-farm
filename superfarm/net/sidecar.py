"""The network sidecar (`<prefix>-net-<id>`): image and `docker run` arguments.

Each Android shares the network namespace of one small sidecar container. The sidecar owns
everything about that namespace: the firewall (kill switch, inbound guard), the VPN client,
the routes Android's own network daemon keeps wiping, and the published ADB port. Android
and sidecar are always torn down and created together, sidecar first.
"""
from __future__ import annotations

import hashlib
import ipaddress
import threading
from pathlib import Path

from ..config import Config
from ..core.docker import Docker
from ..core.models import NET_IMAGE, Resolved
from ..core.store import Store
from ..paths import Paths
from ..resources import resource_path
from ..util.proc import CommandError
from . import killswitch, vpn

NET_CONTEXT = resource_path("deploy", "docker", "net")
CONTEXT_FILES = ("Dockerfile", "entrypoint.sh", "killswitch.sh", "routesync.sh", "healthcheck.sh")
TUN_DEVICE = "/dev/net/tun"

# Options that would let an "extra argument" run a program, load code or write files in the
# sidecar. network.vpn.extra_args is configuration, but configuration can arrive over the API.
_FORBIDDEN_OPENVPN_ARGS = frozenset("""
script-security up down route-up route-pre-down ipchange tls-verify auth-user-pass-verify
client-connect client-disconnect learn-address up-restart up-delay down-pre plugin
management management-client management-external-key management-external-cert
config cd chroot user group daemon log log-append status writepid iproute setenv setenv-safe
dev-node tmp-dir auth-user-pass askpass ca cert key pkcs12 tls-auth tls-crypt tls-crypt-v2 dh
secret crl-verify extra-certs http-proxy socks-proxy redirect-gateway route-noexec ifconfig-noexec
dns-updown disable-dco
""".split())

_build_lock = threading.Lock()
_select_lock = threading.Lock()
_reservations = vpn.Reservations()


# --------------------------------------------------------------------------- image

def context_hash(context: Path = NET_CONTEXT) -> str:
    """Digest of the image's build inputs. It is part of the image tag, so editing a script
    yields a new tag, the start plan changes, and sidecars are recreated with the new code
    instead of quietly keeping the old one."""
    h = hashlib.sha256()
    for name in CONTEXT_FILES:
        h.update(name.encode() + b"\0")
        h.update((context / name).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def image_ref(context: Path = NET_CONTEXT) -> str:
    return f"{NET_IMAGE}:{context_hash(context)[:12]}"


def ensure_image(docker: Docker, *, rebuild: bool = False) -> str:
    """Return the sidecar image for this checkout, building it when missing (once, ~1 minute).

    Building needs internet access for the Alpine packages; running never does.
    """
    missing = [n for n in CONTEXT_FILES if not (NET_CONTEXT / n).is_file()]
    if missing:
        raise RuntimeError(f"the sidecar image sources are incomplete ({', '.join(missing)} missing in {NET_CONTEXT}); "
                           "reinstall Super Farm from a complete checkout")
    digest = context_hash()
    ref = f"{NET_IMAGE}:{digest[:12]}"
    with _build_lock:                       # a bulk start must not launch the same build N times
        if not rebuild and docker.image_exists(ref):
            return ref
        try:
            docker.build(ref, str(NET_CONTEXT), build_args={"SF_CONTEXT_HASH": digest}, timeout=900)
        except CommandError as e:
            tail = " | ".join((e.stderr or e.stdout or "").strip().splitlines()[-3:])
            raise RuntimeError(
                f"building the network sidecar image {ref} failed. It downloads Alpine packages, so the host "
                f"needs internet access for this one step; then start again. Docker said: {tail[:400]}"
            ) from e
        docker.run(["tag", ref, f"{NET_IMAGE}:latest"], timeout=30, check=False)
    return ref


# --------------------------------------------------------------------------- argument helpers

def bind_mount(source: Path, target: str) -> list[str]:
    """Read-only bind mount of one file. `--mount` is used because, unlike `-v`, it never
    creates a missing source as an empty directory owned by root."""
    text = str(source)
    if any(c in text for c in ',"\n\r'):
        raise ValueError(f"cannot mount {text!r}: Docker's mount syntax does not allow commas or quotes in a path; "
                         "move the farm home (SUPERFARM_HOME) to a path without them")
    return ["--mount", f"type=bind,source={text},target={target},readonly"]


def env_args(env: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key in sorted(env):
        value = str(env[key])
        if "\n" in value or "\x00" in value:
            raise ValueError(f"{key} contains a line break")
        out += ["-e", f"{key}={value}"]
    return out


def base_args(*, name: str, labels: dict[str, str], config: Config, restart: str, tunnel: bool,
              sysctls: dict[str, str] | None = None) -> list[str]:
    """Arguments every sidecar-image container shares (Android sidecars and gateways)."""
    args = ["--name", name]
    for key in sorted(labels):
        args += ["--label", f"{key}={labels[key]}"]
    # Capabilities: NET_ADMIN for firewall, routes and tunnel devices; NET_RAW for ping and
    # interface-bound probes. Everything else is dropped. DAC_READ_SEARCH is added only when
    # a profile is mounted: those files are 0600 and owned by the farm's user, not by root.
    args += ["--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW"]
    if tunnel:
        args += ["--cap-add", "DAC_READ_SEARCH"]
    args += ["--security-opt", "no-new-privileges"]
    # State (status, pins, VPN log) lives in RAM: nothing piles up in the container layer.
    args += ["--tmpfs", "/run/sf:rw,nosuid,nodev,noexec,size=4m,mode=0700"]
    for key in sorted(sysctls or {}):
        args += ["--sysctl", f"{key}={sysctls[key]}"]
    # Memory is capped (a sidecar needs ~10 MB); CPU is not, because the VPN's encryption
    # runs here and throttling it would throttle the Android's whole connection.
    args += ["--memory", "256m", "--memory-swap", "256m", "--pids-limit", "256"]
    args += ["--restart", restart, "--stop-timeout", "5"]
    args += ["--log-driver", "json-file", "--log-opt", f"max-size={config.docker.log_max_size}",
             "--log-opt", f"max-file={config.docker.log_max_files}"]
    return args


def check_extra_openvpn_args(extra: list[str]) -> list[str]:
    for arg in extra:
        if not isinstance(arg, str) or "\n" in arg or "\x00" in arg:
            raise ValueError("network.vpn.extra_args: arguments must be single-line strings")
        if arg.startswith("-") and arg.lstrip("-") in _FORBIDDEN_OPENVPN_ARGS:
            raise ValueError(f"network.vpn.extra_args: `{arg}` is not allowed; it could run programs or read files "
                             "inside the sidecar, or undo what the farm sets up")
    if extra and not extra[0].startswith("--"):
        raise ValueError("network.vpn.extra_args must start with an option (`--name`)")
    return list(extra)


def tunnel_parts(config: Config, paths: Paths, profile_name: str, runtime_dir: Path, *, credentials: str,
                 ipv6: bool) -> tuple[list[str], dict[str, str], dict[str, str], list[str], vpn.Profile]:
    """Everything a VPN client container needs for one profile:
    (docker args, env, sysctls, command-line arguments after the image, the profile)."""
    target = "/sf/vpn/profile.ovpn" if vpn.provider_of(profile_name) == "openvpn" else "/sf/vpn/profile.conf"
    profile = vpn.write_runtime_copy(paths, profile_name, runtime_dir / target.rsplit("/", 1)[1])
    args = bind_mount(runtime_dir / target.rsplit("/", 1)[1], target)
    env = {"SF_VPN_PROVIDER": profile.provider, "SF_PROFILE_NAME": profile_name}
    sysctls = {} if ipv6 else {"net.ipv6.conf.all.disable_ipv6": "1"}
    command: list[str] = []
    if profile.provider == "openvpn":
        if not Path(TUN_DEVICE).exists():
            raise ValueError(f"{TUN_DEVICE} does not exist on this host. Load the tunnel module (`sudo modprobe tun`) "
                             "or run `sf host apply`, then start again")
        args += ["--device", TUN_DEVICE]
        if profile.needs_credentials:
            if not vpn.has_credentials(paths, credentials):
                raise ValueError(f"VPN profile {profile_name!r} asks for a username and password, and none is stored "
                                 f"under the name {credentials!r}. Store them with `sf vpn credentials {credentials}`")
            args += bind_mount(vpn.credentials_path(paths, credentials), "/sf/secrets/auth")
        v = config.network.vpn
        env.update(SF_VPN_PING=str(v.ping_seconds), SF_VPN_PING_RESTART=str(v.ping_restart_seconds),
                   SF_VPN_CONNECT_RETRY_MAX=str(v.connect_retry_max))
        command = check_extra_openvpn_args(v.extra_args)
    else:
        # WireGuard marks its own packets so they leave by the uplink; the kernel only
        # honours that mark in reverse-path checks with this switch on.
        sysctls["net.ipv4.conf.all.src_valid_mark"] = "1"
    return args, env, sysctls, command, profile


# --------------------------------------------------------------------------- profile choice

def select_profile(r: Resolved, config: Config, paths: Paths, store: Store, docker: Docker) -> str:
    """The VPN profile this Android starts with: its pinned one, or one picked by
    network.vpn.selection. The pick is remembered in the instance state, so an Android keeps
    its exit address across restarts until the operator changes it."""
    with _select_lock:
        if r.vpn_profile:
            name = r.vpn_profile
            if name not in vpn.profile_names(paths):
                raise ValueError(f"instance {r.id}: pinned VPN profile {name!r} does not exist; see `sf vpn profiles`")
        else:
            v = config.network.vpn
            assigned = {}
            for other in store.ids():
                if other != r.id:
                    held = store.state_get(other, "vpn_profile")
                    if held:
                        assigned[other] = str(held)
            in_use: set[str] = set()
            if v.selection == "unique":
                running = set()
                for c in docker.ps(all=False, labels={"io.superfarm.farm": r.names.prefix, "io.superfarm.role": "net"}):
                    ident = c["labels"].get("io.superfarm.instance", "")
                    if ident.isdigit():
                        running.add(int(ident))
                in_use = {p for i, p in assigned.items() if i in running} | _reservations.held_by_others(r.id)
            name = vpn.choose_profile(
                v.selection, vpn.profile_names(paths, v.provider), fixed=v.profile,
                current=str(store.state_get(r.id, "vpn_profile", "") or ""), assigned=assigned, in_use=in_use)
        _reservations.hold(r.id, name)
        if store.state_get(r.id, "vpn_profile") != name:
            store.state_set(r.id, "vpn_profile", name)
    return name


# --------------------------------------------------------------------------- the sidecar

def publish_arg(bind: str, host_port: int) -> str:
    address = "127.0.0.1" if bind == "localhost" else bind
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError(f"network.adb_bind {bind!r} is not an IP address") from None
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    return f"{host}:{int(host_port)}:{killswitch.ADB_PORT}/tcp"


def sidecar_run_args(r: Resolved, config: Config, paths: Paths, store: Store, docker: Docker) -> list[str]:
    """Complete arguments after `docker run -d` for `<prefix>-net-<id>`.

    Side effects, all idempotent: builds the sidecar image when missing; for `vpn`, chooses
    the profile, records it (`vpn_profile` in the instance state) and writes the sanitised
    copy the container mounts; for `gateway`, brings the shared gateway up.
    Raises ValueError with the fix in the message when the instance cannot be started as
    configured (no profile, missing credentials, ...).
    """
    image = ensure_image(docker)
    # `proxy` is a direct connection as far as the sidecar is concerned; the proxy itself
    # is an Android setting applied after boot.
    mode = "direct" if r.net_mode == "proxy" else r.net_mode
    env = {"SF_MODE": mode, **killswitch.firewall_env(r, config)}
    labels = {**r.names.labels("net"), "io.superfarm.net.mode": r.net_mode}
    sysctls = {} if r.ipv6 else {"net.ipv6.conf.all.disable_ipv6": "1"}
    extra: list[str] = []
    command: list[str] = []
    tunnel = False

    if mode == "vpn":
        name = select_profile(r, config, paths, store, docker)
        t_args, t_env, t_sysctls, command, _ = tunnel_parts(
            config, paths, name, paths.instance(r.id) / "net", credentials=config.network.vpn.credentials, ipv6=r.ipv6)
        extra += t_args
        env.update(t_env)
        sysctls.update(t_sysctls)
        labels["io.superfarm.net.profile"] = name
        tunnel = True
    elif mode == "gateway":
        from . import gateway                       # gateway builds on this module
        member = gateway.member(docker, config, paths, r.gateway)
        # Two networks: the farm's uplink only carries the published ADB port; the gateway
        # network is the only way out. The sidecar tells them apart by these two addresses,
        # because Docker's choice of interface names and default route is not predictable.
        extra += ["--network", member["uplink"], "--network", member["network"]]
        env["SF_GATEWAY_IP"] = member["gateway_ip"]
        env["SF_UPLINK_GW"] = member["uplink_gw"]
        labels["io.superfarm.net.gateway"] = r.gateway

    args = base_args(name=r.names.net, labels=labels, config=config, restart=r.restart_policy, tunnel=tunnel,
                     sysctls=sysctls)
    args += extra
    args += ["-p", publish_arg(r.adb_bind, r.adb_port)]
    args += env_args(env)
    args.append(image)
    args += command
    return args

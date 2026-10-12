"""Shared VPN gateways: one tunnel, many Androids.

    <prefix>-gw-<name>      the sidecar image in `gwserver` mode: VPN client + NAT. Forwards
                            members into the tunnel and nowhere else.
    <prefix>-gwnet-<name>   a bridge network that only connects members to their gateway. It
                            has no NAT and no address on the host, so the host is not a router
                            on it: the gateway container is the only way anywhere.
    <prefix>-bridge         the farm's "uplink": one ordinary bridge network. It carries the
                            gateways' VPN transport and the members' published ADB ports.

A member's sidecar sits on two networks: the uplink (its firewall lets only ADB answers out
there) and the gateway network (its firewall lets everything out there). Three things
together keep a member behind the tunnel: its own firewall, the gateway network having no
other router, and the gateway forwarding only into its tunnel interface.

Two Docker facts shaped this:
  * its built-in `bridge` network cannot be combined with another network on `docker run`,
    hence the farm's own uplink network;
  * an `--internal` network cannot be used for the gateway network: Docker then drops every
    packet on it whose destination is outside the subnet, including what a member sends
    to the gateway for forwarding.

The gateway always has host address .2 of its /24; members get addresses from the upper
half, so a member can never be handed the gateway's address while the gateway is down.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
from collections.abc import Iterable

from ..config import Config
from ..core.docker import Docker
from ..core.models import Names
from ..paths import Paths
from . import sidecar, vpn
# Not `from . import status`: the package re-exports the function status(), which hides the module.
from .status import interpret, read_report

MIN_DOCKER_MAJOR = 25          # first release that accepts several --network options on `docker run`
_OPT_MASQUERADE = "com.docker.network.bridge.enable_ip_masquerade"
_OPT_NO_HOST_IP = "com.docker.network.bridge.inhibit_ipv4"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_lock = threading.Lock()       # a bulk start brings many members up at once; one gateway, one creator


def check_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ValueError(f"bad gateway name {name!r}: use a-z, 0-9 and - (max 31), starting with a letter or digit")
    return name


# --------------------------------------------------------------------------- addresses (pure)

def allocate_subnet(pool: str, used: Iterable[str], prefixlen: int = 24) -> str:
    """First /24 of `pool` that overlaps none of the networks in `used`."""
    try:
        net = ipaddress.ip_network(pool, strict=False)
    except ValueError:
        raise ValueError(f"docker.subnet_pool {pool!r} is not a network (expected e.g. 10.213.0.0/16)") from None
    if net.version != 4 or net.prefixlen > prefixlen:
        raise ValueError(f"docker.subnet_pool {pool!r} must be an IPv4 network of /{prefixlen} or larger")
    taken = []
    for u in used:
        try:
            n = ipaddress.ip_network(u.strip(), strict=False)
        except ValueError:
            continue                  # Docker lists IPv6 and odd entries too; they cannot collide
        if n.version == 4:
            taken.append(n)
    for candidate in net.subnets(new_prefix=prefixlen):
        if not any(candidate.overlaps(t) for t in taken):
            return str(candidate)
    raise ValueError(f"docker.subnet_pool {pool} has no free /{prefixlen} left; enlarge it or remove unused gateways")


def check_subnet(subnet: str) -> str:
    try:
        net = ipaddress.ip_network(subnet, strict=True)
    except ValueError:
        raise ValueError(f"gateway subnet {subnet!r} is not a network address (expected e.g. 10.213.7.0/24)") from None
    if net.version != 4 or net.prefixlen != 24 or not net.is_private:
        raise ValueError(f"gateway subnet {subnet!r} must be a private IPv4 /24")
    return str(net)


def addresses(subnet: str) -> dict[str, str]:
    net = ipaddress.ip_network(check_subnet(subnet))
    base = int(net.network_address)
    return {
        "subnet": str(net),
        "bridge": str(ipaddress.ip_address(base + 1)),        # reserved; nothing answers there
        "gateway_ip": str(ipaddress.ip_address(base + 2)),
        "member_range": f"{ipaddress.ip_address(base + 128)}/25",
    }


# --------------------------------------------------------------------------- planning

def _config(config: Config, name: str):
    check_name(name)
    if name not in config.network.gateways:
        raise ValueError(f"gateway {name!r} is not defined; add it under network.gateways in the configuration")
    return config.network.gateways[name]


def uplink_network(prefix: str) -> str:
    # The name is load-bearing. Of a container's networks, Docker gives the default route and
    # the published ports to the one whose name sorts first. "<prefix>-bridge" sorts before
    # every "<prefix>-gwnet-...", so ADB is always published through the uplink.
    return f"{prefix}-bridge"


def _labels(prefix: str, name: str, role: str) -> dict[str, str]:
    return {"io.superfarm.farm": prefix, "io.superfarm.role": role, "io.superfarm.gateway": name}


def run_args(config: Config, paths: Paths, name: str, *, subnet: str, image: str, uplink_gw: str) -> list[str]:
    """Arguments after `docker run -d` for the gateway container (without the plan label).
    uplink_gw is the bridge address of the farm's uplink network."""
    cfg = _config(config, name)
    prefix = config.docker.name_prefix
    addr = addresses(subnet)
    if vpn.provider_of(cfg.profile) != cfg.provider:
        raise ValueError(f"gateway {name!r}: profile {cfg.profile!r} is not a {cfg.provider} profile")
    t_args, t_env, t_sysctls, command, _ = sidecar.tunnel_parts(
        config, paths, cfg.profile, paths.home / "gateways" / name, credentials=cfg.credentials, ipv6=False)
    env = {
        "SF_MODE": "gwserver", "SF_GATEWAY_IP": addr["gateway_ip"], "SF_UPLINK_GW": _ipv4(uplink_gw),
        "SF_KILLSWITCH": "1", "SF_ALLOW_LAN": "0", "SF_IPV6": "0",
        "SF_DNS": ",".join(d for d in config.network.dns if _is_v4(d)),
        "SF_DNS_LEAK_PROTECTION": "1" if config.network.dns_leak_protection else "0",
        **t_env,
    }
    if not env["SF_DNS"]:
        raise ValueError("network.dns needs at least one IPv4 resolver for a VPN gateway")
    # Forwarding is a per-namespace switch; it has to be given at creation because /proc/sys
    # is read-only inside an unprivileged container.
    sysctls = {"net.ipv4.ip_forward": "1", **t_sysctls}
    args = sidecar.base_args(name=Names.gateway(prefix, name), labels=_labels(prefix, name, "gateway"), config=config,
                             restart=config.performance.restart_policy, tunnel=True, sysctls=sysctls)
    # The uplink carries the VPN transport; the gateway network carries the members.
    args += ["--network", uplink_network(prefix),
             "--network", f"name={Names.gateway_network(prefix, name)},ip={addr['gateway_ip']}"]
    args += t_args
    args += sidecar.env_args(env)
    args.append(image)
    args += command
    return args


def _ipv4(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise RuntimeError(f"the uplink network has no usable IPv4 bridge address ({value!r})") from None


def _is_v4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def _plan(args: list[str], profile_text: str) -> str:
    h = hashlib.sha256("\0".join(args).encode())
    h.update(hashlib.sha256(profile_text.encode()).digest())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- Docker side

def _require_docker(docker: Docker) -> None:
    ok, version = docker.available()
    if not ok:
        raise RuntimeError(f"Docker is not reachable: {version}")
    m = re.match(r"(\d+)\.", version)
    if m and int(m.group(1)) < MIN_DOCKER_MAJOR:
        raise RuntimeError(f"shared VPN gateways need Docker {MIN_DOCKER_MAJOR} or newer (found {version}): "
                           "older releases cannot attach a container to two networks at start")


def _network_info(docker: Docker, network: str) -> dict | None:
    r = docker.run(["network", "inspect", network], timeout=30, check=False)
    if not r.ok:
        return None
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return data[0] if data else None


def uplink_gateway(docker: Docker, config: Config) -> str:
    """Bridge (host-side) address of the uplink network: where published ADB connections come from."""
    info = _network_info(docker, uplink_network(config.docker.name_prefix)) or {}
    for entry in (info.get("IPAM") or {}).get("Config") or []:
        try:
            if ipaddress.ip_network(entry.get("Subnet", ""), strict=False).version == 4:
                return _ipv4(entry.get("Gateway", ""))
        except ValueError:
            continue
    raise RuntimeError(f"Docker network {uplink_network(config.docker.name_prefix)} has no IPv4 subnet")


def ensure_uplink(docker: Docker, config: Config) -> str:
    """Create the farm's uplink network if it is missing; return its name."""
    prefix = config.docker.name_prefix
    network = uplink_network(prefix)
    info = _network_info(docker, network)
    if info is None:
        argv = ["network", "create", "--driver", "bridge",
                "--label", f"io.superfarm.farm={prefix}", "--label", "io.superfarm.role=uplink"]
        docker.run([*argv, network], timeout=60)
    elif (info.get("Labels") or {}).get("io.superfarm.farm") != prefix:
        raise RuntimeError(f"a Docker network named {network} exists but was not created by this farm; "
                           "remove it or change docker.name_prefix")
    elif info.get("Internal"):
        raise RuntimeError(f"Docker network {network} is internal and cannot carry VPN traffic; remove it "
                           f"(`docker network rm {network}`) and start again")
    return network


def _ensure_network(docker: Docker, config: Config, name: str) -> str:
    """Create the gateway network if needed; return its subnet."""
    prefix = config.docker.name_prefix
    network = Names.gateway_network(prefix, name)
    info = _network_info(docker, network)
    if info is not None:
        labels = info.get("Labels") or {}
        if labels.get("io.superfarm.farm") != prefix:
            raise RuntimeError(f"a Docker network named {network} exists but was not created by this farm; "
                               "remove it or choose another gateway name")
        ipam = (info.get("IPAM") or {}).get("Config") or []
        options = info.get("Options") or {}
        if not ipam or info.get("Internal") or options.get(_OPT_MASQUERADE) != "false":
            raise RuntimeError(f"Docker network {network} was not set up as a gateway network (fixed subnet, no NAT); "
                               f"remove it (`docker network rm {network}`) and start again")
        return check_subnet(ipam[0]["Subnet"])
    wanted = config.network.gateways[name].subnet
    subnet = check_subnet(wanted) if wanted else allocate_subnet(config.docker.subnet_pool, docker.network_subnets())
    addr = addresses(subnet)
    # No NAT and no host address: the host cannot route for this network, so a member whose
    # routes went wrong reaches nothing instead of the internet. (Not `--internal`; see the
    # module text.) The option names are Docker's own.
    argv = ["network", "create", "--driver", "bridge", "--subnet", addr["subnet"],
            "--gateway", addr["bridge"], "--ip-range", addr["member_range"],
            "-o", f"{_OPT_MASQUERADE}=false", "-o", f"{_OPT_NO_HOST_IP}=true"]
    for k, v in _labels(prefix, name, "gateway-network").items():
        argv += ["--label", f"{k}={v}"]
    docker.run([*argv, network], timeout=60)
    return subnet


def ensure(docker: Docker, config: Config, paths: Paths, name: str) -> dict:
    """Make sure the gateway's network exists and its container runs with the current
    configuration. Idempotent and cheap when nothing changed.

    Returns {"name", "container", "network", "uplink", "uplink_gw", "subnet", "gateway_ip",
    "action"} where action is one of created | recreated | started | unchanged.
    """
    cfg = _config(config, name)
    prefix = config.docker.name_prefix
    container = Names.gateway(prefix, name)
    with _lock:
        _require_docker(docker)
        ensure_uplink(docker, config)
        subnet = _ensure_network(docker, config, name)
        uplink_gw = uplink_gateway(docker, config)
        image = sidecar.ensure_image(docker)
        args = run_args(config, paths, name, subnet=subnet, image=image, uplink_gw=uplink_gw)
        plan = _plan(args, vpn.load_profile(paths, cfg.profile).text)
        action = "unchanged"
        if docker.exists(container):
            if docker.label(container, "io.superfarm.farm") != prefix:
                raise RuntimeError(f"a container named {container} exists but was not created by this farm")
            if docker.label(container, "io.superfarm.plan") != plan:
                # Members keep their route to .2 and recover by themselves once it answers again.
                docker.rm(container)
                docker.create_and_start(["--label", f"io.superfarm.plan={plan}", *args])
                action = "recreated"
            elif not docker.running(container):
                docker.start(container)
                action = "started"
        else:
            docker.create_and_start(["--label", f"io.superfarm.plan={plan}", *args])
            action = "created"
    return {"name": name, "container": container, "network": Names.gateway_network(prefix, name),
            "uplink": uplink_network(prefix), "uplink_gw": uplink_gw, "subnet": subnet,
            "gateway_ip": addresses(subnet)["gateway_ip"], "action": action}


def member(docker: Docker, config: Config, paths: Paths, name: str) -> dict:
    """What a member sidecar needs to join: brings the gateway up and returns
    {"network", "uplink", "uplink_gw", "gateway_ip", "subnet"}."""
    info = ensure(docker, config, paths, name)
    return {k: info[k] for k in ("network", "uplink", "uplink_gw", "gateway_ip", "subnet")}


def members(docker: Docker, config: Config, name: str) -> list[str]:
    """Names of the containers attached to the gateway network, without the gateway itself."""
    prefix = config.docker.name_prefix
    info = _network_info(docker, Names.gateway_network(prefix, check_name(name)))
    if info is None:
        return []
    names = [c.get("Name", "") for c in (info.get("Containers") or {}).values()]
    return sorted(n for n in names if n and n != Names.gateway(prefix, name))


def remove(docker: Docker, config: Config, name: str, *, force: bool = False) -> dict:
    """Remove a gateway's container and network. Refuses while Androids are attached unless
    force is set (they would be left without any connection)."""
    prefix = config.docker.name_prefix
    container, network = Names.gateway(prefix, check_name(name)), Names.gateway_network(prefix, name)
    attached = members(docker, config, name)
    if attached and not force:
        raise ValueError(f"gateway {name!r} still serves {len(attached)} Android(s) ({', '.join(attached[:5])}); "
                         "stop them first or pass force")
    removed = []
    with _lock:
        if docker.exists(container):
            if docker.label(container, "io.superfarm.farm") != prefix:
                raise RuntimeError(f"container {container} was not created by this farm; not touching it")
            docker.rm(container)
            removed.append(container)
        info = _network_info(docker, network)
        if info is not None:
            if (info.get("Labels") or {}).get("io.superfarm.farm") != prefix:
                raise RuntimeError(f"network {network} was not created by this farm; not touching it")
            if docker.network_rm(network):
                removed.append(network)
        # The uplink goes with the last thing that used it. Docker refuses to remove a network
        # that still has containers, which is exactly the check wanted here.
        up = _network_info(docker, uplink_network(prefix))
        if up is not None and (up.get("Labels") or {}).get("io.superfarm.farm") == prefix and not up.get("Containers"):
            if docker.network_rm(uplink_network(prefix)):
                removed.append(uplink_network(prefix))
    return {"name": name, "removed": removed}


def status(docker: Docker, config: Config, name: str) -> dict:
    """Tunnel state of a gateway plus who is attached. Never raises for a gateway that is down."""
    prefix = config.docker.name_prefix
    container, network = Names.gateway(prefix, check_name(name)), Names.gateway_network(prefix, name)
    out: dict = {"name": name, "container": container, "network": network, "defined": name in config.network.gateways,
                 "running": False, "state": "missing", "ready": False, "subnet": "", "gateway_ip": "",
                 "members": members(docker, config, name), "detail": "", "fix": ""}
    info = _network_info(docker, network)
    if info is not None:
        ipam = (info.get("IPAM") or {}).get("Config") or []
        if ipam:
            out["subnet"] = ipam[0].get("Subnet", "")
            try:
                out["gateway_ip"] = addresses(out["subnet"])["gateway_ip"]
            except ValueError:
                pass
    report = read_report(docker, container)
    if report is None:
        out["state"] = "stopped" if docker.exists(container) else "missing"
        return out
    out.update(interpret(report, mode="gwserver", killswitch_wanted=True))
    out["running"] = True
    return out

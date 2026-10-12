"""The kill switch, seen from the host.

The rules themselves are loaded inside the sidecar (deploy/docker/net/killswitch.sh) before
anything else runs there. This module decides the firewall settings a sidecar is started
with, and checks afterwards that the live rules really have the property we rely on:

    nothing leaves through the uplink interface except the VPN transport to pinned
    server addresses and the answers of an ADB session.

Checking matters as much as setting. The failure this guards against is not a wrong rule
but an absent rule set that everyone assumes is there.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from ..config import Config
from ..core.docker import Docker
from ..core.models import Resolved
from ..util.proc import CommandError

ADB_PORT = 5555
# Interfaces that are the protected path itself, or not a path out at all.
_TUNNEL_PREFIXES = ("lo", "tun", "wg")


@dataclass
class Report:
    active: bool                  # the kill switch is wanted AND verifiably in place
    problems: list[str] = field(default_factory=list)
    guarded: bool = False         # inbound/forward default-deny is in place (every mode)


def firewall_env(r: Resolved, config: Config) -> dict[str, str]:
    """Environment that configures the sidecar's firewall for one Android."""
    resolvers = []
    for d in r.dns:
        try:
            if ipaddress.ip_address(d).version == 4:
                resolvers.append(str(ipaddress.ip_address(d)))
        except ValueError:
            raise ValueError(f"network.dns: {d!r} is not an IP address") from None
    if not resolvers and r.net_mode in ("vpn", "gateway"):
        # The sidecar resolves VPN server names over IPv4 before the tunnel exists.
        raise ValueError("network.dns needs at least one IPv4 resolver when a VPN is used")
    return {
        "SF_KILLSWITCH": "1" if r.killswitch else "0",
        "SF_ALLOW_LAN": "1" if r.allow_lan else "0",
        "SF_DNS": ",".join(resolvers),
        "SF_DNS_LEAK_PROTECTION": "1" if config.network.dns_leak_protection else "0",
        "SF_IPV6": "1" if r.ipv6 else "0",
        "SF_ADB_PORT": str(ADB_PORT),
        # ADB has no authentication. Published on a loopback address, only the host itself
        # can be the peer, so the sidecar refuses everyone else on its bridge. Published
        # wider on purpose, the operator has asked for remote ADB and gets it.
        "SF_ADB_FROM": "gateway" if is_loopback(r.adb_bind) else "any",
    }


def is_loopback(address: str) -> bool:
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def analyse(rules4: str, rules6: str, *, mode: str, enforced: bool, uplink: str = "eth0",
            ipv6_present: bool = False) -> Report:
    """Judge `iptables -S` / `ip6tables -S` output. Pure.

    enforced    a kill switch is wanted (or the mode is `none`)
    uplink      the interface that leads to the Docker bridge, i.e. past the tunnel
    """
    problems: list[str] = []
    lines4 = [ln.strip() for ln in rules4.splitlines() if ln.strip()]
    if not any(ln.startswith("-P ") for ln in lines4):
        return Report(False, ["the IPv4 firewall rules could not be read"])
    for chain in ("INPUT", "FORWARD", "OUTPUT"):
        if f"-P {chain} DROP" not in lines4:
            problems.append(f"IPv4 {chain} is not default-deny")
    guarded = not problems
    if mode == "direct" or not enforced:
        # Direct: no tunnel, nothing to leak around. Not enforced: switched off on purpose.
        # Either way the inbound guard is all there is to verify.
        return Report(False, problems, guarded)
    for ln in lines4:
        if not ln.startswith("-A OUTPUT ") or not ln.endswith("-j ACCEPT"):
            continue
        words = ln.split()
        out_if = words[words.index("-o") + 1] if "-o" in words else ""
        narrowed = any(w in words for w in ("-d", "-p", "-m"))
        if not out_if and not narrowed:
            problems.append("an IPv4 rule accepts all outgoing traffic")
        elif out_if == uplink and not narrowed:
            problems.append(f"an IPv4 rule accepts all traffic leaving through {uplink}")
    lines6 = [ln.strip() for ln in rules6.splitlines() if ln.strip()]
    if "-P OUTPUT DROP" not in lines6:
        # Without the rules, IPv6 must be absent altogether, or it is a way around the tunnel.
        if ipv6_present:
            problems.append("IPv6 is active but its OUTPUT is not default-deny")
    else:
        for ln in lines6:
            words = ln.split()
            if ln.startswith("-A OUTPUT ") and ln.endswith("-j ACCEPT"):
                out_if = words[words.index("-o") + 1] if "-o" in words else ""
                if not out_if.startswith(_TUNNEL_PREFIXES):
                    problems.append("an IPv6 rule accepts traffic outside the tunnel")
    return Report(not problems, problems, guarded)


def probe(docker: Docker, container: str, target: str, *, uplink: str = "eth0", timeout: float = 4) -> dict:
    """Active leak test: try to fetch `target` with the socket bound to the uplink interface,
    which is exactly the path a leak would take. {"leak": bool, "detail": str}.

    target must be an http(s) URL of something that would answer if the path were open.
    """
    if not target.startswith(("http://", "https://")) or any(c.isspace() for c in target):
        raise ValueError("leak probe target must be an http(s) URL")
    try:
        res = docker.exec(container, ["curl", "-sS", "-o", "/dev/null", "--interface", uplink,
                                      "--max-time", str(int(timeout)), "--", target],
                          timeout=timeout + 5, check=False)
    except CommandError as e:
        return {"leak": False, "detail": f"probe did not run: {e}"}
    if res.ok:
        return {"leak": True, "detail": f"{target} answered through {uplink}"}
    return {"leak": False, "detail": (res.stderr or "blocked").strip().splitlines()[-1][:200]}

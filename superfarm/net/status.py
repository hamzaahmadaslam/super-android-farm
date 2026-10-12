"""What a sidecar is doing: read from the container, interpreted here.

The sidecar keeps a small status file and (for OpenVPN) the client's log. One `docker exec`
fetches both together with the live firewall rules; everything after that is pure parsing,
which is what the unit tests cover.
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
from dataclasses import dataclass

from ..core.docker import Docker
from ..core.models import Names, Resolved
from ..util.proc import CommandError
from . import killswitch

REPORT_CMD = ["/sf/healthcheck.sh", "report"]

# States a tunnel can be in, as shown to the operator.
CONNECTED = "connected"
PROBLEM_STATES = ("auth_failed", "tls_error", "dns_error", "unreachable", "config_error", "tun_error")

_FIXES = {
    "auth_failed": "The VPN server rejected the username or password. Store the right ones with "
                   "`sf vpn credentials <name>` and restart the Android.",
    "tls_error": "The secure handshake with the VPN server failed (wrong or expired certificate/key in the "
                 "profile, or something on the path blocks it). Try another profile or re-download this one.",
    "dns_error": "The VPN server's name could not be resolved. Check network.dns and the host's connection.",
    "unreachable": "No VPN server in the profile answered. Check the host's internet connection, or try "
                   "another profile; the provider may have retired this server.",
    "config_error": "OpenVPN refused an option in the profile.",
    "tun_error": "The tunnel device could not be opened. On the host: `sudo modprobe tun` and make sure "
                 "/dev/net/tun exists.",
}

# Ordered: for one log line, the first match wins. (pattern, state, keeps the line as detail)
_MARKERS: tuple[tuple[re.Pattern, str], ...] = tuple((re.compile(p), s) for p, s in (
    (r"Initialization Sequence Completed", CONNECTED),
    (r"AUTH_FAILED|auth-failure", "auth_failed"),
    (r"Cannot ioctl TUNSETIFF|Cannot open TUN/TAP dev|Cannot allocate TUN/TAP dev", "tun_error"),
    (r"Options error:|Unrecognized option or missing", "config_error"),
    (r"RESOLVE: Cannot resolve host address|Temporary failure in name resolution|Name does not resolve", "dns_error"),
    (r"VERIFY (KU |EKU |X509NAME )?ERROR|certificate verify failed|TLS key negotiation failed|TLS handshake failed|TLS Error", "tls_error"),
    (r"All connections have been connect-retry-max", "unreachable"),
    (r"Inactivity timeout|SIGUSR1\[|Restart pause|Connection reset, restarting|\[ECONNREFUSED\]|\[EHOSTUNREACH\]|\[ENETUNREACH\]", "restarting"),
    (r"SIGTERM\[|process exiting|Exiting due to fatal error", "exited"),
))
_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d|\w{3} \w{3} [ \d]\d \d\d:\d\d:\d\d \d{4}) ")


@dataclass(frozen=True)
class LogState:
    state: str            # unknown | connecting | connected | reconnecting | exited | one of PROBLEM_STATES
    detail: str           # the log line that decided it (no timestamp), "" when connected
    fix: str              # what the operator should do, "" when there is nothing to do
    was_connected: bool   # the tunnel was up at some point in this log


def parse_openvpn_log(text: str) -> LogState:
    """Classify an OpenVPN client log. The log is read front to back and the last decisive
    line wins, so a tunnel that reconnected after an error reads as connected again."""
    state, detail, was_connected, seen = "unknown", "", False, False
    for raw in text.splitlines():
        line = _STAMP.sub("", raw.strip())
        if not line or line.startswith("[sf-net]"):
            continue
        seen = True
        for pattern, kind in _MARKERS:
            if not pattern.search(line):
                continue
            if kind == CONNECTED:
                state, detail, was_connected = CONNECTED, "", True
            elif kind == "restarting":
                # A restart line that follows a real cause (TLS, DNS, ...) must not hide it.
                if state in (CONNECTED, "unknown", "connecting", "reconnecting"):
                    state, detail = ("reconnecting" if was_connected else "connecting"), line
            elif kind == "exited":
                if state in (CONNECTED, "unknown", "connecting", "reconnecting"):
                    state, detail = "exited", line
            elif state != kind:
                # Keep the first line of a failure: it names the cause, the rest is fallout.
                state, detail = kind, line
            break
    if state == "unknown" and seen:
        state = "connecting"
    return LogState(state=state, detail=detail[:300], fix=_FIXES.get(state, ""), was_connected=was_connected)


def parse_status_file(text: str) -> dict[str, str]:
    """`key=value` lines written by the sidecar. Values are plain text; nothing is evaluated."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[a-z_0-9]+", key):
            out[key] = value.strip()
    return out


def parse_report(text: str) -> dict[str, str]:
    """Split the output of `healthcheck.sh report` into its `== name` sections."""
    sections: dict[str, list[str]] = {}
    name = ""
    for line in text.splitlines():
        if line.startswith("== "):
            name = line[3:].strip()
            sections[name] = []
        elif name:
            sections[name].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def interpret(report: dict[str, str], *, mode: str, killswitch_wanted: bool) -> dict:
    """Turn a parsed report into the status dict the API returns. Pure."""
    st = parse_status_file(report.get("status", ""))
    check = parse_status_file(report.get("check", ""))
    state = st.get("state", "starting")
    detail = st.get("last_error", "")
    fix = ""
    provider = st.get("provider", "")
    if provider == "openvpn" and report.get("log", "").strip():
        log = parse_openvpn_log(report["log"])
        # The sidecar only sees "the tunnel device exists". The log knows more: a client that
        # keeps its device across a restart looks connected while it is renegotiating.
        if log.state in PROBLEM_STATES and state != CONNECTED:
            state, detail, fix = log.state, log.detail or detail, log.fix
        elif state == CONNECTED and log.state in ("reconnecting", *PROBLEM_STATES):
            state, detail, fix = log.state, log.detail, log.fix
    if state == "auth_failed" and not fix:
        fix = _FIXES["auth_failed"]
    ks = killswitch.analyse(
        report.get("rules4", ""), report.get("rules6", ""), mode=st.get("mode", mode),
        enforced=killswitch_wanted or st.get("mode", mode) == "none", uplink=st.get("uplink", "eth0"),
        ipv6_present=bool(report.get("ipv6", "").strip()))
    if check.get("killswitch_check") == "failed":
        ks = killswitch.Report(False, [*ks.problems, "the sidecar's own check reports the firewall rules as missing"])
    age = check.get("heartbeat_age", "")
    return {
        "mode": st.get("mode", mode), "provider": provider, "profile": st.get("profile", ""),
        "state": state, "ready": st.get("ready") == "1" and state in (CONNECTED, "direct", "isolated"),
        "since": _int(st.get("since")), "started": _int(st.get("started")),
        "tunnel_ip": st.get("tunnel_ip", ""), "tunnel_dev": st.get("tunnel_dev", ""),
        "restarts": _int(st.get("restarts")), "detail": detail, "fix": fix,
        "killswitch": {"enabled": killswitch_wanted, "active": ks.active, "guarded": ks.guarded,
                       "problems": ks.problems},
        "servers": len([ln for ln in report.get("pins", "").splitlines() if ln.strip()]),
        "stalled": age.isdigit() and int(age) > 60,
    }


def _int(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


# --------------------------------------------------------------------------- talking to Docker

def read_report(docker: Docker, container: str, *, timeout: float = 15) -> dict[str, str] | None:
    """One exec for everything. None when the container is not running or does not answer."""
    try:
        r = docker.exec(container, REPORT_CMD, timeout=timeout, check=False)
    except CommandError:
        return None
    return parse_report(r.stdout) if r.ok else None


def _down(docker: Docker, r: Resolved) -> dict:
    """Status for a sidecar that is not running, with the reason when Docker knows one."""
    info = docker.inspect(r.names.net)
    if info is None:
        state, detail = "missing", "the network container does not exist; start the Android"
    else:
        s = info.get("State", {})
        state = "stopped" if s.get("Status") in ("exited", "created") else str(s.get("Status", "stopped"))
        detail = (s.get("Error") or "").strip()
        if not detail and s.get("ExitCode"):
            tail = [ln for ln in docker.logs(r.names.net, tail=5).splitlines() if ln.strip()]
            detail = f"exited with code {s.get('ExitCode')}" + (f": {tail[-1][:200]}" if tail else "")
    return {"mode": r.net_mode, "provider": "", "profile": "", "state": state, "ready": False, "since": 0,
            "started": 0, "tunnel_ip": "", "tunnel_dev": "", "restarts": 0, "detail": detail, "fix": "",
            "killswitch": {"enabled": r.killswitch, "active": False, "guarded": False, "problems": []}, "servers": 0,
            "stalled": False, "public_ip": None}


def status(docker: Docker, r: Resolved) -> dict:
    """Mode, profile, tunnel state, kill switch (verified against the live rules) and the
    last known public address. Never raises for a stopped or missing sidecar."""
    report = read_report(docker, r.names.net)
    if report is None:
        return _down(docker, r)
    out = interpret(report, mode=_sidecar_mode(r), killswitch_wanted=r.killswitch)
    out["mode"] = r.net_mode                       # `proxy` runs a direct sidecar; show what was asked for
    if r.net_mode == "gateway" and r.gateway:
        out["gateway"] = r.gateway
        gw = read_report(docker, Names.gateway(r.names.prefix, r.gateway))
        if gw is None:
            out.update(state="reconnecting", ready=False,
                       detail=f"shared gateway {r.gateway!r} is not running; start any Android that uses it, or `sf gateway up {r.gateway}`")
        else:
            g = interpret(gw, mode="gwserver", killswitch_wanted=True)
            out["profile"] = g["profile"]
            if g["state"] != CONNECTED:
                # A member that reaches its gateway is only online when the gateway's tunnel is.
                out.update(state=g["state"], ready=False, detail=g["detail"], fix=g["fix"])
    out["public_ip"] = _cached_ip(r.names.net, out["since"])
    return out


def _sidecar_mode(r: Resolved) -> str:
    return "direct" if r.net_mode == "proxy" else r.net_mode


def wait_ready(docker: Docker, r: Resolved, timeout: float = 90) -> dict:
    """Block until the sidecar's network is usable: {"ready", "state", "detail"}.

    For VPN and gateway modes ready means the tunnel is up. When a kill switch is asked for,
    ready also requires that its rules are really loaded: a rebuild once ran with old code
    and silently produced containers without one, and nothing noticed until a manual check.
    Returns early (not ready) on failures that waiting cannot fix, such as rejected credentials.
    """
    deadline = time.monotonic() + max(1.0, timeout)
    last: dict = {"state": "starting", "detail": "", "fix": ""}
    while True:
        st = status(docker, r)
        last = st
        if st["state"] in ("missing", "stopped", "exited", "dead"):
            return {"ready": False, "state": st["state"], "detail": st["detail"] or "the network container is not running"}
        ks_ok = st["killswitch"]["active"] or not _needs_killswitch(r)
        if st["ready"] and ks_ok:
            return {"ready": True, "state": st["state"], "detail": ""}
        if st["ready"] and not ks_ok:
            return {"ready": False, "state": "killswitch_missing",
                    "detail": "the tunnel is up but the kill switch is not in place: "
                              + "; ".join(st["killswitch"]["problems"] or ["rules missing"])}
        if st["state"] in ("auth_failed", "config_error", "tun_error"):
            return {"ready": False, "state": st["state"], "detail": " ".join(x for x in (st["detail"], st["fix"]) if x)}
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    detail = " ".join(x for x in (last.get("detail", ""), last.get("fix", "")) if x)
    return {"ready": False, "state": last["state"],
            "detail": detail or f"not ready after {int(timeout)} s (state: {last['state']})"}


def _needs_killswitch(r: Resolved) -> bool:
    return r.net_mode == "none" or (r.killswitch and r.net_mode in ("vpn", "gateway"))


# --------------------------------------------------------------------------- public address

_IP_TTL = 300.0
_ip_cache: dict[str, tuple[float, int, str]] = {}      # container -> (fetched at, tunnel `since`, address)
_ip_lock = threading.Lock()


def _cached_ip(container: str, since: int) -> str | None:
    with _ip_lock:
        hit = _ip_cache.get(container)
    # A reconnect can change the exit address, so an entry from before it is not shown.
    return hit[2] if hit and hit[1] == since else None


def public_ip(docker: Docker, r: Resolved, *, url: str = "https://api.ipify.org", max_age: float = _IP_TTL,
              timeout: float = 10) -> str | None:
    """The address the internet sees this Android under, asked from inside its own network
    namespace. Cached per tunnel session; None when offline or when the answer is not an address."""
    if not re.fullmatch(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", url):
        raise ValueError("network.vpn.public_ip_url must be an http(s) URL")
    st = parse_status_file((read_report(docker, r.names.net) or {}).get("status", ""))
    if not st or st.get("mode") == "none":
        return None
    since = _int(st.get("since"))
    now = time.monotonic()
    with _ip_lock:
        hit = _ip_cache.get(r.names.net)
    if hit and hit[1] == since and now - hit[0] < max_age:
        return hit[2]
    try:
        res = docker.exec(r.names.net, ["curl", "-fsS", "--max-time", str(int(timeout)), "--", url],
                          timeout=timeout + 5, check=False)
    except CommandError:
        return None
    try:
        ip = str(ipaddress.ip_address(res.out)) if res.ok else None
    except ValueError:
        ip = None                                   # a captive portal or an error page, not an address
    if ip:
        with _ip_lock:
            _ip_cache[r.names.net] = (now, since, ip)
    return ip

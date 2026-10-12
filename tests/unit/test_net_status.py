"""Reading a sidecar's state: OpenVPN log classification, the report format, readiness."""
import importlib

import pytest
from net_fakes import (
    PREFIX,
    RULES6_CLOSED,
    RULES_VPN,
    STATUS_CONNECTED,
    FakeDocker,
    make_config,
    make_resolved,
    report,
)

from superfarm.net.status import interpret, parse_openvpn_log, parse_report, parse_status_file
from superfarm.util.proc import Result

# The package re-exports the function status(), so the module is fetched by name.
st = importlib.import_module("superfarm.net.status")

# Log samples in the format OpenVPN 2.6/2.7 writes. Server names and addresses are invented.
LOG_CONNECT = """\
2025-03-04 10:15:01 OpenVPN 2.6.12 x86_64-alpine-linux-musl [SSL (OpenSSL)] [LZO] [LZ4] [EPOLL] [MH/PKTINFO] [AEAD]
2025-03-04 10:15:01 library versions: OpenSSL 3.3.2 3 Sep 2024, LZO 2.10
2025-03-04 10:15:01 NOTE: the current --script-security setting may allow this configuration to call user-defined scripts
2025-03-04 10:15:01 TCP/UDP: Preserving recently used remote address: [AF_INET]203.0.113.45:1194
2025-03-04 10:15:01 UDPv4 link local: (not bound)
2025-03-04 10:15:01 UDPv4 link remote: [AF_INET]203.0.113.45:1194
2025-03-04 10:15:01 TLS: Initial packet from [AF_INET]203.0.113.45:1194, sid=5d1f9a33 0be2c417
2025-03-04 10:15:02 VERIFY OK: depth=1, C=XX, O=Example Networks, CN=Example Root CA
2025-03-04 10:15:02 VERIFY KU OK
2025-03-04 10:15:02 VERIFY EKU OK
2025-03-04 10:15:02 VERIFY OK: depth=0, CN=node-17.vpn.example.net
2025-03-04 10:15:02 Control Channel: TLSv1.3, cipher TLSv1.3 TLS_AES_256_GCM_SHA384, peer certificate: 4096 bits RSA
2025-03-04 10:15:02 [node-17.vpn.example.net] Peer Connection Initiated with [AF_INET]203.0.113.45:1194
2025-03-04 10:15:03 PUSH: Received control message: 'PUSH_REPLY,redirect-gateway def1,dhcp-option DNS 10.8.0.1,route-gateway 10.8.0.1,topology subnet,ping 10,ping-restart 60,ifconfig 10.8.0.6 255.255.255.0,peer-id 4,cipher AES-256-GCM'
2025-03-04 10:15:03 Pushed option removed by filter: 'ping-restart 60'
2025-03-04 10:15:03 TUN/TAP device tun0 opened
2025-03-04 10:15:03 net_addr_v4_add: 10.8.0.6/24 dev tun0
2025-03-04 10:15:03 net_route_v4_add: 0.0.0.0/1 via 10.8.0.1 dev [NULL] table 0 metric -1
2025-03-04 10:15:03 net_route_v4_add: 128.0.0.0/1 via 10.8.0.1 dev [NULL] table 0 metric -1
2025-03-04 10:15:03 Initialization Sequence Completed
"""

LOG_PING_RESTART = LOG_CONNECT + """\
2025-03-04 11:42:20 [node-17.vpn.example.net] Inactivity timeout (--ping-restart), restarting
2025-03-04 11:42:20 SIGUSR1[soft,ping-restart] received, process restarting
2025-03-04 11:42:20 Restart pause, 2 second(s)
2025-03-04 11:42:22 TCP/UDP: Preserving recently used remote address: [AF_INET]203.0.113.45:1194
2025-03-04 11:42:22 UDPv4 link remote: [AF_INET]203.0.113.45:1194
"""

LOG_RECOVERED = LOG_PING_RESTART + """\
2025-03-04 11:42:24 TLS: Initial packet from [AF_INET]203.0.113.45:1194, sid=77aa0c1e 9f3d1b02
2025-03-04 11:42:25 [node-17.vpn.example.net] Peer Connection Initiated with [AF_INET]203.0.113.45:1194
2025-03-04 11:42:26 Preserving previous TUN/TAP instance: tun0
2025-03-04 11:42:26 Initialization Sequence Completed
"""

LOG_AUTH_FAILED = """\
2025-03-04 10:15:01 OpenVPN 2.6.12 x86_64-alpine-linux-musl [SSL (OpenSSL)] [LZO] [LZ4] [EPOLL] [MH/PKTINFO] [AEAD]
2025-03-04 10:15:01 UDPv4 link remote: [AF_INET]203.0.113.45:1194
2025-03-04 10:15:02 [node-17.vpn.example.net] Peer Connection Initiated with [AF_INET]203.0.113.45:1194
2025-03-04 10:15:03 AUTH: Received control message: AUTH_FAILED
2025-03-04 10:15:03 SIGTERM[soft,auth-failure] received, process exiting
"""

LOG_TLS = """\
2025-03-04 10:15:01 UDPv4 link remote: [AF_INET]203.0.113.45:1194
2025-03-04 10:15:01 TLS: Initial packet from [AF_INET]203.0.113.45:1194, sid=e4fd265c 16102819
2025-03-04 10:15:01 VERIFY OK: depth=1, CN=Example Root CA
2025-03-04 10:15:01 Certificate does not have key usage extension
2025-03-04 10:15:01 VERIFY KU ERROR
2025-03-04 10:15:01 OpenSSL: error:0A000086:SSL routines::certificate verify failed:
2025-03-04 10:15:01 TLS_ERROR: BIO read tls_read_plaintext error
2025-03-04 10:15:01 TLS Error: TLS object -> incoming plaintext read error
2025-03-04 10:15:01 TLS Error: TLS handshake failed
2025-03-04 10:15:01 SIGUSR1[soft,tls-error] received, process restarting
2025-03-04 10:15:01 Restart pause, 2 second(s)
"""

LOG_TLS_TIMEOUT = """\
2025-03-04 10:15:01 UDPv4 link remote: [AF_INET]203.0.113.45:1194
2025-03-04 10:16:01 TLS Error: TLS key negotiation failed to occur within 60 seconds (check your network connectivity)
2025-03-04 10:16:01 TLS Error: TLS handshake failed
2025-03-04 10:16:01 SIGUSR1[soft,tls-error] received, process restarting
"""

LOG_UNREACHABLE = """\
2025-03-04 10:15:01 UDPv4 link remote: [AF_INET]203.0.113.45:1194
2025-03-04 10:15:13 [UNDEF] Inactivity timeout (--ping-restart), restarting
2025-03-04 10:15:13 SIGUSR1[soft,ping-restart] received, process restarting
2025-03-04 10:15:13 Restart pause, 2 second(s)
2025-03-04 10:15:27 [UNDEF] Inactivity timeout (--ping-restart), restarting
2025-03-04 10:15:27 SIGUSR1[soft,ping-restart] received, process restarting
2025-03-04 10:15:29 All connections have been connect-retry-max (5) times unsuccessful, exiting
2025-03-04 10:15:29 Exiting due to fatal error
"""

LOG_DNS = """\
2025-03-04 10:15:01 RESOLVE: Cannot resolve host address: node-17.vpn.example.net:1194 (Name does not resolve)
2025-03-04 10:15:01 Could not determine IPv4/IPv6 protocol
2025-03-04 10:15:01 SIGUSR1[soft,Could not determine IPv4/IPv6 protocol] received, process restarting
2025-03-04 10:15:01 Restart pause, 2 second(s)
"""

LOG_OPTIONS = """\
2025-03-04 10:15:01 Options error: Unrecognized option or missing or extra parameter(s) in /sf/vpn/profile.ovpn:14: comp-lzoo (2.6.12)
2025-03-04 10:15:01 Use --help for more information.
"""

LOG_TUN = """\
2025-03-04 10:15:03 PUSH: Received control message: 'PUSH_REPLY,redirect-gateway def1,ifconfig 10.8.0.6 255.255.255.0'
2025-03-04 10:15:03 ERROR: Cannot open TUN/TAP dev /dev/net/tun: No such file or directory (errno=2)
2025-03-04 10:15:03 Exiting due to fatal error
"""

LOG_OLD_STAMPS = """\
Tue Mar  4 10:15:01 2025 UDPv4 link remote: [AF_INET]203.0.113.45:1194
Tue Mar  4 10:15:03 2025 Initialization Sequence Completed
"""


# ------------------------------------------------------------------ log classification

def test_a_completed_handshake_is_connected():
    s = parse_openvpn_log(LOG_CONNECT)
    assert (s.state, s.detail, s.fix, s.was_connected) == ("connected", "", "", True)


def test_a_keepalive_timeout_after_being_up_is_reconnecting():
    s = parse_openvpn_log(LOG_PING_RESTART)
    assert s.state == "reconnecting" and s.was_connected
    assert "Restart pause" in s.detail and not s.detail.startswith("2025")


def test_reconnecting_then_completing_is_connected_again():
    assert parse_openvpn_log(LOG_RECOVERED).state == "connected"


def test_rejected_credentials_are_named_with_the_fix():
    s = parse_openvpn_log(LOG_AUTH_FAILED)
    assert s.state == "auth_failed" and "sf vpn credentials" in s.fix
    assert "AUTH_FAILED" in s.detail          # the exit line that follows must not hide the cause


@pytest.mark.parametrize("log, cause", [(LOG_TLS, "VERIFY KU ERROR"), (LOG_TLS_TIMEOUT, "TLS Error: TLS key negotiation failed")])
def test_a_tls_failure_keeps_its_first_line_as_the_cause(log, cause):
    # neither the fallout lines nor the restart that follows may hide what went wrong
    s = parse_openvpn_log(log)
    assert s.state == "tls_error" and s.detail.startswith(cause) and s.fix and not s.was_connected


def test_a_tls_failure_followed_by_success_is_connected():
    assert parse_openvpn_log(LOG_TLS + LOG_CONNECT).state == "connected"


def test_no_server_answering_is_unreachable():
    s = parse_openvpn_log(LOG_UNREACHABLE)
    assert s.state == "unreachable" and "connect-retry-max" in s.detail and "internet" in s.fix


def test_never_connected_and_retrying_is_connecting():
    assert parse_openvpn_log("\n".join(LOG_UNREACHABLE.splitlines()[:4])).state == "connecting"


@pytest.mark.parametrize("log, state, hint", [
    (LOG_DNS, "dns_error", "network.dns"),
    (LOG_OPTIONS, "config_error", "option"),
    (LOG_TUN, "tun_error", "modprobe tun"),
])
def test_other_failures_carry_their_own_fix(log, state, hint):
    s = parse_openvpn_log(log)
    assert s.state == state and hint in s.fix


def test_empty_and_foreign_lines():
    assert parse_openvpn_log("").state == "unknown"
    assert parse_openvpn_log("[sf-net] state: connecting\n\n").state == "unknown"
    assert parse_openvpn_log("2025-03-04 10:15:01 UDPv4 link local: (not bound)\n").state == "connecting"


def test_old_style_timestamps_are_understood():
    assert parse_openvpn_log(LOG_OLD_STAMPS).state == "connected"


def test_a_clean_stop_reads_as_exited():
    s = parse_openvpn_log(LOG_CONNECT + "2025-03-04 12:00:00 SIGTERM[hard,] received, process exiting\n")
    assert s.state == "exited" and s.was_connected


# ------------------------------------------------------------------ report parsing

def test_status_file_is_data_not_code():
    parsed = parse_status_file("state=connected\nready=1\nlast_error=a=b; $(reboot)\nBad Key=1\nnoequals\n")
    assert parsed == {"state": "connected", "ready": "1", "last_error": "a=b; $(reboot)"}


def test_report_sections():
    r = parse_report(report(STATUS_CONNECTED, rules4=RULES_VPN, rules6=RULES6_CLOSED, log=LOG_CONNECT, pins="udp 198.51.100.20 1194 x"))
    assert set(r) == {"status", "check", "rules4", "rules6", "ipv6", "pins", "log"}
    assert r["rules4"].startswith("-P INPUT DROP") and "Initialization" in r["log"]


def _interp(status=STATUS_CONNECTED, **kw):
    kw.setdefault("rules4", RULES_VPN)
    kw.setdefault("rules6", RULES6_CLOSED)
    return interpret(parse_report(report(status, **kw)), mode="vpn", killswitch_wanted=True)


def test_a_healthy_tunnel():
    out = _interp(log=LOG_CONNECT, pins="udp 198.51.100.20 1194 x\ntcp 198.51.100.20 443 x")
    assert out["state"] == "connected" and out["ready"] and out["tunnel_ip"] == "10.8.0.6"
    assert out["killswitch"] == {"enabled": True, "active": True, "guarded": True, "problems": []}
    assert out["since"] == 1700000100 and out["servers"] == 2 and out["profile"] == "a.ovpn"


def test_the_log_overrules_a_tunnel_device_that_merely_still_exists():
    # persist-tun keeps tun0 through a restart: the sidecar says connected, the log knows better
    out = _interp(log=LOG_PING_RESTART)
    assert out["state"] == "reconnecting" and not out["ready"]


def test_the_log_explains_why_a_tunnel_is_not_coming_up():
    waiting = {**STATUS_CONNECTED, "state": "connecting", "ready": "0", "last_error": "openvpn exited with code 1"}
    out = _interp(waiting, log=LOG_TLS)
    assert out["state"] == "tls_error" and "VERIFY KU ERROR" in out["detail"] and out["fix"]
    auth = {**STATUS_CONNECTED, "state": "auth_failed", "ready": "0", "last_error": "rejected"}
    assert "sf vpn credentials" in _interp(auth, log="")["fix"]


def test_missing_rules_are_reported_even_when_the_tunnel_is_up():
    out = _interp(rules4="-P INPUT ACCEPT\n-P FORWARD ACCEPT\n-P OUTPUT ACCEPT", log=LOG_CONNECT)
    assert out["state"] == "connected" and not out["killswitch"]["active"]
    assert any("OUTPUT is not default-deny" in p for p in out["killswitch"]["problems"])
    out = _interp(check="killswitch_check=failed", log=LOG_CONNECT)
    assert not out["killswitch"]["active"]


def test_a_stalled_supervisor_is_flagged():
    assert _interp(check="killswitch_check=ok\nheartbeat_age=400")["stalled"]
    assert not _interp()["stalled"]


# ------------------------------------------------------------------ status / wait_ready / public_ip

def _farm(mode="vpn", **network):
    config = make_config(mode=mode, **network)
    r = make_resolved(config)
    docker = FakeDocker()
    docker.containers[r.names.net] = {"labels": {}, "running": True, "args": []}
    return docker, r


def test_status_of_a_running_vpn_sidecar():
    docker, r = _farm()
    docker.exec_output[r.names.net] = Result(0, report(STATUS_CONNECTED, rules4=RULES_VPN, rules6=RULES6_CLOSED, log=LOG_CONNECT), "")
    out = st.status(docker, r)
    assert out["state"] == "connected" and out["mode"] == "vpn" and out["killswitch"]["active"]
    assert out["public_ip"] is None                      # nothing cached yet; status never goes online itself
    assert len(docker.called("exec")) == 1               # one exec for the whole picture


def test_status_never_raises_for_a_missing_or_stopped_sidecar():
    docker, r = _farm()
    docker.containers[r.names.net]["running"] = False
    docker.containers[r.names.net]["exit"] = 1
    out = st.status(docker, r)
    assert out["state"] == "stopped" and not out["ready"] and "exited with code 1" in out["detail"]
    del docker.containers[r.names.net]
    assert st.status(docker, r)["state"] == "missing"


def test_proxy_mode_shows_as_proxy_although_the_sidecar_runs_direct():
    docker, r = _farm(mode="proxy", proxy="10.0.0.9:3128")
    direct = {**STATUS_CONNECTED, "mode": "direct", "provider": "", "state": "direct", "tunnel_ip": ""}
    docker.exec_output[r.names.net] = Result(0, report(direct, rules4=RULES_VPN, rules6=RULES6_CLOSED), "")
    out = st.status(docker, r)
    assert out["mode"] == "proxy" and out["state"] == "direct" and out["ready"]


def test_a_gateway_member_is_only_online_when_the_gateway_tunnel_is():
    config = make_config(mode="gateway", gateway="eu", gateways={"eu": {"profile": "a.ovpn"}})
    r = make_resolved(config)
    docker = FakeDocker()
    docker.containers[r.names.net] = {"labels": {}, "running": True, "args": []}
    member = {**STATUS_CONNECTED, "mode": "gateway", "provider": "", "tunnel_dev": "sfgw0", "tunnel_ip": ""}
    rules = RULES_VPN.replace("-A OUTPUT -o tun+ -j ACCEPT", "-A OUTPUT -o sfgw0 -j ACCEPT")
    docker.exec_output[r.names.net] = Result(0, report(member, rules4=rules, rules6=RULES6_CLOSED), "")
    out = st.status(docker, r)
    assert out["state"] == "reconnecting" and not out["ready"] and "sf gateway up eu" in out["detail"]

    gw = f"{PREFIX}-gw-eu"
    docker.containers[gw] = {"labels": {}, "running": True, "args": []}
    down = {**STATUS_CONNECTED, "mode": "gwserver", "state": "connecting", "ready": "0"}
    docker.exec_output[gw] = Result(0, report(down, rules4=RULES_VPN, rules6=RULES6_CLOSED, log=LOG_TLS), "")
    out = st.status(docker, r)
    assert out["state"] == "tls_error" and not out["ready"]

    up = {**STATUS_CONNECTED, "mode": "gwserver"}
    docker.exec_output[gw] = Result(0, report(up, rules4=RULES_VPN, rules6=RULES6_CLOSED, log=LOG_CONNECT), "")
    out = st.status(docker, r)
    assert out["state"] == "connected" and out["ready"] and out["gateway"] == "eu" and out["profile"] == "a.ovpn"
    assert out["killswitch"]["active"]


def test_wait_ready_returns_as_soon_as_the_tunnel_is_up(monkeypatch):
    docker, r = _farm()
    waiting = {**STATUS_CONNECTED, "state": "connecting", "ready": "0"}
    answers = [report(waiting, rules4=RULES_VPN, rules6=RULES6_CLOSED), report(waiting, rules4=RULES_VPN, rules6=RULES6_CLOSED),
               report(STATUS_CONNECTED, rules4=RULES_VPN, rules6=RULES6_CLOSED, log=LOG_CONNECT)]
    slept = []
    monkeypatch.setattr(st.time, "sleep", lambda s: (slept.append(s), docker.exec_output.update({r.names.net: Result(0, answers.pop(0), "")})))
    docker.exec_output[r.names.net] = Result(0, answers.pop(0), "")
    assert st.wait_ready(docker, r, timeout=30) == {"ready": True, "state": "connected", "detail": ""}
    assert len(slept) == 2


def test_wait_ready_does_not_wait_on_failures_that_waiting_cannot_fix(monkeypatch):
    docker, r = _farm()
    monkeypatch.setattr(st.time, "sleep", lambda s: pytest.fail("must not wait"))
    auth = {**STATUS_CONNECTED, "state": "auth_failed", "ready": "0", "last_error": "rejected"}
    docker.exec_output[r.names.net] = Result(0, report(auth, rules4=RULES_VPN, rules6=RULES6_CLOSED, log=LOG_AUTH_FAILED), "")
    out = st.wait_ready(docker, r, timeout=30)
    assert not out["ready"] and out["state"] == "auth_failed" and "sf vpn credentials" in out["detail"]
    del docker.containers[r.names.net]
    assert st.wait_ready(docker, r, timeout=30)["state"] == "missing"


def test_wait_ready_refuses_a_tunnel_without_its_kill_switch(monkeypatch):
    """Connected is not enough: the rules must be verifiably there."""
    docker, r = _farm()
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    open_rules = "-P INPUT DROP\n-P FORWARD DROP\n-P OUTPUT ACCEPT"
    docker.exec_output[r.names.net] = Result(0, report(STATUS_CONNECTED, rules4=open_rules, rules6=RULES6_CLOSED, log=LOG_CONNECT), "")
    out = st.wait_ready(docker, r, timeout=5)
    assert not out["ready"] and out["state"] == "killswitch_missing" and "OUTPUT is not default-deny" in out["detail"]
    # with the kill switch switched off on purpose the same rules are acceptable
    docker2, r2 = _farm(killswitch=False)
    off = RULES_VPN + "\n-A OUTPUT -j ACCEPT"
    docker2.exec_output[r2.names.net] = Result(0, report(STATUS_CONNECTED, rules4=off, rules6=RULES6_CLOSED, log=LOG_CONNECT), "")
    assert st.wait_ready(docker2, r2, timeout=5)["ready"]


def test_wait_ready_times_out_with_the_last_known_reason(monkeypatch):
    docker, r = _farm()
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(st.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    waiting = {**STATUS_CONNECTED, "state": "connecting", "ready": "0", "last_error": ""}
    docker.exec_output[r.names.net] = Result(0, report(waiting, rules4=RULES_VPN, rules6=RULES6_CLOSED, log=LOG_UNREACHABLE), "")
    out = st.wait_ready(docker, r, timeout=25)
    assert not out["ready"] and out["state"] == "unreachable" and "connect-retry-max" in out["detail"]


def test_public_ip_is_validated_and_cached_per_tunnel_session(monkeypatch):
    docker, r = _farm()
    st._ip_cache.clear()
    base = report(STATUS_CONNECTED, rules4=RULES_VPN, rules6=RULES6_CLOSED)
    answers = {"report": base, "curl": Result(0, "203.0.113.99\n", "")}

    def answer(argv):
        return answers["curl"] if "curl" in argv else Result(0, answers["report"], "")
    monkeypatch.setattr(docker, "_answer", lambda a: answer(a) if a[0] == "exec" else FakeDocker._answer(docker, a))
    assert st.public_ip(docker, r, url="http://ip.example.net/") == "203.0.113.99"
    curl = [c for c in docker.called("exec") if "curl" in c][0]
    assert curl[-2:] == ["--", "http://ip.example.net/"] and "--max-time" in curl
    answers["curl"] = Result(0, "198.51.100.1\n", "")
    assert st.public_ip(docker, r, url="http://ip.example.net/") == "203.0.113.99"        # cached
    assert st.status(docker, r)["public_ip"] == "203.0.113.99"
    # a reconnect (new `since`) invalidates it
    answers["report"] = report({**STATUS_CONNECTED, "since": "1700009999"}, rules4=RULES_VPN, rules6=RULES6_CLOSED)
    assert st.status(docker, r)["public_ip"] is None
    assert st.public_ip(docker, r, url="http://ip.example.net/") == "198.51.100.1"
    # a captive portal page is not an address
    answers["report"] = report({**STATUS_CONNECTED, "since": "1700019999"}, rules4=RULES_VPN, rules6=RULES6_CLOSED)
    answers["curl"] = Result(0, "<html>Please log in</html>", "")
    assert st.public_ip(docker, r, url="http://ip.example.net/") is None
    with pytest.raises(ValueError):
        st.public_ip(docker, r, url="file:///etc/passwd")
    with pytest.raises(ValueError):
        st.public_ip(docker, r, url="http://x/ -o /tmp/y")

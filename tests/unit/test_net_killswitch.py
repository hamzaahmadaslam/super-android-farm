"""The kill switch: settings handed to the sidecar, the rule set its script generates for
every mode (run with the host's `sh`, nothing is loaded), and the judgement of live rules."""
import shutil
import subprocess

import pytest
from net_fakes import RULES6_CLOSED, RULES_VPN, FakeDocker, make_config, make_resolved

from superfarm.net import killswitch
from superfarm.net.killswitch import analyse, firewall_env, is_loopback
from superfarm.paths import REPO_DIR
from superfarm.util.proc import Result

SCRIPT = REPO_DIR / "deploy" / "docker" / "net" / "killswitch.sh"
LAN = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10")


def generate(tmp_path, **env) -> tuple[list[str], list[str]]:
    """The IPv4 and IPv6 rule sets `killswitch.sh init` would load for this configuration."""
    values = {"SF_MODE": "vpn", "SF_KILLSWITCH": "1", "SF_ALLOW_LAN": "0", "SF_DNS": "1.1.1.1,8.8.8.8", "SF_IPV6": "0",
              "SF_ADB_PORT": "5555", "SF_ADB_FROM": "gateway", "WAN_IF": "eth0", "WAN_GW": "172.17.0.1", "GW_IF": "",
              "TUN_MATCH": "tun+", **env}
    (tmp_path / "env").write_text("".join(f"{k}='{v}'\n" for k, v in values.items()))
    out = subprocess.run(["sh", str(SCRIPT), "print"], env={"SF_RUN": str(tmp_path), "PATH": "/usr/bin:/bin"},
                         capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    v4, v6 = out.stdout.split("# IPv6")
    return [ln for ln in v4.splitlines() if ln and not ln.startswith("#")], [ln for ln in v6.splitlines() if ln]


def as_listing(rules: list[str]) -> str:
    """iptables-restore input -> the `iptables -S` form the analyser reads."""
    out = []
    for ln in rules:
        if ln.startswith(":") and ln.split()[1] in ("DROP", "ACCEPT"):
            out.append(f"-P {ln[1:].split()[0]} {ln.split()[1]}")
        elif ln.startswith("-A"):
            out.append(ln)
    return "\n".join(out)


needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")


# ------------------------------------------------------------------ generated rule sets

@needs_sh
@pytest.mark.parametrize("mode", ["direct", "vpn", "gateway", "none", "gwserver"])
def test_every_mode_starts_from_default_deny_in_both_families(tmp_path, mode):
    v4, v6 = generate(tmp_path, SF_MODE=mode, GW_IF="sfgw0" if mode in ("gateway", "gwserver") else "")
    for rules in (v4, v6):
        assert rules[0] == "*filter" and rules[-1] == "COMMIT"
        assert {":INPUT DROP [0:0]", ":FORWARD DROP [0:0]", ":OUTPUT DROP [0:0]"} <= set(rules)
    # IPv6 is off: loopback and nothing else
    assert [r for r in v6 if r.startswith("-A")] == ["-A INPUT -i lo -j ACCEPT", "-A OUTPUT -o lo -j ACCEPT"]


@needs_sh
def test_vpn_mode_lets_only_the_tunnel_pins_and_adb_answers_out(tmp_path):
    v4, _ = generate(tmp_path)
    out = [r for r in v4 if r.startswith("-A OUTPUT")]
    assert out == [
        "-A OUTPUT -o lo -j ACCEPT",
        "-A OUTPUT -o eth0 -p tcp --sport 5555 -m conntrack --ctstate ESTABLISHED -j ACCEPT",
        "-A OUTPUT -o tun+ -j ACCEPT",
        "-A OUTPUT -o eth0 -j SF-PIN",
        "-A OUTPUT -o eth0 -j SF-DNS",
    ]
    # the pin and DNS chains start empty: nothing is reachable until a server is pinned
    assert not [r for r in v4 if r.startswith(("-A SF-PIN", "-A SF-DNS"))]
    assert analyse(as_listing(v4), RULES6_CLOSED, mode="vpn", enforced=True).active


@needs_sh
def test_adb_is_only_accepted_from_the_host_unless_published_wider(tmp_path):
    v4, _ = generate(tmp_path)
    assert "-A INPUT -i eth0 -p tcp -s 172.17.0.1 --dport 5555 -j ACCEPT" in v4
    wide, _ = generate(tmp_path, SF_ADB_FROM="any")
    assert "-A INPUT -i eth0 -p tcp --dport 5555 -j ACCEPT" in wide
    assert not [r for r in v4 if r.startswith("-A INPUT") and "ACCEPT" in r and "-s" not in r
                and "lo" not in r and "conntrack" not in r]


@needs_sh
def test_none_mode_has_no_way_out_even_with_the_kill_switch_off(tmp_path):
    v4, _ = generate(tmp_path, SF_MODE="none", SF_KILLSWITCH="0")
    assert [r for r in v4 if r.startswith("-A OUTPUT")] == [
        "-A OUTPUT -o lo -j ACCEPT",
        "-A OUTPUT -o eth0 -p tcp --sport 5555 -m conntrack --ctstate ESTABLISHED -j ACCEPT",
    ]


@needs_sh
def test_direct_mode_keeps_the_lan_and_the_host_out_of_reach(tmp_path):
    v4, _ = generate(tmp_path, SF_MODE="direct", SF_DNS="192.168.1.1,1.1.1.1")
    out = [r for r in v4 if r.startswith("-A OUTPUT")]
    for net in LAN:
        assert f"-A OUTPUT -o eth0 -d {net} -j DROP" in out
    assert out[-1] == "-A OUTPUT -o eth0 -j ACCEPT"
    # a private resolver configured on purpose is still reachable, and only on port 53
    dns = out.index("-A OUTPUT -o eth0 -p udp -d 192.168.1.1 --dport 53 -j ACCEPT")
    assert dns < out.index("-A OUTPUT -o eth0 -d 192.168.0.0/16 -j DROP")
    open_lan, _ = generate(tmp_path, SF_MODE="direct", SF_ALLOW_LAN="1")
    assert not [r for r in open_lan if "-j DROP" in r and r.startswith("-A")]


@needs_sh
def test_allow_lan_with_a_vpn_opens_private_ranges_only(tmp_path):
    v4, _ = generate(tmp_path, SF_ALLOW_LAN="1")
    for net in LAN:
        assert f"-A OUTPUT -o eth0 -d {net} -j ACCEPT" in v4
    assert "-A OUTPUT -o eth0 -j ACCEPT" not in v4
    assert analyse(as_listing(v4), RULES6_CLOSED, mode="vpn", enforced=True).active


@needs_sh
def test_a_gateway_member_may_only_use_the_gateway_network(tmp_path):
    v4, _ = generate(tmp_path, SF_MODE="gateway", GW_IF="sfgw0")
    assert [r for r in v4 if r.startswith("-A OUTPUT")][2:] == ["-A OUTPUT -o sfgw0 -j ACCEPT"]
    # before the gateway network is attached there is no exit at all
    early, _ = generate(tmp_path, SF_MODE="gateway", GW_IF="")
    assert len([r for r in early if r.startswith("-A OUTPUT")]) == 2
    assert analyse(as_listing(v4), RULES6_CLOSED, mode="gateway", enforced=True, uplink="eth0").active


@needs_sh
def test_a_gateway_forwards_members_into_the_tunnel_and_nowhere_else(tmp_path):
    for ks in ("1", "0"):          # holds even with the kill switch setting off
        v4, _ = generate(tmp_path, SF_MODE="gwserver", GW_IF="sfgw0", SF_KILLSWITCH=ks)
        assert [r for r in v4 if r.startswith("-A FORWARD")] == [
            "-A FORWARD -i sfgw0 -o tun+ -j ACCEPT",
            "-A FORWARD -i tun+ -o sfgw0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
        ]
        assert ":FORWARD DROP [0:0]" in v4


@needs_sh
def test_switching_the_kill_switch_off_is_explicit_and_detected(tmp_path):
    v4, _ = generate(tmp_path, SF_KILLSWITCH="0")
    assert v4[-2] == "-A OUTPUT -j ACCEPT"
    assert ":INPUT DROP [0:0]" in v4                  # the inbound guard stays
    rep = analyse(as_listing(v4), RULES6_CLOSED, mode="vpn", enforced=True)
    assert not rep.active and "accepts all outgoing traffic" in rep.problems[0]
    off = analyse(as_listing(v4), RULES6_CLOSED, mode="vpn", enforced=False)
    assert not off.active and off.guarded and off.problems == []


@needs_sh
def test_wireguard_uses_its_own_interface_name(tmp_path):
    v4, _ = generate(tmp_path, TUN_MATCH="wg0")
    assert "-A OUTPUT -o wg0 -j ACCEPT" in v4 and not [r for r in v4 if "tun+" in r]


@needs_sh
def test_ipv6_when_enabled_stays_inside_the_tunnel(tmp_path):
    _, v6 = generate(tmp_path, SF_IPV6="1")
    assert "-A OUTPUT -o tun+ -j ACCEPT" in v6 and not [r for r in v6 if "eth0" in r]
    _, direct = generate(tmp_path, SF_IPV6="1", SF_MODE="direct")
    assert "-A OUTPUT -o eth0 -j ACCEPT" in direct and "-A INPUT -i eth0 -p ipv6-icmp -j ACCEPT" in direct


@needs_sh
def test_garbage_resolvers_never_reach_a_rule(tmp_path):
    v4, _ = generate(tmp_path, SF_MODE="direct", SF_DNS="1.1.1.1,999.1.1.1,8.8.8.8;reboot,a.b.c.d,1.2.3")
    dns = [r for r in v4 if "--dport 53" in r]
    assert len(dns) == 2 and all("-d 1.1.1.1 " in r for r in dns)


# ------------------------------------------------------------------ judging live rules

def test_the_expected_rule_set_is_active():
    rep = analyse(RULES_VPN, RULES6_CLOSED, mode="vpn", enforced=True)
    assert rep.active and rep.guarded and rep.problems == []


@pytest.mark.parametrize("rules4, problem", [
    ("", "could not be read"),
    ("iptables: Permission denied", "could not be read"),
    (RULES_VPN.replace("-P OUTPUT DROP", "-P OUTPUT ACCEPT"), "OUTPUT is not default-deny"),
    (RULES_VPN.replace("-P INPUT DROP", "-P INPUT ACCEPT"), "INPUT is not default-deny"),
    (RULES_VPN + "\n-A OUTPUT -j ACCEPT", "accepts all outgoing traffic"),
    (RULES_VPN + "\n-A OUTPUT -o eth0 -j ACCEPT", "leaving through eth0"),
])
def test_a_missing_or_opened_rule_set_is_not_active(rules4, problem):
    rep = analyse(rules4, RULES6_CLOSED, mode="vpn", enforced=True)
    assert not rep.active and any(problem in p for p in rep.problems)


def test_ipv6_must_be_closed_or_absent():
    assert analyse(RULES_VPN, "", mode="vpn", enforced=True, ipv6_present=False).active
    rep = analyse(RULES_VPN, "", mode="vpn", enforced=True, ipv6_present=True)
    assert not rep.active and "IPv6" in rep.problems[0]
    leaky = RULES6_CLOSED + "\n-A OUTPUT -o eth0 -j ACCEPT"
    assert not analyse(RULES_VPN, leaky, mode="vpn", enforced=True).active
    assert analyse(RULES_VPN, RULES6_CLOSED + "\n-A OUTPUT -o tun+ -j ACCEPT", mode="vpn", enforced=True).active


def test_direct_mode_has_a_guard_but_no_kill_switch_to_be_active():
    direct = RULES_VPN + "\n-A OUTPUT -o eth0 -j ACCEPT"
    rep = analyse(direct, RULES6_CLOSED, mode="direct", enforced=True)
    assert not rep.active and rep.guarded and rep.problems == []


# ------------------------------------------------------------------ settings for a sidecar

def test_firewall_env_follows_the_resolved_instance():
    config = make_config(mode="vpn", allow_lan=True, dns=["9.9.9.9", "2620:fe::fe"], dns_leak_protection=False)
    env = firewall_env(make_resolved(config, killswitch=False), config)
    assert env == {"SF_KILLSWITCH": "0", "SF_ALLOW_LAN": "1", "SF_DNS": "9.9.9.9", "SF_DNS_LEAK_PROTECTION": "0",
                   "SF_IPV6": "0", "SF_ADB_PORT": "5555", "SF_ADB_FROM": "gateway"}
    default = make_config()
    assert firewall_env(make_resolved(default), default)["SF_KILLSWITCH"] == "1"


def test_adb_published_beyond_loopback_is_reachable_from_beyond_the_host():
    config = make_config(adb_bind="0.0.0.0")
    assert firewall_env(make_resolved(config), config)["SF_ADB_FROM"] == "any"
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost") and is_loopback("127.8.9.1")
    assert not is_loopback("192.168.1.5") and not is_loopback("0.0.0.0") and not is_loopback("nonsense")


def test_resolvers_are_validated():
    config = make_config(mode="vpn", dns=["2620:fe::fe"])
    with pytest.raises(ValueError, match="IPv4 resolver"):
        firewall_env(make_resolved(config), config)
    bad = make_config(dns=["dns.example.net"])
    with pytest.raises(ValueError, match="not an IP address"):
        firewall_env(make_resolved(bad), bad)


# ------------------------------------------------------------------ active probe

def test_probe_binds_to_the_uplink_and_reports_a_leak():
    docker = FakeDocker()
    docker.containers["c"] = {"labels": {}, "running": True, "args": []}
    docker.exec_output["c"] = Result(28, "", "curl: (28) Connection timed out after 4001 milliseconds")
    out = killswitch.probe(docker, "c", "http://198.51.100.9:8080/")
    assert out["leak"] is False and "timed out" in out["detail"]
    argv = docker.called("exec")[0]
    assert argv[argv.index("--interface") + 1] == "eth0" and argv[-2:] == ["--", "http://198.51.100.9:8080/"]
    docker.exec_output["c"] = Result(0, "", "")
    assert killswitch.probe(docker, "c", "http://198.51.100.9:8080/")["leak"] is True
    for bad in ("198.51.100.9", "http://x/ --output /etc/x", "ftp://x"):
        with pytest.raises(ValueError):
            killswitch.probe(docker, "c", bad)

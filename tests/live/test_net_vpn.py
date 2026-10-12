"""Live tests of the VPN sidecar, the kill switch and shared gateways.

No VPN account is needed and no public VPN server is touched: the tests stand up their own
throwaway OpenVPN server (PKI generated inside its container) on private Docker networks,
plus a tiny "what is my address" HTTP service on the host:

    sidecar ──(wan network)── server ──(far network)── host:ECHO_PORT
       └── kill switch                └── NAT

    - through the tunnel the service sees the *server's* address;
    - directly (a leak) it would see the sidecar's own address. That must never happen.

Run:  SF_LIVE=1 .venv/bin/python -m pytest tests/live/test_net_vpn.py -v
      add SF_LIVE_ANDROID=1 to also boot one real Android (image `redroid:13-noflag`, ~1.5 GB RAM)
      inside a VPN sidecar and check that its own network daemon and the route sync get along.
Needs Docker and internet access once (to build the images). Creates only containers named
sftnet-* / sftest-net-* and networks sftest-net-* / sftnet-gwnet-* / sftnet-bridge, and removes
them again.
"""
from __future__ import annotations

import contextlib
import http.server
import ipaddress
import os
import socket
import socketserver
import struct
import threading
import time
from pathlib import Path

import pytest

from superfarm.config import Config
from superfarm.core.docker import Docker
from superfarm.core.models import InstanceSpec, resolve
from superfarm.core.store import Store
from superfarm.net import ensure_image, gateway, killswitch, public_ip, sidecar_run_args, vpn, wait_ready
from superfarm.net import status as net_status
from superfarm.paths import get_paths

pytestmark = pytest.mark.live

PREFIX = "sftnet"                 # containers of the farm under test: sftnet-net-<id>, sftnet-gw-<name>
LAB = "sftest-net"                # everything else this file creates
ADB_BASE = 17100
ECHO_PORT = 17190
POOL = "10.214.0.0/16"
SERVER = f"{LAB}-ovpn"
USER, PASSWORD = "tester", "not-a-real-password"

SERVER_SCRIPT = r"""
set -eu
mkdir -p /pki && cd /pki
if [ ! -f ca.crt ]; then
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key
  openssl req -x509 -new -key ca.key -sha256 -days 3 -subj "/CN=sftest-ca" -out ca.crt 2>/dev/null
  for n in server client; do
    openssl ecparam -name prime256v1 -genkey -noout -out $n.key
    openssl req -new -key $n.key -subj "/CN=sftest-$n" -out $n.csr 2>/dev/null
    if [ $n = server ]; then eku=serverAuth; else eku=clientAuth; fi
    printf 'keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=%s\n' "$eku" > $n.ext
    openssl x509 -req -in $n.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 3 -sha256 -extfile $n.ext -out $n.crt 2>/dev/null
  done
  printf '#!/bin/sh\n[ "$(sed -n 1p "$1")" = "%s" ] && [ "$(sed -n 2p "$1")" = "%s" ]\n' "$SRV_USER" "$SRV_PASS" > verify.sh
  chmod 755 verify.sh
fi
iptables -t nat -C POSTROUTING -s 10.99.0.0/24 -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s 10.99.0.0/24 -j MASQUERADE
exec openvpn --dev tun --topology subnet --server 10.99.0.0 255.255.255.0 --proto udp --port 1194 \
  --ca ca.crt --cert server.crt --key server.key --dh none --keepalive 2 8 --duplicate-cn --verb 3 \
  --tmp-dir /tmp --script-security 2 --auth-user-pass-verify /pki/verify.sh via-file
"""


# --------------------------------------------------------------------------- the lab

class Echo:
    """Answers every GET with the caller's address and remembers who called which listener."""

    def __init__(self, addresses: list[str]):
        self.hits: list[tuple[str, str]] = []
        self.servers = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = self.client_address[0].encode()
                outer.hits.append((self.server.server_address[0], self.client_address[0]))
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        for address in addresses:
            srv = Server((address, ECHO_PORT), Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.servers.append(srv)

    def close(self) -> None:
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()


class Lab:
    def __init__(self, docker: Docker, image: str):
        self.docker, self.image = docker, image
        self.wan, self.far = f"{LAB}-wan", f"{LAB}-far"
        self.echo: Echo | None = None

    # -- lifecycle
    def up(self) -> None:
        self.down()
        used = self.docker.network_subnets()
        self.wan_subnet = gateway.allocate_subnet(POOL, used)
        self.far_subnet = gateway.allocate_subnet(POOL, [*used, self.wan_subnet])
        self.docker.network_create(self.wan, subnet=self.wan_subnet)
        self.docker.network_create(self.far, subnet=self.far_subnet)
        self.wan_host = self._host(self.wan_subnet, 1)        # the host's address on each bridge
        self.far_host = self._host(self.far_subnet, 1)
        self.server_wan = self._host(self.wan_subnet, 2)
        self.docker.run(["build", "-q", "-t", f"{LAB}-server:latest", "-"], timeout=600,
                        input=f"FROM {self.image}\nRUN apk add --no-cache openssl\nHEALTHCHECK NONE\n")
        self.docker.create_and_start([
            "--name", SERVER, "--network", self.wan, "--ip", self.server_wan, "--cap-add", "NET_ADMIN",
            "--device", "/dev/net/tun", "--sysctl", "net.ipv4.ip_forward=1", "--memory", "128m",
            "-e", f"SRV_USER={USER}", "-e", f"SRV_PASS={PASSWORD}", "--entrypoint", "sh",
            f"{LAB}-server:latest", "-c", SERVER_SCRIPT])
        self.docker.run(["network", "connect", self.far, SERVER], timeout=60)
        self.server_far = self.docker.inspect(SERVER)["NetworkSettings"]["Networks"][self.far]["IPAddress"]
        self.wait_server()
        self.echo = Echo([self.wan_host, self.far_host])

    def down(self) -> None:
        if self.echo:
            self.echo.close()
            self.echo = None
        mine = [c["name"] for c in self.docker.ps(name_prefix=LAB)]
        mine += [c["name"] for c in self.docker.ps(labels={"io.superfarm.farm": PREFIX}) if c["name"].startswith(PREFIX + "-")]
        self.docker.rm(*mine)
        for net in (self.wan, self.far, f"{LAB}-lan"):
            self.docker.network_rm(net)
        nets = self.docker.run(["network", "ls", "--format", "{{.Name}}", "--filter", f"label=io.superfarm.farm={PREFIX}"],
                               timeout=30, check=False).stdout.split()
        for net in nets:
            if net.startswith(PREFIX + "-"):
                self.docker.network_rm(net)
        self.docker.run(["image", "rm", f"{LAB}-server:latest"], timeout=60, check=False)

    @staticmethod
    def _host(subnet: str, index: int) -> str:
        return str(ipaddress.ip_network(subnet).network_address + index)

    # -- the throwaway server
    def wait_server(self, timeout: float = 40) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "Initialization Sequence Completed" in self.docker.logs(SERVER, tail=50):
                return
            time.sleep(0.5)
        raise AssertionError("throwaway OpenVPN server did not start:\n" + self.docker.logs(SERVER, tail=30))

    def stop_server(self) -> None:
        self.docker.stop(SERVER, grace=1, check=True)

    def start_server(self) -> None:
        self.docker.start(SERVER)
        self.wait_server()

    def profile(self, remote: str) -> bytes:
        def pki(name: str) -> str:
            return self.docker.exec(SERVER, ["cat", f"/pki/{name}"], timeout=20).stdout.strip()
        return (f"client\ndev tun\nproto udp\nremote {remote} 1194\nnobind\nremote-cert-tls server\nauth-user-pass\nverb 3\n"
                f"up /bin/this-must-be-stripped\n<ca>\n{pki('ca.crt')}\n</ca>\n<cert>\n{pki('client.crt')}\n</cert>\n"
                f"<key>\n{pki('client.key')}\n</key>\n").encode()

    # -- URLs
    @property
    def far_url(self) -> str:       # reachable only through the tunnel (or, without a kill switch, via the host)
        return f"http://{self.far_host}:{ECHO_PORT}/"

    @property
    def wan_url(self) -> str:       # on the plain Docker network next to the sidecar
        return f"http://{self.wan_host}:{ECHO_PORT}/"


@pytest.fixture(scope="module")
def docker() -> Docker:
    d = Docker()
    ok, why = d.available()
    if not ok:
        pytest.skip(f"Docker is not usable: {why}")
    return d


@pytest.fixture(scope="module")
def lab(docker):
    lab = Lab(docker, ensure_image(docker))
    try:
        lab.up()
        yield lab
    finally:
        lab.down()


class Farm:
    """A throwaway farm home plus helpers to run its sidecars inside the lab."""

    def __init__(self, home: Path, lab: Lab, **network):
        self.lab, self.docker = lab, lab.docker
        self.paths = get_paths(home).ensure()
        self.store = Store(self.paths)
        self.clients: list[str] = []
        self.configure(**network)

    def configure(self, **network) -> None:
        net = {"adb_port_base": ADB_BASE, "dns": ["127.0.0.11"],
               "vpn": {"ping_seconds": 2, "ping_restart_seconds": 10, "connect_retry_max": 2,
                       "public_ip_url": self.lab.far_url, **network.pop("vpn", {})}, **network}
        self.config = Config.model_validate({"docker": {"name_prefix": PREFIX}, "network": net})

    def resolved(self, instance_id: int, **network):
        spec = InstanceSpec.model_validate({"id": instance_id, "network": network})
        self.store.save(spec)
        return resolve(spec, self.config)

    def start(self, r, *, network: str | None = None):
        """Run the sidecar exactly as the farm would; `network` moves it from the default
        bridge onto a lab network (inserted before the image, changing nothing else)."""
        args = sidecar_run_args(r, self.config, self.paths, self.store, self.docker)
        if network:
            image = next(i for i, a in enumerate(args) if a.startswith("superfarm/net:"))
            args[image:image] = ["--network", network]
        self.docker.rm(r.names.net)
        self.docker.create_and_start(args)
        return r

    def client(self, r) -> str:
        """A second container in the sidecar's network namespace, standing in for the Android:
        it listens on the ADB port (echo) and is where 'app traffic' is sent from."""
        name = f"{LAB}-client-{r.id}"
        self.docker.rm(name)
        self.docker.create_and_start(["--name", name, "--network", f"container:{r.names.net}", "--no-healthcheck",
                                      "--memory", "64m", "--entrypoint", "sh", self.lab.image, "-c",
                                      "nc -lk -p 5555 -e cat & exec sleep 3600"])
        self.clients.append(name)
        return name

    def fetch(self, container: str, url: str, timeout: int = 4) -> str | None:
        """Body of `url` fetched from inside `container`, or None when it cannot be reached."""
        res = self.docker.exec(container, ["curl", "-sS", "--max-time", str(timeout), "--", url], timeout=timeout + 10, check=False)
        return res.out if res.ok else None

    def cleanup(self) -> None:
        names = [c["name"] for c in self.docker.ps(labels={"io.superfarm.farm": PREFIX}) if c["name"].startswith(PREFIX + "-")]
        self.docker.rm(*self.clients, *names)


@pytest.fixture
def farm(lab, tmp_path):
    f = Farm(tmp_path / "home", lab)
    lab.echo.hits.clear()
    try:
        yield f
    finally:
        f.cleanup()


def adb_echo(port: int, payload: bytes = b"ping-over-adb-port\n") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(payload)
        s.settimeout(5)
        return s.recv(len(payload))


def wait_for(predicate, timeout: float, what: str, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what} (last: {last!r})")


def vpn_instance(farm: Farm, instance_id: int = 1, *, remote: str | None = None, password: str = PASSWORD, **network):
    vpn.import_profile(farm.paths, "lab.ovpn", farm.lab.profile(remote or SERVER))
    vpn.set_credentials(farm.paths, "default", USER, password)
    farm.configure(mode="vpn", **network)
    return farm.start(farm.resolved(instance_id), network=farm.lab.wan)


# --------------------------------------------------------------------------- VPN + kill switch

def test_tunnel_carries_everything_and_nothing_leaves_beside_it(farm, lab):
    r = vpn_instance(farm)
    ready = wait_ready(farm.docker, r, timeout=60)
    assert ready == {"ready": True, "state": "connected", "detail": ""}, farm.docker.logs(r.names.net, tail=40)
    client = farm.client(r)
    own = farm.docker.inspect(r.names.net)["NetworkSettings"]["Networks"][lab.wan]["IPAddress"]

    st = net_status(farm.docker, r)
    assert st["state"] == "connected" and st["mode"] == "vpn" and st["provider"] == "openvpn"
    assert st["profile"] == "lab.ovpn" and st["tunnel_ip"].startswith("10.99.0.") and st["servers"] == 1
    assert st["killswitch"] == {"enabled": True, "active": True, "guarded": True, "problems": []}

    # the profile's hook was stripped before the container ever saw it
    assert "this-must-be-stripped" not in farm.docker.exec(r.names.net, ["cat", "/sf/vpn/profile.ovpn"]).stdout
    # the server name was resolved once, pinned, and DNS on the uplink closed again
    assert f"{lab.server_wan} {SERVER}" in farm.docker.exec(r.names.net, ["cat", "/etc/hosts"]).stdout
    rules = farm.docker.exec(r.names.net, ["iptables", "-S"]).stdout
    assert f"-A SF-PIN -d {lab.server_wan}/32 -p udp -m udp --dport 1194 -j ACCEPT" in rules
    assert "-A SF-DNS" not in rules
    # credentials: mounted, never in the environment or the log
    env = "\n".join(farm.docker.inspect(r.names.net)["Config"]["Env"])
    assert PASSWORD not in env and PASSWORD not in farm.docker.logs(r.names.net, tail=500)

    # 1. traffic goes through the tunnel: the service sees the VPN server, not us
    assert "dev tun0" in farm.docker.exec(client, ["ip", "route", "get", "1.1.1.1"]).stdout
    assert farm.fetch(client, lab.far_url) == lab.server_far
    assert public_ip(farm.docker, r, url=lab.far_url) == lab.server_far
    assert net_status(farm.docker, r)["public_ip"] == lab.server_far
    # 2. the plain Docker network next to the sidecar is closed even while connected
    assert farm.fetch(client, lab.wan_url) is None
    assert killswitch.probe(farm.docker, r.names.net, lab.wan_url)["leak"] is False
    # 3. ADB still works through the published loopback port
    assert adb_echo(r.adb_port) == b"ping-over-adb-port\n"

    # ---- the server dies
    lab.stop_server()
    assert farm.fetch(client, lab.far_url) is None, "no tunnel, no traffic"
    assert farm.fetch(client, lab.wan_url) is None
    down = wait_for(lambda: (s := net_status(farm.docker, r))["state"] != "connected" and s, 40, "the sidecar to notice")
    assert down["state"] in ("reconnecting", "unreachable", "connecting") and not down["ready"]
    assert down["killswitch"]["active"]
    # the route now points at the uplink - and the firewall is what stops the packets
    assert "dev eth0" in farm.docker.exec(client, ["ip", "route", "get", lab.far_host]).stdout
    assert farm.fetch(client, lab.far_url) is None, "LEAK: reached the service around the tunnel"
    assert farm.fetch(client, lab.wan_url) is None
    for target in (lab.far_url, lab.wan_url):
        assert killswitch.probe(farm.docker, r.names.net, target)["leak"] is False
    assert adb_echo(r.adb_port) == b"ping-over-adb-port\n", "ADB must survive a VPN outage"
    assert farm.docker.running(r.names.net), "the sidecar must not exit: the Android lives in its namespace"

    # ---- the server returns: reconnect without any DNS on the uplink
    lab.start_server()
    back = wait_ready(farm.docker, r, timeout=120)
    assert back["ready"], f"did not reconnect: {back}\n" + farm.docker.logs(r.names.net, tail=40)
    assert farm.fetch(client, lab.far_url) == lab.server_far
    assert net_status(farm.docker, r)["restarts"] >= 0

    # the whole time, the service never once saw the sidecar's own address
    assert own not in {who for _, who in lab.echo.hits}, f"LEAK recorded by the service: {lab.echo.hits}"
    assert {who for _, who in lab.echo.hits} == {lab.server_far}


def test_control_without_a_kill_switch_the_same_probe_does_leak(farm, lab):
    """Proves the test above can fail: with the kill switch off the service is reached
    directly and sees the sidecar's own address."""
    r = vpn_instance(farm, killswitch=False)
    assert wait_ready(farm.docker, r, timeout=60)["ready"]
    own = farm.docker.inspect(r.names.net)["NetworkSettings"]["Networks"][lab.wan]["IPAddress"]
    assert farm.fetch(r.names.net, lab.wan_url) == own
    assert killswitch.probe(farm.docker, r.names.net, lab.wan_url)["leak"] is True
    st = net_status(farm.docker, r)
    assert st["killswitch"]["enabled"] is False and st["killswitch"]["active"] is False and st["killswitch"]["guarded"]


def test_wrong_password_is_reported_quickly_and_stays_closed(farm, lab):
    r = vpn_instance(farm, password="wrong-on-purpose")
    started = time.monotonic()
    out = wait_ready(farm.docker, r, timeout=90)
    assert not out["ready"] and out["state"] == "auth_failed", out
    assert "sf vpn credentials" in out["detail"] and time.monotonic() - started < 60
    assert farm.fetch(r.names.net, lab.wan_url) is None and farm.fetch(r.names.net, lab.far_url) is None
    assert farm.docker.running(r.names.net)
    assert "wrong-on-purpose" not in farm.docker.logs(r.names.net, tail=500)


def test_a_server_given_by_address_needs_no_dns_at_all(farm, lab):
    r = vpn_instance(farm, remote=lab.server_wan, dns=["192.0.2.53"])       # a resolver that does not exist
    assert wait_ready(farm.docker, r, timeout=60)["ready"], farm.docker.logs(r.names.net, tail=40)
    assert farm.fetch(r.names.net, lab.far_url) == lab.server_far


def test_an_unresolvable_server_keeps_everything_closed_and_explains(farm, lab):
    vpn.import_profile(farm.paths, "lab.ovpn", lab.profile("no-such-server.invalid"))
    vpn.set_credentials(farm.paths, "default", USER, PASSWORD)
    farm.configure(mode="vpn")
    r = farm.start(farm.resolved(1), network=lab.wan)
    out = wait_ready(farm.docker, r, timeout=12)
    assert not out["ready"] and "cannot find the VPN server" in out["detail"], out
    assert farm.fetch(r.names.net, lab.wan_url) is None
    # the DNS window is open only while a lookup is being attempted (retries back off to once a minute)
    wait_for(lambda: "-A SF-DNS" not in farm.docker.exec(r.names.net, ["iptables", "-S"]).stdout, 20,
             "the DNS window to close again", interval=0.5)


def test_none_mode_has_adb_and_nothing_else(farm, lab):
    farm.configure(mode="none")
    r = farm.start(farm.resolved(2), network=lab.wan)
    assert wait_ready(farm.docker, r, timeout=30) == {"ready": True, "state": "isolated", "detail": ""}
    client = farm.client(r)
    assert farm.fetch(client, lab.wan_url) is None and farm.fetch(client, lab.far_url) is None
    assert adb_echo(r.adb_port) == b"ping-over-adb-port\n"
    assert net_status(farm.docker, r)["killswitch"]["active"]
    assert public_ip(farm.docker, r, url=lab.far_url) is None


def test_a_wiped_firewall_is_noticed_and_put_back(farm, lab):
    """Verify, do not assume: the failure that matters is a rule set that is silently gone."""
    r = vpn_instance(farm, 4)
    assert wait_ready(farm.docker, r, timeout=60)["ready"]
    for cmd in (["iptables", "-P", "OUTPUT", "ACCEPT"], ["iptables", "-F", "OUTPUT"]):
        farm.docker.exec(r.names.net, cmd, timeout=20)
    # seen from the host at once ...
    st = net_status(farm.docker, r)
    assert not st["killswitch"]["active"] and st["killswitch"]["problems"], st["killswitch"]
    out = wait_ready(farm.docker, r, timeout=2)
    assert not out["ready"] and out["state"] == "killswitch_missing"
    assert not farm.docker.exec(r.names.net, ["/sf/healthcheck.sh"], timeout=20, check=False).ok
    # ... and repaired by the sidecar itself within its check interval, pins included
    wait_for(lambda: net_status(farm.docker, r)["killswitch"]["active"], 45, "the sidecar to reload its rules", interval=2)
    assert farm.fetch(r.names.net, lab.wan_url) is None
    assert wait_ready(farm.docker, r, timeout=60)["ready"]
    assert farm.fetch(r.names.net, lab.far_url) == lab.server_far
    wait_for(lambda: farm.docker.inspect(r.names.net)["State"].get("Health", {}).get("Status") == "healthy", 70,
             "Docker to report the sidecar healthy", interval=3)


@pytest.mark.skipif(os.path.exists("/sys/module/wireguard"), reason="this host has WireGuard loaded; the error path cannot be shown")
def test_wireguard_without_kernel_support_says_how_to_fix_it(farm, lab):
    key = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="
    conf = f"[Interface]\nPrivateKey = {key}\nAddress = 10.66.0.2/32\n\n[Peer]\nPublicKey = {key}\nEndpoint = {lab.server_wan}:51820\nAllowedIPs = 0.0.0.0/0\n"
    vpn.import_profile(farm.paths, "lab.conf", conf.encode())
    farm.configure(mode="vpn", vpn={"provider": "wireguard"})
    r = farm.start(farm.resolved(3), network=lab.wan)
    st = wait_for(lambda: (s := net_status(farm.docker, r))["state"] == "error" and s, 30, "the WireGuard error")
    assert "modprobe wireguard" in st["detail"] and not st["ready"]
    assert farm.fetch(r.names.net, lab.wan_url) is None, "a failed tunnel must not open the uplink"
    assert farm.docker.running(r.names.net)


# --------------------------------------------------------------------------- shared gateway

def test_two_androids_share_one_gateway_and_cannot_get_around_it(farm, lab):
    farm.configure(mode="gateway", gateway="lab", gateways={"lab": {"profile": "lab.ovpn"}})
    gw_name = f"{PREFIX}-gw-lab"
    # The gateway dials out over the farm's uplink network; put the throwaway server there.
    uplink = gateway.ensure_uplink(farm.docker, farm.config)
    farm.docker.run(["network", "connect", uplink, SERVER], timeout=60)
    server_ip = farm.docker.inspect(SERVER)["NetworkSettings"]["Networks"][uplink]["IPAddress"]
    vpn.import_profile(farm.paths, "lab.ovpn", lab.profile(server_ip))
    vpn.set_credentials(farm.paths, "default", USER, PASSWORD)

    members = [farm.start(farm.resolved(i)) for i in (11, 12)]          # the farm's own arguments, unmodified
    for r in members:
        out = wait_ready(farm.docker, r, timeout=90)
        assert out["ready"], f"{out}\n{farm.docker.logs(r.names.net, tail=30)}\n{farm.docker.logs(gw_name, tail=30)}"
    clients = [farm.client(r) for r in members]

    gw = gateway.status(farm.docker, farm.config, "lab")
    assert gw["state"] == "connected" and gw["running"] and gw["killswitch"]["active"]
    assert gw["members"] == sorted(r.names.net for r in members)
    for r, client in zip(members, clients, strict=True):
        st = net_status(farm.docker, r)
        assert st["state"] == "connected" and st["gateway"] == "lab" and st["killswitch"]["active"]
        # Android would see what it sees with a VPN: eth0 (uplink, ADB only) plus an interface
        # it does not manage. No second eth<N> for it to run DHCP on.
        links = farm.docker.exec(client, ["ls", "/sys/class/net"]).stdout.split()
        assert sorted(links) == ["eth0", "lo", "sfgw0"], links
        route = farm.docker.exec(client, ["ip", "route", "get", "1.1.1.1"]).stdout
        assert f"via {gw['gateway_ip']} dev sfgw0" in route, route
        table = 1000 + int(farm.docker.exec(client, ["cat", "/sys/class/net/eth0/ifindex"]).out)
        mirrored = farm.docker.exec(client, ["ip", "route", "show", "table", str(table)]).stdout
        assert f"0.0.0.0/1 via {gw['gateway_ip']} dev sfgw0" in mirrored and "128.0.0.0/1" in mirrored, mirrored
        assert farm.fetch(client, lab.far_url) == lab.server_far, "member traffic must leave through the gateway's tunnel"
        # every address is reached through the tunnel, so the service always sees the server
        assert farm.fetch(client, lab.wan_url) == lab.server_wan
        # ... and nothing gets out by the uplink bridge, the only other interface there is
        for target in (lab.wan_url, lab.far_url):
            assert killswitch.probe(farm.docker, r.names.net, target, uplink="eth0")["leak"] is False
        assert adb_echo(r.adb_port) == b"ping-over-adb-port\n"

    # members cannot reach each other's ADB over the shared gateway network
    other_ip = farm.docker.inspect(members[1].names.net)["NetworkSettings"]["Networks"][gw["network"]]["IPAddress"]
    probe = farm.docker.exec(clients[0], ["nc", "-w", "2", "-z", other_ip, "5555"], timeout=15, check=False)
    assert not probe.ok, "one Android reached another Android's ADB port"

    with pytest.raises(ValueError, match="still serves 2 Android"):
        gateway.remove(farm.docker, farm.config, "lab")

    # ---- the gateway goes away: members have no other exit
    farm.docker.stop(gw_name, grace=1, check=True)
    for r, client in zip(members, clients, strict=True):
        assert farm.fetch(client, lab.far_url) is None and farm.fetch(client, lab.wan_url) is None
        assert killswitch.probe(farm.docker, r.names.net, lab.far_url, uplink="eth0")["leak"] is False
    st = wait_for(lambda: (s := net_status(farm.docker, members[0]))["state"] != "connected" and s, 30, "a member to notice")
    assert not st["ready"]

    # ---- and comes back (ensure is what any member start calls)
    assert gateway.ensure(farm.docker, farm.config, farm.paths, "lab")["action"] == "started"
    for r, client in zip(members, clients, strict=True):
        assert wait_ready(farm.docker, r, timeout=90)["ready"]
        assert farm.fetch(client, lab.far_url) == lab.server_far

    own = {n["IPAddress"] for r in members for n in farm.docker.inspect(r.names.net)["NetworkSettings"]["Networks"].values()}
    assert not own & {who for _, who in lab.echo.hits}, f"LEAK recorded by the service: {lab.echo.hits}"
    assert {who for _, who in lab.echo.hits} <= {lab.server_far, lab.server_wan}

    farm.docker.rm(*clients, *(r.names.net for r in members))
    farm.docker.run(["network", "disconnect", uplink, SERVER], timeout=60)
    assert gateway.remove(farm.docker, farm.config, "lab")["removed"] == [gw_name, gw["network"], uplink]


# --------------------------------------------------------------------------- a real Android

ANDROID_IMAGE = os.environ.get("SF_LIVE_ANDROID_IMAGE", "redroid:13-noflag")


def android_fetch(docker: Docker, android: str, host: str, port: int) -> str | None:
    """HTTP GET from inside Android as the unprivileged `shell` user; the body or None."""
    request = f"printf 'GET / HTTP/1.0\\r\\n\\r\\n' | nc -w 4 {host} {port}"
    res = docker.exec(android, ["sh", "-c", request], user="2000", timeout=20, check=False)
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    return lines[-1] if res.ok and lines and " 200 " in lines[0] else None


def adb_hello(port: int) -> bytes:
    """Open the published ADB port and send the protocol's connect packet; returns the
    command name of the answer (b"CNXN" when the daemon accepts, b"AUTH" when it wants a key)."""
    payload = b"host::\x00"
    cmd = 0x4E584E43
    header = struct.pack("<6I", cmd, 0x01000001, 256 * 1024, len(payload), sum(payload), cmd ^ 0xFFFFFFFF)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(header + payload)
        s.settimeout(8)
        return s.recv(4)


needs_android = pytest.mark.skipif(os.environ.get("SF_LIVE_ANDROID") != "1",
                                   reason="set SF_LIVE_ANDROID=1 to boot a real Android (needs ~1.5 GB RAM)")


@contextlib.contextmanager
def booted_android(farm: Farm, r):
    """One Android in r's sidecar namespace, booted; removed (with its data volume) afterwards."""
    if not farm.docker.image_exists(ANDROID_IMAGE):
        pytest.skip(f"Android image {ANDROID_IMAGE} is not present locally")
    android, volume = r.names.android, r.names.volume
    farm.docker.rm(android)
    farm.docker.volume_rm(volume)
    try:
        farm.docker.create_and_start([
            "--name", android, "--privileged", "--memory", "1536m", "--cpus", "2",
            "--label", f"io.superfarm.farm={PREFIX}", "--network", f"container:{r.names.net}",
            "-v", f"{volume}:/data", ANDROID_IMAGE,
            "androidboot.redroid_width=480", "androidboot.redroid_height=854", "androidboot.redroid_dpi=160",
            "androidboot.redroid_gpu_mode=guest"])
        wait_for(lambda: farm.docker.exec(android, ["getprop", "sys.boot_completed"], timeout=15, check=False).out == "1",
                 240, "Android to boot", interval=3)
        time.sleep(15)          # let its network stack settle and do its worst to our routes
        yield android
    finally:
        farm.docker.rm(android)
        farm.docker.volume_rm(volume)


@needs_android
def test_a_real_android_lives_behind_the_tunnel(farm, lab):
    """The reason the route sync exists: Android's network daemon rewrites the policy rules
    and its own routing table in the namespace it shares with the sidecar."""
    r = vpn_instance(farm, 31)
    assert wait_ready(farm.docker, r, timeout=60)["ready"]
    own = farm.docker.inspect(r.names.net)["NetworkSettings"]["Networks"][lab.wan]["IPAddress"]
    with booted_android(farm, r) as android:
        rules = farm.docker.exec(r.names.net, ["ip", "rule", "show"]).stdout
        table = 1000 + int(farm.docker.exec(r.names.net, ["cat", "/sys/class/net/eth0/ifindex"]).out)
        mirrored = farm.docker.exec(r.names.net, ["ip", "route", "show", "table", str(table)]).stdout
        print("ip rule:\n" + rules + f"\ntable {table}:\n" + mirrored)
        assert "lookup main" in rules.splitlines()[1], "rule 100 must be back in place after Android's daemon started"
        assert "unreachable" in rules, "expected Android's own policy rules in the shared namespace"
        assert "0.0.0.0/1 via 10.99.0.1 dev tun0" in mirrored and "128.0.0.0/1 via 10.99.0.1 dev tun0" in mirrored

        # Android's traffic leaves through the tunnel ...
        assert android_fetch(farm.docker, android, lab.far_host, ECHO_PORT) == lab.server_far
        # ... the bridge next to it is closed, ADB works, the sidecar is still healthy
        assert android_fetch(farm.docker, android, lab.wan_host, ECHO_PORT) is None
        assert adb_hello(r.adb_port) in (b"CNXN", b"AUTH", b"STLS")
        st = net_status(farm.docker, r)
        assert st["state"] == "connected" and st["killswitch"]["active"], st

        # ---- tunnel down: Android is cut off, not rerouted
        lab.stop_server()
        wait_for(lambda: net_status(farm.docker, r)["state"] != "connected", 40, "the sidecar to notice")
        assert android_fetch(farm.docker, android, lab.far_host, ECHO_PORT) is None, "LEAK: Android got out around the tunnel"
        assert android_fetch(farm.docker, android, lab.wan_host, ECHO_PORT) is None
        assert adb_hello(r.adb_port) in (b"CNXN", b"AUTH", b"STLS"), "ADB must survive a VPN outage"

        # ---- tunnel back: Android is online again without being restarted
        lab.start_server()
        assert wait_ready(farm.docker, r, timeout=120)["ready"]
        wait_for(lambda: android_fetch(farm.docker, android, lab.far_host, ECHO_PORT) == lab.server_far, 30,
                 "Android to be back online through the tunnel", interval=2)
        assert own not in {who for _, who in lab.echo.hits}, f"LEAK recorded by the service: {lab.echo.hits}"


@needs_android
def test_a_real_android_behind_a_shared_gateway(farm, lab):
    """Android manages every interface named eth<N>. The member's gateway interface is renamed
    for that reason; this checks that Android indeed leaves it alone and gets out through it."""
    farm.configure(mode="gateway", gateway="lab", gateways={"lab": {"profile": "lab.ovpn"}})
    uplink = gateway.ensure_uplink(farm.docker, farm.config)
    farm.docker.run(["network", "connect", uplink, SERVER], timeout=60)
    try:
        server_ip = farm.docker.inspect(SERVER)["NetworkSettings"]["Networks"][uplink]["IPAddress"]
        vpn.import_profile(farm.paths, "lab.ovpn", lab.profile(server_ip))
        vpn.set_credentials(farm.paths, "default", USER, PASSWORD)
        r = farm.start(farm.resolved(32))
        assert wait_ready(farm.docker, r, timeout=90)["ready"], farm.docker.logs(r.names.net, tail=30)
        before = farm.docker.exec(r.names.net, ["ip", "-4", "-o", "addr", "show", "dev", "sfgw0"]).out
        assert before
        with booted_android(farm, r) as android:
            after = farm.docker.exec(r.names.net, ["ip", "-4", "-o", "addr", "show", "dev", "sfgw0"]).out
            assert after == before, "Android changed the gateway interface's address"
            assert sorted(farm.docker.exec(android, ["ls", "/sys/class/net"]).stdout.split()) == ["eth0", "lo", "sfgw0"]
            assert android_fetch(farm.docker, android, lab.far_host, ECHO_PORT) == lab.server_far
            assert adb_hello(r.adb_port) in (b"CNXN", b"AUTH", b"STLS")
            st = net_status(farm.docker, r)
            assert st["state"] == "connected" and st["killswitch"]["active"], st
            # gateway gone: Android has no other way out
            farm.docker.stop(f"{PREFIX}-gw-lab", grace=1, check=True)
            assert android_fetch(farm.docker, android, lab.far_host, ECHO_PORT) is None
            assert adb_hello(r.adb_port) in (b"CNXN", b"AUTH", b"STLS")
        own = {n["IPAddress"] for n in farm.docker.inspect(r.names.net)["NetworkSettings"]["Networks"].values()}
        assert not own & {who for _, who in lab.echo.hits}, f"LEAK recorded by the service: {lab.echo.hits}"
    finally:
        farm.cleanup()
        farm.docker.run(["network", "disconnect", uplink, SERVER], timeout=60, check=False)
        gateway.remove(farm.docker, farm.config, "lab", force=True)

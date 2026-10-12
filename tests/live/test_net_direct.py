"""Live tests of the sidecar in direct mode (no VPN): what an Android gets by default.

    - a second container in the sidecar's network namespace (standing in for the Android)
      reaches the internet;
    - the ADB port is published on 127.0.0.1 and only the host can use it;
    - private addresses (the LAN, the host itself) are out of reach unless allow_lan is set.

Run:  SF_LIVE=1 .venv/bin/python -m pytest tests/live/test_net_direct.py -v
Needs Docker and internet access. Creates only containers named sftnet-* / sftest-net-* and
the network sftest-net-lan, and removes them again.
"""
from __future__ import annotations

import http.server
import ipaddress
import socket
import threading

import pytest

from superfarm.config import Config
from superfarm.core.docker import Docker
from superfarm.core.models import InstanceSpec, resolve
from superfarm.core.store import Store
from superfarm.net import ensure_image, gateway, public_ip, sidecar_run_args, wait_ready
from superfarm.net import status as net_status
from superfarm.paths import get_paths

pytestmark = pytest.mark.live

PREFIX = "sftnet"
LAB = "sftest-net"
ADB_BASE = 17100
LAN_PORT = 17191


@pytest.fixture(scope="module")
def docker() -> Docker:
    d = Docker()
    ok, why = d.available()
    if not ok:
        pytest.skip(f"Docker is not usable: {why}")
    return d


@pytest.fixture(scope="module")
def lan(docker):
    """A stand-in for "a device on your LAN": an HTTP service on a private address of the
    host that containers can reach unless something stops them."""
    name = f"{LAB}-lan"
    docker.network_rm(name)
    subnet = gateway.allocate_subnet("10.214.0.0/16", docker.network_subnets())
    docker.network_create(name, subnet=subnet)
    address = str(ipaddress.ip_network(subnet).network_address + 1)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = self.client_address[0].encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer((address, LAN_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://{address}:{LAN_PORT}/"
    finally:
        server.shutdown()
        server.server_close()
        docker.network_rm(name)


@pytest.fixture
def farm(docker, tmp_path):
    image = ensure_image(docker)
    paths = get_paths(tmp_path / "home").ensure()
    store = Store(paths)
    created: list[str] = []

    def start(instance_id: int, **network):
        config = Config.model_validate({"docker": {"name_prefix": PREFIX}, "network": {"adb_port_base": ADB_BASE, **network}})
        spec = InstanceSpec(id=instance_id)
        store.save(spec)
        r = resolve(spec, config)
        docker.rm(r.names.net)
        docker.create_and_start(sidecar_run_args(r, config, paths, store, docker))     # exactly what the farm runs
        client = f"{LAB}-client-{instance_id}"
        docker.rm(client)
        created.extend([client, r.names.net])
        ready = wait_ready(docker, r, timeout=30)
        docker.create_and_start(["--name", client, "--network", f"container:{r.names.net}", "--no-healthcheck", "--memory", "64m",
                                 "--entrypoint", "sh", image, "-c", "nc -lk -p 5555 -e cat & exec sleep 3600"])
        return r, client, ready, config

    try:
        yield start
    finally:
        docker.rm(*created)


def fetch(docker: Docker, container: str, url: str) -> str | None:
    res = docker.exec(container, ["curl", "-sS", "--max-time", "5", "--", url], timeout=20, check=False)
    return res.out if res.ok else None


def adb_echo(port: int, payload: bytes = b"ping-over-adb-port\n") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(payload)
        s.settimeout(5)
        return s.recv(len(payload))


def test_direct_mode_reaches_the_internet_and_publishes_adb_on_loopback(docker, farm, lan):
    r, client, ready, config = farm(21)
    assert ready == {"ready": True, "state": "direct", "detail": ""}, docker.logs(r.names.net, tail=30)

    # the internet, from the "Android's" side of the namespace
    address = public_ip(docker, r, url=config.network.vpn.public_ip_url)
    assert address and ipaddress.ip_address(address).is_global, "no internet from the sidecar's namespace (is the host online?)"
    assert fetch(docker, client, "https://example.com/") is not None

    # ADB: published on loopback, answered through the sidecar's firewall
    assert adb_echo(r.adb_port) == b"ping-over-adb-port\n"
    port = docker.inspect(r.names.net)["NetworkSettings"]["Ports"]["5555/tcp"][0]
    assert port == {"HostIp": "127.0.0.1", "HostPort": str(ADB_BASE + 21)}

    # ... and not reachable by a neighbour on the same Docker bridge (ADB has no authentication)
    own = docker.inspect(r.names.net)["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
    neighbour = docker.run(["run", "--rm", "--name", f"{LAB}-neighbour", "--no-healthcheck", "--entrypoint", "nc",
                            ensure_image(docker), "-w", "2", "-z", own, "5555"], timeout=30, check=False)
    assert not neighbour.ok, "another container reached this Android's ADB port"

    # private addresses are out of reach by default: neither your LAN nor the host's own services
    assert fetch(docker, client, lan) is None

    st = net_status(docker, r)
    assert st["mode"] == "direct" and st["state"] == "direct" and st["ready"]
    assert st["killswitch"]["guarded"] and not st["killswitch"]["problems"]
    assert st["public_ip"] == address                      # cached by the call above
    # the sidecar reports healthy to Docker as well
    assert docker.exec(r.names.net, ["/sf/healthcheck.sh"], timeout=20, check=False).ok


def test_allow_lan_opens_private_addresses(docker, farm, lan):
    r, client, ready, _ = farm(22, allow_lan=True)
    assert ready["ready"]
    own = docker.inspect(r.names.net)["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
    assert fetch(docker, client, lan) == own
    assert adb_echo(r.adb_port) == b"ping-over-adb-port\n"

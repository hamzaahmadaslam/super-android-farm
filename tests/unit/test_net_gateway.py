"""Shared VPN gateways: address planning (pure) and the Docker choreography (scripted CLI)."""
import contextlib

import pytest
from net_fakes import OVPN, PREFIX, FakeDocker, at_once, env_of, make_config, option_values

from superfarm.net import gateway, sidecar, vpn


@pytest.fixture
def farm(farm_home, monkeypatch, tmp_path):
    """A farm with one profile, credentials, a defined gateway and an already built image."""
    tun = tmp_path / "tun"
    tun.write_text("")
    monkeypatch.setattr(sidecar, "TUN_DEVICE", str(tun))       # unit tests must not depend on the host's devices
    vpn.import_profile(farm_home, "exit.ovpn", OVPN.encode())
    vpn.set_credentials(farm_home, "default", "user", "hunter2-not-real")
    config = make_config(gateways={"eu": {"profile": "exit.ovpn"}})
    docker = FakeDocker()
    docker.images.add(sidecar.image_ref())
    return config, farm_home, docker


# ------------------------------------------------------------------ addresses

def test_allocation_skips_everything_docker_already_uses():
    used = ["172.17.0.0/16", "10.213.0.0/24", "10.213.1.0/25", "fd00:dead::/64", "", "garbage", "10.213.2.7/24"]
    assert gateway.allocate_subnet("10.213.0.0/16", used) == "10.213.3.0/24"
    assert gateway.allocate_subnet("10.213.0.0/16", []) == "10.213.0.0/24"
    # a larger network that covers the start of the pool blocks all of it
    assert gateway.allocate_subnet("10.213.0.0/16", ["10.213.0.0/17"]) == "10.213.128.0/24"


def test_allocation_errors_are_actionable():
    with pytest.raises(ValueError, match="no free /24"):
        gateway.allocate_subnet("10.213.0.0/24", ["10.213.0.0/16"])
    with pytest.raises(ValueError, match="not a network"):
        gateway.allocate_subnet("10.213.0.0/33", [])
    with pytest.raises(ValueError, match="/24 or larger"):
        gateway.allocate_subnet("10.213.0.0/28", [])
    with pytest.raises(ValueError, match="IPv4"):
        gateway.allocate_subnet("fd00::/48", [])


def test_fixed_addresses_inside_a_gateway_subnet():
    assert gateway.addresses("10.213.5.0/24") == {
        "subnet": "10.213.5.0/24", "bridge": "10.213.5.1", "gateway_ip": "10.213.5.2", "member_range": "10.213.5.128/25"}
    for bad in ("10.213.5.0/23", "8.8.8.0/24", "10.213.5.7/24", "nonsense", "fd00::/64"):
        with pytest.raises(ValueError):
            gateway.check_subnet(bad)


@pytest.mark.parametrize("bad", ["", "EU", "a b", "-x", "x" * 40, "a/b", "a;b"])
def test_gateway_names_are_plain(bad):
    with pytest.raises(ValueError):
        gateway.check_name(bad)


# ------------------------------------------------------------------ the gateway container

def test_run_args_describe_a_forwarding_vpn_client(farm):
    config, paths, _ = farm
    args = gateway.run_args(config, paths, "eu", subnet="10.213.0.0/24", image="superfarm/net:test", uplink_gw="172.18.0.1")
    assert args[args.index("--name") + 1] == f"{PREFIX}-gw-eu"
    assert option_values(args, "--network") == [f"{PREFIX}-bridge", f"name={PREFIX}-gwnet-eu,ip=10.213.0.2"]
    # Docker publishes ports through the network whose name sorts first: that must be the uplink
    assert gateway.uplink_network(PREFIX) < f"{PREFIX}-gwnet-0" and gateway.uplink_network("z9") < "z9-gwnet-a"
    env = env_of(args)
    assert env["SF_MODE"] == "gwserver" and env["SF_GATEWAY_IP"] == "10.213.0.2" and env["SF_KILLSWITCH"] == "1"
    assert env["SF_UPLINK_GW"] == "172.18.0.1"
    assert env["SF_VPN_PROVIDER"] == "openvpn" and env["SF_PROFILE_NAME"] == "exit.ovpn"
    assert "net.ipv4.ip_forward=1" in option_values(args, "--sysctl")
    assert option_values(args, "--cap-drop") == ["ALL"] and "NET_ADMIN" in option_values(args, "--cap-add")
    assert "--privileged" not in args and "-p" not in args          # a gateway publishes nothing
    mounts = option_values(args, "--mount")
    assert any(m.endswith("target=/sf/vpn/profile.ovpn,readonly") for m in mounts)
    assert any(m.endswith("target=/sf/secrets/auth,readonly") for m in mounts)
    assert "hunter2-not-real" not in " ".join(args)
    assert args[-1] == "superfarm/net:test"
    labels = dict(v.split("=", 1) for v in option_values(args, "--label"))
    assert labels == {"io.superfarm.farm": PREFIX, "io.superfarm.role": "gateway", "io.superfarm.gateway": "eu"}


def test_run_args_refuse_what_cannot_work(farm):
    config, paths, _ = farm
    with pytest.raises(ValueError, match="not defined"):
        gateway.run_args(config, paths, "asia", subnet="10.213.0.0/24", image="i", uplink_gw="172.18.0.1")
    vpn.delete_credentials(paths, "default")
    with pytest.raises(ValueError, match="sf vpn credentials set default"):
        gateway.run_args(config, paths, "eu", subnet="10.213.0.0/24", image="i", uplink_gw="172.18.0.1")
    wrong = make_config(gateways={"eu": {"profile": "exit.ovpn", "provider": "wireguard"}})
    with pytest.raises(ValueError, match="not a wireguard profile"):
        gateway.run_args(wrong, paths, "eu", subnet="10.213.0.0/24", image="i", uplink_gw="172.18.0.1")


def test_ensure_creates_the_networks_and_the_container_once(farm):
    config, paths, docker = farm
    docker.networks["bridge"] = {"Name": "bridge", "IPAM": {"Config": [{"Subnet": "10.213.0.0/24"}]}, "Labels": {}}
    out = gateway.ensure(docker, config, paths, "eu")
    assert out == {"name": "eu", "container": f"{PREFIX}-gw-eu", "network": f"{PREFIX}-gwnet-eu",
                   "uplink": f"{PREFIX}-bridge", "uplink_gw": "172.19.0.1", "subnet": "10.213.1.0/24",
                   "gateway_ip": "10.213.1.2", "action": "created"}
    uplink, create = docker.called("network", "create")
    assert uplink[-1] == f"{PREFIX}-bridge" and "--internal" not in uplink, "the uplink carries the VPN transport"
    # no NAT and no host address: the host is not a router on the gateway network. Not
    # --internal: Docker would then drop what members send to the gateway for forwarding.
    assert "--internal" not in create
    assert set(option_values(create, "-o")) == {"com.docker.network.bridge.enable_ip_masquerade=false",
                                                "com.docker.network.bridge.inhibit_ipv4=true"}
    assert create[create.index("--subnet") + 1] == "10.213.1.0/24"
    assert create[create.index("--ip-range") + 1] == "10.213.1.128/25", "members must never be handed the gateway's address"
    assert create[create.index("--gateway") + 1] == "10.213.1.1"
    assert docker.containers[f"{PREFIX}-gw-eu"]["labels"]["io.superfarm.plan"]

    again = gateway.ensure(docker, config, paths, "eu")
    assert again["action"] == "unchanged" and again["subnet"] == "10.213.1.0/24"
    assert len(docker.called("run", "-d")) == 1 and len(docker.called("network", "create")) == 2


def test_ensure_starts_a_stopped_gateway_and_recreates_a_changed_one(farm):
    config, paths, docker = farm
    gateway.ensure(docker, config, paths, "eu")
    docker.containers[f"{PREFIX}-gw-eu"]["running"] = False
    assert gateway.ensure(docker, config, paths, "eu")["action"] == "started"
    # the profile's content changed: same arguments, but the container must be rebuilt
    vpn.import_profile(paths, "exit.ovpn", OVPN.replace("vpn1.example.net", "vpn2.example.net").encode())
    assert gateway.ensure(docker, config, paths, "eu")["action"] == "recreated"
    assert gateway.ensure(docker, config, paths, "eu")["action"] == "unchanged"


def test_ensure_honours_a_configured_subnet(farm):
    _, paths, docker = farm
    config = make_config(gateways={"eu": {"profile": "exit.ovpn", "subnet": "10.99.7.0/24"}})
    assert gateway.ensure(docker, config, paths, "eu")["gateway_ip"] == "10.99.7.2"


def test_ensure_refuses_old_docker_and_foreign_objects(farm):
    config, paths, docker = farm
    docker.version = "24.0.7"
    with pytest.raises(RuntimeError, match="Docker 25 or newer"):
        gateway.ensure(docker, config, paths, "eu")
    docker.version = "29.1.3"
    docker.networks[f"{PREFIX}-gwnet-eu"] = {"Name": "x", "Internal": False, "Labels": {}, "IPAM": {"Config": [{"Subnet": "10.1.1.0/24"}]}}
    with pytest.raises(RuntimeError, match="not created by this farm"):
        gateway.ensure(docker, config, paths, "eu")
    docker.networks[f"{PREFIX}-gwnet-eu"] = {"Name": "x", "Internal": False, "Labels": {"io.superfarm.farm": PREFIX},
                                             "Options": {}, "IPAM": {"Config": [{"Subnet": "10.1.1.0/24"}]}}
    with pytest.raises(RuntimeError, match="not set up as a gateway network"):
        gateway.ensure(docker, config, paths, "eu")
    docker.networks[f"{PREFIX}-bridge"] = {"Name": "x", "Internal": False, "Labels": {"io.superfarm.farm": "other"},
                                           "IPAM": {"Config": [{"Subnet": "10.1.2.0/24"}]}}
    with pytest.raises(RuntimeError, match="bridge exists but was not created by this farm"):
        gateway.ensure(docker, config, paths, "eu")
    assert not docker.called("run", "-d")


def assert_one_gateway(docker, results):
    assert sorted(str(r.get("action") if isinstance(r, dict) else r) for r in results) == ["created"] + ["unchanged"] * (len(results) - 1)
    assert len({(r["subnet"], r["gateway_ip"], r["uplink_gw"]) for r in results}) == 1
    assert len(docker.called("run", "-d")) == 1 and len(docker.called("network", "create")) == 2
    assert list(docker.containers) == [f"{PREFIX}-gw-eu"] and set(docker.networks) == {f"{PREFIX}-bridge", f"{PREFIX}-gwnet-eu"}


def test_bringing_a_gateway_up_twice_at_once_creates_it_once(farm):
    """`up` pressed twice, or several Androids of one gateway started together."""
    config, paths, docker = farm
    docker.delay = 0.002
    assert_one_gateway(docker, at_once(*[lambda: gateway.ensure(docker, config, paths, "eu")] * 4))


def test_two_farm_processes_bringing_a_gateway_up_create_it_once(farm, monkeypatch):
    """A second daemon started by mistake on the same farm home shares no memory with the
    first one; the lock both of them take is a file in that home."""
    config, paths, docker = farm
    docker.delay = 0.002
    monkeypatch.setattr(gateway, "_lock", contextlib.nullcontext())      # every caller its own process
    assert_one_gateway(docker, at_once(*[lambda: gateway.ensure(docker, config, paths, "eu")] * 4))


def test_a_gateway_that_could_not_start_is_not_created_a_second_time(farm):
    config, paths, docker = farm
    docker.start_fails = "docker: Error response from daemon: error gathering device information: no such device"
    with pytest.raises(RuntimeError, match="no such device"):
        gateway.ensure(docker, config, paths, "eu")
    with pytest.raises(RuntimeError, match="no such device"):               # still broken: same answer, nothing piles up
        gateway.ensure(docker, config, paths, "eu")
    docker.start_fails = ""
    # the leftover is this farm's own, with the current plan: it is started, not duplicated
    assert gateway.ensure(docker, config, paths, "eu")["action"] == "started"
    assert len(docker.called("run", "-d")) == 1 and len(docker.called("network", "create")) == 2
    assert docker.containers[f"{PREFIX}-gw-eu"]["running"] and list(docker.containers) == [f"{PREFIX}-gw-eu"]


def test_member_brings_the_gateway_up_and_says_where_to_join(farm):
    config, paths, docker = farm
    assert gateway.member(docker, config, paths, "eu") == {
        "network": f"{PREFIX}-gwnet-eu", "uplink": f"{PREFIX}-bridge", "uplink_gw": "172.18.0.1",
        "gateway_ip": "10.213.0.2", "subnet": "10.213.0.0/24"}
    assert docker.containers[f"{PREFIX}-gw-eu"]["running"]


def test_remove_refuses_while_androids_are_attached(farm):
    config, paths, docker = farm
    gateway.ensure(docker, config, paths, "eu")
    net = docker.networks[f"{PREFIX}-gwnet-eu"]
    net["Containers"] = {"a": {"Name": f"{PREFIX}-gw-eu"}, "b": {"Name": f"{PREFIX}-net-4"}}
    assert gateway.members(docker, config, "eu") == [f"{PREFIX}-net-4"]
    with pytest.raises(ValueError, match="still serves 1 Android"):
        gateway.remove(docker, config, "eu")
    assert f"{PREFIX}-gw-eu" in docker.containers
    net["Containers"] = {"a": {"Name": f"{PREFIX}-gw-eu"}}
    assert gateway.remove(docker, config, "eu")["removed"] == [f"{PREFIX}-gw-eu", f"{PREFIX}-gwnet-eu", f"{PREFIX}-bridge"]
    assert not docker.containers and not docker.networks
    assert gateway.remove(docker, config, "eu")["removed"] == []       # removing twice is not an error


def test_down_pressed_twice_and_up_against_down_leave_nothing_half_done(farm):
    config, paths, docker = farm
    everything = ({f"{PREFIX}-gw-eu"}, {f"{PREFIX}-bridge", f"{PREFIX}-gwnet-eu"})
    gateway.ensure(docker, config, paths, "eu")
    docker.delay = 0.002
    first, second = at_once(*[lambda: gateway.remove(docker, config, "eu")] * 2)
    assert set(first["removed"] + second["removed"]) == everything[0] | everything[1]
    assert len(first["removed"] + second["removed"]) == 3, "nothing is removed twice"
    assert not docker.containers and not docker.networks
    # up and down at the same moment: whichever comes second wins, as a whole
    up, down = at_once(lambda: gateway.ensure(docker, config, paths, "eu"), lambda: gateway.remove(docker, config, "eu"))
    assert up["action"] == "created" and isinstance(down, dict), (up, down)
    assert (set(docker.containers), set(docker.networks)) in ((set(), set()), everything)


def test_remove_never_touches_objects_of_another_farm(farm):
    config, _, docker = farm
    docker.containers[f"{PREFIX}-gw-eu"] = {"labels": {"io.superfarm.farm": "other"}, "running": True, "args": []}
    with pytest.raises(RuntimeError, match="not created by this farm"):
        gateway.remove(docker, config, "eu")
    assert f"{PREFIX}-gw-eu" in docker.containers


def test_status_of_a_missing_gateway_does_not_raise(farm):
    config, paths, docker = farm
    out = gateway.status(docker, config, "eu")
    assert out["state"] == "missing" and not out["running"] and out["defined"] and out["members"] == []
    gateway.ensure(docker, config, paths, "eu")
    docker.containers[f"{PREFIX}-gw-eu"]["running"] = False
    out = gateway.status(docker, config, "eu")
    assert out["state"] == "stopped" and out["gateway_ip"] == "10.213.0.2"

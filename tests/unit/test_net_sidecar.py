"""The sidecar container: image handling and the `docker run` arguments for every mode."""
import re

import pytest
from net_fakes import OVPN, PREFIX, WG, FakeDocker, at_once, env_of, make_config, make_resolved, option_values

from superfarm.core.models import NET_IMAGE, InstanceSpec
from superfarm.core.store import Store
from superfarm.net import ensure_image, sidecar, sidecar_run_args, vpn
from superfarm.paths import REPO_DIR

SECRET = "hunter2-not-real"


@pytest.fixture
def farm(farm_home, monkeypatch, tmp_path):
    tun = tmp_path / "tun"
    tun.write_text("")
    monkeypatch.setattr(sidecar, "TUN_DEVICE", str(tun))
    monkeypatch.setattr(sidecar, "_reservations", vpn.Reservations())
    docker = FakeDocker()
    docker.images.add(sidecar.image_ref())
    return farm_home, Store(farm_home), docker


def args_for(farm, config, instance_id=1, **network):
    paths, store, docker = farm
    if not store.exists(instance_id):
        store.save(InstanceSpec(id=instance_id))            # the farm only counts Androids that exist
    return sidecar_run_args(make_resolved(config, instance_id, **network), config, paths, store, docker)


# ------------------------------------------------------------------ image

def test_the_image_tag_follows_the_build_inputs(tmp_path):
    for name in sidecar.CONTEXT_FILES:
        (tmp_path / name).write_text(name)
    first = sidecar.context_hash(tmp_path)
    assert first == sidecar.context_hash(tmp_path) and re.fullmatch(r"[0-9a-f]{64}", first)
    (tmp_path / "killswitch.sh").write_text("changed")
    assert sidecar.context_hash(tmp_path) != first
    assert sidecar.image_ref(tmp_path) == f"{NET_IMAGE}:{sidecar.context_hash(tmp_path)[:12]}"


def test_the_shipped_build_context_is_complete_and_free_of_buildkit_only_syntax():
    for name in sidecar.CONTEXT_FILES:
        assert (sidecar.NET_CONTEXT / name).is_file(), name
    dockerfile = (sidecar.NET_CONTEXT / "Dockerfile").read_text()
    assert re.search(r"^ARG ALPINE_VERSION=\d+\.\d+(\.\d+)?$", dockerfile, re.M), "the base image must be pinned"
    assert "COPY --chmod" not in dockerfile and "RUN --mount" not in dockerfile
    assert "apk add" in dockerfile and "apk add" not in (sidecar.NET_CONTEXT / "entrypoint.sh").read_text()


def test_ensure_image_builds_once_and_only_when_missing():
    docker = FakeDocker()
    ref = ensure_image(docker)
    assert ref == sidecar.image_ref() and ref in docker.images and f"{NET_IMAGE}:latest" in docker.images
    build = docker.called("build")[0]
    assert build[-1] == str(sidecar.NET_CONTEXT) and f"SF_CONTEXT_HASH={sidecar.context_hash()}" in build
    assert ensure_image(docker) == ref and len(docker.called("build")) == 1
    ensure_image(docker, rebuild=True)
    assert len(docker.called("build")) == 2


def test_a_failed_build_says_what_it_needs():
    docker = FakeDocker()
    docker.build_fails = True
    with pytest.raises(RuntimeError, match="internet access"):
        ensure_image(docker)


# ------------------------------------------------------------------ direct, none, proxy

def test_direct_sidecar(farm):
    config = make_config()
    args = args_for(farm, config, 7)
    assert args[args.index("--name") + 1] == f"{PREFIX}-net-7"
    labels = dict(v.split("=", 1) for v in option_values(args, "--label"))
    assert labels == {"io.superfarm.farm": PREFIX, "io.superfarm.instance": "7", "io.superfarm.role": "net",
                      "io.superfarm.net.mode": "direct"}
    # ADB: on loopback only, on the sidecar itself
    assert option_values(args, "-p") == ["127.0.0.1:17107:5555/tcp"]
    env = env_of(args)
    assert env["SF_MODE"] == "direct" and env["SF_ADB_FROM"] == "gateway" and env["SF_DNS"] == "1.1.1.1,8.8.8.8"
    # least privilege: no tunnel device, no file-reading capability, never privileged
    assert option_values(args, "--cap-drop") == ["ALL"]
    assert option_values(args, "--cap-add") == ["NET_ADMIN", "NET_RAW"]
    assert "--privileged" not in args and "--device" not in args and "--mount" not in args
    assert "no-new-privileges" in option_values(args, "--security-opt")
    assert "net.ipv6.conf.all.disable_ipv6=1" in option_values(args, "--sysctl")
    assert option_values(args, "--restart") == ["no"] and "--network" not in args
    assert option_values(args, "--log-opt") == ["max-size=10m", "max-file=2"]
    assert args[-1] == sidecar.image_ref()


def test_arguments_are_deterministic(farm):
    """The instance manager hashes them to decide whether containers must be recreated."""
    config = make_config()
    assert args_for(farm, config) == args_for(farm, config)
    assert args_for(farm, config) != args_for(farm, make_config(allow_lan=True))


def test_none_and_proxy_modes(farm):
    none = args_for(farm, make_config(mode="none"))
    assert env_of(none)["SF_MODE"] == "none" and option_values(none, "-p"), "ADB stays published without a network"
    proxy = args_for(farm, make_config(mode="proxy", proxy="10.0.0.9:3128"))
    assert env_of(proxy)["SF_MODE"] == "direct"
    assert "io.superfarm.net.mode=proxy" in option_values(proxy, "--label")


def test_ipv6_and_remote_adb_are_opt_in(farm):
    args = args_for(farm, make_config(ipv6=True, adb_bind="0.0.0.0"))
    assert env_of(args)["SF_IPV6"] == "1" and env_of(args)["SF_ADB_FROM"] == "any"
    assert not option_values(args, "--sysctl") and option_values(args, "-p") == ["0.0.0.0:17101:5555/tcp"]


def test_publish_argument():
    assert sidecar.publish_arg("localhost", 17105) == "127.0.0.1:17105:5555/tcp"
    assert sidecar.publish_arg("::1", 17105) == "[::1]:17105:5555/tcp"
    with pytest.raises(ValueError, match="not an IP address"):
        sidecar.publish_arg("my-host", 17105)


# ------------------------------------------------------------------ vpn

def vpn_farm(farm, **vpn_cfg):
    paths, _, _ = farm
    for name in ("a.ovpn", "b.ovpn", "c.ovpn"):
        vpn.import_profile(paths, name, OVPN.encode())
    vpn.set_credentials(paths, "default", "user", SECRET)
    return make_config(mode="vpn", vpn=vpn_cfg)


def test_vpn_sidecar_mounts_a_sanitised_copy_and_the_credentials(farm):
    paths, store, _ = farm
    config = vpn_farm(farm, selection="fixed", profile="b.ovpn", extra_args=["--verb", "4"])
    (paths.vpn / "b.ovpn").write_text(OVPN + "up /bin/evil\n")            # edited by hand after import
    args = args_for(farm, config, 3)
    env = env_of(args)
    assert env["SF_MODE"] == "vpn" and env["SF_VPN_PROVIDER"] == "openvpn" and env["SF_PROFILE_NAME"] == "b.ovpn"
    assert (env["SF_VPN_PING"], env["SF_VPN_PING_RESTART"], env["SF_VPN_CONNECT_RETRY_MAX"]) == ("10", "120", "5")
    assert env["SF_KILLSWITCH"] == "1" and env["SF_DNS_LEAK_PROTECTION"] == "1"
    runtime = paths.instance(3) / "net" / "profile.ovpn"
    assert option_values(args, "--mount") == [
        f"type=bind,source={runtime},target=/sf/vpn/profile.ovpn,readonly",
        f"type=bind,source={paths.secrets / 'vpn-default.auth'},target=/sf/secrets/auth,readonly",
    ]
    assert "up /bin/evil" not in runtime.read_text(), "the container never sees the stored file, only the sanitised copy"
    assert option_values(args, "--device") == [sidecar.TUN_DEVICE]
    assert option_values(args, "--cap-add") == ["NET_ADMIN", "NET_RAW", "DAC_READ_SEARCH"]
    # extra openvpn arguments travel as arguments after the image, never through a shell
    image = args.index(sidecar.image_ref())
    assert args[image + 1:] == ["--verb", "4"]
    assert store.state_get(3, "vpn_profile") == "b.ovpn"
    assert "io.superfarm.net.profile=b.ovpn" in option_values(args, "--label")


def test_secrets_never_appear_in_the_arguments(farm):
    config = vpn_farm(farm)
    text = "\n".join(args_for(farm, config))
    assert SECRET not in text and "BEGIN PRIVATE KEY" not in text and "user\n" not in text


def test_the_profile_choice_is_recorded_and_sticky(farm):
    paths, store, _ = farm
    config = vpn_farm(farm)                                        # round-robin
    picked = [env_of(args_for(farm, config, i))["SF_PROFILE_NAME"] for i in (1, 2, 3, 4)]
    assert picked == ["a.ovpn", "b.ovpn", "c.ovpn", "a.ovpn"]
    assert [store.state_get(i, "vpn_profile") for i in (1, 2, 3, 4)] == picked
    # starting again keeps the exit, even after the selection policy changed
    assert env_of(args_for(farm, vpn_farm(farm, selection="random"), 2))["SF_PROFILE_NAME"] == "b.ovpn"
    # ... until the operator clears the choice or pins another profile
    store.state_set(2, "vpn_profile", None)
    assert env_of(args_for(farm, config, 2))["SF_PROFILE_NAME"] == "b.ovpn"       # least used again
    assert env_of(args_for(farm, config, 2, vpn_profile="c.ovpn"))["SF_PROFILE_NAME"] == "c.ovpn"
    assert store.state_get(2, "vpn_profile") == "c.ovpn"


def test_unique_selection_respects_running_androids_and_parallel_starts(farm):
    paths, store, docker = farm
    config = vpn_farm(farm, selection="unique")
    first = env_of(args_for(farm, config, 1))["SF_PROFILE_NAME"]
    # instance 1 has no container yet (a bulk start is in flight): 2 must still get another one
    second = env_of(args_for(farm, config, 2))["SF_PROFILE_NAME"]
    assert {first, second} == {"a.ovpn", "b.ovpn"}
    for i in (1, 2):
        docker.containers[f"{PREFIX}-net-{i}"] = {"running": True, "args": [], "labels": {
            "io.superfarm.farm": PREFIX, "io.superfarm.role": "net", "io.superfarm.instance": str(i)}}
    assert env_of(args_for(farm, config, 3))["SF_PROFILE_NAME"] == "c.ovpn"
    with pytest.raises(ValueError, match="in use by running Androids"):
        args_for(farm, config, 4)


def test_vpn_start_problems_are_explained(farm):
    paths, _, _ = farm
    with pytest.raises(ValueError, match="sf vpn import"):
        args_for(farm, make_config(mode="vpn"))
    config = vpn_farm(farm)
    with pytest.raises(ValueError, match="pinned VPN profile 'zz.ovpn' does not exist"):
        args_for(farm, config, vpn_profile="zz.ovpn")
    vpn.delete_credentials(paths, "default")
    with pytest.raises(ValueError, match="sf vpn credentials default"):
        args_for(farm, config)


def test_a_missing_tunnel_device_is_reported_before_docker_is(farm, monkeypatch):
    config = vpn_farm(farm)
    monkeypatch.setattr(sidecar, "TUN_DEVICE", "/nonexistent/net/tun")
    with pytest.raises(ValueError, match="modprobe tun"):
        args_for(farm, config)


def test_wireguard_sidecar(farm):
    paths, _, _ = farm
    vpn.import_profile(paths, "wg.conf", WG.encode())
    args = args_for(farm, make_config(mode="vpn", vpn={"provider": "wireguard"}))
    env = env_of(args)
    assert env["SF_VPN_PROVIDER"] == "wireguard" and env["SF_PROFILE_NAME"] == "wg.conf"
    assert option_values(args, "--mount")[0].endswith("target=/sf/vpn/profile.conf,readonly")
    assert "net.ipv4.conf.all.src_valid_mark=1" in option_values(args, "--sysctl")
    assert "--device" not in args, "WireGuard lives in the kernel and needs no tun device"
    assert args[-1] == sidecar.image_ref()
    # the configured provider decides which profiles are candidates; a pin overrides it
    vpn.import_profile(paths, "a.ovpn", OVPN.replace("auth-user-pass\n", "").encode())
    pinned = args_for(farm, make_config(mode="vpn", vpn={"provider": "wireguard"}), vpn_profile="a.ovpn")
    assert env_of(pinned)["SF_VPN_PROVIDER"] == "openvpn"


@pytest.mark.parametrize("extra", [["--up", "/bin/sh"], ["--script-security", "3"], ["--plugin", "x.so"], ["--config", "/etc/x"],
                                   ["--management", "0.0.0.0", "7505"], ["--log", "/x"], ["verb", "4"], ["--verb", "4\n--up x"]])
def test_extra_openvpn_arguments_cannot_run_programs(farm, extra):
    with pytest.raises(ValueError, match="network.vpn.extra_args"):
        args_for(farm, vpn_farm(farm, extra_args=extra))


# ------------------------------------------------------------------ gateway member

def test_gateway_member_joins_both_networks_and_brings_the_gateway_up(farm):
    paths, _, docker = farm
    vpn.import_profile(paths, "exit.ovpn", OVPN.encode())
    vpn.set_credentials(paths, "default", "user", SECRET)
    config = make_config(mode="gateway", gateway="eu", gateways={"eu": {"profile": "exit.ovpn"}})
    args = args_for(farm, config, 5)
    # Docker refuses its built-in bridge next to another network, so the farm's own uplink is used
    assert option_values(args, "--network") == [f"{PREFIX}-bridge", f"{PREFIX}-gwnet-eu"]
    env = env_of(args)
    assert env["SF_MODE"] == "gateway" and env["SF_GATEWAY_IP"] == "10.213.0.2" and env["SF_KILLSWITCH"] == "1"
    assert env["SF_UPLINK_GW"] == "172.18.0.1"          # how the sidecar tells its two networks apart
    assert option_values(args, "-p") == ["127.0.0.1:17105:5555/tcp"]
    assert "--device" not in args and "--mount" not in args, "a member holds no VPN material at all"
    assert docker.containers[f"{PREFIX}-gw-eu"]["running"] and f"{PREFIX}-gwnet-eu" in docker.networks
    assert SECRET not in " ".join(args)


# ------------------------------------------------------------------ started twice at once

def test_two_starts_of_one_android_at_once_agree_and_build_the_image_once(farm):
    """Start pressed twice. Both calls must describe the same container: the instance manager
    hashes these arguments, and Docker lets only one container have the name."""
    paths, store, docker = farm
    config = vpn_farm(farm, selection="unique")
    docker.images.clear()                                # first start ever: the image is not built yet
    docker.delay = 0.002
    store.save(InstanceSpec(id=1))
    r = make_resolved(config, 1)
    results = at_once(*[lambda: sidecar_run_args(r, config, paths, store, docker)] * 4)
    assert all(isinstance(args, list) for args in results), results
    assert all(args == results[0] for args in results)
    assert len(docker.called("build")) == 1
    profile = env_of(results[0])["SF_PROFILE_NAME"]
    assert store.state_get(1, "vpn_profile") == profile
    assert (paths.instance(1) / "net" / "profile.ovpn").read_text() == vpn.load_profile(paths, profile).text
    assert sorted(f.name for f in (paths.instance(1) / "net").iterdir()) == ["profile.ovpn"], "no half-written copies left"
    assert not docker.called("run", "-d") and not docker.networks, "preparing a start creates no container"


def test_androids_of_one_gateway_starting_at_once_bring_it_up_once(farm):
    paths, store, docker = farm
    vpn.import_profile(paths, "exit.ovpn", OVPN.encode())
    vpn.set_credentials(paths, "default", "user", SECRET)
    config = make_config(mode="gateway", gateway="eu", gateways={"eu": {"profile": "exit.ovpn"}})
    docker.delay = 0.002
    for i in (1, 2):
        store.save(InstanceSpec(id=i))
    first, again, other = at_once(*[lambda i=i: sidecar_run_args(make_resolved(config, i), config, paths, store, docker)
                                    for i in (1, 1, 2)])
    assert first == again and isinstance(other, list)
    for args in (first, other):
        assert option_values(args, "--network") == [f"{PREFIX}-bridge", f"{PREFIX}-gwnet-eu"]
        assert env_of(args)["SF_GATEWAY_IP"] == "10.213.0.2"
    assert len(docker.called("run", "-d")) == 1 and list(docker.containers) == [f"{PREFIX}-gw-eu"]
    assert sorted(docker.networks) == [f"{PREFIX}-bridge", f"{PREFIX}-gwnet-eu"]


# ------------------------------------------------------------------ small helpers

def test_bind_mount_refuses_paths_docker_would_misparse(tmp_path):
    assert sidecar.bind_mount(tmp_path / "f", "/x") == ["--mount", f"type=bind,source={tmp_path / 'f'},target=/x,readonly"]
    for bad in ("a,b", 'a"b', "a\nb"):
        with pytest.raises(ValueError, match="SUPERFARM_HOME"):
            sidecar.bind_mount(tmp_path / bad, "/x")


def test_env_values_are_single_line():
    assert sidecar.env_args({"B": "2", "A": "1"}) == ["-e", "A=1", "-e", "B=2"]
    with pytest.raises(ValueError):
        sidecar.env_args({"A": "1\nSF_KILLSWITCH=0"})


def test_nothing_personal_or_app_specific_in_the_network_code():
    """The project rule, checked for this area: no private names, no app names, no home paths."""
    words = ["".join(p) for p in (("bi", "go"), ("cla", "ude"), ("anthr", "opic"), ("ham", "za"), ("/home", "/"), ("/run", "/media"))]
    files = [*(REPO_DIR / "superfarm" / "net").glob("*.py"), *(REPO_DIR / "deploy" / "docker" / "net").iterdir(),
             *(REPO_DIR / "tests" / "unit").glob("*net_*.py"), *(REPO_DIR / "tests" / "live").glob("test_net_*.py")]
    assert len(files) >= 15
    for f in files:
        text = f.read_text(errors="replace").lower()
        for w in words:
            assert w not in text, f"{f.name} contains {w!r}"

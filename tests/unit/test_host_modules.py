"""Kernel module state against a fake /proc, /sys and /lib/modules."""
import gzip

from superfarm.config import Config
from superfarm.host import modules

REL = "6.8.0-test"


def fake_root(tmp_path, *, loaded=(), on_disk=(), builtin=(), modprobe="/sbin/modprobe\n", filesystems=(),
              config=None, sys_modules=()):
    (tmp_path / "proc/sys/kernel").mkdir(parents=True)
    (tmp_path / "proc/sys/kernel/osrelease").write_text(REL + "\n")
    (tmp_path / "proc/sys/kernel/modprobe").write_text(modprobe)
    (tmp_path / "proc/modules").write_text("".join(f"{m} 16384 0 - Live 0x0\n" for m in loaded))
    (tmp_path / "proc/filesystems").write_text("nodev\tsysfs\n\text4\n" + "".join(f"nodev\t{f}\n" for f in filesystems))
    mdir = tmp_path / "lib/modules" / REL
    mdir.mkdir(parents=True)
    (mdir / "modules.dep").write_text("".join(f"kernel/x/{m}.ko.zst: kernel/x/dep.ko.zst\n" for m in on_disk))
    (mdir / "modules.builtin").write_text("".join(f"kernel/y/{m}.ko\n" for m in builtin))
    if config is not None:
        (tmp_path / "boot").mkdir()
        (tmp_path / "boot" / f"config-{REL}").write_text(config)
    for m in sys_modules:
        (tmp_path / "sys/module" / m).mkdir(parents=True)
    return tmp_path


def test_state_distinguishes_loaded_builtin_available_missing(tmp_path):
    root = fake_root(tmp_path, loaded=["veth", "xt_MASQUERADE"], on_disk=["veth", "binder_linux", "xt-nat"],
                     builtin=["tun"], sys_modules=["overlay"])
    assert modules.state("veth", root=root) == "loaded"
    assert modules.state("tun", root=root) == "builtin"
    assert modules.state("overlay", root=root) == "builtin"       # /sys/module entry without a .ko
    assert modules.state("binder_linux", root=root) == "available"
    assert modules.state("xt_nat", root=root) == "available"      # dash and underscore are the same name
    assert modules.state("wireguard", root=root) == "missing"


def test_compressed_and_plain_module_files_are_all_recognised(tmp_path):
    root = fake_root(tmp_path)
    dep = root / "lib/modules" / REL / "modules.dep"
    dep.write_text("kernel/a/one.ko:\nkernel/a/two.ko.xz: kernel/a/one.ko\nkernel/a/three.ko.gz:\nextra/four.ko.zst:\nnot-a-module.txt:\n")
    assert modules.installed(root) == {"one", "two", "three", "four"}


def test_autoload_off_when_modprobe_helper_is_empty(tmp_path):
    assert modules.autoload_enabled(fake_root(tmp_path)) is True
    sub = tmp_path / "second"
    sub.mkdir()
    assert modules.autoload_enabled(fake_root(sub, modprobe="\n")) is False
    assert modules.autoload_enabled(tmp_path / "does-not-exist") is True


def test_features_follow_the_config():
    assert modules.features_of(Config()) == set()
    cfg = Config.model_validate({"media": {"camera": {"enabled": True}}, "performance": {"zram": {"enabled": True}},
                                 "network": {"vpn": {"provider": "wireguard"}}})
    assert modules.features_of(cfg) == {"camera", "zram", "wireguard"}
    names = [m.name for m in modules.wanted(modules.features_of(cfg))]
    assert {"v4l2loopback", "videodev", "zram", "wireguard"} <= set(names)
    assert "v4l2loopback" not in [m.name for m in modules.wanted()]
    gw = Config.model_validate({"network": {"gateways": {"eu": {"profile": "a.conf", "provider": "wireguard"}}}})
    assert "wireguard" in modules.features_of(gw)


def test_the_modules_docker_needed_by_name_are_required():
    required = {m.name for m in modules.MODULES if m.required and not m.feature}
    # The three that stopped Docker on the reference machine, plus binder and tun.
    assert {"veth", "xt_tcpudp", "xt_nat", "binder_linux", "tun"} <= required


def test_modules_load_conf_lists_only_what_this_kernel_has(tmp_path):
    root = fake_root(tmp_path, on_disk=["binder_linux", "veth", "bridge", "nf_conntrack", "nf_nat", "xt_tcpudp", "xt_nat",
                                        "xt_MASQUERADE", "xt_conntrack", "xt_addrtype", "ip_tables"],
                     builtin=["tun", "overlay"])
    lines = modules.modules_load_conf(root=root).splitlines()
    body = [ln for ln in lines if not ln.startswith("#")]
    assert "binder_linux" in body and "veth" in body and "ip_tables" in body
    assert "tun" not in body and "overlay" not in body      # built in: nothing to load
    assert "iptable_nat" not in body                        # optional and absent: would fail the systemd unit
    assert all(ln.startswith("#") for ln in lines[:2])


def test_required_module_missing_from_disk_is_still_listed_and_reported(tmp_path):
    root = fake_root(tmp_path, on_disk=["veth"])
    assert "binder_linux" in modules.modules_load_conf(root=root).splitlines()
    names = [m.name for m in modules.missing(root=root)]
    assert "binder_linux" in names and "tun" in names and "veth" in names
    assert "ip_tables" not in names and "ip_tables" in [m.name for m in modules.missing(root=root, required_only=False)]


def test_modprobe_conf_and_load_argv_carry_the_binder_devices():
    conf = modules.modprobe_conf({"camera"})
    assert "options binder_linux devices=binder,hwbinder,vndbinder" in conf
    assert "options v4l2loopback devices=0 max_buffers=8" in conf
    binder = next(m for m in modules.MODULES if m.name == "binder_linux")
    assert modules.load_argv(binder) == ["modprobe", "binder_linux", "devices=binder,hwbinder,vndbinder"]
    assert modules.load_argv(next(m for m in modules.MODULES if m.name == "veth")) == ["modprobe", "veth"]


def test_binder_through_binderfs(tmp_path):
    root = fake_root(tmp_path, loaded=["binder_linux"], filesystems=["binder"], config="CONFIG_ANDROID_BINDER_IPC=m\n")
    b = modules.binder_status(root)
    assert b["ok"] and b["how"] == "binderfs" and b["module"] == "loaded" and b["config_ipc"] == "m"


def test_binder_through_classic_devices(tmp_path):
    root = fake_root(tmp_path, builtin=["binder_linux"])
    (root / "dev").mkdir()
    for d in ("binder", "hwbinder", "vndbinder"):
        (root / "dev" / d).write_text("")
    b = modules.binder_status(root)
    assert b["ok"] and b["how"] == "devices" and b["devices"] == ["binder", "hwbinder", "vndbinder"]


def test_binder_module_present_but_not_loaded(tmp_path):
    b = modules.binder_status(fake_root(tmp_path, on_disk=["binder_linux"]))
    assert not b["ok"] and b["how"] == "module-not-loaded"


def test_binder_unsupported_kernel(tmp_path):
    b = modules.binder_status(fake_root(tmp_path, config="# CONFIG_ANDROID_BINDER_IPC is not set\nCONFIG_TUN=y\n"))
    assert not b["ok"] and b["how"] == "unsupported"


def test_kernel_config_from_proc_config_gz(tmp_path):
    root = fake_root(tmp_path)
    with gzip.open(root / "proc/config.gz", "wt") as f:
        f.write('CONFIG_ANDROID_BINDER_IPC=y\nCONFIG_ANDROID_BINDERFS=y\nCONFIG_MODULE_SIG_HASH="sha512"\n')
    cfg = modules.kernel_config(root)
    assert cfg["CONFIG_ANDROID_BINDERFS"] == "y" and cfg["CONFIG_MODULE_SIG_HASH"] == "sha512"
    assert modules.binder_status(root)["how"] == "binderfs-not-registered"


def test_status_rows_have_a_reason_for_every_module(tmp_path):
    rows = modules.status({"camera"}, root=fake_root(tmp_path, loaded=["veth"]))
    assert all(r["why"] and r["group"] and r["state"] in ("loaded", "builtin", "available", "missing") for r in rows)
    assert next(r for r in rows if r["name"] == "veth")["state"] == "loaded"

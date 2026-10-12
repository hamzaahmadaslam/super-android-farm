"""Host setup: the plan, dry runs, idempotent application against a fake root."""
import getpass
import grp
import os
import re

import pytest

from superfarm.config import Config
from superfarm.host import distro, setup
from superfarm.paths import get_paths
from superfarm.resources import resource_path
from superfarm.util.proc import CommandError, Result

REL = "6.8.0-test"
ME = getpass.getuser()
# Owner of the runtime folders in apply tests. An unprivileged run never changes owners, so a
# name that does not exist proves that; as root (the distro containers) the group must be real.
RUN_GROUP = grp.getgrgid(os.getgid()).gr_name if os.geteuid() == 0 else "nogroup-in-test"
UBUNTU = distro.Distro(id="ubuntu", name="Ubuntu", family="debian", kernel=REL)
FEDORA = distro.Distro(id="fedora", name="Fedora", family="fedora", kernel=REL)


def w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def fake_root(tmp_path, *, loaded=(), on_disk=("binder_linux", "veth", "bridge", "zram", "videodev", "v4l2loopback"),
              builtin=("tun", "overlay"), groups="docker:x:999:\nvideo:x:44:\nrender:x:991:\naudio:x:29:\n", total_gb=32):
    root = tmp_path / "root"
    w(root, "proc/sys/kernel/osrelease", REL + "\n")
    w(root, "proc/modules", "".join(f"{m} 1 0 - Live 0x0\n" for m in loaded))
    w(root, f"lib/modules/{REL}/modules.dep", "".join(f"kernel/{m}.ko:\n" for m in on_disk))
    w(root, f"lib/modules/{REL}/modules.builtin", "".join(f"kernel/{m}.ko\n" for m in builtin))
    w(root, "etc/group", groups)
    w(root, "proc/meminfo", f"MemTotal: {total_gb * 1048576} kB\nMemAvailable: 1 kB\n")
    w(root, "proc/swaps", "Filename Type Size Used Priority\n")
    w(root, "proc/sys/fs/inotify/max_user_instances", "128\n")
    w(root, "proc/sys/kernel/pid_max", "4194304\n")
    w(root, "sys/kernel/mm/ksm/run", "0\n")
    return root


@pytest.fixture
def paths(tmp_path):
    return get_paths(tmp_path / "farm")


def plan(root, paths, config=None, **kw):
    kw.setdefault("which", lambda n: None)
    kw.setdefault("distro", UBUNTU)
    kw.setdefault("has_nvidia", False)
    kw.setdefault("user", ME)
    return {s["id"]: s for s in setup.plan(config or Config(), paths, root=root, **kw)}


def argvs(step):
    return [a["argv"] for a in step["actions"] if a["kind"] == "run"]


def files(step):
    return {a["path"]: a["content"] for a in step["actions"] if a["kind"] == "write"}


class Recorder:
    """Stands in for proc.run: records every command, fails the ones it is told to."""
    def __init__(self, fail=(), zram="/dev/zram0\n"):
        self.calls, self.fail, self.zram = [], set(fail), zram

    def __call__(self, argv, timeout=60, check=True, **kw):
        self.calls.append(list(argv))
        if argv[0] in self.fail or " ".join(argv) in self.fail:
            raise CommandError(argv, 1, "", f"{argv[0]}: failed on purpose")
        return Result(0, self.zram if argv[0] == "zramctl" else "", "")


# ------------------------------------------------------------------ plan

def test_full_plan_has_every_step_in_order(tmp_path, paths):
    steps = setup.plan(Config(), paths, root=fake_root(tmp_path), which=lambda n: None, distro=UBUNTU, has_nvidia=False, user=ME)
    assert [s["id"] for s in steps] == ["packages", "docker-service", "groups", "modules", "sysctl", "run-dirs", "systemd"]
    assert all(s["title"] and "boot" in s for s in steps)


def test_boot_plan_only_redoes_what_a_reboot_loses(tmp_path, paths):
    cfg = Config.model_validate({"performance": {"zram": {"enabled": True}, "ksm": True}, "media": {"camera": {"enabled": True, "slots": 2}}})
    steps = setup.plan(cfg, paths, root=fake_root(tmp_path), boot=True, which=lambda n: None, distro=UBUNTU, user=ME)
    assert [s["id"] for s in steps] == ["modules", "run-dirs", "zram", "ksm", "camera"]
    assert all(s["boot"] for s in steps)
    assert not any(a["kind"] == "write" for s in steps for a in s["actions"])      # nothing in /etc at boot


def test_boot_mode_follows_the_unit_environment(tmp_path, paths, monkeypatch):
    monkeypatch.setenv("SUPERFARM_BOOT", "1")
    steps = setup.plan(Config(), paths, root=fake_root(tmp_path), which=lambda n: None, distro=UBUNTU, user=ME)
    assert [s["id"] for s in steps] == ["modules", "run-dirs"]


def test_packages_required_in_one_command_optional_one_by_one(tmp_path, paths):
    p = plan(fake_root(tmp_path), paths)["packages"]
    cmds = argvs(p)
    assert cmds[0] == ["apt-get", "update"]
    assert cmds[1] == ["apt-get", "install", "-y", "--no-install-recommends", "docker.io", "adb", "ffmpeg", "kmod", "iproute2", "curl"]
    assert ["apt-get", "install", "-y", "--no-install-recommends", "scrcpy"] in cmds
    optional = [a for a in p["actions"] if a["argv"][-1] == "scrcpy"][0]
    assert optional["optional"] is True and p["actions"][1]["optional"] is False


def test_installed_tools_are_not_reinstalled(tmp_path, paths):
    have = {"docker", "adb", "ffmpeg", "modprobe", "ip", "curl", "scrcpy", "lspci", "zstd", "pigz", "patchelf", "git"}
    p = plan(fake_root(tmp_path), paths, which=lambda n: f"/usr/bin/{n}" if n in have else None)["packages"]
    assert p["actions"] == [] and p["status"] == "ok"      # in particular Docker's own packages are left alone


def test_fedora_scrcpy_becomes_a_note_and_nvidia_adds_relay_libraries(tmp_path, paths):
    p = plan(fake_root(tmp_path), paths, distro=FEDORA, has_nvidia=True)["packages"]
    cmds = argvs(p)
    assert cmds[0][:3] == ["dnf", "install", "-y"] and "moby-engine" in cmds[0]
    assert not any("scrcpy" in c for c in cmds) and "scrcpy" in p["detail"] and "github.com" in p["detail"]
    assert any("vulkan-loader" in c for c in cmds)


def test_camera_adds_build_packages(tmp_path, paths):
    cfg = Config.model_validate({"media": {"camera": {"enabled": True}}})
    cmds = argvs(plan(fake_root(tmp_path), paths, cfg)["packages"])
    assert ["apt-get", "install", "-y", "--no-install-recommends", f"linux-headers-{REL}"] in cmds
    assert any(c[-1] == "dkms" for c in cmds) and any(c[-1] == "mokutil" for c in cmds)


def test_unsupported_distribution_installs_nothing_and_says_so(tmp_path, paths):
    p = plan(fake_root(tmp_path), paths, distro=distro.Distro(name="Mystery OS"))["packages"]
    assert p["actions"] == [] and "Mystery OS" in p["detail"]


def test_groups_only_existing_ones_and_only_missing_memberships(tmp_path, paths):
    g = plan(fake_root(tmp_path), paths, user="farmer")["groups"]
    assert argvs(g) == [["usermod", "-aG", "docker,video,render", "farmer"]] and "log out" in g["detail"]
    root = fake_root(tmp_path / "b", groups="docker:x:999:farmer\nvideo:x:44:other,farmer\n")
    assert plan(root, paths, user="farmer")["groups"]["actions"] == []           # no render group here, already in the rest


@pytest.mark.parametrize("bad", ["root; rm -rf /", "a b", "../x", "UPPER", ""])
def test_bad_user_and_group_names_are_refused(tmp_path, paths, bad):
    if bad:
        with pytest.raises(ValueError):
            setup.resolve_user(bad)
    with pytest.raises(ValueError):
        setup.resolve_group(bad or "bad group")


def test_modules_step_writes_configs_and_loads_what_is_not_loaded(tmp_path, paths):
    m = plan(fake_root(tmp_path, loaded=["veth"]), paths)["modules"]
    f = files(m)
    assert "binder_linux" in f["/etc/modules-load.d/superfarm.conf"].splitlines()
    assert "options binder_linux devices=binder,hwbinder,vndbinder" in f["/etc/modprobe.d/superfarm.conf"]
    cmds = argvs(m)
    assert ["modprobe", "binder_linux", "devices=binder,hwbinder,vndbinder"] in cmds and ["modprobe", "bridge"] in cmds
    assert ["modprobe", "veth"] not in cmds and ["modprobe", "tun"] not in cmds        # loaded / built in
    by_name = {a["argv"][1]: a for a in m["actions"] if a["kind"] == "run"}
    assert by_name["binder_linux"]["optional"] is False
    assert by_name["ip_tables"]["optional"] is True                                    # Android's netd extras
    assert by_name["nf_nat"]["optional"] is True       # required but absent from this fake kernel: tried, not fatal here


def test_sysctl_file_has_every_floor_and_optional_swappiness(tmp_path, paths):
    s = plan(fake_root(tmp_path), paths)["sysctl"]
    text = files(s)["/etc/sysctl.d/90-superfarm.conf"]
    for item in setup.SYSCTLS:
        assert f"{item.key} = {item.minimum}" in text
    assert "vm.swappiness" not in text and "fs.inotify.max_user_instances" in s["detail"]
    assert argvs(s) == [["sysctl", "-q", "-p", "/etc/sysctl.d/90-superfarm.conf"]]
    cfg = Config.model_validate({"performance": {"swappiness": 100}})
    assert "vm.swappiness = 100" in files(plan(fake_root(tmp_path / "b"), paths, cfg)["sysctl"])["/etc/sysctl.d/90-superfarm.conf"]


def test_run_dirs_belong_to_the_group_and_root_stays_under_run(tmp_path, paths):
    r = plan(fake_root(tmp_path), paths, group="docker")["run-dirs"]
    dirs = [(a["path"], a["mode"], a["owner"], a["group"]) for a in r["actions"] if a["kind"] == "mkdir"]
    assert dirs == [("/run/superfarm", 0o2770, "root", "docker"), ("/run/superfarm/venus", 0o2770, "root", "docker")]
    assert "d /run/superfarm 2770 root docker -" in files(r)["/etc/tmpfiles.d/superfarm.conf"]
    # A socket folder configured outside /run/superfarm is the user's business, never root's.
    for elsewhere in ("/home/someone/venus", "/run/superfarm/../etc/venus", "/run/superfarmX"):
        cfg = Config.model_validate({"gpu": {"nvidia": {"socket_dir": elsewhere}}})
        r = plan(fake_root(tmp_path / elsewhere.strip("/").replace("/", "_")), paths, cfg)["run-dirs"]
        assert [a["path"] for a in r["actions"] if a["kind"] == "mkdir"] == ["/run/superfarm"]


def test_zram_sized_from_ram_and_skipped_when_already_active(tmp_path, paths):
    cfg = Config.model_validate({"performance": {"zram": {"enabled": True, "size_percent": 25, "algorithm": "lz4"}}})
    root = fake_root(tmp_path)
    z = plan(root, paths, cfg)["zram"]
    assert argvs(z) == [["zramctl", "--find", "--size", "8192M", "--algorithm", "lz4"], ["mkswap", "{ZRAM}"],
                        ["swapon", "--priority", "100", "{ZRAM}"]]
    assert z["actions"][0]["capture"] == "ZRAM"
    assert ["modprobe", "zram"] in argvs(plan(root, paths, cfg)["modules"])
    w(root, "proc/swaps", "Filename Type Size Used Priority\n/dev/zram0 partition 1 0 100\n")
    again = plan(root, paths, cfg)["zram"]
    assert again["actions"] == [] and "already active" in again["detail"]
    assert "zram" not in plan(root, paths)


def test_zram_algorithm_is_validated(tmp_path, paths):
    cfg = Config.model_validate({"performance": {"zram": {"enabled": True, "algorithm": "zstd; reboot"}}})
    with pytest.raises(ValueError, match="algorithm"):
        setup.plan(cfg, paths, root=fake_root(tmp_path), which=lambda n: None, distro=UBUNTU, user=ME)


def test_ksm_written_once(tmp_path, paths):
    cfg = Config.model_validate({"performance": {"ksm": True}})
    root = fake_root(tmp_path)
    k = plan(root, paths, cfg)["ksm"]
    assert k["actions"] == [{"kind": "value", "path": "/sys/kernel/mm/ksm/run", "value": "1"}]
    w(root, "sys/kernel/mm/ksm/run", "1\n")
    assert plan(root, paths, cfg)["ksm"]["actions"] == []


def test_camera_devices_one_per_slot_skipping_existing(tmp_path, paths):
    cfg = Config.model_validate({"media": {"camera": {"enabled": True, "slots": 3, "device_base": 100}}})
    root = fake_root(tmp_path)
    w(root, "dev/video102", "")
    c = plan(root, paths, cfg, which=lambda n: "/usr/local/bin/v4l2loopback-ctl" if n == "v4l2loopback-ctl" else None)["camera"]
    cmds = argvs(c)
    assert cmds[0] == ["modprobe", "videodev"]
    assert cmds[1] == ["modprobe", "v4l2loopback", "devices=0", "max_buffers=8"]
    assert cmds[2:] == [
        ["/usr/local/bin/v4l2loopback-ctl", "add", "-n", "Super Farm Camera 1", "-x", "1", "-b", "8", "/dev/video101"],
        ["/usr/local/bin/v4l2loopback-ctl", "add", "-n", "Super Farm Camera 3", "-x", "1", "-b", "8", "/dev/video103"],
    ]
    assert "/dev/video101 .. /dev/video103" in c["detail"]


def test_camera_without_the_module_explains_how_to_get_it(tmp_path, paths):
    cfg = Config.model_validate({"media": {"camera": {"enabled": True}}})
    c = plan(fake_root(tmp_path, on_disk=["videodev"]), paths, cfg)["camera"]
    assert c["actions"] == [] and "native/v4l2loopback/build.sh install" in c["detail"]
    assert "sf host sign-module -- v4l2loopback" in c["detail"] and "`sf host apply`" in c["detail"]


# ------------------------------------------------------------------ systemd units

HOST_VALUES = dict(user="farmer", run_group="docker", superfarm_home="/srv/farm", python="/opt/sf/bin/python")


def test_boot_unit_template_renders_completely():
    assert setup.UNITS == ("superfarm-host.service",)         # the daemon is a user unit, not installed by root
    out = setup.render_unit(resource_path("deploy", "systemd", "superfarm-host.service").read_text(), **HOST_VALUES)
    assert not re.search(r"@[A-Z_]+@", out)
    assert "[Unit]" in out and "[Service]" in out and "[Install]" in out


def test_boot_unit_is_a_root_oneshot_in_boot_mode():
    out = setup.render_unit(resource_path("deploy", "systemd", "superfarm-host.service").read_text(), **HOST_VALUES)
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert "Type=oneshot" in lines and "User=root" in lines and "RemainAfterExit=yes" in lines
    assert "ExecStart=/opt/sf/bin/python -m superfarm.host.setup apply --boot" in lines
    assert "Environment=SUPERFARM_HOME=/srv/farm" in lines and "Environment=SUPERFARM_USER=farmer" in lines
    assert "Environment=SUPERFARM_BOOT=1" in lines and "Environment=SUPERFARM_GROUP=docker" in lines
    assert "Before=docker.service" in lines          # a system unit cannot order against the user's daemon unit


@pytest.mark.parametrize("field,value", [("user", "x\nExecStartPre=/bin/evil"), ("superfarm_home", "/home/a b/farm"),
                                         ("python", "/x/%h/python"), ("user", 'a"b'), ("python", "/x/y\\z")])
def test_unit_values_cannot_inject_directives(field, value):
    with pytest.raises(ValueError):
        setup.render_unit("User=@USER@\nEnvironment=SUPERFARM_HOME=@SUPERFARM_HOME@\nExecStart=@PYTHON@\nX=@RUN_GROUP@\n",
                          **{**HOST_VALUES, field: value})


def test_unknown_or_unfilled_placeholder_is_an_error():
    with pytest.raises(ValueError, match="@TYPO@"):
        setup.render_unit("X=@TYPO@\n", user="a")
    with pytest.raises(ValueError, match="@PYTHON@"):
        setup.render_unit("ExecStart=@PYTHON@\nUser=@USER@\n", user="a")


def test_systemd_step_installs_only_the_boot_unit(tmp_path, paths):
    s = plan(fake_root(tmp_path), paths)["systemd"]
    written = files(s)
    assert set(written) == {"/etc/systemd/system/superfarm-host.service"}
    assert f"Environment=SUPERFARM_HOME={paths.home}" in written["/etc/systemd/system/superfarm-host.service"]
    assert f"Environment=SUPERFARM_USER={ME}" in written["/etc/systemd/system/superfarm-host.service"]
    assert argvs(s) == [["systemctl", "daemon-reload"], ["systemctl", "enable", "superfarm-host.service"]]
    assert "sf service install" in s["detail"] and "user service" in s["detail"]


# ------------------------------------------------------------------ apply

def test_real_run_needs_root(tmp_path, paths):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    with pytest.raises(PermissionError, match="run `sf host apply`, which asks for sudo itself"):
        setup.apply(Config(), paths, root=fake_root(tmp_path), user=ME)


def test_dry_run_changes_nothing_and_lists_every_command(tmp_path, paths):
    root = fake_root(tmp_path)
    before = sorted(str(p) for p in root.rglob("*"))
    steps = setup.apply(Config(), paths, dry_run=True, root=root, user=ME)
    assert sorted(str(p) for p in root.rglob("*")) == before
    assert {s["status"] for s in steps} <= {"dry-run", "ok"}
    text = setup.format_steps(steps)
    assert "modprobe binder_linux devices=binder,hwbinder,vndbinder" in text
    assert "write /etc/sysctl.d/90-superfarm.conf (mode 0644):" in text and "fs.inotify.max_user_instances = 8192" in text
    assert "install -d -m 2770 -o root -g docker /run/superfarm" in text
    assert "systemctl enable superfarm-host.service" in text


def test_dry_run_shows_captured_values_as_shell_variables():
    assert setup.render({"kind": "run", "argv": ["mkswap", "{ZRAM}"], "optional": False, "capture": ""}) == ['mkswap "$ZRAM"']
    line = setup.render({"kind": "run", "argv": ["zramctl", "--find"], "optional": False, "capture": "ZRAM"})[0]
    assert line == "ZRAM=$(zramctl --find)"
    assert setup.render({"kind": "run", "argv": ["x", "a b"], "optional": True, "capture": ""})[0].startswith("x 'a b'")


def test_apply_writes_files_runs_commands_and_is_idempotent(tmp_path, paths):
    cfg = Config.model_validate({"performance": {"zram": {"enabled": True}, "ksm": True}})
    root = fake_root(tmp_path)
    rec = Recorder()
    steps = setup.apply(cfg, paths, root=root, runner=rec, user=ME, group=None,
                        steps=[s for s in setup.plan(cfg, paths, root=root, which=lambda n: None, distro=UBUNTU, has_nvidia=False,
                                                     user=ME, group=RUN_GROUP) if s["id"] != "groups"])
    assert [s["status"] for s in steps] == ["ok"] * len(steps), [(s["id"], s["detail"]) for s in steps if s["status"] != "ok"]
    assert (root / "etc/modules-load.d/superfarm.conf").read_text().startswith("# Written by")
    assert "kernel.pid_max = 4194304" in (root / "etc/sysctl.d/90-superfarm.conf").read_text()
    assert (root / "etc/systemd/system/superfarm-host.service").exists()
    assert not (root / "etc/systemd/system/superfarm.service").exists()      # the daemon unit belongs to the user
    assert (root / "run/superfarm/venus").is_dir() and (root / "run/superfarm").stat().st_mode & 0o7777 == 0o2770
    assert (root / "sys/kernel/mm/ksm/run").read_text() == "1"
    # The device name printed by zramctl flows into the following commands.
    assert ["mkswap", "/dev/zram0"] in rec.calls and ["swapon", "--priority", "100", "/dev/zram0"] in rec.calls
    assert ["systemctl", "enable", "--now", "docker.service"] in rec.calls

    stamp = (root / "etc/sysctl.d/90-superfarm.conf").stat().st_mtime_ns
    setup.apply(cfg, paths, root=root, runner=Recorder(), user=ME,
                steps=[s for s in setup.plan(cfg, paths, root=root, which=lambda n: None, distro=UBUNTU, has_nvidia=False,
                                             user=ME, group=RUN_GROUP) if s["id"] == "sysctl"])
    assert (root / "etc/sysctl.d/90-superfarm.conf").stat().st_mtime_ns == stamp          # unchanged file is not rewritten


def test_only_limits_a_run_to_the_named_steps(tmp_path, paths):
    root = fake_root(tmp_path)
    rec = Recorder()
    steps = setup.apply(Config(), paths, root=root, runner=rec, user=ME, only=["packages"],
                        steps=setup.plan(Config(), paths, root=root, which=lambda n: None, distro=FEDORA,
                                         has_nvidia=False, user=ME, group=RUN_GROUP))
    assert [s["id"] for s in steps] == ["packages"] and steps[0]["status"] == "ok"
    assert rec.calls and all(c[:3] == ["dnf", "install", "-y"] for c in rec.calls)      # nothing but package installs
    assert not (root / "etc/modules-load.d").exists() and not (root / "etc/systemd").exists()
    with pytest.raises(ValueError, match="unknown step.*nonsense.*steps are: packages"):
        setup.apply(Config(), paths, root=root, runner=rec, user=ME, only=["nonsense"], dry_run=True)


def test_step_ids_constant_matches_what_plan_produces(tmp_path, paths):
    cfg = Config.model_validate({"performance": {"zram": {"enabled": True}, "ksm": True},
                                 "media": {"camera": {"enabled": True, "slots": 1}}})
    ids = [s["id"] for s in setup.plan(cfg, paths, root=fake_root(tmp_path), which=lambda n: None, distro=UBUNTU,
                                       has_nvidia=False, user=ME)]
    assert tuple(ids) == setup.STEP_IDS


def test_module_entry_point_accepts_only(tmp_path, paths, capsys, monkeypatch):
    monkeypatch.setenv("SUPERFARM_USER", ME)
    assert setup.main(["apply", "--dry-run", "--only", "packages", "--home", str(paths.home)]) == 0
    out = capsys.readouterr().out
    assert "Install packages" in out and "Load kernel modules" not in out
    assert setup.main(["apply", "--dry-run", "--only", "nope", "--home", str(paths.home)]) == 2
    assert "unknown step" in capsys.readouterr().err


def test_one_failing_step_does_not_stop_the_others(tmp_path, paths):
    root = fake_root(tmp_path)
    rec = Recorder(fail={"apt-get", "modprobe ip_tables"})
    steps = {s["id"]: s for s in setup.apply(
        Config(), paths, root=root, runner=rec, user=ME,
        steps=[s for s in setup.plan(Config(), paths, root=root, which=lambda n: None, distro=UBUNTU, has_nvidia=False,
                                     user=ME, group=RUN_GROUP) if s["id"] != "groups"])}
    assert steps["packages"]["status"] == "failed" and "failed on purpose" in steps["packages"]["detail"]
    assert steps["modules"]["status"] == "ok"          # an optional module that will not load is tolerated ...
    assert "optional step did not succeed: modprobe ip_tables (exit 1: modprobe: failed on purpose)" in steps["modules"]["detail"]   # ... and said
    assert steps["sysctl"]["status"] == "ok" and steps["systemd"]["status"] == "ok"
    assert ["systemctl", "daemon-reload"] in rec.calls


def test_an_optional_package_that_does_not_install_is_reported_with_its_hint(tmp_path, paths):
    # Debian 12 has no scrcpy package: the step stays ok, says what failed and where to get it instead.
    root = fake_root(tmp_path)
    rec = Recorder(fail={"apt-get install -y --no-install-recommends scrcpy"})
    plan_steps = setup.plan(Config(), paths, root=root, which=lambda n: None, distro=UBUNTU, has_nvidia=False, user=ME,
                            group=RUN_GROUP)
    step = setup.apply(Config(), paths, root=root, runner=rec, user=ME, steps=plan_steps, only=["packages"])[0]
    assert step["status"] == "ok"
    assert "optional step did not succeed: apt-get install -y --no-install-recommends scrcpy (exit 1" in step["detail"]
    assert "upstream static build" in step["detail"]
    assert ["apt-get", "install", "-y", "--no-install-recommends", "pigz"] in rec.calls      # the others still ran


def test_zram_tool_without_a_device_name_fails_that_step_cleanly(tmp_path, paths):
    cfg = Config.model_validate({"performance": {"zram": {"enabled": True}}})
    root = fake_root(tmp_path)
    rec = Recorder(zram="something unexpected\n")
    plan_steps = [s for s in setup.plan(cfg, paths, root=root, boot=True, which=lambda n: None, distro=UBUNTU, user=ME,
                                        group=RUN_GROUP)]
    steps = {s["id"]: s for s in setup.apply(cfg, paths, root=root, runner=rec, steps=plan_steps)}
    assert steps["zram"]["status"] == "failed" and "no device name" in steps["zram"]["detail"]
    assert not any(c[0] in ("mkswap", "swapon") for c in rec.calls)        # nothing ran against a made-up device


def test_cli_entry_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SUPERFARM_HOME", str(tmp_path / "farm"))
    monkeypatch.delenv("SUPERFARM_BOOT", raising=False)
    assert setup.main(["apply", "--boot", "--dry-run", "--user", ME]) == 0
    out = capsys.readouterr().out
    assert "Create runtime folders" in out and "Install packages" not in out
    assert setup.main(["plan", "--user", "Bad User"]) == 2
    assert "bad user name" in capsys.readouterr().err

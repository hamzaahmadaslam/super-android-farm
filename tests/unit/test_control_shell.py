"""Shell commands on the fleet: bounds, quoting, the denylist."""
import importlib.util
import stat
import sys
import time
from pathlib import Path

import pytest

from superfarm.config import Config
from superfarm.control import shell as sh_mod
from superfarm.control.shell import Refused, Shell, check_command, destructive_reason, run_bounded, selector_text, settings
from superfarm.core.adb import AdbError
from superfarm.core.tasks import TaskManager


def load_support():
    name = "sf_control_support"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_control_support.py"))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


S = load_support()
PY = sys.executable

# Plays the part of the adb binary: `fake-adb -s <serial> shell <line>` runs <line> in a
# local shell, so quoting, exit codes, deadlines and output caps are exercised for real.
FAKE_ADB = """#!/bin/sh
[ "$1" = "-s" ] && [ "$3" = "shell" ] || exit 64
case "$2" in fake:2) echo "device $2 is broken" >&2; exit 5;; esac
exec sh -c "$4"
"""


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "fake-adb"
    fake.write_text(FAKE_ADB)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    mgr, tasks = S.FakeManager(ids=(1, 2, 3)), TaskManager(workers=4)
    for dev in mgr.devices.values():
        dev.binary = str(fake)
    yield mgr, tasks, Shell(mgr, tasks)
    tasks.shutdown()


# ----------------------------------------------------------------------------- bounded execution

def test_run_bounded_returns_output_and_exit_code():
    r = run_bounded([PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], timeout=20, max_bytes=1000)
    assert r.code == 3 and "out" in r.output and "err" in r.output and not r.truncated and not r.timed_out and not r.ok
    assert run_bounded([PY, "-c", "print('fine')"], timeout=20, max_bytes=1000).ok
    assert run_bounded(["/nonexistent/binary"], timeout=5, max_bytes=10).code == 127


def test_run_bounded_stops_a_flood_at_the_cap():
    started = time.monotonic()
    r = run_bounded([PY, "-c", "import sys\nwhile True: sys.stdout.write('x' * 65536)"], timeout=30, max_bytes=100_000)
    assert r.truncated and len(r.output) == 100_000 and r.code is None and not r.ok
    assert time.monotonic() - started < 10                     # stopped by the cap, not by the deadline


def test_run_bounded_kills_at_the_deadline():
    started = time.monotonic()
    r = run_bounded([PY, "-c", "import time; print('started', flush=True); time.sleep(60)"], timeout=1.0, max_bytes=1000)
    assert r.timed_out and r.code is None and "started" in r.output and time.monotonic() - started < 8


def test_run_bounded_does_not_wait_for_orphans_holding_the_pipe():
    started = time.monotonic()
    r = run_bounded(["sh", "-c", "sleep 30 & echo parent-done"], timeout=20, max_bytes=1000)
    assert r.code == 0 and "parent-done" in r.output and time.monotonic() - started < 10


def test_check_command():
    assert check_command(["ls", 5]) == ["ls", "5"]
    for bad in ("ls -la", b"ls", [], None):
        with pytest.raises(TypeError):
            check_command(bad)
    with pytest.raises(ValueError):
        check_command(["echo", "a\x00b"])
    with pytest.raises(ValueError):
        check_command(["echo", "x" * (sh_mod.MAX_COMMAND_BYTES + 1)])


def test_settings_fall_back_to_defaults_and_selectors_normalise():
    assert settings(None).concurrency == 8 and settings(object()).shell_timeout == 60
    assert settings(Config.model_validate({"control": {"concurrency": 3}})).concurrency == 3
    assert selector_text("tag:eu") == "tag:eu" and selector_text(7) == "7" and selector_text([3, 1]) == "3,1"
    with pytest.raises(ValueError):
        selector_text([1, -4])


# ----------------------------------------------------------------------------- fleet runs

def test_run_quotes_arguments_and_keeps_full_output(env):
    mgr, tasks, shell = env
    task = shell.run("1,3", ["echo", "a b", "$(echo injected)", "; echo second", "`id`", "x'y"])
    tasks.wait(task, timeout=20)
    assert task.state == "done" and task.kind == "shell" and task.results[1]["ok"]
    expected = "a b $(echo injected) ; echo second `id` x'y\n"
    outputs = shell.outputs(task.id)
    assert sorted(outputs) == [1, 3]
    for result in outputs.values():
        assert (result["code"], result["output"], result["truncated"], result["timed_out"]) == (0, expected, False, False)
    assert task.results[1]["detail"] == expected.strip()


def test_failures_are_per_device(env):
    mgr, tasks, shell = env
    mgr.devices[3].online = False
    task = tasks.wait(shell.run("all", ["sh", "-c", "echo working; exit 0"]), timeout=20)
    assert task.output == {"ok": 1, "failed": 2}
    assert task.results[1] == {"ok": True, "detail": "working"}
    assert task.results[2]["detail"] == "RuntimeError: exit 5: device fake:2 is broken"
    assert "does not answer on ADB" in task.results[3]["detail"] and 3 not in shell.outputs(task.id)


def test_timeout_and_output_cap_apply_per_device(env):
    _, tasks, shell = env
    slow = tasks.wait(shell.run("1", ["sleep", "30"], timeout=1), timeout=20)
    assert slow.results[1] == {"ok": False, "detail": "TimeoutError: no result after 1s; command stopped"}
    assert shell.outputs(slow.id)[1]["timed_out"]
    flood = tasks.wait(shell.run("1", ["sh", "-c", "while :; do echo xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; done"], max_output_kb=4), timeout=20)
    assert "printed more than 4 KB" in flood.results[1]["detail"] and len(shell.outputs(flood.id)[1]["output"]) == 4096
    with pytest.raises(ValueError):
        shell.run("1", ["true"], timeout=99999)


def test_exec_runs_now_on_one_device(env):
    mgr, _, shell = env
    result = shell.exec(1, ["sh", "-c", "echo hi; exit 4"])
    assert result.pop("seconds") >= 0
    assert result == {"code": 4, "output": "hi\n", "truncated": False, "timed_out": False}
    mgr.devices[3].online = False
    with pytest.raises(AdbError, match="does not answer"):
        shell.exec(3, ["true"])


def test_only_recent_outputs_are_kept(env):
    mgr, tasks, _ = env
    shell = Shell(mgr, tasks, keep=2)
    ids = [tasks.wait(shell.run("1", ["echo", str(n)]), timeout=20).id for n in range(3)]
    assert shell.outputs(ids[0]) == {} and shell.outputs(ids[2])[1]["output"] == "2\n" and shell.outputs("t-unknown") == {}


def test_root_prefixes_su_when_the_shell_is_not_root(env):
    mgr, _, _ = env
    dev = S.FakeAdb().on(r"^id -u$", "2000")
    assert sh_mod.root_argv(dev, ["am", "restart"]) == ["su", "0", "am", "restart"]
    assert sh_mod.root_argv(S.FakeAdb().on(r"^id -u$", "0"), ["am", "restart"]) == ["am", "restart"]


# ----------------------------------------------------------------------------- denylist

@pytest.mark.parametrize("command", [
    ["rm", "-rf", "/"], ["rm", "-rf", "/sdcard"], ["rm", "-r", "/sdcard/"], ["rm", "-fr", "/data/data"], ["rm", "/sdcard/*"],
    ["rm", "-rf", "/sdcard/Android/data"], ["rm", "-rf", "--", "/data/media/0"], ["/system/bin/rm", "-rf", "/data"],
    ["sh", "-c", "cd /tmp && rm -rf /data/*"], ["sh", "-c", "echo hi\nreboot"], ["su", "0", "sh", "-c", "rm -rf /system"],
    ["su", "-c", "svc power shutdown"], ["su", "root", "reboot"], ["reboot"], ["reboot", "-p"], ["poweroff"],
    ["setprop", "sys.powerctl", "shutdown"], ["setprop", "ctl.stop", "zygote"], ["stop"], ["timeout", "5", "reboot"],
    ["nohup", "reboot"], ["busybox", "reboot"], ["env", "A=1", "reboot"], ["mkfs.ext4", "/dev/block/sda"],
    ["dd", "if=/dev/zero", "of=/dev/block/vda"], ["sh", "-c", "echo c > /proc/sysrq-trigger"],
    ["find", "/sdcard", "-delete"], ["find", "/data", "-type", "f", "-exec", "rm", "{}", ";"], ["mv", "/sdcard", "/sdcard2"],
    ["chmod", "-R", "000", "/data"], ["chown", "-R", "root", "/system"],
    ["iptables", "-F"], ["iptables", "-F", "OUTPUT"], ["iptables", "-P", "OUTPUT", "ACCEPT"], ["ip6tables", "-D", "OUTPUT", "1"],
    ["su", "0", "iptables", "-A", "OUTPUT", "-j", "ACCEPT"], ["nft", "flush", "ruleset"], ["ndc", "network", "destroy", "100"],
    ["ip", "route", "flush", "table", "1002"], ["ip", "rule", "del", "pref", "100"], ["ip", "link", "set", "eth0", "down"],
    ["ifconfig", "eth0", "down"], ["kill", "-9", "1"], ["killall", "system_server"], ["pkill", "-9", "zygote"],
    ["pm", "uninstall", "com.android.systemui"], ["pm", "uninstall", "-k", "--user", "0", "com.android.phone"],
    ["cmd", "package", "uninstall", "com.android.settings"], ["pm", "disable-user", "com.android.shell"], ["pm", "clear", "android"],
    ["cmd", "testharness", "enable"], ["am", "broadcast", "-a", "android.intent.action.MASTER_CLEAR"],
    ["am", "broadcast", "-a", "android.intent.action.FACTORY_RESET", "-p", "android"], ["locksettings", "set-pin", "1234"],
    ["recovery", "--wipe_data"], ["sh", "-c", ":(){ :|:& };:"],
])
def test_destructive_commands_are_refused(command, env):
    _, tasks, shell = env
    assert destructive_reason(command), command
    with pytest.raises(Refused, match="refused: it "):
        shell.run("all", command)
    with pytest.raises(Refused):
        shell.exec(1, command)
    assert tasks.list() == []


@pytest.mark.parametrize("command", [
    ["ls", "-la", "/"], ["rm", "-rf", "/sdcard/Download/old"], ["rm", "/sdcard/a.txt"], ["rm", "-rf", "/data/local/tmp/work"],
    ["sh", "-c", "rm -rf /data/local/tmp/* && echo ok"], ["getprop", "ro.product.model"], ["pm", "uninstall", "org.example.app"],
    ["pm", "clear", "org.example.app"], ["pm", "disable-user", "--user", "0", "com.android.music"], ["kill", "-9", "1234"],
    ["am", "force-stop", "org.example.app"], ["iptables", "-L", "OUTPUT", "-n", "-v"], ["iptables", "-t", "nat", "-S"],
    ["nft", "list", "ruleset"], ["ip", "route", "show", "table", "all"], ["ip", "addr"], ["ifconfig"],
    ["find", "/sdcard", "-name", "*.tmp", "-delete"], ["find", "/sdcard/Download", "-delete"], ["dd", "if=/sdcard/a", "of=/sdcard/b"],
    ["echo", "rm -rf /"], ["setprop", "debug.layout", "true"], ["input", "keyevent", "KEYCODE_POWER"], ["svc", "power", "stayon", "true"],
    ["dumpsys", "battery", "reset"], ["settings", "put", "global", "http_proxy", ":0"], ["chmod", "-R", "770", "/sdcard/Download/x"],
])
def test_ordinary_commands_pass(command):
    assert destructive_reason(command) is None, command


def test_force_and_config_switch_off_the_denylist(env):
    mgr, tasks, shell = env
    task = tasks.wait(shell.run("1", ["echo", "rm", "-rf"], force=True), timeout=20)
    assert task.results[1]["ok"]
    refused = ["sh", "-c", "true || reboot"]
    with pytest.raises(Refused):
        shell.run("1", refused)
    assert tasks.wait(shell.run("1", refused, force=True), timeout=20).results[1]["ok"]
    mgr.config = Config.model_validate({"control": {"shell_denylist": False}})
    assert tasks.wait(shell.run("1", refused), timeout=20).results[1]["ok"]


def test_concurrency_comes_from_config_and_restarts_respect_the_boot_limit(env, monkeypatch):
    mgr, tasks, _ = env
    seen = []
    monkeypatch.setattr(sh_mod, "run_on_instances", lambda *a, **kw: seen.append(kw["concurrency"]))
    mgr.config = Config.model_validate({"control": {"concurrency": 12}, "performance": {"start_concurrency": 3}})
    sh_mod.fleet(mgr, tasks, "k", "t", "all", lambda i, adb, task: "ok")
    sh_mod.fleet(mgr, tasks, "k", "t", "all", lambda i, adb, task: "ok", heavy=True)
    assert seen == [12, 3]


def test_reach_reconnects_a_stale_device_once(env):
    mgr, _, _ = env
    dev = mgr.devices[1]
    assert sh_mod.reach(mgr, 1) is dev and (dev.connects, dev.disconnects) == (0, 0)     # healthy: no extra round trips
    states = iter(["offline", "device"])
    dev.state = lambda: next(states)
    assert sh_mod.reach(mgr, 1) is dev and (dev.connects, dev.disconnects) == (1, 1)     # stale entry dropped, then reconnected
    mgr.devices[2].online = False
    with pytest.raises(AdbError, match="Android 2 does not answer on ADB"):
        sh_mod.reach(mgr, 2)
    with pytest.raises(ValueError):
        sh_mod.reach(mgr, "1; reboot")

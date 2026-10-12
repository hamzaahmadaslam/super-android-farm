"""Local commands: they work with no daemon, by calling the modules directly."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys

import pytest
import yaml
from pydantic import BaseModel
from cli_fakes import make_home, run_sf

from superfarm import __version__
from superfarm.cli import configdocs, local
from superfarm.config import Config, load_config
from superfarm.paths import REPO_DIR
from superfarm.resources import resource_path


@pytest.fixture
def home(tmp_path, monkeypatch):
    return make_home(tmp_path, monkeypatch, token=None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing in this file may open a connection: these commands are the ones that must
    work before a daemon exists."""
    def refuse(*args, **kwargs):
        raise AssertionError("a local command tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)


# ------------------------------------------------------------------ version

def test_version(home, capsys):
    assert run_sf(capsys, "version").out == f"superfarm {__version__}\n"
    assert run_sf(capsys, "--json", "version").json["version"] == __version__
    with pytest.raises(SystemExit):
        from superfarm.cli.main import main
        main(["--version"])
    assert f"superfarm {__version__}" in capsys.readouterr().out


# ------------------------------------------------------------------ config docs

def test_config_docs_is_deterministic():
    first = configdocs.render()
    assert first == configdocs.render()
    # Same text from a fresh interpreter with another hash seed: nothing depends on set order.
    other = subprocess.run([sys.executable, "-c", "from superfarm.cli import configdocs; print(configdocs.render(), end='')"],
                           capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONHASHSEED": "12345"})
    assert other.returncode == 0 and other.stdout == first
    assert first.endswith("\n") and "\n\n\n" not in first


def _documented_keys(text: str) -> set[str]:
    return {line.split("`")[1] for line in text.splitlines() if line.startswith("| `")}


def _leaf_keys(model, prefix="") -> set[str]:
    from typing import get_args

    from pydantic import BaseModel
    out: set[str] = set()
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out |= _leaf_keys(annotation, f"{prefix}{name}.")
            continue
        out.add(f"{prefix}{name}")
        for inner in get_args(annotation):
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                out |= _leaf_keys(inner, f"{prefix}{name}.<name>.")
    return out


def test_config_docs_lists_every_setting_exactly_once():
    text = configdocs.render()
    rows = [line.split("`")[1] for line in text.splitlines() if line.startswith("| `")]
    assert len(rows) == len(set(rows))
    assert set(rows) == _leaf_keys(Config)


def test_config_docs_rows_carry_type_default_constraints_and_description():
    rows = {line.split("`")[1]: [c.strip() for c in line.strip("|").split(" | ")]
            for line in configdocs.render().splitlines() if line.startswith("| `")}
    assert rows["server.port"][1:4] == ["whole number", "`7700`", "1 to 65535"]
    assert rows["server.port"][4] == "Port of the panel and API."
    assert rows["gpu.mode"][1:3] == ["choice", '`"auto"`'] and "`igpu`, `dgpu`" in rows["gpu.mode"][3]
    assert rows["resources.cpus"][1:4] == ["number", "`2.0`", "more than 0, at most 256"]
    assert rows["network.dns"][1:3] == ["list of text", '`["1.1.1.1", "8.8.8.8"]`']
    assert rows["android.extra_props"][1:3] == ["map of text to text", "`{}`"]
    assert rows["docker.name_prefix"][3] == "matches `^[a-z][a-z0-9]{0,11}$`"
    assert rows["farm_name"][3] == "up to 60 characters"
    assert rows["android.images"][1:3] == ["map of name to settings", "see `android.images.<name>`"]
    assert rows["android.images.<name>.base"][2] == "required"
    assert rows["network.gateways.<name>.profile"][2] == "required"
    assert rows["performance.zram.size_percent"][3] == "5 to 200"


def test_config_docs_sections_and_markdown_safety():
    text = configdocs.render()
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings[0] == "# Configuration reference" and headings[1] == "## General"
    top = [h[3:] for h in headings if h.startswith("## ")][1:]
    assert top == [name for name, field in Config.model_fields.items()
                   if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel)]
    assert "### gpu.nvidia" in headings and "### network.vpn" in headings and "### media.camera" in headings
    assert "### android.images.\\<name\\>" in headings
    for line in text.splitlines():
        if line.startswith("| `"):
            # Six unescaped bars = five cells; a bare `<home>` outside a code span would vanish as HTML.
            unescaped = line.replace("\\|", "")
            assert unescaped.count("|") == 6, line
            outside_code = "".join(unescaped.split("`")[::2])
            assert "<" not in outside_code.replace("\\<", ""), line


def test_every_setting_has_a_description():
    assert configdocs.undocumented() == []


def test_committed_reference_is_up_to_date_when_present():
    """This is the CI check: once docs/CONFIGURATION.md exists it must equal the generated text."""
    committed = REPO_DIR / "docs" / "CONFIGURATION.md"
    if not committed.is_file():
        pytest.skip("docs/CONFIGURATION.md has not been generated yet (sf config docs -o docs/CONFIGURATION.md)")
    assert committed.read_text(encoding="utf-8") == configdocs.render(), \
        "regenerate with: sf config docs -o docs/CONFIGURATION.md"


def test_config_docs_command_prints_writes_and_checks(home, capsys, tmp_path):
    text = configdocs.render()
    assert run_sf(capsys, "config", "docs").out == text
    target = tmp_path / "docs" / "CONFIGURATION.md"
    assert run_sf(capsys, "config", "docs", "-o", str(target)).code == 0 and target.read_text() == text
    assert run_sf(capsys, "config", "docs", "--check", str(target)).code == 0
    target.write_text(text + "edited by hand\n")
    run = run_sf(capsys, "config", "docs", "--check", str(target))
    assert run.code == 1 and "out of date" in run.err and "sf config docs -o" in run.err
    assert run_sf(capsys, "config", "docs", "--check", str(tmp_path / "missing.md")).code == 1


# ------------------------------------------------------------------ config get / set / unset / show / path

def test_config_set_get_unset_round_trip(home, capsys):
    assert run_sf(capsys, "config", "get", "android.fps").out == "20\n"
    assert run_sf(capsys, "config", "set", "android.fps", "25").code == 0
    assert run_sf(capsys, "config", "get", "android.fps").out == "25\n"
    assert yaml.safe_load(home.config.read_text()) == {"android": {"fps": 25}}      # only what differs is stored
    assert stat.S_IMODE(home.config.stat().st_mode) == 0o640
    run = run_sf(capsys, "config", "unset", "android.fps")
    assert run.code == 0 and "back to its default (20)" in run.err
    assert load_config(home).android.fps == 20 and (yaml.safe_load(home.config.read_text()) or {}) == {}


def test_config_set_types_follow_the_model(home, capsys):
    run_sf(capsys, "config", "set", "android.default_version", "12")            # text, though it looks like a number
    run_sf(capsys, "config", "set", "network.dns", "9.9.9.9, 1.1.1.1")
    run_sf(capsys, "config", "set", "performance.zram.enabled", "yes")
    run_sf(capsys, "config", "set", "resources.cpus", "1.5")
    run_sf(capsys, "config", "set", "android.extra_props.ro.foo.bar", "baz")
    run_sf(capsys, "config", "set", "network.gateways.eu", "{profile: nl.ovpn}")
    config = load_config(home)
    assert config.android.default_version == "12" and config.network.dns == ["9.9.9.9", "1.1.1.1"]
    assert config.performance.zram.enabled is True and config.resources.cpus == 1.5
    assert config.android.extra_props == {"ro.foo.bar": "baz"} and config.network.gateways["eu"].profile == "nl.ovpn"
    assert run_sf(capsys, "config", "get", "network.gateways.eu.profile").out == "nl.ovpn\n"
    assert run_sf(capsys, "--json", "config", "get", "network.dns").json == ["9.9.9.9", "1.1.1.1"]
    assert run_sf(capsys, "config", "unset", "network.gateways.eu").code == 0
    assert run_sf(capsys, "config", "unset", "android.extra_props.ro.foo.bar").code == 0
    config = load_config(home)
    assert config.network.gateways == {} and config.android.extra_props == {}


@pytest.mark.parametrize("key, value, message", [
    ("android.fps", "1", "greater than or equal to 5"),
    ("android.fps", "fast", "whole number"),
    ("gpu.mode", "turbo", "not one of"),
    ("nope.key", "1", "no field 'nope'"),
    ("android.default_version", "99", "has no entry in android.images"),
    ("server.auth_required", "maybe", "true or false"),
])
def test_config_set_rejects_bad_values_and_saves_nothing(home, capsys, key, value, message):
    run = run_sf(capsys, "config", "set", key, value)
    assert run.code == 2 and message in run.err and not home.config.exists()


def test_config_set_refuses_an_unsafe_combination(home, capsys):
    assert run_sf(capsys, "config", "set", "server.host", "0.0.0.0").code == 0
    run = run_sf(capsys, "config", "set", "server.auth_required", "false")
    assert run.code == 2 and "auth_required cannot be off" in run.err
    assert load_config(home).server.auth_required is True


def test_config_unset_of_something_not_set(home, capsys):
    assert run_sf(capsys, "config", "unset", "network.gateways.eu").code == 1
    assert run_sf(capsys, "config", "get", "network.gateways.eu.profile").code == 1


def test_config_show_and_path(home, capsys):
    run_sf(capsys, "config", "set", "android.fps", "25")
    shown = yaml.safe_load(run_sf(capsys, "config", "show").out)
    assert shown == Config.model_validate({"android": {"fps": 25}}).model_dump(mode="json")
    assert yaml.safe_load(run_sf(capsys, "config", "show", "--changed").out) == {"android": {"fps": 25}}
    assert run_sf(capsys, "--json", "config", "show", "--changed").json == {"android": {"fps": 25}}
    assert run_sf(capsys, "config", "path").out == f"{home.config}\n"


def test_an_invalid_config_file_is_reported_in_one_line(home, capsys):
    home.config.write_text("android:\n  fps: 2\nbogus: 1\n")
    run = run_sf(capsys, "config", "get", "android.fps")
    assert run.code == 1 and "config.yaml is not valid" in run.err and "android.fps" in run.err and "bogus" in run.err
    assert "Traceback" not in run.err and "pydantic" not in run.err


def test_a_config_file_that_is_not_yaml_is_reported_in_one_line(home, capsys):
    home.config.write_text("android: [unclosed\n  fps: {\n")
    run = run_sf(capsys, "config", "show")
    assert run.code == 1 and run.err.startswith("sf: cannot read ") and run.err.count("\n") == 1
    assert "Traceback" not in run.err


def test_home_option_selects_another_farm(home, capsys, tmp_path):
    other = tmp_path / "other farm"
    assert run_sf(capsys, "--home", str(other), "config", "set", "android.fps", "30").code == 0
    assert run_sf(capsys, "config", "get", "android.fps", "--home", str(other)).out == "30\n"
    assert run_sf(capsys, "config", "get", "android.fps").out == "20\n"


# ------------------------------------------------------------------ token

def test_token_is_created_owner_only_and_stable(home, capsys):
    shutil.rmtree(home.secrets)                                  # even the folder is made with the right mode
    first = run_sf(capsys, "token")
    token = first.out.strip()
    assert first.code == 0 and len(token) >= 43 and first.out == token + "\n"
    path = home.secrets / "api-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.secrets.stat().st_mode) == 0o700
    assert path.read_text().strip() == token
    assert run_sf(capsys, "token").out.strip() == token          # printing does not change it


def test_token_rotate_replaces_it(home, capsys):
    old = run_sf(capsys, "token").out.strip()
    rotated = run_sf(capsys, "token", "--rotate")
    new = rotated.out.strip()
    assert new != old and "stops working at once" in rotated.err
    assert stat.S_IMODE((home.secrets / "api-token").stat().st_mode) == 0o600
    assert run_sf(capsys, "--json", "token").json == {"token": new, "path": str(home.secrets / "api-token")}


def test_token_is_the_one_the_client_sends(home, capsys):
    from superfarm.cli.client import read_token
    assert read_token(home) == ""
    token = run_sf(capsys, "token").out.strip()
    assert read_token(home) == token


def test_quiet_token_prints_only_the_token(home, capsys):
    run = run_sf(capsys, "-q", "token", "--rotate")
    assert run.err == "" and len(run.out.strip().split()) == 1


# ------------------------------------------------------------------ doctor / host

def test_doctor_reports_checks_and_fails_on_a_failed_one(home, capsys, monkeypatch):
    from superfarm.host import doctor
    checks = [doctor.Check("cpu", "CPU", "ok", "x86_64"),
              doctor.Check("binder", "Android binder", "fail", "not available", "sudo modprobe binder_linux\nsudo sf host apply"),
              doctor.Check("swap", "Swap", "warn", "none", "sudo swapon -a")]
    monkeypatch.setattr(doctor, "run_checks", lambda config, paths: checks)
    run = run_sf(capsys, "doctor")
    assert run.code == 1
    assert run.out.splitlines()[:4] == ["STATUS  CHECK           DETAIL", "ok      CPU             x86_64",
                                        "fail    Android binder  not available", "warn    Swap            none"]
    assert "[fail] Android binder: not available\n    sudo modprobe binder_linux\n    sudo sf host apply" in run.out
    assert "1 ok, 1 warnings, 1 failed, 0 skipped" in run.out and "not ready" in run.out
    data = run_sf(capsys, "--json", "doctor").json
    assert [c["id"] for c in data["checks"]] == ["cpu", "binder", "swap"] and data["summary"]["ready"] is False

    monkeypatch.setattr(doctor, "run_checks", lambda config, paths: checks[:1])
    assert run_sf(capsys, "doctor").code == 0


def test_doctor_json_has_the_shape_of_the_api_route(home, capsys, monkeypatch):
    from superfarm.host import doctor
    monkeypatch.setattr(doctor, "run_checks", lambda config, paths: [doctor.Check("cpu", "CPU", "ok")])
    data = run_sf(capsys, "--json", "doctor").json
    assert set(data) == {"checks", "summary"} and set(data["checks"][0]) == {"id", "title", "status", "detail", "fix"}


def test_host_plan_is_a_dry_run(home, capsys, monkeypatch):
    from superfarm.host import setup
    seen = {}

    def fake_apply(config, paths, **kwargs):
        seen.update(kwargs)
        return [{"id": "modules", "title": "Load kernel modules", "status": "dry-run", "detail": "", "boot": True,
                 "actions": [], "commands": ["modprobe veth"]}]

    monkeypatch.setattr(setup, "apply", fake_apply)
    run = run_sf(capsys, "host", "plan", "--boot")
    assert seen == {"dry_run": True, "boot": True}
    assert run.out == "[dry-run] Load kernel modules\n    modprobe veth\n" and "Nothing was changed" in run.err
    run_sf(capsys, "host", "apply", "--dry-run")
    assert seen == {"dry_run": True, "boot": None}                # None: follow SUPERFARM_BOOT, as the module does
    assert run_sf(capsys, "--json", "host", "plan").json[0]["commands"] == ["modprobe veth"]


def test_host_apply_as_a_user_re_execs_under_sudo_with_the_users_farm_home(home, capsys, monkeypatch):
    calls = []

    def fake_exec(program, argv):
        calls.append((program, argv))
        raise SystemExit(0)                    # execvp never returns; neither may this

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "execvp", fake_exec)
    monkeypatch.setattr(local.proc, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(local.pwd, "getpwuid", lambda uid: type("P", (), {"pw_name": "farmer"})())
    run = run_sf(capsys, "host", "apply", "--boot")
    program, argv = calls[0]
    assert program == "sudo"
    # sudo resets the environment and $HOME: the farm and the user are named explicitly, and
    # the root side is the same module the boot unit runs.
    assert argv == ["sudo", "--", sys.executable, "-m", "superfarm.host.setup", "apply",
                    "--home", str(home.home.resolve()), "--user", "farmer", "--boot"]
    assert "continuing under sudo" in run.err


def test_the_sudo_command_line_is_accepted_by_the_setup_module(home, monkeypatch):
    """What `sf host apply` hands to sudo must be something host/setup.py really parses."""
    from superfarm.cli.common import Ctx
    from superfarm.cli.render import Output
    from superfarm.host import setup
    monkeypatch.setattr(local.pwd, "getpwuid", lambda uid: type("P", (), {"pw_name": "farmer"})())
    monkeypatch.setenv("SUPERFARM_GROUP", "farm")
    args = type("A", (), {"home": None, "boot": True, "dry_run": False})()
    argv = local.sudo_argv(Ctx(args, Output()), boot=True)
    assert argv[:6] == ["sudo", "--", sys.executable, "-m", "superfarm.host.setup", "apply"]
    seen = {}

    def fake_apply(config, paths, **kwargs):
        seen.update(kwargs, home=paths.home)
        return []

    monkeypatch.setattr(setup, "apply", fake_apply)
    assert setup.main(argv[5:]) == 0
    wanted = {"dry_run": False, "boot": True, "user": "farmer", "group": "farm", "home": home.home.resolve()}
    assert {k: seen[k] for k in wanted} == wanted


def test_host_apply_without_sudo_prints_the_command_to_run_as_root(home, capsys, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(local.proc, "which", lambda name: None)
    monkeypatch.setattr(os, "execvp", lambda *a: pytest.fail("must not exec"))
    run = run_sf(capsys, "host", "apply")
    assert run.code == 1 and "sudo was not found" in run.err and "-m superfarm.host.setup apply --home" in run.err


def test_host_apply_as_root_runs_the_setup_and_reports_failures(home, capsys, monkeypatch):
    from superfarm.host import setup
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "execvp", lambda *a: pytest.fail("root must not re-exec"))
    seen = {}

    def fake_apply(config, paths, **kwargs):
        seen.update(kwargs, home=paths.home)
        return [{"id": "sysctl", "title": "Raise kernel limits", "status": "failed", "detail": "sysctl exit 1",
                 "actions": [], "commands": []}]

    monkeypatch.setattr(setup, "apply", fake_apply)
    run = run_sf(capsys, "host", "apply")
    assert seen == {"boot": None, "home": home.home} and run.code == 1
    assert run.out == "[failed] Raise kernel limits - sysctl exit 1\n"


def test_under_sudo_the_invoking_users_farm_home_is_used(tmp_path, monkeypatch, capsys):
    """`sudo sf ...`: `~` is root's home there; host.setup.sudo_home() names the caller's farm."""
    from superfarm.host import setup
    monkeypatch.delenv("SUPERFARM_HOME", raising=False)
    monkeypatch.setattr(setup, "sudo_home", lambda: str(tmp_path / "callers-farm"))
    assert run_sf(capsys, "config", "path").out == f"{tmp_path / 'callers-farm' / 'config.yaml'}\n"
    # An explicit --home still wins.
    assert run_sf(capsys, "--home", str(tmp_path / "x"), "config", "path").out == f"{tmp_path / 'x' / 'config.yaml'}\n"


def test_host_capacity_and_gpus_render_the_modules_results(home, capsys, monkeypatch):
    from superfarm.host import gpu
    card = gpu.GpuInfo(node="/dev/dri/renderD128", driver="i915", vendor="intel", name="Intel UHD 770", pci="0000:00:02.0",
                       usable_modes=["intel"])
    monkeypatch.setattr(gpu, "detect_gpus", lambda *a, **k: [card])
    run = run_sf(capsys, "host", "gpus")
    assert run.code == 0 and "/dev/dri/renderD128  intel   integrated  Intel UHD 770  i915" in run.out
    usable = run.out.split("Rendering modes usable now: ")[1].splitlines()[0].split(", ")
    assert usable[0] == "software" and "intel" in usable and "nvidia" not in usable
    assert run_sf(capsys, "--json", "host", "gpus").json["available_modes"] == usable
    run = run_sf(capsys, "host", "capacity", "--workload", "light")
    assert run.code == 0 and "Androids fit on this machine in intel mode (light workload)" in run.out
    assert "RAM" in run.out and "CPU" in run.out
    data = run_sf(capsys, "--json", "host", "capacity").json
    assert data["binding"] in ("ram", "cpu") and data["workload"] == "video"


def test_host_sign_module_status_needs_no_root_and_signing_goes_through_sudo(home, capsys, monkeypatch):
    calls = []

    def fake_exec(program, argv):
        calls.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "execvp", fake_exec)
    monkeypatch.setattr(local.proc, "which", lambda name: f"/usr/bin/{name}")
    run_sf(capsys, "host", "sign-module")
    run_sf(capsys, "host", "sign-module", "--", "--create-key", "--enroll", "v4l2loopback")
    script = str(resource_path("scripts", "host", "sign-module.sh"))
    assert calls == [["bash", script, "--status"],
                     ["sudo", "--", "bash", script, "--create-key", "--enroll", "v4l2loopback"]]


# ------------------------------------------------------------------ serve

def test_serve_hands_over_to_the_api_server(home, capsys, monkeypatch):
    server = pytest.importorskip("superfarm.api.server")
    seen = {}

    def fake_serve(host=None, port=None, home=None):
        seen.update(host=host, port=port, home=home)
        return 3

    monkeypatch.setattr(server, "serve", fake_serve)
    assert run_sf(capsys, "serve", "--host", "127.0.0.1", "--port", "17899").code == 3
    assert seen == {"host": "127.0.0.1", "port": 17899, "home": str(home.home)}
    run_sf(capsys, "serve")
    assert seen["host"] is None and seen["port"] is None             # the config decides


# ------------------------------------------------------------------ service (systemd --user)

@pytest.fixture
def user_units(monkeypatch, tmp_path):
    """systemctl replaced by a recorder, and the user's unit folder by a temporary one."""
    calls: list[list[str]] = []
    fail: set[str] = set()

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        bad = argv[2] in fail
        return local.proc.Result(1 if bad else 0, "", "Unit superfarm.service not found." if bad else "")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(local.proc, "run", fake_run)
    monkeypatch.setattr(local.proc, "which", lambda name: f"/usr/bin/{name}")
    return calls, fail, tmp_path / "xdg" / "systemd" / "user" / "superfarm.service"


def test_service_install_calls_the_host_module_and_relays_its_notes(home, capsys, monkeypatch):
    from superfarm.host import service
    seen = {}

    def fake_install(paths, **kwargs):
        seen["home"] = paths.home
        return {"unit": "/u/superfarm.service", "changed": True, "enabled": True, "imported": ["DISPLAY"],
                "linger": False, "notes": ["the service starts at your first login"]}

    monkeypatch.setattr(service, "install", fake_install)
    run = run_sf(capsys, "service", "install")
    assert run.code == 0 and seen == {"home": home.home} and run.out == "/u/superfarm.service\n"
    assert "installed and enabled" in run.err and "note: the service starts at your first login" in run.err
    assert "sf service start" in run.err
    assert run_sf(capsys, "--json", "service", "install").json["unit"] == "/u/superfarm.service"

    def refused(paths, **kwargs):
        raise service.ServiceError("run this as that user, not as root")

    monkeypatch.setattr(service, "install", refused)
    run = run_sf(capsys, "service", "install")
    assert run.code == 1 and run.err == "sf: run this as that user, not as root\n"


def test_service_start_and_stop_need_the_unit_and_call_systemctl(home, capsys, user_units):
    calls, fail, unit = user_units
    run = run_sf(capsys, "service", "start")
    assert run.code == 1 and "sf service install" in run.err and calls == []      # not installed yet
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    assert run_sf(capsys, "service", "start").code == 0 and run_sf(capsys, "service", "stop").code == 0
    assert calls == [["systemctl", "--user", "start", "superfarm.service"], ["systemctl", "--user", "stop", "superfarm.service"]]
    fail.add("start")
    run = run_sf(capsys, "service", "start")
    assert run.code == 1 and "systemctl --user start superfarm.service failed: Unit superfarm.service not found." in run.err


def test_service_status_exit_code_follows_systemctl_is_active(home, capsys, monkeypatch):
    from superfarm.host import service
    state = {"unit": "/u/superfarm.service", "installed": True, "active": "active", "enabled": "enabled", "linger": False,
             "manager": True, "display": ["DISPLAY", "WAYLAND_DISPLAY"]}
    monkeypatch.setattr(service, "status", lambda **kwargs: dict(state))
    run = run_sf(capsys, "service", "status")
    assert run.code == 0 and "state                 active" in run.out and "DISPLAY, WAYLAND_DISPLAY" in run.out
    assert "no (the service stops at your last logout)" in run.out
    state.update(active="inactive", display=[])
    run = run_sf(capsys, "service", "status")
    assert run.code == 3 and "device windows will not open" in run.out
    assert run_sf(capsys, "--json", "service", "status").json == state
    state.update(installed=False, active="unknown")
    run = run_sf(capsys, "service", "status")
    assert run.code == 3 and "sf service install" in run.out


def test_service_logs_execs_journalctl(home, capsys, monkeypatch):
    calls = []

    def fake_exec(program, argv):
        calls.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", fake_exec)
    monkeypatch.setattr(local.proc, "which", lambda name: f"/usr/bin/{name}")
    run_sf(capsys, "service", "logs", "-n", "20", "-f")
    assert calls == [["journalctl", "--user", "--unit", "superfarm.service", "--no-pager", "--lines", "20", "--follow"]]


def test_the_installed_unit_starts_sf_serve_for_this_farm_home(home):
    """The contract between `sf service` and `sf serve`: the unit runs exactly that command."""
    from superfarm.host import service
    text = service.unit_text(home, sf_bin="/opt/venv/bin/sf")
    assert "ExecStart=/opt/venv/bin/sf serve\n" in text
    assert f"Environment=SUPERFARM_HOME={home.home.resolve()}\n" in text


def test_json_output_of_local_commands_is_valid_json(home, capsys):
    for argv in (["version"], ["config", "path"], ["config", "show"], ["token"]):
        assert isinstance(json.loads(run_sf(capsys, "--json", *argv).out), dict)

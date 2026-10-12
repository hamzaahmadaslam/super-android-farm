"""The daemon as a systemd user service: the unit, its installation, and what it reports."""
import os
import re

import pytest

from superfarm.host import service
from superfarm.paths import PACKAGE_DIR, get_paths
from superfarm.resources import resource_path
from superfarm.util.proc import CommandError, Result

CHECKOUT = PACKAGE_DIR.parent
MANAGER_ENV = ("HOME=/home/farmer\nXDG_RUNTIME_DIR=/run/user/1000\nDBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
               "DISPLAY=:0\nWAYLAND_DISPLAY=wayland-0\nXAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.ABC\n")


class Systemd:
    """Stands in for proc.run: answers systemctl --user / loginctl, records every call."""
    def __init__(self, *, env=MANAGER_ENV, linger="no", bus=True, active="inactive", enabled="disabled", missing=False):
        self.calls, self.env, self.linger, self.bus = [], env, linger, bus
        self.active, self.enabled, self.missing = active, enabled, missing

    def __call__(self, argv, timeout=60, check=False, **kw):
        argv = list(argv)
        self.calls.append(argv)
        if self.missing:
            raise CommandError(argv, 127, "", f"{argv[0]}: command not found")
        if argv[0] == "loginctl":
            return Result(0, self.linger + "\n", "")
        assert argv[:2] == ["systemctl", "--user"], argv
        if not self.bus:
            return Result(1, "", "Failed to connect to user scope bus via local transport: No medium found\n")
        verb = argv[2]
        if verb == "show-environment":
            return Result(0, self.env, "")
        if verb == "is-active":
            return Result(0 if self.active == "active" else 3, self.active + "\n", "")
        if verb == "is-enabled":
            return Result(0 if self.enabled == "enabled" else 1, self.enabled + "\n", "")
        return Result(0, "", "")

    def verbs(self):
        return [c[2:] for c in self.calls if c[0] == "systemctl"]


@pytest.fixture
def paths(tmp_path):
    return get_paths(tmp_path / "farm")


@pytest.fixture
def not_root(monkeypatch):
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000)     # the suite also runs as root in the distro containers


def directives(text):
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


# ------------------------------------------------------------------ the unit

def test_unit_is_a_user_unit_for_this_farm_home(paths):
    text = service.unit_text(paths, sf_bin="/opt/sf/bin/sf")
    lines = directives(text)
    assert not re.search(r"@[A-Z_]+@", text)
    assert "ExecStart=/opt/sf/bin/sf serve" in lines and f"Environment=SUPERFARM_HOME={paths.home.resolve()}" in lines
    assert "WantedBy=default.target" in lines                # the user manager's target; multi-user.target is the system's
    assert "KillMode=process" in lines                       # a daemon restart must not kill the NVIDIA relay
    assert "Restart=on-failure" in lines and "LimitNOFILE=65536" in lines and "NoNewPrivileges=true" in lines


def test_unit_has_nothing_that_only_a_system_unit_may_have(paths):
    lines = directives(service.unit_text(paths, sf_bin="/opt/sf/bin/sf"))
    # A user manager cannot switch users or groups, and cannot order against system units.
    assert not [ln for ln in lines if ln.startswith(("User=", "Group=", "SupplementaryGroups="))]
    assert not [ln for ln in lines if "docker.service" in ln or "multi-user.target" in ln or "network-online" in ln]
    # The session comes from the user manager's environment, not from values frozen into the file.
    assert not [ln for ln in lines if re.match(r"Environment(File)?=.*(DISPLAY|XAUTHORITY|XDG_RUNTIME_DIR|DBUS|PULSE)", ln)]
    # Each of these puts a user unit into a user namespace, or hides the GPU / camera / X11 socket.
    assert not [ln for ln in lines if ln.startswith(("Protect", "Private", "DevicePolicy=", "BindReadOnlyPaths="))]


def test_unit_documents_linger_and_the_display_import(paths):
    text = resource_path("deploy", "systemd", "superfarm.service").read_text()
    assert "loginctl enable-linger" in text
    assert "systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY" in text
    assert "sf service install" in text


def test_default_entry_point_sits_next_to_the_interpreter(monkeypatch):
    assert service.default_sf_bin().name == "sf" and str(service.default_sf_bin().parent) == os.path.dirname(service.sys.executable)
    # Started as ../venv/bin/python the path must not keep the `..` (the unit outlives the folder it was typed in).
    monkeypatch.setattr(service.sys, "executable", "/srv/work/../venv/bin/python")
    assert str(service.default_sf_bin()) == "/srv/venv/bin/sf"


def test_paths_that_would_break_the_unit_are_refused(tmp_path):
    with pytest.raises(ValueError, match="whitespace"):
        service.unit_text(get_paths(tmp_path / "my farm"), sf_bin="/opt/sf/bin/sf")
    with pytest.raises(ValueError, match="not allowed"):
        service.unit_text(get_paths(tmp_path / "farm"), sf_bin="/opt/%h/sf")


def test_unit_dir_follows_xdg_config_home(tmp_path):
    assert service.unit_dir({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "systemd" / "user"
    assert service.unit_dir({}) == service.Path(os.path.expanduser("~")) / ".config" / "systemd" / "user"


# ------------------------------------------------------------------ install

def test_install_writes_the_unit_publishes_the_display_and_enables(tmp_path, paths, not_root):
    sd = Systemd()
    sf = tmp_path / "bin" / "sf"
    sf.parent.mkdir()
    sf.write_text("#!/bin/sh\n")
    sf.chmod(0o755)
    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "XAUTHORITY": ""}
    r = service.install(paths, sf_bin=sf, env=env, runner=sd)
    unit = tmp_path / "config" / "systemd" / "user" / "superfarm.service"
    assert r["unit"] == str(unit) and r["changed"] and r["enabled"]
    assert unit.read_text() == service.unit_text(paths, sf_bin=sf) and unit.stat().st_mode & 0o777 == 0o644
    # Order matters: the manager must know the unit before it is enabled. Only variables that are
    # set are published (an empty XAUTHORITY would erase a good one).
    assert sd.verbs() == [["daemon-reload"], ["import-environment", "DISPLAY", "WAYLAND_DISPLAY"],
                          ["enable", "superfarm.service"]]
    assert r["imported"] == ["DISPLAY", "WAYLAND_DISPLAY"]
    assert r["linger"] is False and any("loginctl enable-linger" in n for n in r["notes"])
    assert not any("does not exist" in n for n in r["notes"])


def test_install_again_changes_nothing_but_still_reloads(tmp_path, paths, not_root):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "DISPLAY": ":0"}
    service.install(paths, sf_bin="/opt/sf/bin/sf", env=env, runner=Systemd())
    unit = tmp_path / "config" / "systemd" / "user" / "superfarm.service"
    stamp = unit.stat().st_mtime_ns
    sd = Systemd(linger="yes")
    r = service.install(paths, sf_bin="/opt/sf/bin/sf", env=env, runner=sd)
    assert r["changed"] is False and unit.stat().st_mtime_ns == stamp
    assert ["daemon-reload"] in sd.verbs() and r["linger"] is True
    assert not any("enable-linger" in n for n in r["notes"])
    assert any("does not exist yet" in n for n in r["notes"])           # /opt/sf/bin/sf is not there


def test_install_from_a_login_without_display_says_so(tmp_path, paths, not_root):
    sd = Systemd()
    r = service.install(paths, sf_bin="/opt/sf/bin/sf", env={"XDG_CONFIG_HOME": str(tmp_path)}, runner=sd, enable=False)
    assert r["imported"] == [] and not any(v[0] == "import-environment" for v in sd.verbs())
    assert not any(v[0] == "enable" for v in sd.verbs()) and r["enabled"] is False
    assert any("no display in this login" in n for n in r["notes"])


def test_install_refuses_root(paths, monkeypatch):
    monkeypatch.setattr(service.os, "geteuid", lambda: 0)
    with pytest.raises(service.ServiceError, match="not as root"):
        service.install(paths, runner=Systemd())


def test_install_without_a_user_manager_explains(tmp_path, paths, not_root):
    with pytest.raises(service.ServiceError, match="daemon-reload failed: Failed to connect.*no systemd user session"):
        service.install(paths, sf_bin="/x/sf", env={"XDG_CONFIG_HOME": str(tmp_path)}, runner=Systemd(bus=False))
    with pytest.raises(service.ServiceError, match="systemctl is not usable.*sf serve"):
        service.install(paths, sf_bin="/x/sf", env={"XDG_CONFIG_HOME": str(tmp_path)}, runner=Systemd(missing=True))


def test_install_reports_an_unwritable_unit_folder(tmp_path, paths, not_root):
    (tmp_path / "config").write_text("a file where a folder is needed")
    with pytest.raises(service.ServiceError, match="cannot write"):
        service.install(paths, sf_bin="/x/sf", env={"XDG_CONFIG_HOME": str(tmp_path / "config")}, runner=Systemd())


# ------------------------------------------------------------------ status

def test_manager_variables_are_names_only():
    assert service.manager_variables(Systemd()) >= {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"}
    assert service.manager_variables(Systemd(env="HOME=/h\n")) == {"HOME"}
    assert service.manager_variables(Systemd(bus=False)) is None
    assert service.manager_variables(Systemd(missing=True)) is None


def test_status_reports_unit_state_linger_and_display(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    s = service.status(env=env, runner=Systemd(active="active", enabled="enabled", linger="yes"))
    assert s == {"unit": str(tmp_path / "systemd" / "user" / "superfarm.service"), "installed": False, "active": "active",
                 "enabled": "enabled", "linger": True, "manager": True, "display": ["DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"]}
    headless = service.status(env=env, runner=Systemd(env="HOME=/h\nXDG_RUNTIME_DIR=/run/user/1000\n"))
    assert headless["display"] == [] and headless["manager"] is True and headless["linger"] is False
    none = service.status(env=env, runner=Systemd(missing=True))
    assert none["manager"] is False and none["active"] == "unknown" and none["linger"] is None


def test_module_entry_point_shows_the_unit(paths, capsys):
    assert service.main(["show", "--home", str(paths.home)]) == 0
    out = capsys.readouterr().out
    assert "[Service]" in out and f"Environment=SUPERFARM_HOME={paths.home.resolve()}" in out


# ------------------------------------------------------------------ command names in fix texts

# Addendum 5 of the build spec: the commands the `sf` front end has. A fix line that names
# anything else sends the operator to a command that does not exist.
CLI = {
    "doctor": None, "serve": None, "version": None, "token": None, "recreate": None, "ls": None, "start": None,
    "stop": None, "restart": None, "top": None, "tasks": None, "open": None,
    "host": {"plan", "apply", "capacity", "gpus", "sign-module"},
    "config": {"get", "set", "unset", "show", "path", "docs"},
    "service": {"install", "start", "stop", "status", "logs"},
    "backup": {"create", "list", "verify", "restore", "prune", "farm"},
}
HOST_SOURCES = [*sorted((CHECKOUT / "superfarm" / "host").glob("*.py")), CHECKOUT / "install.sh",
                *sorted((CHECKOUT / "deploy" / "systemd").glob("*.service")),
                *sorted((CHECKOUT / "scripts" / "host").glob("*")), CHECKOUT / "native" / "v4l2loopback" / "build.sh"]


@pytest.mark.parametrize("source", HOST_SOURCES, ids=lambda p: p.name)
def test_every_sf_command_named_in_host_texts_exists(source):
    text = source.read_text()
    assert "sudo sf " not in text.replace("`sudo sf`", "")        # `sf host apply` asks for sudo itself
    found = re.findall(r"(?<![\w/.-])sf ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", text)
    for first, second in found:
        assert first in CLI, f"{source.name}: `sf {first}` is not a command"
        if CLI[first] is not None:
            assert second in CLI[first], f"{source.name}: `sf {first} {second}` is not a command"

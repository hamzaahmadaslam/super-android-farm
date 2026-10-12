"""The daemon as a systemd *user* service.

Why a user service (and not a system unit with `User=`): native device windows and
"listen to a device" belong to the desktop session of the person at the machine. That
session's variables live in the user's own service manager:

* XDG_RUNTIME_DIR and the session bus are always there, and the PipeWire / PulseAudio
  sockets are found through XDG_RUNTIME_DIR, so sound needs nothing else;
* DISPLAY, WAYLAND_DISPLAY and XAUTHORITY are published to the user manager by the
  desktop at login (GNOME, KDE and the distributions' xinit scripts do it themselves;
  `import-environment` does it by hand for the rest).

A user service inherits all of that when it starts, with no file to keep in sync and no
root. The price: the service lives and dies with the user manager, which without
`loginctl enable-linger` means from first login to last logout.

install() writes the unit from deploy/systemd/superfarm.service, reloads the user manager,
publishes the calling session's display variables to it and enables the unit. Nothing
here needs or accepts root.

    python -m superfarm.host.service install|status      (what `sf service install` builds on)
"""
from __future__ import annotations

import argparse
import os
import pwd
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from ..paths import Paths, get_paths
from ..resources import resource_path
from ..util import proc
from ..util.files import atomic_write
from .setup import render_unit

UNIT = "superfarm.service"
# What a window needs from the desktop session. XDG_RUNTIME_DIR and the bus address are
# set by the user manager itself and are never imported.
DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")


class ServiceError(RuntimeError):
    """The user service cannot be managed here; the message says why and what to do."""


def unit_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where systemd looks for a user's own units."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "systemd" / "user"


def default_sf_bin() -> Path:
    """The `sf` entry point installed next to the running interpreter (a virtual environment's bin/).

    Made absolute without following symbolic links: a virtual environment's python is a link
    to the system interpreter, and following it would leave the environment."""
    return Path(os.path.abspath(sys.executable)).parent / "sf"


def unit_text(paths: Paths, *, sf_bin: str | os.PathLike | None = None) -> str:
    """The user unit for this farm home, rendered from the shipped template."""
    template = resource_path("deploy", "systemd", UNIT).read_text()
    return render_unit(template, superfarm_home=paths.home.expanduser().resolve(), sf_bin=sf_bin or default_sf_bin())


def _systemctl(runner: Callable[..., proc.Result], *args: str, check: bool = True) -> proc.Result:
    try:
        r = runner(["systemctl", "--user", *args], timeout=60, check=False)
    except proc.CommandError as e:
        raise ServiceError(f"systemctl is not usable ({e}); without systemd run the daemon with `sf serve` "
                           "under your own supervisor") from None
    if check and not r.ok:
        why = (r.stderr or r.stdout).strip().splitlines()[-1:] or [f"exit {r.code}"]
        hint = ""
        if "bus" in why[0].lower():
            # Typical under `su`, in a container, or over SSH on a system without a user manager.
            hint = (" (no systemd user session for this login: log in directly as this user, or set "
                    "XDG_RUNTIME_DIR=/run/user/$(id -u))")
        raise ServiceError(f"systemctl --user {' '.join(args)} failed: {why[0]}{hint}")
    return r


def manager_variables(runner: Callable[..., proc.Result] | None = None) -> set[str] | None:
    """Names of the variables the user manager hands to a service started now; None when
    there is no user manager to ask. Only names: values can name session cookie files and
    are of no use to the caller."""
    try:
        r = _systemctl(runner or proc.run, "show-environment")
    except ServiceError:
        return None
    return {line.partition("=")[0] for line in r.stdout.splitlines() if "=" in line}


def linger(user: str | None = None, runner: Callable[..., proc.Result] | None = None) -> bool | None:
    """Whether the user's manager (and so the service) runs without a login. None = unknown."""
    user = user or pwd.getpwuid(os.getuid()).pw_name
    runner = runner or proc.run
    try:
        r = runner(["loginctl", "show-user", user, "--property=Linger", "--value"], timeout=10, check=False)
    except proc.CommandError:
        return None
    value = r.stdout.strip().lower()
    return {"yes": True, "no": False}.get(value) if r.ok else None


def install(paths: Paths, *, sf_bin: str | os.PathLike | None = None, enable: bool = True,
            env: Mapping[str, str] | None = None, runner: Callable[..., proc.Result] | None = None,
            directory: Path | None = None) -> dict:
    """Install (or refresh) the user unit. Safe to repeat.

    Returns {unit, changed, enabled, imported, linger, notes}: `imported` are the display
    variables of the calling session that were published to the user manager; `notes` are
    the things the operator should know, in plain sentences."""
    if os.geteuid() == 0:
        raise ServiceError("the daemon is a service of your desktop user: run this as that user, not as root "
                           "or with sudo (root has no display and no sound)")
    env = os.environ if env is None else env
    runner = runner or proc.run
    sf = Path(sf_bin) if sf_bin else default_sf_bin()
    text = unit_text(paths, sf_bin=sf)
    target = (directory or unit_dir(env)) / UNIT
    try:
        changed = not target.is_file() or target.read_text() != text
        if changed:
            atomic_write(target, text, mode=0o644)
    except OSError as e:
        raise ServiceError(f"cannot write {target}: {e.strerror or e}") from None
    _systemctl(runner, "daemon-reload")
    # Publish this session's display to the user manager so a start from here on has it.
    # Desktops that do this themselves make it a no-op; from an SSH login there is nothing
    # to publish and the step is skipped.
    present = [v for v in DISPLAY_VARS if env.get(v)]
    if present:
        _systemctl(runner, "import-environment", *present)
    if enable:
        _systemctl(runner, "enable", UNIT)
    lingering = linger(runner=runner)
    notes: list[str] = []
    if not os.access(sf, os.X_OK):
        notes.append(f"{sf} does not exist yet; the service cannot start until the `sf` command is installed there")
    if not present:
        notes.append("no display in this login (DISPLAY / WAYLAND_DISPLAY are unset): device windows need the "
                     "service to be started, or restarted once, from inside your desktop session")
    if lingering is False:
        notes.append("the service starts at your first login and stops at your last logout; to run it from boot "
                     "and keep it through logouts: loginctl enable-linger \"$USER\"")
    return {"unit": str(target), "changed": changed, "enabled": enable, "imported": present,
            "linger": lingering, "notes": notes}


def status(*, env: Mapping[str, str] | None = None, runner: Callable[..., proc.Result] | None = None,
           directory: Path | None = None) -> dict:
    """What `sf service status` and the doctor show. Never raises."""
    env = os.environ if env is None else env
    runner = runner or proc.run
    target = (directory or unit_dir(env)) / UNIT
    out: dict = {"unit": str(target), "installed": target.is_file(), "active": "unknown", "enabled": "unknown",
                 "linger": linger(runner=runner), "manager": False, "display": []}
    names = manager_variables(runner)
    if names is None:
        return out
    out["manager"] = True
    out["display"] = [v for v in DISPLAY_VARS if v in names]
    for key, verb in (("active", "is-active"), ("enabled", "is-enabled")):
        try:
            out[key] = _systemctl(runner, verb, UNIT, check=False).stdout.strip() or "unknown"
        except ServiceError:
            pass
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="superfarm.host.service",
                                 description="Install the Super Farm daemon as a systemd user service.")
    ap.add_argument("command", choices=["install", "status", "show"])
    ap.add_argument("--home", default=None, help="farm home (default: $SUPERFARM_HOME)")
    ap.add_argument("--no-enable", action="store_true", help="install the unit without enabling it")
    args = ap.parse_args(argv)
    paths = get_paths(args.home)
    try:
        if args.command == "show":
            print(unit_text(paths), end="")
            return 0
        if args.command == "status":
            for key, value in status().items():
                print(f"{key}: {', '.join(value) if isinstance(value, list) else value}")
            return 0
        result = install(paths, enable=not args.no_enable)
    except (ServiceError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{'installed' if result['changed'] else 'unchanged'}: {result['unit']}"
          + (" (enabled: starts with your session)" if result["enabled"] else ""))
    if result["imported"]:
        print(f"published to the user manager: {', '.join(result['imported'])}")
    for note in result["notes"]:
        print(f"note: {note}")
    print("start it now:  systemctl --user start superfarm    (or: sf service start)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

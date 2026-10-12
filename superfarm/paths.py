"""Where Super Farm keeps things.

Everything lives under one *home* directory so a farm can be moved, backed up or
run twice on one machine (a second home = a second, fully separate farm).

    $SUPERFARM_HOME            default: ~/.local/share/superfarm
      config.yaml              all settings (see superfarm.config)
      secrets/                 0700; VPN credentials, API tokens (files 0600)
      instances/<id>/          one folder per Android: instance.yaml + runtime state
      apps/                    the app library (APK / XAPK / APKS / APKM files)
      vpn/                     VPN profiles (.ovpn, .conf)
      media/                   videos and audio usable as camera / microphone sources
      automation/              datasets, trained programs, flows, values, fix memory
      overlays/                files layered onto Android images (GPU runtime, camera HAL)
      backups/                 default backup destination
      logs/                    daemon, audit and task logs
      run/                     sockets, FIFOs, pid files (recreated at start)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
# Shell scripts, Dockerfiles and native sources shipped with the project. In a source
# checkout they sit next to the package; in an installed wheel they are package data.
REPO_DIR = PACKAGE_DIR.parent


def default_home() -> Path:
    env = os.environ.get("SUPERFARM_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "superfarm"


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def config(self) -> Path: return self.home / "config.yaml"
    @property
    def secrets(self) -> Path: return self.home / "secrets"
    @property
    def instances(self) -> Path: return self.home / "instances"
    @property
    def apps(self) -> Path: return self.home / "apps"
    @property
    def vpn(self) -> Path: return self.home / "vpn"
    @property
    def media(self) -> Path: return self.home / "media"
    @property
    def automation(self) -> Path: return self.home / "automation"
    @property
    def overlays(self) -> Path: return self.home / "overlays"
    @property
    def backups(self) -> Path: return self.home / "backups"
    @property
    def logs(self) -> Path: return self.home / "logs"
    @property
    def run(self) -> Path: return self.home / "run"

    def instance(self, instance_id: int) -> Path:
        return self.instances / str(int(instance_id))

    def ensure(self) -> Paths:
        """Create the tree. Secrets are owner-only; everything else is owner + group."""
        for d in (self.home, self.instances, self.apps, self.vpn, self.media, self.automation,
                  self.overlays, self.backups, self.logs, self.run):
            d.mkdir(parents=True, exist_ok=True)
        self.secrets.mkdir(parents=True, exist_ok=True)
        os.chmod(self.secrets, 0o700)
        return self


def get_paths(home: str | os.PathLike | None = None) -> Paths:
    return Paths(Path(home).expanduser() if home else default_home())

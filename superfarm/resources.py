"""Where the files that ship next to the Python package are: Dockerfiles, systemd unit
templates, helper scripts and the native sources.

    deploy/    Docker build contexts, systemd unit templates
    scripts/   helper programs started by the daemon (camera feeders) or by the operator
    native/    sources, patches and build scripts for the GPU relay, camera HAL and v4l2loopback

They exist in two layouts, and callers must not care which one they are running from:

* a source checkout (also an editable install): the three folders sit next to the
  `superfarm` package folder;
* an installed wheel: `pyproject.toml` ships them as data files, which every installer puts
  under `<install prefix>/share/superfarm-resources/`. The package itself is then at
  `<install prefix>/lib/pythonX.Y/site-packages/superfarm`, so the prefix is three folders
  above it. That relation holds for a virtual environment, `pip install --user` and a
  system-wide install alike, which is why no installer metadata has to be read.

The folder name is deliberately not `share/superfarm`: with `pip install --user` that would be
`~/.local/share/superfarm`, the default farm home.
"""
from __future__ import annotations

import sysconfig
from functools import lru_cache
from pathlib import Path

from .paths import PACKAGE_DIR

TOP_LEVEL = ("deploy", "scripts", "native")
INSTALLED_SUBDIR = Path("share") / "superfarm-resources"


def _candidates() -> list[Path]:
    out = [PACKAGE_DIR.parent]                                  # source checkout / editable install
    if len(PACKAGE_DIR.parents) > 3:
        out.append(PACKAGE_DIR.parents[3] / INSTALLED_SUBDIR)   # wheel: <prefix>/lib/pythonX.Y/site-packages/superfarm
    data = sysconfig.get_path("data")                           # the running interpreter's own prefix, as a fallback
    if data:
        out.append(Path(data) / INSTALLED_SUBDIR)
    return out


@lru_cache(maxsize=1)
def resource_root() -> Path:
    """The folder holding deploy/, scripts/ and native/.

    When none is found (a broken or partial install) the checkout location is returned, so
    the "file is missing" message a caller then raises names a path a person can make sense of."""
    for root in _candidates():
        # deploy/systemd is required, not just deploy/: an unrelated `deploy` folder in
        # site-packages must not be mistaken for ours.
        if (root / "deploy" / "systemd").is_dir():
            return root
    return PACKAGE_DIR.parent


def resource_path(*parts: str) -> Path:
    """Path of a shipped file or folder, e.g. resource_path("deploy", "docker", "net").

    The path is returned whether or not it exists; callers already report a missing file
    in their own words. Only the three shipped top-level folders can be named."""
    if not parts or parts[0] not in TOP_LEVEL:
        raise ValueError(f"resource paths start with one of {TOP_LEVEL}, got {parts[:1]!r}")
    for p in parts:
        if not p or p in (".", "..") or "/" in p or "\\" in p:
            raise ValueError(f"bad resource path component {p!r}")
    return resource_root().joinpath(*parts)

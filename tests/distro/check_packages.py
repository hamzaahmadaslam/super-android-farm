"""Ask this distribution's package manager whether every package the farm would install exists.

Run inside a throwaway container by tests/distro/inside.sh, with the project's virtual
environment. Nothing is installed here: each name goes through the package manager's own
resolver (a simulated or dry-run install where there is one), because that is exactly what
`sf host apply` relies on, including names that are provided by another package.

Exit code 0 when every name resolves or is a documented exception, 1 otherwise.
"""
from __future__ import annotations

import subprocess
import sys

from superfarm.host import distro

# Named after the running kernel. A container runs on the host's kernel, for which another
# distribution has no package, so these cannot be judged here.
KERNEL_BOUND = {"kernel-headers"}
# zypper tells "no such package" (exit 104) apart from "it exists, but installing it here
# needs a decision" (exit 4: a dependency problem). The second is not a wrong name; it is
# reported with zypper's own words so a person can judge it.
ZYPPER_DEPENDENCY_PROBLEM = 4
CONFLICT = "exists, but cannot be installed here without a choice"


def resolves(family: str, name: str) -> tuple[bool, str]:
    """(resolvable, the package manager's last lines when it is not)."""
    if family == "debian":
        argv = ["apt-get", "install", "--simulate", "--no-install-recommends", name]
    elif family == "fedora":
        argv = ["dnf", "-q", "repoquery", "--whatprovides", name]
    elif family == "arch":
        argv = ["pacman", "-Sp", "--print-format", "%n", name]
    elif family == "suse":
        argv = ["zypper", "--non-interactive", "install", "--dry-run", "--no-recommends", name]
    else:
        raise SystemExit(f"no package manager known for family {family!r}")
    r = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    ok = r.returncode == 0 and (family != "fedora" or bool(r.stdout.strip()))
    said = " | ".join(line.strip() for line in (r.stdout + r.stderr).strip().splitlines()[-14:] if line.strip())
    if family == "suse" and r.returncode == ZYPPER_DEPENDENCY_PROBLEM:
        return False, f"{CONFLICT}: {said[:1200]}"
    return ok, "" if ok else f"exit {r.returncode}: {said[:600]}"


def main() -> int:
    dist = distro.detect()
    print(f"{dist.name}: family {dist.family}, package manager {dist.pkg_manager}")
    if not dist.supported:
        print("unsupported family")
        return 1
    bad: list[str] = []
    for key in distro.PACKAGE_KEYS:
        ps = dist.packages(key)
        if key in KERNEL_BOUND:
            print(f"  skip  {key:16} {' '.join(ps.names)}  (named after the running kernel; not checkable in a container)")
            continue
        if not ps.names:
            print(f"  hint  {key:16} not packaged here; the installer prints: {ps.hints.get(key, '')[:90]}")
            continue
        results = {n: resolves(dist.family, n) for n in ps.names}
        missing = [n for n, (ok, _) in results.items() if not ok]
        for n in missing:
            print(f"        {dist.pkg_manager} said about {n}: {results[n][1]}")
        if not missing:
            print(f"  ok    {key:16} {' '.join(ps.names)}")
        elif all(results[n][1].startswith(CONFLICT) for n in missing):
            print(f"  warn  {key:16} {' '.join(missing)}: the name is right, the install needs a choice (see above)")
        elif key in ps.hints:
            # The table carries a "your release may not have it" hint for this key.
            print(f"  warn  {key:16} not in this release: {' '.join(missing)} (the installer prints a hint)")
        else:
            print(f"  FAIL  {key:16} no such package: {' '.join(missing)}  (of: {' '.join(ps.names)})")
            bad += [f"{key}:{n}" for n in missing]
    print("unresolved: " + (", ".join(bad) if bad else "none"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

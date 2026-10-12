#!/usr/bin/env bash
# Super Farm installer: a thin bootstrap.
#
#   ./install.sh [--dry-run] [--home DIR] [--venv DIR] [--no-host] [--dev] [--container-test]
#
# Run it as your normal desktop user from a checkout of the project. It:
#   1. finds out which distribution this is (apt / dnf / pacman / zypper),
#   2. makes sure Python >= 3.10 with venv support exists (asks sudo for that one install),
#   3. creates a virtual environment and installs the project into it,
#   4. hands over to host setup (the same code as `sf host apply`), which does everything
#      that needs root and is the single place that knows how: packages, kernel modules,
#      kernel limits, your user's docker/video/render groups, the boot unit,
#   5. prints what to do next.
#
# Safe to run again at any time: every step checks what is already there.
# --dry-run prints every command (including every command and file of step 4) and changes nothing.
#
# The daemon itself never runs as root. Root is used for host setup only.
#
# --container-test (or SF_INSTALL_NO_SYSTEM=1) checks the installer on another distribution
# inside a throwaway container (tests/distro/run.sh). It runs as root without sudo, really
# installs the distribution packages and the virtual environment, and skips everything a
# container does not have: kernel modules, kernel limits, groups, systemd.
# Do not use it on a real machine: the result is not a working farm host.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DRY=0
HOST=1
DEV=0
NO_SYSTEM=0
case "${SF_INSTALL_NO_SYSTEM:-0}" in 1|true|yes) NO_SYSTEM=1 ;; esac
VENV="$REPO/.venv"
FARM_HOME="${SUPERFARM_HOME:-}"

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
say()  { printf '\n== %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# Print a command, then run it unless this is a dry run. Arguments are passed as an
# argv array: nothing is ever re-parsed by a shell.
run() {
  printf '+'; printf ' %q' "$@"; printf '\n'
  [ "$DRY" = 1 ] || "$@"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --no-host) HOST=0 ;;
    --dev)     DEV=1 ;;
    --container-test) NO_SYSTEM=1 ;;
    --home)    [ $# -ge 2 ] || die "--home needs a folder"; FARM_HOME=$2; shift ;;
    --venv)    [ $# -ge 2 ] || die "--venv needs a folder"; VENV=$2; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         die "unknown option $1 (see --help)" ;;
  esac
  shift
done

[ "$(uname -s)" = Linux ] || die "Super Farm runs on Linux only."
[ "$(uname -m)" = x86_64 ] || die "Super Farm needs an x86_64 machine (this is $(uname -m))."
# Commands that need root are prefixed with this. Empty in a container test, where the
# script already is root and sudo does not exist.
SUDO=(sudo)
if [ "$NO_SYSTEM" = 1 ]; then
  [ "$(id -u)" = 0 ] || die "--container-test runs as root inside a throwaway container (see tests/distro/run.sh)."
  SUDO=()
elif [ "$(id -u)" = 0 ]; then
  die "run this as your normal desktop user, not as root or with sudo. It asks for sudo itself where needed."
fi
as_root() { run ${SUDO[@]+"${SUDO[@]}"} "$@"; }
[ -f "$REPO/pyproject.toml" ] || die "$REPO is not a Super Farm checkout (pyproject.toml missing)."
case "$VENV$FARM_HOME" in
  *[[:space:]]*) die "paths with spaces are not supported for --venv / --home (systemd units cannot name them)." ;;
esac

# ------------------------------------------------------------------ 1. distribution
say "Distribution"
ID=unknown; ID_LIKE=""; PRETTY_NAME="Linux"
if [ -r /etc/os-release ]; then
  # Read in a subshell-free but harmless way: os-release is a list of shell assignments by definition.
  # shellcheck disable=SC1091
  . /etc/os-release
elif [ -r /usr/lib/os-release ]; then
  # shellcheck disable=SC1091
  . /usr/lib/os-release
fi
FAMILY=unknown
for word in $ID $ID_LIKE; do
  case "$word" in
    debian|ubuntu|linuxmint|pop|raspbian)             FAMILY=debian; break ;;
    fedora|rhel|centos|rocky|almalinux|ol|amzn)       FAMILY=fedora; break ;;
    arch|manjaro|endeavouros|cachyos|garuda)          FAMILY=arch;   break ;;
    suse|opensuse*|sles)                              FAMILY=suse;   break ;;
  esac
done
echo "$PRETTY_NAME (family: $FAMILY, kernel $(uname -r))"
[ "$FAMILY" != unknown ] || warn "unknown distribution: packages must be installed by hand; continuing with what is already here."

# ------------------------------------------------------------------ 2. Python
say "Python"
python_ok() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || return 1
  # Debian and Ubuntu ship venv/ensurepip in a separate package.
  python3 -c 'import venv, ensurepip' >/dev/null 2>&1
}
if python_ok; then
  echo "$(python3 --version) with venv support"
else
  echo "Python >= 3.10 with venv support is missing; installing it."
  case "$FAMILY" in
    # python3-dev: needed as soon as one dependency has no prebuilt wheel for this interpreter.
    debian) as_root apt-get update
            as_root apt-get install -y --no-install-recommends python3 python3-venv python3-dev ;;
    fedora) as_root dnf install -y python3 python3-devel ;;
    arch)   # A real system has a package database and must not get a newer one without a
            # full upgrade (pacman's "partial upgrade" trap). A fresh container has none.
            [ "$NO_SYSTEM" = 0 ] || as_root pacman -Sy --noconfirm
            as_root pacman -S --needed --noconfirm python ;;
    suse)   as_root zypper --non-interactive install --no-recommends python3 python3-devel ;;
    *)      die "install Python >= 3.10 with the venv module, then run this again." ;;
  esac
  [ "$DRY" = 1 ] || python_ok || die "Python >= 3.10 with venv is still not available. Your distribution may be too old (Python $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo '?'))."
fi

# ------------------------------------------------------------------ 3. virtual environment
say "Virtual environment: $VENV"
if [ -x "$VENV/bin/python" ]; then
  echo "exists"
else
  run python3 -m venv "$VENV"
fi
run "$VENV/bin/python" -m pip install --quiet --upgrade pip
if [ "$DEV" = 1 ]; then
  run "$VENV/bin/python" -m pip install --quiet -e "$REPO[dev]"
else
  # Editable on purpose: an update is `git pull`, with no reinstall. (A plain wheel install
  # works too; superfarm.resources finds the Dockerfiles and scripts in either layout.)
  run "$VENV/bin/python" -m pip install --quiet -e "$REPO"
fi

if [ -z "$FARM_HOME" ]; then
  FARM_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/superfarm"
fi
case "$FARM_HOME" in
  *[[:space:]]*) die "the farm home '$FARM_HOME' contains spaces; pass --home with a path without spaces." ;;
esac
echo "farm home: $FARM_HOME"

# ------------------------------------------------------------------ 4. host setup (root)
# `sf host apply` and `python -m superfarm.host.setup apply` are the same code; the module
# form is used so this script does not depend on the command-line front end.
host_apply() {
  env_args=("SUPERFARM_HOME=$FARM_HOME" "SUPERFARM_USER=$(id -un)")
  setup=("$VENV/bin/python" -m superfarm.host.setup apply)
  # A container has no kernel of its own, no systemd and no desktop user: packages only.
  [ "$NO_SYSTEM" = 0 ] || setup+=(--only packages)
  if [ "$DRY" = 1 ]; then
    echo "+ ${SUDO[*]+${SUDO[*]} }env ${env_args[*]} ${setup[*]}"
    if [ -x "$VENV/bin/python" ]; then
      echo "  which would do the following:"
      env "${env_args[@]}" "${setup[@]}" --dry-run | sed 's/^/  /'
    else
      echo "  (the virtual environment does not exist yet, so the individual commands cannot be listed;"
      echo "   after a real run of step 3:  ${setup[*]} --dry-run)"
    fi
    return 0
  fi
  ${SUDO[@]+"${SUDO[@]}"} env "${env_args[@]}" "${setup[@]}"
}

if [ "$HOST" = 1 ]; then
  if [ "$NO_SYSTEM" = 1 ]; then
    say "Host setup: packages only (container test: no kernel modules, limits, groups or systemd)"
  else
    say "Host setup (needs root: packages, kernel modules, limits, groups, boot unit)"
  fi
  if ! host_apply; then
    warn "host setup reported problems (see above). Fix them and run:  $VENV/bin/sf host apply"
    # The container test exists to find exactly these problems; a real install carries on
    # so the operator still gets the next steps.
    [ "$NO_SYSTEM" = 0 ] || exit 3
  fi
else
  say "Host setup skipped (--no-host)"
  echo "Run it later:  SUPERFARM_HOME=$FARM_HOME $VENV/bin/sf host apply"
fi

# ------------------------------------------------------------------ 5. next steps
if [ "$NO_SYSTEM" = 1 ]; then
  say "Container test: packages and the virtual environment are installed; nothing else was set up."
  exit 0
fi
say "Next steps"
cat <<EOF
  1. Log out and in again if your user was just added to the docker / video / render groups.
  2. Check the machine:          $VENV/bin/sf doctor
  3. Start the farm:             $VENV/bin/sf serve          (panel on http://127.0.0.1:7700)
     or as your own service:     $VENV/bin/sf service install && $VENV/bin/sf service start
                                 It starts when you log in. To run it from boot and keep it
                                 through logouts:  loginctl enable-linger "\$USER"
  4. Optional, NVIDIA cards:     $REPO/native/venus/build.sh all
     Optional, virtual cameras:  sudo $REPO/native/v4l2loopback/build.sh install
                                 (with Secure Boot: $VENV/bin/sf host sign-module -- v4l2loopback)

  Put $VENV/bin on your PATH (or: ln -s $VENV/bin/sf ~/.local/bin/sf) to type just \`sf\`.
EOF
[ "$DRY" = 0 ] || printf '\nDry run: nothing was changed.\n'

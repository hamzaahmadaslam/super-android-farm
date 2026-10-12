#!/usr/bin/env bash
# Build the virtual camera kernel module (v4l2loopback) with the farm's patch.
#
#   native/v4l2loopback/build.sh <step> [options]
#
#   steps    build     fetch upstream v0.15.1 (pinned commit), apply the patch, compile module + v4l2loopback-ctl
#            install   (root) build if needed, install the module for the kernel and v4l2loopback-ctl
#                      into /usr/local/bin. Repeat after every kernel update, or use `dkms` instead.
#            dkms      (root) register the patched source with DKMS so it is rebuilt (and, where DKMS
#                      has a signing key, signed) automatically at every kernel update
#            check     only verify that the patch applies to the pinned upstream commit
#            clean     delete the work folder
#
#   options  --kernel REL       kernel release to build for   (default: the running kernel)
#            --work DIR         where sources are kept        (default: ~/.cache/superfarm/v4l2loopback)
#            --source DIR       use this existing upstream checkout instead of downloading (must be at the pinned commit)
#            --max-devices N    most cameras the module can create (default: 256; upstream default is 8)
#
# With Secure Boot the installed module must also be signed before it loads:
#   sudo scripts/host/sign-module.sh v4l2loopback
#
# This script never loads or unloads a module. `sudo sf host apply` does the loading.
# v4l2loopback and the patch in this folder are GPL-2.0-or-later (see COPYING and README.md).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UPSTREAM=https://github.com/v4l2loopback/v4l2loopback.git
TAG=v0.15.1
COMMIT=7546a42a66d856933a75c2b0896cb735e3ca00c0      # what tag v0.15.1 points at; the checkout is refused if it differs
VERSION=0.15.1
PATCH="$HERE/v4l2loopback-$VERSION-farm.patch"
DKMS_VERSION="$VERSION.superfarm"

KREL=$(uname -r)
WORK=${SUPERFARM_V4L2_WORK:-${XDG_CACHE_HOME:-$HOME/.cache}/superfarm/v4l2loopback}
SOURCE=""
MAX_DEVICES=256
STEP=""

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required${2:+ ($2)}"; }
usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --kernel)      [ $# -ge 2 ] || die "--kernel needs a release"; KREL=$2; shift ;;
    --work)        [ $# -ge 2 ] || die "--work needs a folder"; WORK=$2; shift ;;
    --source)      [ $# -ge 2 ] || die "--source needs a folder"; SOURCE=$2; shift ;;
    --max-devices) [ $# -ge 2 ] || die "--max-devices needs a number"; MAX_DEVICES=$2; shift ;;
    -h|--help)     usage; exit 0 ;;
    -*)            die "unknown option $1 (see --help)" ;;
    *)             [ -z "$STEP" ] || die "one step at a time (got '$STEP' and '$1')"; STEP=$1 ;;
  esac
  shift
done
[ -n "$STEP" ] || { usage; exit 2; }
case "$KREL" in *[!A-Za-z0-9._+~-]*|"") die "bad kernel release '$KREL'" ;; esac
case "$MAX_DEVICES" in ''|*[!0-9]*) die "--max-devices must be a number" ;; esac
[ "$MAX_DEVICES" -ge 1 ] && [ "$MAX_DEVICES" -le 1024 ] || die "--max-devices must be between 1 and 1024"

KDIR="/lib/modules/$KREL/build"
TREE="$WORK/v4l2loopback-$VERSION"

headers_hint() {
  cat >&2 <<EOF
Kernel headers for $KREL are missing ($KDIR). Install them:
  Debian/Ubuntu:  sudo apt-get install linux-headers-$KREL build-essential
  Fedora/RHEL:    sudo dnf install kernel-devel-$KREL gcc make
  Arch:           sudo pacman -S linux-headers base-devel        (linux-lts-headers / linux-zen-headers for those kernels)
  openSUSE:       sudo zypper install kernel-default-devel gcc make
EOF
}

# A pristine tree at the pinned commit, with the patch applied exactly once.
prepare() {
  need git; need patch
  mkdir -p "$WORK"
  if [ -f "$TREE/.superfarm-patched" ] && [ "$(cat "$TREE/.superfarm-patched")" = "$(sha256sum <"$PATCH")" ]; then
    return 0
  fi
  rm -rf "$TREE"
  if [ -n "$SOURCE" ]; then
    [ -d "$SOURCE/.git" ] || die "$SOURCE is not a git checkout of v4l2loopback"
    # Local clone of committed history only: local edits in that checkout are not carried over.
    git -c advice.detachedHead=false clone --quiet --no-hardlinks "$SOURCE" "$TREE"
    git -C "$TREE" checkout --quiet --detach "$COMMIT" 2>/dev/null \
      || die "$SOURCE does not contain commit $COMMIT ($TAG)"
  else
    timeout 300 git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$TAG" "$UPSTREAM" "$TREE" \
      || die "could not download $UPSTREAM (network?). Pass --source DIR to use an existing checkout."
  fi
  local head
  head=$(git -C "$TREE" rev-parse HEAD)
  [ "$head" = "$COMMIT" ] || { rm -rf "$TREE"; die "upstream $TAG is commit $head, expected $COMMIT. The tag moved or the download was tampered with; nothing was built."; }
  git -C "$TREE" apply --check "$PATCH" || { rm -rf "$TREE"; die "$(basename "$PATCH") does not apply to $TAG"; }
  git -C "$TREE" apply "$PATCH"
  sha256sum <"$PATCH" >"$TREE/.superfarm-patched"
  echo "v4l2loopback $TAG ($COMMIT) patched in $TREE"
}

step_check() {
  say "Patch check"
  prepare
  echo "$(basename "$PATCH") applies cleanly to $TAG"
}

step_build() {
  say "Build for kernel $KREL"
  need make; need gcc "install your distribution's build tools"
  [ -d "$KDIR" ] || { headers_hint; exit 1; }
  prepare
  # MAX_DEVICES: upstream allows 8 cameras; a farm needs one per Android.
  ( cd "$TREE" && timeout 900 make KERNELRELEASE="$KREL" KERNEL_DIR="$KDIR" KCPPFLAGS="-DMAX_DEVICES=$MAX_DEVICES" v4l2loopback.ko utils )
  [ -f "$TREE/v4l2loopback.ko" ] || die "the module was not produced; see the compiler output above"
  echo "-> $TREE/v4l2loopback.ko"
  echo "-> $TREE/utils/v4l2loopback-ctl"
}

need_root() { [ "$(id -u)" = 0 ] || die "this step needs root: sudo $0 $STEP"; }

sb_note() {
  if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -qi enabled; then
    cat <<EOF

Secure Boot is on: the module will not load until it is signed with an enrolled key.
  sudo $(cd "$HERE/../.." && pwd)/scripts/host/sign-module.sh v4l2loopback
EOF
  fi
}

step_install() {
  need_root
  [ -f "$TREE/v4l2loopback.ko" ] && [ -f "$TREE/.superfarm-patched" ] || step_build
  say "Install for kernel $KREL"
  # modules_install puts it under /lib/modules/<release>/updates or extra, where modprobe finds it.
  # Under Secure Boot it prints a signing error (no kernel build key here); that is expected.
  ( cd "$TREE" && timeout 300 make KERNELRELEASE="$KREL" KERNEL_DIR="$KDIR" install ) || die "modules_install failed"
  depmod -a "$KREL"
  install -m 755 "$TREE/utils/v4l2loopback-ctl" /usr/local/bin/v4l2loopback-ctl
  echo "installed: $(modinfo -k "$KREL" -n v4l2loopback 2>/dev/null || echo 'module (path unknown)') and /usr/local/bin/v4l2loopback-ctl"
  sb_note
  echo
  echo "Next: sudo sf host apply     (loads the module and creates the camera devices)"
}

step_dkms() {
  need_root
  need dkms "package: dkms"
  prepare
  say "Register with DKMS as v4l2loopback/$DKMS_VERSION"
  local dst="/usr/src/v4l2loopback-$DKMS_VERSION"
  if dkms status "v4l2loopback/$DKMS_VERSION" 2>/dev/null | grep -q .; then
    timeout 300 dkms remove "v4l2loopback/$DKMS_VERSION" --all >/dev/null 2>&1 || true
  fi
  rm -rf "$dst"
  mkdir -p "$dst"
  # Source without git metadata: DKMS copies this tree for every kernel it builds for.
  git -C "$TREE" ls-files -z | ( cd "$TREE" && xargs -0 cp --parents -t "$dst" )
  cp "$TREE/v4l2loopback.c" "$TREE/v4l2loopback_formats.h" "$dst/"      # the patched files, not the committed ones
  sed -i -e "s/^PACKAGE_VERSION=.*/PACKAGE_VERSION=\"$DKMS_VERSION\"/" \
         -e "s|^MAKE\[0\]=.*|MAKE[0]=\"make KERNEL_DIR=\${kernel_source_dir} KCPPFLAGS=-DMAX_DEVICES=$MAX_DEVICES v4l2loopback\"|" \
         "$dst/dkms.conf"
  grep -q "MAX_DEVICES=$MAX_DEVICES" "$dst/dkms.conf" || die "could not adjust dkms.conf (upstream layout changed?)"
  timeout 120 dkms add "v4l2loopback/$DKMS_VERSION"
  timeout 900 dkms install "v4l2loopback/$DKMS_VERSION" -k "$KREL"
  if [ ! -x /usr/local/bin/v4l2loopback-ctl ]; then
    ( cd "$TREE" && timeout 300 make utils ) && install -m 755 "$TREE/utils/v4l2loopback-ctl" /usr/local/bin/v4l2loopback-ctl
  fi
  dkms status "v4l2loopback/$DKMS_VERSION" || true
  cat <<EOF

DKMS rebuilds the module at every kernel update. Under Secure Boot DKMS signs it with its own
key (usually /var/lib/dkms/mok.pub, on Debian/Ubuntu /var/lib/shim-signed/mok/MOK.der); that key
must be enrolled once:   sudo scripts/host/sign-module.sh --status     shows whether it is.
Next: sudo sf host apply
EOF
}

step_clean() {
  case "$WORK" in /|"$HOME"|"") die "refusing to delete '$WORK'" ;; esac
  rm -rf -- "$WORK"
  echo "removed $WORK"
}

case "$STEP" in
  build)   step_build ;;
  install) step_install ;;
  dkms)    step_dkms ;;
  check)   step_check ;;
  clean)   step_clean ;;
  *)       die "unknown step '$STEP' (see --help)" ;;
esac

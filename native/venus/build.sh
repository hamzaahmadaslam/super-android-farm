#!/usr/bin/env bash
# Build everything NVIDIA mode needs, reproducibly, from pinned sources.
#
#   native/venus/build.sh <step>... [options]
#
#   steps    all            fetch + images + driver + gralloc + relay + install-config + verify
#            fetch          download Mesa, libdrm, virglrenderer (pinned URL + sha256), unpack, apply the patches
#            images         build the two builder Docker images
#            driver         Mesa Venus Vulkan driver for Android  -> <home>/overlays/venus/vulkan.virtio.so
#            gralloc        the allocator, 64- and 32-bit         -> <home>/overlays/venus/gralloc.venus{,.x86}.so
#            relay          patched virgl_test_server             -> <home>/overlays/venus-relay/
#            install-config gpu_config.sh with the `venus` mode   -> <home>/overlays/venus/gpu_config.sh
#            tests          optional test programs (probe, fmts, ahbtest, yuvtest) -> <work>/tests
#            check-patches  only verify that both patches apply to the pristine sources
#            verify         list what was built
#            clean          delete the work folder (sources and build trees), keep the outputs
#
#   options  --home DIR        farm home                 (default: $SUPERFARM_HOME or ~/.local/share/superfarm)
#            --work DIR        sources and build trees   (default: ~/.cache/superfarm/venus-build, ~6 GB)
#            --ndk DIR         Android NDK r27c          (default: $ANDROID_NDK_HOME / $ANDROID_NDK_ROOT)
#            --fetch-ndk       download NDK r27c into the work folder (about 650 MB)
#            --native          build the relay on this host instead of in the builder image
#            --base-image IMG  base image of the builders (default: ubuntu:26.04)
#            --jobs N          parallel compile jobs     (default: all CPUs)
#
# Built for Android 13 x86_64 (API 33). The driver is 64-bit only: 32-bit apps have no
# NVIDIA driver. Only the allocator also exists in 32-bit, because Android's camera
# service is a 32-bit program. See README.md in this folder for the design.
#
# The relay must end up at the path it was configured for: its helper's location is
# compiled in. That is why it is built straight into <home>/overlays/venus-relay.
# Nothing here needs root, and nothing here touches a running relay or Android.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

MESA_VERSION=26.0.8
MESA_URL="https://archive.mesa3d.org/mesa-${MESA_VERSION}.tar.xz"
MESA_SHA256=caf1c0061a68e88dfa74967a7e780c0e85d65b6c4e334cd69095a5dc54ad78bc
LIBDRM_VERSION=2.4.131
LIBDRM_URL="https://dri.freedesktop.org/libdrm/libdrm-${LIBDRM_VERSION}.tar.xz"
LIBDRM_SHA256=45ba9983b51c896406a3d654de81d313b953b76e6391e2797073d543c5f617d5
VIRGL_VERSION=1.2.0
VIRGL_URL="https://gitlab.freedesktop.org/virgl/virglrenderer/-/archive/${VIRGL_VERSION}/virglrenderer-${VIRGL_VERSION}.tar.gz"
VIRGL_SHA256=8bae92909021ea87e2087ae96df322f90140c414bd5f38810af3f3e5146a567b
NDK_RELEASE=r27c
NDK_REVISION=27.2.12479018
NDK_URL="https://dl.google.com/android/repository/android-ndk-${NDK_RELEASE}-linux.zip"
# SHA-1 as published on the NDK downloads page. Override with NDK_SHA1=... if Google's page says otherwise.
NDK_SHA1=${NDK_SHA1:-090e8083a715fdb1a3e402d0763c388abb03fb4e}
API=33

IMAGE_ANDROID=superfarm/venus-builder
IMAGE_RELAY=superfarm/venus-builder-host
BASE_IMAGE=ubuntu:26.04
DOCKER=${DOCKER:-docker}

FARM_HOME=${SUPERFARM_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/superfarm}
WORK=${SUPERFARM_VENUS_WORK:-${XDG_CACHE_HOME:-$HOME/.cache}/superfarm/venus-build}
NDK=${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}
FETCH_NDK=0
NATIVE=0
JOBS=$(nproc 2>/dev/null || echo 4)
STEP_TIMEOUT=${STEP_TIMEOUT:-3600}      # seconds one compile step may take before it is stopped
STEPS=()

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required${2:+ ($2)}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --home)       [ $# -ge 2 ] || die "--home needs a folder"; FARM_HOME=$2; shift ;;
    --work)       [ $# -ge 2 ] || die "--work needs a folder"; WORK=$2; shift ;;
    --ndk)        [ $# -ge 2 ] || die "--ndk needs a folder"; NDK=$2; shift ;;
    --fetch-ndk)  FETCH_NDK=1 ;;
    --native)     NATIVE=1 ;;
    --base-image) [ $# -ge 2 ] || die "--base-image needs an image"; BASE_IMAGE=$2; shift ;;
    --jobs)       [ $# -ge 2 ] || die "--jobs needs a number"; JOBS=$2; shift ;;
    -h|--help)    sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)           die "unknown option $1 (see --help)" ;;
    *)            STEPS+=("$1") ;;
  esac
  shift
done
[ ${#STEPS[@]} -gt 0 ] || { sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2; }
case "$JOBS" in ''|*[!0-9]*) die "--jobs must be a number" ;; esac
[ "$(uname -m)" = x86_64 ] || die "the Android images and this build are x86_64 only"

# These paths are mounted into containers and written into build commands; keep them plain.
case "$WORK$FARM_HOME" in
  *[!A-Za-z0-9_./@+-]*) die "the work folder and farm home may only contain letters, digits and _ . / @ + - (got '$WORK' and '$FARM_HOME')" ;;
esac
mkdir -p "$WORK"
WORK=$(cd "$WORK" && pwd)
OUT_RUNTIME="$FARM_HOME/overlays/venus"
OUT_RELAY="$FARM_HOME/overlays/venus-relay"
SRC_MESA="$WORK/mesa-$MESA_VERSION"
SRC_DRM="$WORK/libdrm-$LIBDRM_VERSION"
SRC_VIRGL="$WORK/virglrenderer-$VIRGL_VERSION"


# ------------------------------------------------------------------ helpers
download() {   # url sha256 dest
  local url=$1 sum=$2 dest=$3
  if [ -f "$dest" ] && echo "$sum  $dest" | sha256sum -c --status 2>/dev/null; then
    echo "have $(basename "$dest")"
    return 0
  fi
  need curl
  echo "downloading $url"
  curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 30 --max-time 1800 \
       --output "$dest.part" "$url"
  echo "$sum  $dest.part" | sha256sum -c --status \
    || { rm -f "$dest.part"; die "checksum mismatch for $url (expected sha256 $sum). Nothing was unpacked."; }
  mv -f "$dest.part" "$dest"
}

unpack_patched() {   # tarball srcdir patch
  local tarball=$1 dir=$2 patch=$3
  if [ -f "$dir/.superfarm-patched" ] && [ "$(cat "$dir/.superfarm-patched")" = "$(sha256sum <"$patch")" ]; then
    echo "$(basename "$dir"): sources already patched"
    return 0
  fi
  # A half-patched or differently-patched tree is thrown away, never patched twice.
  rm -rf "$dir"
  tar -C "$WORK" -xf "$tarball"
  [ -d "$dir" ] || die "$(basename "$tarball") did not unpack to $dir"
  ( cd "$dir" && patch -p1 --forward --no-backup-if-mismatch --silent <"$patch" ) \
    || die "$(basename "$patch") does not apply to $(basename "$dir")"
  sha256sum <"$patch" >"$dir/.superfarm-patched"
  echo "$(basename "$dir"): unpacked and patched"
}

find_ndk() {
  if [ -z "$NDK" ] && [ -d "$WORK/android-ndk-$NDK_RELEASE" ]; then NDK="$WORK/android-ndk-$NDK_RELEASE"; fi
  if [ -z "$NDK" ] && [ "$FETCH_NDK" = 1 ]; then
    need unzip "it keeps the toolchain's executable bits"
    need sha1sum
    local zip="$WORK/android-ndk-$NDK_RELEASE-linux.zip"
    if ! { [ -f "$zip" ] && echo "$NDK_SHA1  $zip" | sha1sum -c --status 2>/dev/null; }; then
      echo "downloading $NDK_URL"
      curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 30 --max-time 3600 \
           --output "$zip.part" "$NDK_URL"
      echo "$NDK_SHA1  $zip.part" | sha1sum -c --status \
        || { rm -f "$zip.part"; die "checksum mismatch for the NDK (expected sha1 $NDK_SHA1; compare with the NDK downloads page and set NDK_SHA1=...)"; }
      mv -f "$zip.part" "$zip"
    fi
    unzip -q -o "$zip" -d "$WORK"
    NDK="$WORK/android-ndk-$NDK_RELEASE"
  fi
  [ -n "$NDK" ] || die "Android NDK $NDK_RELEASE not found. Pass --ndk DIR (an installed NDK $NDK_REVISION), set ANDROID_NDK_HOME, or add --fetch-ndk."
  NDK=$(cd "$NDK" && pwd) || die "NDK folder $NDK does not exist"
  [ -x "$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/x86_64-linux-android$API-clang" ] \
    || die "$NDK is not a usable NDK (no x86_64-linux-android$API-clang)"
  local rev
  rev=$(sed -n 's/^Pkg.Revision *= *//p' "$NDK/source.properties" 2>/dev/null || true)
  [ "$rev" = "$NDK_REVISION" ] || echo "note: NDK revision is ${rev:-unknown}; the reference build used $NDK_REVISION ($NDK_RELEASE)"
  case "$NDK" in *[!A-Za-z0-9_./@+-]*) die "the NDK path may only contain letters, digits and _ . / @ + -" ;; esac
}

# Run a command inside a builder image as the calling user, bounded by STEP_TIMEOUT.
in_builder() {   # image extra-docker-args... -- command...
  local image=$1; shift
  local margs=()
  while [ "$1" != "--" ]; do margs+=("$1"); shift; done
  shift
  "$DOCKER" run --rm --network "${BUILD_NETWORK:-bridge}" -u "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$WORK:/w" "${margs[@]}" "$image" timeout "$STEP_TIMEOUT" "$@"
}

have_image() { "$DOCKER" image inspect "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------ steps
step_fetch() {
  say "Sources"
  need tar; need patch; need sha256sum
  download "$MESA_URL" "$MESA_SHA256" "$WORK/mesa-$MESA_VERSION.tar.xz"
  download "$LIBDRM_URL" "$LIBDRM_SHA256" "$WORK/libdrm-$LIBDRM_VERSION.tar.xz"
  download "$VIRGL_URL" "$VIRGL_SHA256" "$WORK/virglrenderer-$VIRGL_VERSION.tar.gz"
  unpack_patched "$WORK/mesa-$MESA_VERSION.tar.xz" "$SRC_MESA" "$HERE/mesa-$MESA_VERSION-farm.patch"
  unpack_patched "$WORK/virglrenderer-$VIRGL_VERSION.tar.gz" "$SRC_VIRGL" "$HERE/virglrenderer-$VIRGL_VERSION-farm.patch"
  [ -d "$SRC_DRM" ] || tar -C "$WORK" -xf "$WORK/libdrm-$LIBDRM_VERSION.tar.xz"
  # The driver and the allocator must agree on the buffer handle byte for byte.
  cmp -s "$HERE/farm_gralloc_handle.h" "$SRC_MESA/src/util/u_gralloc/farm_gralloc_handle.h" \
    || die "farm_gralloc_handle.h here differs from the copy inside the Mesa patch; update the patch and rebuild both."
}

step_check_patches() {
  say "Patch check"
  need tar; need patch; need sha256sum
  download "$MESA_URL" "$MESA_SHA256" "$WORK/mesa-$MESA_VERSION.tar.xz"
  download "$VIRGL_URL" "$VIRGL_SHA256" "$WORK/virglrenderer-$VIRGL_VERSION.tar.gz"
  local tmp
  tmp=$(mktemp -d "$WORK/check.XXXXXX")
  # Only the files the patches touch are unpacked; a full Mesa tree is not needed for this.
  local files
  files=$(sed -n 's#^--- a/##p' "$HERE/mesa-$MESA_VERSION-farm.patch" | sed "s#^#mesa-$MESA_VERSION/#")
  # shellcheck disable=SC2086
  tar -C "$tmp" -xf "$WORK/mesa-$MESA_VERSION.tar.xz" $files
  ( cd "$tmp/mesa-$MESA_VERSION" && patch -p1 --dry-run --silent <"$HERE/mesa-$MESA_VERSION-farm.patch" ) \
    && echo "mesa-$MESA_VERSION-farm.patch applies cleanly" || { rm -rf "$tmp"; die "Mesa patch does not apply"; }
  files=$(sed -n 's#^--- a/##p' "$HERE/virglrenderer-$VIRGL_VERSION-farm.patch" | sed "s#^#virglrenderer-$VIRGL_VERSION/#")
  # shellcheck disable=SC2086
  tar -C "$tmp" -xf "$WORK/virglrenderer-$VIRGL_VERSION.tar.gz" $files
  ( cd "$tmp/virglrenderer-$VIRGL_VERSION" && patch -p1 --dry-run --silent <"$HERE/virglrenderer-$VIRGL_VERSION-farm.patch" ) \
    && echo "virglrenderer-$VIRGL_VERSION-farm.patch applies cleanly" || { rm -rf "$tmp"; die "virglrenderer patch does not apply"; }
  rm -rf "$tmp"
}

step_images() {
  say "Builder images"
  need "$DOCKER"
  "$DOCKER" build -t "$IMAGE_ANDROID" --build-arg "BASE_IMAGE=$BASE_IMAGE" -f "$HERE/Dockerfile.android" "$HERE"
  if [ "$NATIVE" = 0 ]; then
    "$DOCKER" build -t "$IMAGE_RELAY" --build-arg "BUILDER_IMAGE=$IMAGE_ANDROID" -f "$HERE/Dockerfile.relay" "$HERE"
  fi
}

step_driver() {
  say "Mesa Venus driver for Android $API (x86_64)"
  [ -f "$SRC_MESA/.superfarm-patched" ] || step_fetch
  have_image "$IMAGE_ANDROID" || step_images
  find_ndk
  install -m 644 "$HERE/android-x86_64.cross" "$WORK/android-x86_64.cross"
  install -m 755 "$HERE/pkgconf-android" "$WORK/pkgconf-android"
  # libdrm first, static, into a private sysroot: the driver links it in, so nothing but
  # Android's own system libraries is needed at run time.
  if [ ! -f "$WORK/sysroot/lib/libdrm.a" ]; then
    in_builder "$IMAGE_ANDROID" -v "$NDK:/ndk:ro" -- sh -ec "
      rm -rf /w/build-drm
      meson setup /w/build-drm /w/libdrm-$LIBDRM_VERSION --cross-file /w/android-x86_64.cross \
        --prefix /w/sysroot --libdir lib -Ddefault_library=static -Dbuildtype=release \
        -Dintel=disabled -Dradeon=disabled -Damdgpu=disabled -Dnouveau=disabled -Dvmwgfx=disabled \
        -Dfreedreno=disabled -Dvc4=disabled -Detnaviv=disabled -Dcairo-tests=disabled \
        -Dman-pages=disabled -Dvalgrind=disabled -Dtests=false -Dudev=false
      ninja -C /w/build-drm -j $JOBS install"
  fi
  # Same options as the reference build. zlib and expat come from meson's wrap database
  # (meson verifies their checksums) because the NDK sysroot has no pkg-config files.
  in_builder "$IMAGE_ANDROID" -v "$NDK:/ndk:ro" -- sh -ec "
    [ -f /w/build-android/build.ninja ] || meson setup /w/build-android /w/mesa-$MESA_VERSION \
      --cross-file /w/android-x86_64.cross -Dbuildtype=release \
      -Dplatforms=android -Dplatform-sdk-version=$API -Dandroid-stub=true -Dandroid-libbacktrace=disabled \
      -Dgallium-drivers= -Dvulkan-drivers=virtio -Dopengl=false -Degl=disabled -Dgbm=disabled -Dglx=disabled \
      -Dgles1=disabled -Dgles2=disabled -Dllvm=disabled -Dzstd=disabled -Dxmlconfig=disabled \
      -Dvalgrind=disabled -Dlibunwind=disabled -Dforce_fallback_for=zlib,expat
    ninja -C /w/build-android -j $JOBS"
  install -d "$OUT_RUNTIME"
  install -m 755 "$WORK/build-android/src/virtio/vulkan/libvulkan_virtio.so" "$OUT_RUNTIME/vulkan.virtio.so"
  echo "-> $OUT_RUNTIME/vulkan.virtio.so"
}

step_gralloc() {
  say "gralloc.venus (64- and 32-bit)"
  [ -f "$SRC_MESA/.superfarm-patched" ] || step_fetch
  have_image "$IMAGE_ANDROID" || step_images
  find_ndk
  mkdir -p "$WORK/gralloc"
  # Android's HAL headers come from Mesa's stub copies; liblog and libvulkan from the NDK.
  local flags="-O2 -fPIC -shared -fvisibility=hidden -I/w/mesa-$MESA_VERSION/include/android_stub -I/src"
  in_builder "$IMAGE_ANDROID" -v "$NDK:/ndk:ro" -v "$HERE:/src:ro" -- sh -ec "
    B=/ndk/toolchains/llvm/prebuilt/linux-x86_64/bin
    \$B/x86_64-linux-android$API-clang $flags -o /w/gralloc/gralloc.venus.so /src/gralloc_venus.c -llog -lvulkan
    \$B/i686-linux-android$API-clang   $flags -o /w/gralloc/gralloc.venus.x86.so /src/gralloc_venus.c -llog -lvulkan"
  install -d "$OUT_RUNTIME"
  install -m 755 "$WORK/gralloc/gralloc.venus.so" "$WORK/gralloc/gralloc.venus.x86.so" "$OUT_RUNTIME/"
  echo "-> $OUT_RUNTIME/gralloc.venus.so, gralloc.venus.x86.so"
}

relay_meson() {   # printed once, used by both the container and the native build
  cat <<EOF
[ -f "$1/build.ninja" ] || meson setup "$1" "$2" -Dbuildtype=release -Dc_std=gnu11 \
  -Dvenus=true -Drender-server-worker=process -Dtests=false --prefix "$OUT_RELAY" --libdir lib
ninja -C "$1" -j $JOBS install
EOF
}

step_relay() {
  say "Relay (virgl_test_server --venus)"
  [ -f "$SRC_VIRGL/.superfarm-patched" ] || step_fetch
  if pgrep -x virgl_test_serv >/dev/null 2>&1 && [ -x "$OUT_RELAY/bin/virgl_test_server" ]; then
    # Installing over the files of a running relay is safe for the running process, but
    # Androids started afterwards would talk to a helper of a different build.
    echo "note: a relay is running. The new build takes effect when the relay is next started;"
    echo "      every NVIDIA Android must be restarted after that. Nothing is stopped by this script."
  fi
  install -d "$OUT_RELAY"
  if [ "$NATIVE" = 1 ]; then
    need meson; need ninja
    ( timeout "$STEP_TIMEOUT" sh -ec "$(relay_meson "$WORK/build-vr-native" "$SRC_VIRGL")" )
  else
    have_image "$IMAGE_RELAY" || step_images
    # Mounted at its real path: the helper's location is compiled into the binary.
    in_builder "$IMAGE_RELAY" -v "$OUT_RELAY:$OUT_RELAY" -- sh -ec "$(relay_meson /w/build-vr "/w/virglrenderer-$VIRGL_VERSION")"
  fi
  echo "-> $OUT_RELAY/bin/virgl_test_server"
  if ! "$OUT_RELAY/bin/virgl_test_server" --help >/dev/null 2>&1; then
    if ldd "$OUT_RELAY/bin/virgl_test_server" 2>&1 | grep -q 'not found\|GLIBC'; then
      echo "warning: the relay does not start on this host:" >&2
      LD_LIBRARY_PATH="$OUT_RELAY/lib" ldd "$OUT_RELAY/bin/virgl_test_server" 2>&1 | grep 'not found\|GLIBC' >&2 || true
      echo "         install the missing libraries (sf doctor names the packages), or, if the C library is" >&2
      echo "         too old for the builder image, rebuild with:  $0 relay --native" >&2
    fi
  fi
}

step_install_config() {
  say "gpu_config.sh"
  install -d "$OUT_RUNTIME"
  install -m 755 "$HERE/gpu_config_venus.sh" "$OUT_RUNTIME/gpu_config.sh"
  {
    echo "Built by native/venus/build.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Target: Android 13 x86_64 (API $API). 64-bit driver only; 32-bit allocator for the camera service."
    echo "Mesa $MESA_VERSION, libdrm $LIBDRM_VERSION, virglrenderer $VIRGL_VERSION, NDK ${NDK_RELEASE}"
  } >"$OUT_RUNTIME/BUILD_INFO"
  echo "-> $OUT_RUNTIME/gpu_config.sh"
}

step_tests() {
  say "Test programs"
  [ -f "$SRC_MESA/.superfarm-patched" ] || step_fetch
  have_image "$IMAGE_RELAY" || step_images
  find_ndk
  mkdir -p "$WORK/tests"
  in_builder "$IMAGE_RELAY" -v "$NDK:/ndk:ro" -v "$HERE:/src:ro" -- sh -ec "
    B=/ndk/toolchains/llvm/prebuilt/linux-x86_64/bin
    for t in ahbtest yuvtest; do
      \$B/x86_64-linux-android$API-clang -O2 -o /w/tests/\$t /src/\$t.c -lEGL -lGLESv2 -landroid -lnativewindow
    done
    for t in probe fmts; do cc -O2 -o /w/tests/\$t /src/\$t.c -lvulkan; done"
  cat <<EOF
-> $WORK/tests
   ahbtest, yuvtest   run inside an NVIDIA Android:   docker cp <file> <android>:/data/local/tmp/ && docker exec <android> /data/local/tmp/ahbtest
   probe, fmts        run in a plain Linux container that has a Linux build of the Venus driver and
                      the relay socket mounted (see README.md, "Testing the buffer path")
EOF
}

step_verify() {
  say "Outputs"
  local ok=1 f
  for f in "$OUT_RUNTIME/vulkan.virtio.so" "$OUT_RUNTIME/gralloc.venus.so" "$OUT_RUNTIME/gralloc.venus.x86.so" \
           "$OUT_RUNTIME/gpu_config.sh" "$OUT_RELAY/bin/virgl_test_server" "$OUT_RELAY/libexec/virgl_render_server"; do
    if [ -e "$f" ]; then
      printf 'ok       %s' "$f"
      if command -v file >/dev/null 2>&1; then printf '  (%s)' "$(file -b "$f" | cut -d, -f1-2)"; fi
      printf '\n'
    else
      printf 'MISSING  %s\n' "$f"; ok=0
    fi
  done
  [ "$ok" = 1 ] || die "the runtime is incomplete; NVIDIA mode stays unavailable until every file exists"
  echo "NVIDIA mode is ready: set gpu.mode to nvidia (or leave it on auto) and start an Android."
}

step_clean() {
  say "Clean"
  case "$WORK" in /|"$HOME"|"") die "refusing to delete '$WORK'" ;; esac
  rm -rf -- "$WORK"
  echo "removed $WORK (outputs in $FARM_HOME/overlays are kept)"
}

for step in "${STEPS[@]}"; do
  case "$step" in
    all)            step_fetch; step_images; step_driver; step_gralloc; step_relay; step_install_config; step_verify ;;
    fetch)          step_fetch ;;
    images)         step_images ;;
    driver)         step_driver ;;
    gralloc)        step_gralloc ;;
    relay)          step_relay ;;
    install-config) step_install_config ;;
    tests)          step_tests ;;
    check-patches)  step_check_patches ;;
    verify)         step_verify ;;
    clean)          step_clean ;;
    *)              die "unknown step '$step' (see --help)" ;;
  esac
done

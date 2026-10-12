#!/usr/bin/env bash
# Build the camera + microphone overlay for Android 13 from a BlissOS image.
#
#   extract_hal.sh [options] <BlissOS .iso | system.efs | system.sfs | system.img | folder>
#
#     --image REF     redroid image the overlay is for
#                     (default: redroid/redroid:13.0.0-latest)
#     --out DIR       where the overlay goes
#                     (default: $SUPERFARM_HOME/overlays/camera/13,
#                      SUPERFARM_HOME defaulting to ~/.local/share/superfarm)
#     --fps-prop N    also patch the display module so the frame rate is read from
#                     /vendor/build.prop (ro.farm.redroid_fps=N). Off by default; see README.md.
#     --work DIR      scratch folder (default: a temporary one, removed at the end).
#                     An ISO needs about 8 GB there.
#     --keep-work     do not remove the scratch folder
#
# The input is something you download yourself: a BlissOS 16.x (Android 13, x86_64) image.
# Nothing from it is redistributed by this project. `folder` is an already unpacked or
# mounted system tree (the folder that contains `system/vendor`, or `vendor`).
#
# What it does (README.md explains why):
#   1. finds the external-camera HAL service in the BlissOS system tree
#   2. copies it, its init file, its config, and every library it needs that the redroid
#      image does not already have
#   3. patches the HAL to report a front camera, and its config to orientation 270
#   4. builds the two shims and hooks them into copies of redroid's gralloc and audio modules
#   5. declares a 48 kHz stereo microphone in the audio policy
#   6. writes the VINTF manifest fragment that registers the HAL
#
# No root needed. Tools: docker, python3, gcc, patchelf, binutils (readelf, nm), and
# depending on the input: debugfs (e2fsprogs) for system.img; fsck.erofs (erofs-utils) or
# unsquashfs (squashfs-tools) for system.efs / system.sfs; bsdtar, 7z, xorriso or isoinfo
# for an ISO.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SVC=android.hardware.camera.provider@2.5-external-service
FACING_LIB=camera.device@3.4-external-impl.so
IMAGE=redroid/redroid:13.0.0-latest
OUT=${SUPERFARM_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/superfarm}/overlays/camera/13
FPS_PROP=""
WORK=""
KEEP_WORK=0
INPUT=""

die() { echo "extract_hal: $*" >&2; exit 1; }
say() { echo "extract_hal: $*"; }
need() { command -v "$1" >/dev/null || die "$1 is not installed${2:+ ($2)}"; }

while [ $# -gt 0 ]; do
  case $1 in
    --image) IMAGE=${2:?--image needs a value}; shift 2 ;;
    --out) OUT=${2:?--out needs a value}; shift 2 ;;
    --fps-prop) FPS_PROP=${2:?--fps-prop needs a value}; shift 2 ;;
    --work) WORK=${2:?--work needs a value}; shift 2 ;;
    --keep-work) KEEP_WORK=1; shift ;;
    -h|--help) sed -n '2,36p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option $1 (see --help)" ;;
    *) [ -z "$INPUT" ] || die "only one input, got '$INPUT' and '$1'"; INPUT=$1; shift ;;
  esac
done
[ -n "$INPUT" ] || die "no input given (see --help)"
[ -e "$INPUT" ] || die "no such file or folder: $INPUT"
case $FPS_PROP in ''|*[!0-9]*) [ -z "$FPS_PROP" ] || die "--fps-prop needs a number" ;; esac

need docker; need python3; need readelf "binutils"; need nm "binutils"; need patchelf; need gcc

if [ -z "$WORK" ]; then
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/superfarm-hal.XXXXXX")
  MADE_WORK=1
else
  mkdir -p "$WORK"; MADE_WORK=0
fi
WORK=$(cd "$WORK" && pwd)
PROBE=${SF_HAL_PROBE_NAME:-superfarm-halprobe-$$}
PROBE_MADE=0
cleanup() {
  [ "$PROBE_MADE" = 1 ] && docker rm -f "$PROBE" >/dev/null 2>&1
  if [ "$KEEP_WORK" = 0 ] && [ "$MADE_WORK" = 1 ]; then rm -rf "$WORK"; fi
  return 0
}
trap cleanup EXIT

# ---------------------------------------------------------------- 1. open the BlissOS tree
# After this block one of two modes is set:
#   MODE=dir  TREE=<folder that contains vendor/ and lib/>       (the Android /system folder)
#   MODE=img  IMG=<ext4 image>  PREFIX=<path of that folder inside the image>
MODE=""; TREE=""; IMG=""; PREFIX=""

use_folder() {  # <folder>: accept it if it holds the HAL service
  local d
  for d in "$1/system" "$1"; do
    if [ -f "$d/vendor/bin/hw/$SVC" ]; then MODE=dir; TREE=$d; return 0; fi
  done
  return 1
}

use_image() {  # <ext4 image>
  need debugfs "e2fsprogs"
  local p
  for p in /system ""; do
    if debugfs -R "stat $p/vendor/bin/hw/$SVC" "$1" 2>/dev/null | grep -q '^Inode:'; then
      MODE=img; IMG=$1; PREFIX=$p; return 0
    fi
  done
  return 1
}

unpack_fs() {  # <system.efs | system.sfs> -> folder with its content
  local dest=$WORK/sysfs
  rm -rf "$dest"; mkdir -p "$dest"
  case $(head -c 4 "$1" | od -An -tx1 | tr -d ' ') in
    68737173)   # "hsqs": squashfs
      need unsquashfs "squashfs-tools"
      unsquashfs -q -n -f -d "$dest" "$1" >/dev/null || die "unsquashfs could not unpack $1" ;;
    *)
      need fsck.erofs "erofs-utils 1.5 or newer"
      fsck.erofs "--extract=$dest" "$1" >/dev/null || die "fsck.erofs could not unpack $1" ;;
  esac
  echo "$dest"
}

open_packed() {  # <system.efs | system.sfs>
  local dest
  say "unpacking $(basename "$1") (this takes a few minutes and several GB)"
  dest=$(unpack_fs "$1") || return 1
  if [ -f "$dest/system.img" ]; then use_image "$dest/system.img"; else use_folder "$dest"; fi
}

iso_get() {  # <iso> <file name in the ISO root> <dest folder>; returns 1 when the ISO has no such file
  local iso=$1 name=$2 dest=$3
  rm -f "$dest/$name"
  if command -v bsdtar >/dev/null; then bsdtar -x -f "$iso" -C "$dest" "$name" 2>/dev/null || true
  elif command -v 7z >/dev/null; then 7z x -y "-o$dest" "$iso" "$name" >/dev/null 2>&1 || true
  elif command -v 7zz >/dev/null; then 7zz x -y "-o$dest" "$iso" "$name" >/dev/null 2>&1 || true
  elif command -v xorriso >/dev/null; then xorriso -osirrox on -indev "$iso" -extract "/$name" "$dest/$name" >/dev/null 2>&1 || true
  elif command -v isoinfo >/dev/null; then isoinfo -i "$iso" -R -x "/$name" > "$dest/$name" 2>/dev/null || true
  else die "need one of bsdtar (libarchive-tools), 7z (p7zip), xorriso or isoinfo to read an ISO"
  fi
  [ -s "$dest/$name" ]
}

if [ -d "$INPUT" ]; then
  use_folder "$INPUT" || die "$INPUT does not contain vendor/bin/hw/$SVC (pass the folder that holds 'system' or 'vendor')"
else
  case $(basename "$INPUT" | tr 'A-Z' 'a-z') in
    *.iso)
      mkdir -p "$WORK/iso"
      found=""
      for name in system.efs system.sfs system.img; do
        if iso_get "$INPUT" "$name" "$WORK/iso"; then found=$name; break; fi
      done
      [ -n "$found" ] || die "no system.efs, system.sfs or system.img in $INPUT: is this a BlissOS x86_64 ISO?"
      if [ "$found" = system.img ]; then use_image "$WORK/iso/$found"; else open_packed "$WORK/iso/$found"; fi ;;
    *.efs|*.sfs) open_packed "$INPUT" ;;
    *) use_image "$INPUT" ;;
  esac || die "the external camera HAL ($SVC) was not found in $INPUT. It ships in BlissOS 16.x (Android 13) x86_64 builds."
fi
say "BlissOS tree: ${TREE:-$IMG:$PREFIX}"

bliss_get() {  # <path below the system folder, e.g. vendor/lib/x.so> <dest file>; returns 1 when absent
  local dest_dir dest_name
  rm -f "$2"
  if [ "$MODE" = dir ]; then
    [ -f "$TREE/$1" ] || return 1
    cp "$TREE/$1" "$2"
  else
    # debugfs has its own argument parser: run it in the destination folder so the only
    # path it sees is the plain file name.
    dest_dir=$(dirname "$2"); dest_name=$(basename "$2")
    (cd "$dest_dir" && debugfs -R "dump $PREFIX/$1 $dest_name" "$IMG" >/dev/null 2>&1) || true
    [ -s "$2" ] || { rm -f "$2"; return 1; }
  fi
}

# ---------------------------------------------------------------- 2. what the redroid image has
say "reading $IMAGE (not started; pulled first if it is not local)"
docker create --name "$PROBE" "$IMAGE" >/dev/null || die "cannot create a container from $IMAGE"
PROBE_MADE=1
docker export "$PROBE" | tar -t > "$WORK/image_files.txt" || die "cannot list the files of $IMAGE"
ORIG=$WORK/orig
for lib in lib lib64; do
  mkdir -p "$ORIG/$lib/hw"
  for module in gralloc.redroid.so audio.primary.default.so hwcomposer.redroid.so; do
    docker cp "$PROBE:/vendor/$lib/hw/$module" "$ORIG/$lib/hw/$module" >/dev/null \
      || die "$IMAGE has no /vendor/$lib/hw/$module: this is not a redroid image"
  done
done
mkdir -p "$ORIG/etc"
docker cp "$PROBE:/vendor/etc/primary_audio_policy_configuration.xml" "$ORIG/etc/" >/dev/null \
  || die "$IMAGE has no /vendor/etc/primary_audio_policy_configuration.xml"
docker cp "$PROBE:/vendor/build.prop" "$ORIG/build.prop" >/dev/null || die "$IMAGE has no /vendor/build.prop"
docker rm -f "$PROBE" >/dev/null; PROBE_MADE=0
chmod -R u+rw "$ORIG"

in_image() { grep -q -x -F "$1" "$WORK/image_files.txt"; }
in_image_glob() { grep -q -E "$1" "$WORK/image_files.txt"; }

# Libraries a vendor process may load straight from /system (the LL-NDK). Anything else
# under /system/lib is invisible to the HAL, even when the file exists.
LLNDK=" libc.so libm.so libdl.so liblog.so libEGL.so libGLESv1_CM.so libGLESv2.so libGLESv3.so \
libandroid_net.so libbinder_ndk.so libcgrouprc.so libft2.so libmediandk.so libnativewindow.so \
libneuralnetworks.so libselinux.so libsync.so libvndksupport.so libvulkan.so libstdc++.so libRS.so "

lib_in_image() {  # can the HAL (a 32-bit vendor process) already load this library in the redroid image?
  local lib=$1 quoted
  quoted=$(printf '%s' "$lib" | sed 's/[.[\*^$+?(){}|]/\\&/g')
  in_image "vendor/lib/$lib" && return 0
  in_image "vendor/lib/hw/$lib" && return 0
  in_image_glob "^system/apex/com\.android\.vndk\.[^/]+/lib/$quoted\$" && return 0
  case $LLNDK in *" $lib "*) in_image "system/lib/$lib" && return 0 ;; esac
  return 1
}

# ---------------------------------------------------------------- 3. stage the HAL
STAGE=$WORK/stage
rm -rf "$STAGE"
mkdir -p "$STAGE/bin/hw" "$STAGE/etc/init" "$STAGE/etc/vintf/manifest" "$STAGE/lib/hw" "$STAGE/lib64/hw"
bliss_get "vendor/bin/hw/$SVC" "$STAGE/bin/hw/$SVC" || die "cannot read vendor/bin/hw/$SVC"
bliss_get "vendor/etc/init/$SVC.rc" "$STAGE/etc/init/$SVC.rc" || die "cannot read vendor/etc/init/$SVC.rc"
bliss_get "vendor/etc/external_camera_config.xml" "$WORK/external_camera_config.xml" \
  || die "cannot read vendor/etc/external_camera_config.xml"
readelf -h "$STAGE/bin/hw/$SVC" | grep -q 'ELF32' \
  || die "the HAL service in this image is not a 32-bit program; the shims and patches here are for the 32-bit one"

# Walk the dependency tree of the service. A library the redroid image can already provide
# is left alone (two copies of one HIDL interface library in a process do not mix); every
# other one is taken from BlissOS, and its own dependencies are walked in turn.
needed_of() { readelf -d "$1" 2>/dev/null | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p'; }
todo=("$STAGE/bin/hw/$SVC"); seen=" "; copied=0; missing=""
while [ ${#todo[@]} -gt 0 ]; do
  next=()
  for file in "${todo[@]}"; do
    while IFS= read -r lib; do
      [ -n "$lib" ] || continue
      case $seen in *" $lib "*) continue ;; esac
      seen="$seen$lib "
      lib_in_image "$lib" && continue
      if bliss_get "vendor/lib/$lib" "$STAGE/lib/$lib" || bliss_get "lib/$lib" "$STAGE/lib/$lib"; then
        next+=("$STAGE/lib/$lib"); copied=$((copied + 1))
      else
        missing="$missing $lib"
      fi
    done < <(needed_of "$file")
  done
  todo=("${next[@]+"${next[@]}"}")
done
[ -z "$missing" ] || die "libraries needed by the HAL exist neither in $IMAGE nor in the BlissOS image:$missing"
say "copied the HAL service and $copied libraries the redroid image lacks"

# ---------------------------------------------------------------- 4. patches
[ -f "$STAGE/lib/$FACING_LIB" ] || die "$FACING_LIB was not among the copied libraries; cannot apply the front-camera patch"
python3 "$HERE/hal_patch.py" lens-facing "$STAGE/lib/$FACING_LIB" "$STAGE/lib/$FACING_LIB.new"
mv "$STAGE/lib/$FACING_LIB.new" "$STAGE/lib/$FACING_LIB"
python3 "$HERE/hal_patch.py" camera-config "$WORK/external_camera_config.xml" "$STAGE/etc/external_camera_config.xml"
python3 "$HERE/hal_patch.py" audio-policy "$ORIG/etc/primary_audio_policy_configuration.xml" \
  "$STAGE/etc/primary_audio_policy_configuration.xml"
bash "$HERE/build_shim.sh" "$HERE" "$STAGE" "$ORIG"
if [ -n "$FPS_PROP" ]; then
  for lib in lib lib64; do
    python3 "$HERE/hal_patch.py" fps-property "$ORIG/$lib/hw/hwcomposer.redroid.so" "$STAGE/$lib/hw/hwcomposer.redroid.so"
  done
  python3 "$HERE/hal_patch.py" build-prop "$ORIG/build.prop" "$STAGE/build.prop" --fps "$FPS_PROP"
  say "display frame rate now comes from /vendor/build.prop: ro.farm.redroid_fps=$FPS_PROP"
fi

# Registers the HAL with the framework. Without this fragment hwservicemanager refuses the
# service and cameraserver never learns that a provider named external/0 exists.
cat > "$STAGE/etc/vintf/manifest/android.hardware.camera.provider@2.5-external.xml" <<'EOF'
<manifest version="1.0" type="device">
    <hal format="hidl">
        <name>android.hardware.camera.provider</name>
        <transport>hwbinder</transport>
        <version>2.5</version>
        <interface>
            <name>ICameraProvider</name>
            <instance>external/0</instance>
        </interface>
    </hal>
</manifest>
EOF

# `docker cp` and image builds keep these modes; init will not start a service it cannot execute.
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/bin/hw/$SVC"

# ---------------------------------------------------------------- 5. install
mkdir -p "$OUT"
if [ -n "$(ls -A "$OUT")" ]; then
  # Only ever empty a folder that visibly holds a previous overlay: a mistyped --out must
  # not cost anybody their files.
  [ -f "$OUT/bin/hw/$SVC" ] || die "$OUT is not empty and does not look like a camera overlay; choose another --out"
  say "replacing the overlay already in $OUT"
  rm -rf "${OUT:?}/bin" "${OUT:?}/etc" "${OUT:?}/lib" "${OUT:?}/lib64" "${OUT:?}/build.prop"
  [ -z "$(ls -A "$OUT")" ] || die "$OUT contains files that are not part of an overlay; remove them or choose another --out"
fi
cp -a "$STAGE/." "$OUT/"
say "overlay written to $OUT ($(find "$OUT" -type f | wc -l) files)"
say "next: install it into your Androids (the camera's 'install HAL' action) or bake it into the image; see README.md"

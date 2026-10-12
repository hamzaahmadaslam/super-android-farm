#!/usr/bin/env bash
# Build the two shims for Android x86 and x86_64 and hook each into a copy of the redroid
# module it fixes:
#
#   libfarmcam_shim.so -> hw/gralloc.redroid.so        (camera frame buffers)
#   libfarmmic_shim.so -> hw/audio.primary.default.so  (microphone)
#
#   build_shim.sh <dir with the .c files> <overlay vendor dir> <dir with the original modules>
#
# The third argument must contain lib/hw/ and lib64/hw/ with the *unmodified*
# gralloc.redroid.so and audio.primary.default.so of the redroid image the overlay is for
# (extract_hal.sh copies them out of the image). The results are written below the second
# argument, in lib/, lib/hw/, lib64/ and lib64/hw/.
#
# No Android NDK is needed. The shims use nothing but a handful of libc, libdl and liblog
# functions, so they are built freestanding with the host gcc and linked against empty stub
# libraries that only carry the right names; Android's linker binds the real ones at load
# time. `patchelf --add-needed` then makes the original module pull the shim in, and the
# shim's constructor installs its hooks.
#
# Needs: gcc that can target 32-bit x86 (`gcc -m32`; on Debian/Ubuntu the package
# gcc-multilib is NOT required because nothing from the host C library is used),
# patchelf, binutils (readelf, nm).
set -euo pipefail

if [ $# -ne 3 ]; then
  echo "usage: build_shim.sh <dir with the .c files> <overlay vendor dir> <dir with the original modules>" >&2
  exit 2
fi
SRC=$1; VENDOR=$2; ORIG=$3

for tool in gcc patchelf readelf nm; do
  command -v "$tool" >/dev/null || { echo "build_shim: $tool is not installed" >&2; exit 1; }
done
for f in gralloc_shim.c mic_shim.c; do
  [ -f "$SRC/$f" ] || { echo "build_shim: $SRC/$f not found" >&2; exit 1; }
done
for lib in lib lib64; do
  for module in gralloc.redroid.so audio.primary.default.so; do
    [ -f "$ORIG/$lib/hw/$module" ] || { echo "build_shim: original module $ORIG/$lib/hw/$module not found" >&2; exit 1; }
  done
done

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

# Stub libraries: correct sonames and symbol names, no behaviour.
echo 'void *dlopen(const char *a, int b) { return 0; } void *dlsym(void *a, const char *b) { return 0; }' > "$T/dl.c"
cat > "$T/c.c" <<'EOF'
int open(const char *a, int b, ...) { return 0; }
long read(int a, void *b, unsigned long c) { return 0; }
int poll(void *a, unsigned long b, int c) { return 0; }
int usleep(unsigned a) { return 0; }
int clock_gettime(int a, void *b) { return 0; }
EOF
echo 'int __android_log_print(int a, const char *b, const char *c, ...) { return 0; }' > "$T/log.c"

build() {  # build <32|64> <lib dir> <shim name> <source file> <module to hook>
  local arch=$1 lib=$2 shim=$3 source=$4 module=$5
  gcc "-m$arch" -shared -fPIC -nostdlib -ffreestanding -fno-stack-protector -fno-asynchronous-unwind-tables -O2 \
      -fno-builtin "-Wl,-soname,$shim.so" -Wl,--hash-style=both -Wl,-z,noexecstack -Wl,--build-id=none \
      -o "$VENDOR/$lib/$shim.so" "$SRC/$source" "-L$T/$arch" -lc -ldl -llog
  cp "$ORIG/$lib/hw/$module" "$VENDOR/$lib/hw/$module"
  patchelf --add-needed "$shim.so" "$VENDOR/$lib/hw/$module"
  chmod 644 "$VENDOR/$lib/$shim.so" "$VENDOR/$lib/hw/$module"
  # The module must now need the shim exactly once ...
  if [ "$(readelf -d "$VENDOR/$lib/hw/$module" | grep -c "\[$shim.so\]")" != 1 ]; then
    echo "build_shim: $module does not list $shim.so as needed" >&2; exit 1
  fi
  # ... and the shim must not need compiler helpers Android does not have (64-bit division
  # on 32-bit x86 would pull in libgcc's __udivdi3 and friends).
  if nm -D --undefined-only "$VENDOR/$lib/$shim.so" | grep -q -E '__(u?div|u?mod)di3'; then
    echo "build_shim: $shim.so ($arch-bit) needs libgcc division helpers" >&2; exit 1
  fi
  echo "ok $arch-bit $shim -> $module"
}

for arch in 32 64; do
  lib=lib; [ "$arch" = 64 ] && lib=lib64
  mkdir -p "$T/$arch" "$VENDOR/$lib/hw"
  if ! gcc "-m$arch" -shared -fPIC -nostdlib -Wl,-soname,libdl.so -o "$T/$arch/libdl.so" "$T/dl.c" 2>"$T/err"; then
    cat "$T/err" >&2
    echo "build_shim: this gcc cannot build $arch-bit x86 code (try a distribution gcc for x86_64)" >&2
    exit 1
  fi
  gcc "-m$arch" -shared -fPIC -nostdlib -fno-builtin -Wl,-soname,libc.so -o "$T/$arch/libc.so" "$T/c.c"
  gcc "-m$arch" -shared -fPIC -nostdlib -fno-builtin -Wl,-soname,liblog.so -o "$T/$arch/liblog.so" "$T/log.c"
  build "$arch" "$lib" libfarmcam_shim gralloc_shim.c gralloc.redroid.so
  build "$arch" "$lib" libfarmmic_shim mic_shim.c audio.primary.default.so
done

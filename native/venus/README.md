# NVIDIA mode: Android graphics on an NVIDIA card

**Built for Android 13 x86_64 (API 33).** The Vulkan driver is 64-bit only: 32-bit apps
have no NVIDIA driver. The allocator alone also exists in 32-bit, because Android 13's
camera service is a 32-bit program. Other Android versions need a rebuild against their
API level and have not been tried.

## Why this exists

redroid's graphics stack is Mesa. Intel and AMD cards work out of the box because Mesa has
drivers for them. NVIDIA's proprietary driver is not Mesa: pointing redroid's normal GPU
mode at an NVIDIA render node fails at boot (`GRALLOC-GBM: failed to create gbm device`,
then `no suitable EGLConfig found`).

So an NVIDIA Android draws through a relay instead:

```
Android process (app, SurfaceFlinger, ...)
  GLES   -> ANGLE (already in the image) -> Vulkan
  Vulkan -> Mesa "Venus" driver (vulkan.virtio.so, built here)
         -> unix socket /venus/vtest.sock        (bind-mounted from the host)
Host
  virgl_test_server --venus (patched here) -> NVIDIA Vulkan driver -> the card

Buffers (windows, textures, video frames)
  gralloc.venus.so (written here) allocates each one on the card through the same driver,
  exports it as a dma-buf, and every process that receives the handle imports it.
```

No full Android source build is needed: the driver is cross-compiled with the NDK.

## What is in this folder

| File | What |
|---|---|
| `mesa-26.0.8-farm.patch` | all driver changes (Venus vtest backend, Android buffer import, the gralloc backend `u_gralloc_farm.c`) |
| `virglrenderer-1.2.0-farm.patch` | relay changes: the import command, and a build fix for glibc 2.43 |
| `gralloc_venus.c`, `farm_gralloc_handle.h` | the allocator (a gralloc0 HAL module) and the buffer handle it shares with the driver |
| `gpu_config_venus.sh` | redroid's `gpu_config.sh` plus a `venus` mode; installed as `/vendor/bin/gpu_config.sh` |
| `Dockerfile.android`, `Dockerfile.relay` | the two builder images |
| `android-x86_64.cross`, `pkgconf-android` | meson cross file for the NDK and its pkg-config wrapper |
| `build.sh` | the reproducible build |
| `probe.c`, `fmts.c` | host-side tests, run in a plain Linux container |
| `ahbtest.c`, `yuvtest.c` | in-Android tests |

### What had to be built, and why

1. **Mesa 26.0.8 Venus driver for Android 13**, cross-compiled with NDK r27c.
2. **A new relay command**, `VCMD_RESOURCE_IMPORT_FD` (id 39), so a process can hand an
   existing dma-buf to the host. Stock vtest can only *create* buffers, not import them,
   which makes sharing a buffer between two Android processes impossible. It is patched into
   virglrenderer 1.2.0 and into the driver's vtest backend.
3. **`gralloc.venus.so`**. redroid ships two allocators: one for CPU memory (software
   rendering) and one for a DRM render node (Intel/AMD). Neither can hand out buffers an
   NVIDIA card can draw into. This one allocates each buffer as a Vulkan image through the
   Venus driver, exports it, and frees its own Vulkan objects again; the dma-buf keeps the
   memory alive. The driver has the matching backend (`u_gralloc_farm.c`).
4. **A `venus` mode in `gpu_config.sh`** that sets `ro.hardware.egl=angle`,
   `ro.hardware.vulkan=virtio`, `ro.hardware.gralloc=venus`, `mesa.vn.debug=vtest`,
   `mesa.vtest.socket.name=/venus/vtest.sock`. With `gpu.nvidia.debug: true` the farm adds
   `androidboot.redroid_gpu_debug=1`, which turns on verbose driver logging.

`farm_gralloc_handle.h` is shared by the allocator and the driver (a copy is inside the Mesa
patch; `build.sh` refuses to build when the two differ). If it changes, rebuild both and
restart every NVIDIA Android.

## NVIDIA-specific findings

Each of these was a failing test before it was a line of code.

- **Imports must be "dedicated".** NVIDIA reports that it does not prefer a dedicated
  allocation for these images, then rejects a non-dedicated dma-buf import with
  `VK_ERROR_OUT_OF_DEVICE_MEMORY` for most sizes and tiled layouts. A dedicated import always
  works, so the driver always uses one for window buffers. Because the allocation is normally
  asynchronous, the failure otherwise shows up later as an abort in `vn_ring_submit_locked`,
  far from its cause.
- **The card draws only into its tiled ("block-linear") layouts**, modifiers
  `0x03000000006060 10..15`. The plain LINEAR layout can be sampled and copied but not
  rendered to. So every RGB buffer is tiled, and `lock()`/`unlock()` in the allocator convert
  to and from plain rows for the CPU. The layout is the Turing-and-later 2D colour
  arrangement (64 x 8 byte blocks; the same one Mesa's `src/nouveau/nil/copy.rs` handles in
  `CopyGOBTuring2D`).
- **Video and camera formats (YUV 4:2:0) are offered only as LINEAR and only for sampling**,
  which is exactly what they need: the decoder or camera service fills the planes in place
  and the GPU samples them.
- **A dma-buf reports only memory types without the DEVICE_LOCAL flag.** ANGLE gives up
  before asking the driver when it finds no such type, so the driver offers every type and
  Venus picks a valid one at import.
- **The Android build of the driver hid the fd/dma-buf export extensions.** The allocator
  needs them, so they are exposed.
- **The relay cannot export "frame finished" sync files.** The driver waits for the work to
  finish and returns "already signalled" instead.
- **Many clients on one card work.** 16 Linux containers and later 13 full Androids shared
  one relay with no errors.
- **One relay process per card is enough.** Running the relay's workers as threads of a
  single process instead of one helper process per client saved nothing measurable (927 MB
  against 944 MB of system memory per Android), so the default (process workers) is kept.

Measured cost: about 0.3 GB more system RAM per Android than Intel mode (mostly inside
NVIDIA's driver), and about the same CPU. The relay itself used 0.6% of one core per Android
in a live video. Video memory: 17 Androids idling in an app held 7.6 GB on the card together
with a desktop session that uses about 1.5 GB by itself, so roughly 360 MB per Android at
480x854; the card sat at 20% load.

## Building

Needs Docker, about 6 GB of disk, network access for the three source archives, and the
Android NDK r27c (27.2.12479018). Nothing needs root.

```bash
native/venus/build.sh all --ndk /path/to/android-ndk-r27c      # or --fetch-ndk to download it
```

It downloads by pinned URL and checks each archive before unpacking:

| Source | Version | sha256 |
|---|---|---|
| Mesa | 26.0.8 | `caf1c0061a68e88dfa74967a7e780c0e85d65b6c4e334cd69095a5dc54ad78bc` |
| libdrm | 2.4.131 | `45ba9983b51c896406a3d654de81d313b953b76e6391e2797073d543c5f617d5` |
| virglrenderer | 1.2.0 | `8bae92909021ea87e2087ae96df322f90140c414bd5f38810af3f3e5146a567b` |

Then it applies the two patches and builds into the farm home:

```
<home>/overlays/venus/          what Androids load
    vulkan.virtio.so            -> /vendor/lib64/hw/vulkan.virtio.so
    gralloc.venus.so            -> /vendor/lib64/hw/gralloc.venus.so
    gralloc.venus.x86.so        -> /vendor/lib/hw/gralloc.venus.so      (32-bit)
    gpu_config.sh               -> /vendor/bin/gpu_config.sh
<home>/overlays/venus-relay/    the relay, run on the host by the farm daemon
    bin/virgl_test_server  libexec/virgl_render_server  lib/libvirglrenderer.so.1
```

The four runtime files are bind-mounted read-only into each NVIDIA Android; the image itself
is not modified. Single steps (`fetch`, `driver`, `gralloc`, `relay`, `tests`,
`check-patches`, `verify`) are listed by `build.sh --help`.

Things to know:

- **The relay runs on the host**, so it must be built against a C library no newer than the
  host's. The builder images are Ubuntu 26.04, as the reference build was. On another host
  either pass `--base-image` with something matching it, or run `build.sh relay --native`
  to compile on the host itself (needs meson, ninja and the development packages listed in
  `Dockerfile.relay`). At run time the host needs libepoxy, libgbm, libdrm, libvulkan, libEGL,
  libva and libX11; `sf host apply` installs them when an NVIDIA card is present.
- **The relay's helper path is compiled in**, so the relay is built directly into its final
  folder. The farm also passes `RENDER_SERVER_EXEC_PATH`, so a moved folder keeps working.
- The Mesa build pulls zlib and expat through meson's wrap database (checksums verified by
  meson), because the NDK has no pkg-config files for them.
- The gitlab archive for virglrenderer is generated on request. Its checksum has been stable,
  but if it ever changes, compare the unpacked tree with tag `1.2.0` before changing the pin.

### What was and was not reproduced

The patches in this folder were checked against the pristine archives: both apply cleanly,
and the patched files are byte-identical to the source trees the reference binaries were
built from. The Dockerfiles and meson options were reconstructed from the reference builder
images (`docker history`) and from the recorded meson command lines. **The full build through
`build.sh` has not been run end to end** (it is long and needs the NDK); `fetch` and
`check-patches` have.

## Running

The farm does this for you (`superfarm.host.venus`); by hand it is:

```bash
R=<home>/overlays/venus-relay
LD_LIBRARY_PATH=$R/lib VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/nvidia_icd.json \
  $R/bin/virgl_test_server --venus --use-egl-surfaceless \
  --rendernode /dev/dri/renderD129 --socket-path /run/superfarm/venus/renderD129/vtest.sock
```

Rules that matter:

- **Start the relay before any NVIDIA Android.**
- **Never restart a running relay.** Every NVIDIA Android holds connections to it; if it
  stops, all of them lose their graphics and must be restarted. The farm leaves a running
  relay alone for that reason, and its systemd unit does not kill it when the daemon restarts.
- **Do not use `pkill -f virgl_test_server` in a script**: it matches the calling shell too.
  The process name is cut to 15 characters by the kernel: `pkill -x virgl_test_serv`.
- **Changing an Android's renderer invalidates its apps' shader caches.** Apps crash at start
  with a cache built for another renderer; the farm clears them when the mode changes.

## Testing the buffer path

`build.sh tests` compiles the four test programs into `<work>/tests`.

- `probe [width height]`, `fmts`: run in a plain Linux container that has a Linux build of the
  patched Venus driver and the relay socket mounted. `probe` allocates an exportable image the
  way the allocator does, clears it to a known colour, exports a dma-buf, imports it over a
  second connection (standing in for another Android process) and reads the colour back.
  `fmts` lists which layouts the host offers for the video and camera formats. A Linux build
  of the driver uses the same patched tree with
  `-Dplatforms= -Dvulkan-drivers=virtio -Dgallium-drivers= -Dopengl=false -Degl=disabled -Dgbm=disabled -Dglx=disabled -Dgles1=disabled -Dgles2=disabled -Dllvm=disabled -Dxmlconfig=disabled`,
  then `VK_ICD_FILENAMES=<prefix>/share/vulkan/icd.d/virtio_icd.x86_64.json VN_DEBUG=vtest VTEST_SOCKET_NAME=<socket>`.
- `ahbtest [w h usage_hex]`: inside an NVIDIA Android, imports a hardware buffer the way the
  compositor does, with EGL's debug callback on so ANGLE says why it refuses.
- `yuvtest [w h format_hex]`: inside an NVIDIA Android, fills a video buffer on the CPU,
  samples it on the GPU and compares.

## Open points

- 32-bit apps other than the camera service have no driver.
- A few error-level log lines on failed imports are left in the driver. Harmless.
- Hardware video decoding on the card has not been tried; video is decoded on the CPU.
- Only one card model and one driver series (an RTX 40-series card, NVIDIA open kernel module
  595) have been tested.

## Licences

The patches modify Mesa and virglrenderer and carry those projects' MIT licence.
`gpu_config_venus.sh` derives from redroid's `gpu_config.sh` (Apache-2.0). The allocator and
the test programs are part of this project (Apache-2.0).

# Camera and microphone for Android 13 containers

This folder holds everything that has to exist *inside* an Android before
`superfarm.media.camera` can give it a virtual front camera and a microphone:

| File | What it is |
|---|---|
| `extract_hal.sh` | builds the overlay from a BlissOS image you download yourself |
| `hal_patch.py` | the individual edits `extract_hal.sh` applies (each one small and testable) |
| `gralloc_shim.c` | lets redroid's software graphics module hand out camera (YUV) buffers |
| `mic_shim.c` | gives redroid's stub audio module a real microphone, read from a FIFO |
| `build_shim.sh` | compiles both shims (no NDK) and hooks them into copies of redroid's modules |

**Android 13 only.** The HAL binaries are built against Android 13's vendor interface
(VNDK 33) and the patches are located by the machine code of that build. Android 12, 14
or 15 need their own HAL build and their own check of every step below; nothing here is
expected to load on them. The host must be x86_64.

**No third-party binaries are in this repository.** The camera HAL is taken from a BlissOS
image on your machine; the result stays in your farm home.

## Build the overlay

1. Download a **BlissOS 16.x (Android 13, "Typhoon") x86_64** ISO from the BlissOS
   project. The procedure was worked out with 16.9.7 (about 2.4 GB).
2. Have the redroid Android 13 image you run locally (`docker image ls`). The overlay is
   built *for that image*: its own graphics and audio modules are copied out and extended.
3. Install the tools: `docker`, `python3`, `gcc`, `patchelf`, `binutils`, `e2fsprogs`
   (for `debugfs`), `erofs-utils` (for `fsck.erofs`) or `squashfs-tools`, and one ISO
   reader (`libarchive-tools` for `bsdtar`, or `p7zip`, `xorriso`, `genisoimage`).
4. Run, as your normal user:

   ```bash
   native/camera/extract_hal.sh --image redroid/redroid:13.0.0-latest ~/Downloads/Bliss-v16.9.7-x86_64.iso
   ```

   It needs about 8 GB of scratch space for an ISO (`--work DIR` chooses where) and
   writes the overlay to `<farm home>/overlays/camera/13` (`--out DIR` to change;
   `media.camera.hal_overlay` tells the farm about a different place).

   If unpacking fails on your system, mount the image yourself and pass the folder:

   ```bash
   sudo mount -o loop,ro system.img /somewhere        # system.img is inside system.efs / system.sfs
   native/camera/extract_hal.sh --image <your image> /somewhere
   ```

The overlay is a copy-onto-`/vendor` tree:

```
bin/hw/android.hardware.camera.provider@2.5-external-service     the HAL (32-bit)
etc/init/android.hardware.camera.provider@2.5-external-service.rc
etc/external_camera_config.xml                                   patched: orientation 270
etc/vintf/manifest/android.hardware.camera.provider@2.5-external.xml
etc/primary_audio_policy_configuration.xml                       patched: 48 kHz stereo mic
lib/*.so                                                         HAL libraries redroid lacks
lib/libfarmcam_shim.so  lib64/libfarmcam_shim.so                 built here
lib/libfarmmic_shim.so  lib64/libfarmmic_shim.so                 built here
lib/hw/gralloc.redroid.so        lib64/hw/gralloc.redroid.so         redroid's own, + shim
lib/hw/audio.primary.default.so  lib64/hw/audio.primary.default.so   redroid's own, + shim
```

## Put it into Androids

* **Existing Android:** the camera's *install HAL* action (`Camera.install_hal(ids)`)
  streams the overlay into the container's `/vendor` with every file owned by root.
  Restart that Android afterwards. The copy lives in the container: recreating the
  container loses it. (Do not use a plain `docker cp <overlay>/. <container>:/vendor/` as
  an ordinary user: it hands the files, and the `/vendor` folders it touches, to your own
  numeric uid, which inside Android is usually the `system` user.)
* **For good:** bake it into the image. The image builder layers
  `<farm home>/overlays/android/13/` onto `/`, so:

  ```bash
  mkdir -p "$SUPERFARM_HOME/overlays/android/13/vendor"
  cp -a "$SUPERFARM_HOME/overlays/camera/13/." "$SUPERFARM_HOME/overlays/android/13/vendor/"
  ```

  and rebuild the Android 13 image. By hand it is two lines of Dockerfile:
  `FROM <your redroid 13 image>` and `COPY vendor/ /vendor/`.

Then enable `media.camera.enabled`, run `sudo sf host apply` once (creates the loopback
devices), and recreate the Androids that should have a microphone (the FIFO mount is part
of how the container is created).

## What `extract_hal.sh` does, and why

### 1. Open the BlissOS system tree

```
Bliss-*.iso
  └─ system.efs            EROFS image (older builds: system.sfs, SquashFS)
       └─ system.img       ext4, about 4.7 GB
            └─ /system/vendor/...      (system-as-root: vendor lives under /system)
```

The ISO member is extracted with `bsdtar`/`7z`/`xorriso`/`isoinfo`, the EROFS image with
`fsck.erofs --extract`, SquashFS with `unsquashfs`, and single files are read from the
ext4 image with `debugfs` (`dump`), so nothing is mounted and no root is needed.

### 2. Take the HAL and only the libraries redroid lacks

From `/system/vendor`:

* `bin/hw/android.hardware.camera.provider@2.5-external-service` — AOSP's *external
  camera* provider: it turns any V4L2 capture device (`/dev/video*`) into an Android
  camera. redroid does not ship it. In BlissOS 13 it is a **32-bit** program.
* `etc/init/android.hardware.camera.provider@2.5-external-service.rc` — starts it
  (`user cameraserver`, `class hal`).
* `etc/external_camera_config.xml` — its settings.

Then the service's dependency tree is walked with `readelf -d` (`NEEDED` entries). For
each library:

* already loadable in the redroid image → **left alone**. "Loadable" means: in
  `/vendor/lib`, `/vendor/lib/hw`, the VNDK APEX (`/system/apex/com.android.vndk.*/lib`),
  or in `/system/lib` *and* on the LL-NDK list (the few system libraries vendor code may
  use: libc, libm, libdl, liblog, libEGL, libGLESv1_CM, libGLESv2, libGLESv3,
  libandroid_net, libbinder_ndk, libcgrouprc, libft2, libmediandk, libnativewindow,
  libneuralnetworks, libselinux, libsync, libvndksupport, libvulkan, libstdc++, libRS).
  The file list of the image comes from `docker create` + `docker export | tar -t`; the
  container is never started.
* otherwise → copied from BlissOS (`/system/vendor/lib`, else `/system/lib`) into the
  overlay's `lib/`, and walked in turn.

With BlissOS 16.9.7 and redroid 13 that is 19 libraries: `android.hardware.camera.common@1.0`,
`android.hardware.camera.device@{1.0,3.2,3.3,3.4,3.5,3.6}`,
`android.hardware.camera.provider@{2.4,2.4-external,2.5,2.5-external}`,
`android.hidl.allocator@1.0`, `camera.device@{3.2,3.3,3.4,3.5}-impl` and
`camera.device@{3.4,3.5,3.6}-external-impl`.

### 3. The front-camera patch

The external HAL reports `LENS_FACING = EXTERNAL`. Apps that ask for "the front camera"
ignore such a camera. In `camera.device@3.4-external-impl.so`,
`ExternalCameraDevice::initDefaultCharsKeys()` contains

```cpp
const uint8_t facing = ANDROID_LENS_FACING_EXTERNAL;     // 2
UPDATE(ANDROID_LENS_FACING, &facing, 1);                 // tag 0x00080005
```

which the BlissOS build compiled to

```
c6 44 24 2d 02      movb  $0x2, 0x2d(%esp)     <- the 02 becomes 00 (ANDROID_LENS_FACING_FRONT)
8d 44 24 2d         lea   0x2d(%esp), %eax
6a 01               push  $0x1
50                  push  %eax
68 05 00 08 00      push  $0x80005
```

`hal_patch.py lens-facing` finds this by shape (a byte store to a stack slot, the address
of the same slot, count 1, the tag) and changes the one immediate byte. It refuses to
write anything unless it finds the sequence exactly once. In the 16.9.7 library the byte
is at file offset `0x2c031`:

| | SHA-256 |
|---|---|
| original | `80b312f367ffb1c3d5c0cad741ec020ff734daaad510537ba972f7f6625d8e0f` |
| patched | `f54807a1004bf802eb5f25612641ea0df4104fec542f2620795c9b8b1dc2308e` |

### 4. Orientation 270

`hal_patch.py camera-config` adds `<Orientation degree="270"/>` to the `<Device>` section
of `external_camera_config.xml`. A phone's front sensor is mounted sideways and apps rotate
its picture accordingly; with the external default of 0 a portrait app shows the picture
lying on its side. The host side matches: the feed stores the upright portrait picture
rotated (`transpose=1`) inside a landscape 640x360 frame.

### 5. Register the HAL

`etc/vintf/manifest/android.hardware.camera.provider@2.5-external.xml` declares
`android.hardware.camera.provider@2.5::ICameraProvider/external/0` (hwbinder). Without
this manifest fragment the service is refused at registration and the camera service
never asks it for cameras.

### 6. Camera buffers: `gralloc_shim.c`

redroid's software graphics module (`gralloc.redroid.so`) implements only plain `lock()`
and refuses to allocate `YCbCr_420_888`. The camera HAL cannot write preview frames into
such buffers and apps cannot read them. The shim is added to the module as a dependency
(`patchelf --add-needed libfarmcam_shim.so`); its constructor installs a `lock_ycbcr`
that describes Android's standard YV12 layout over a normally locked buffer, and makes
`YCbCr_420_888` allocate as YV12. It is built for both 32- and 64-bit because gralloc is
loaded by every process that touches a buffer. It concerns the software renderer's
allocator only; the GPU modes bring their own.

### 7. Microphone: `mic_shim.c` and the audio policy

redroid's audio module (`audio.primary.default.so`) is a stub: its input stream sleeps
and returns silence. The shim (hooked in the same way) wraps `open_input_stream` so that
a 48 kHz / stereo / 16-bit input stream reads from the FIFO **`/dev/farm_mic`**; a stream
in any other format, or a missing FIFO, falls back to the stub's silence.
`hal_patch.py audio-policy` therefore sets the `primary input` mix port in
`primary_audio_policy_configuration.xml` to `samplingRates="48000"
channelMasks="AUDIO_CHANNEL_IN_STEREO"`, so Android opens the input in exactly that format.

`/dev/farm_mic` is a FIFO on the host (`<farm home>/run/camera/<id>/mic`), bind-mounted
into the container when it is created. The path is compiled into the shim; do not change
one without the other.

### 8. Optional: frame rate from `build.prop` (`--fps-prop N`)

Stock redroid takes the display frame rate from the boot argument
`androidboot.redroid_fps` (property `ro.boot.redroid_fps`), which is fixed when the
container is created. `hal_patch.py fps-property` renames that string inside
`hwcomposer.redroid.so` to the equally long `ro.farm.redroid_fps`, and `build-prop`
appends `ro.farm.redroid_fps=N` to a copy of the image's `/vendor/build.prop`; the rate
then comes from that file and changes with a restart instead of a recreate.

It is **off by default**: Super Farm recreates a container whenever its settings change,
so the boot argument is enough, and a patched display module would ignore `android.fps`.
Use it only for an image that must keep its containers. Note that the overlay then
carries a full copy of that image's `build.prop`.

## How it comes together at run time

```
host                                               Android container
ffmpeg (one per video) ── FIFO ── ffmpeg copier ─▶ /dev/videoN (v4l2loopback)
                                                     └─ mknod /dev/video0 (same major:minor) ─▶ external camera HAL
                      └─ FIFO ── mic_forwarder.py ─▶ <run>/camera/<id>/mic ═ bind mount ═▶ /dev/farm_mic ─▶ mic shim
```

* The container is privileged and inherits every host video node at start. `attach`
  removes them all inside the container and creates only `/dev/video0` for the Android's
  own device. The HAL reads the frame size once when it finds the node, so the device is
  primed with black frames first and `keep_format=1` pins the size.
* Nothing extra runs inside Android; the hooks only work while an app has the camera or
  the microphone open.

## Checking an Android

```bash
docker exec <android> sh -c 'ls -l /dev/video0 /dev/farm_mic; pidof android.hardware.camera.provider@2.5-external-service'
docker exec <android> sh -c 'dumpsys media.camera > /data/local/tmp/cam.txt; grep "Number of camera devices" /data/local/tmp/cam.txt'
docker exec <android> logcat -d -s farm_mic ExtCamPrvdr ExtCamDev
```

**Never pipe `dumpsys media.camera` into something that stops reading early** (`head`,
`grep -m1`, `grep -q`). The HAL writes its own state into that pipe; closing it sends
SIGPIPE into the HAL and kills it, and the camera is gone for several seconds. Write the
dump to a file, or use a reader that consumes everything (`grep -c`).

## Testing a camera and a microphone

Make a test clip and put it in the farm's media folder:

```bash
scripts/camera/make_test_clip.py "$SUPERFARM_HOME/media/test_bars_beeps.mp4"
```

It shows a red stripe on the **left** edge and a running clock, and plays short beeps that
step through 400, 800, 1200 and 1600 Hz.

* **Mirroring.** Apps treat a front camera as a mirror. In a live broadcast test the
  viewer received the picture reversed (red on the right) until the feed flipped it;
  with the flip, broadcaster preview and viewer both showed the source the right way
  round. `media.camera.mirror` is therefore **on by default**. Turn it off for an app
  that shows your source reversed.
* **Do not test sound with a steady tone.** Voice and live-video apps run noise
  suppression, which removes a stationary tone completely: a steady test tone was recorded
  by the app and still reached the viewer as silence. Beeps of changing pitch, speech
  and music pass.
* The Android microphone can be recorded without any app:
  `scrcpy --serial=<adb address> --no-video --no-control --no-window --no-playback
  --audio-source=mic --audio-codec=raw --record=mic.wav --time-limit=8`.

## What has and has not been verified

* On the farm this was developed on, with this overlay: stock Camera app preview and
  recording, a live-video app's preview and a real broadcast watched from a second Android
  (picture and sound arrive; see "Mirroring" above), and microphone recordings without
  dropouts, with the software renderer and with the NVIDIA relay. The Intel/AMD GPU modes
  use a different graphics allocator; the camera was not checked there.
* `extract_hal.sh` was checked from the `system.img`, `system.sfs` and folder stages
  onward: fed the original BlissOS 16.9.7 files it reproduces that farm's overlay byte
  for byte (shims included). The ISO and EROFS unpacking steps are written from the
  documented tools' interfaces and have **not** been run against a real ISO here.
* The host microphone FIFO mounted at `/dev/farm_mic` was checked end to end on a real
  Android 13 container: beeps played by the farm were recorded from the Android
  microphone at source level, mute gave digital silence, unmute restored the sound.

## Other Android versions

A different Android version needs, at least: a HAL service built for that version's VNDK
(newer Android versions use an AIDL camera provider instead of this HIDL one), a fresh look at which
libraries the redroid image of that version lacks, the front-camera change located in
that build's code (or built from source with `ANDROID_LENS_FACING_FRONT`), and a check
that the gralloc and audio stubs still have the layout the shims assume. Put the result
in `<farm home>/overlays/camera/<version>`.

# Virtual camera kernel module (v4l2loopback) with the farm's patch

Each Android gets its camera from a [v4l2loopback](https://github.com/v4l2loopback/v4l2loopback)
device on the host: `/dev/video(100 + N)` for Android N, fed with MJPEG by ffmpeg and handed to
the container as `/dev/video0`. Upstream v0.15.1 needs three small changes for that, and they
live here as a patch plus a build script.

**Licence: GPL-2.0-or-later**, the same as upstream. `v4l2loopback-0.15.1-farm.patch` is a
modification of a GPL work and is distributed under that licence; the full text is in
[`COPYING`](COPYING). The rest of the Super Farm repository is Apache-2.0 and does not link
against or include this module: it only loads it and talks to it through the kernel's V4L2
interface.

## What the patch changes

| Change | Why |
|---|---|
| Take the `v4l2_fh` from the file in every ioctl handler; pass the file to `v4l2_fh_add()` / `v4l2_fh_del()` | Kernel 6.18 and later tie the handle to the file and no longer pass it to ioctl handlers. Without this the module does not compile on current kernels (found on 7.0). Older kernels keep the old calls through a version check. |
| MJPEG `depth` 32 -> 16 in `v4l2loopback_formats.h` | The module sizes its frame buffers as width x height x depth / 8. Stock, an MJPEG buffer is as large as an uncompressed 32-bit frame, which Android's external-camera driver does not cope with; the reference farm has always run with half of that, still far more than a compressed frame needs. |
| (build option, not in the patch) `-DMAX_DEVICES=256` | Upstream allows 8 devices. A farm needs one per Android. `build.sh --max-devices N` changes it. |

The patch is exactly `git diff` of the tree the reference farm runs, against upstream commit
`7546a42a66d856933a75c2b0896cb735e3ca00c0` (tag `v0.15.1`).

## Building

```bash
native/v4l2loopback/build.sh build            # as your user: fetch, patch, compile
sudo native/v4l2loopback/build.sh install     # install for the running kernel + v4l2loopback-ctl
sf host apply                            # load it and create the camera devices
```

Needs the kernel headers and a compiler (`sf doctor` prints the exact package names for your
distribution). The script pins upstream by commit id and refuses to build anything else.

### Kernel updates: DKMS

A module built by hand is for one kernel. After a kernel update it must be rebuilt, or:

```bash
sudo native/v4l2loopback/build.sh dkms
```

registers the patched source with DKMS as `v4l2loopback/0.15.1.superfarm`, so it is rebuilt
automatically for every new kernel. Remove a distribution `v4l2loopback-dkms` package first;
two providers of the same module name conflict.

### Secure Boot

With Secure Boot on, the kernel rejects modules that are not signed by a key enrolled in the
firmware. A module you build is not. Once per machine, create and enrol a key; after every
build (every kernel update), sign:

```bash
sudo scripts/host/sign-module.sh --status          # Secure Boot state, key found, enrolled?
sudo scripts/host/sign-module.sh --create-key --enroll    # only if no key exists yet; then reboot and confirm
sudo scripts/host/sign-module.sh v4l2loopback
```

DKMS signs by itself when it has a key (`/var/lib/dkms/mok.*`, or the distribution's MOK on
Debian/Ubuntu); that key still has to be enrolled once.

## How the farm uses it

- Loaded with `devices=0 max_buffers=8`: no camera exists until one is created for a
  specific Android, so device numbers always match Android ids. 8 buffers is what the
  Android camera driver expects.
- Devices are created by root at boot (`superfarm-host.service`) with
  `v4l2loopback-ctl add -n "Super Farm Camera N" -x 1 -b 8 /dev/video(100+N)`; `-x 1`
  (exclusive caps) makes the device look like a plain capture camera.
- Each device is primed with black frames at the sensor size and set to `keep_format=1`
  before Android opens it; that part belongs to `superfarm.media.camera`.

## What was verified

On Ubuntu 26.04, kernel 7.0.0: the patch applies to the pinned commit, and
`build.sh build` compiles the module and `v4l2loopback-ctl` against the running kernel's
headers. The same patched source is what the reference farm has loaded. `install` and `dkms`
need root and were **not run** while writing this.

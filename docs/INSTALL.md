# Install

For someone setting up Super Farm on a Linux machine for the first time, and for anyone updating or removing it.

Anything marked "not verified" was read from the code and never run on real hardware. [Verification](VERIFICATION.md) lists what was run.

## What was tested and what was not

The installer ran inside throwaway containers on Ubuntu 24.04, Debian 12, Fedora 44, Arch (rolling) and openSUSE Tumbleweed. In each one the packages installed, the wheel installed into a fresh virtual environment, `sf` and the doctor ran, and a 603-test subset passed (Python 3.11, 3.12, 3.13 and 3.14 across them).

A container has no kernel of its own and no init system. These steps therefore ran only as a dry run or in unit tests, never for real on a second machine:

- `sf host apply` with real packages, kernel modules, sysctl values, zram, groups and virtual camera devices
- the boot service (`superfarm-host.service`) running
- `sf service install` and `sf service start`
- kernel module signing for Secure Boot

Everything that was run live (Androids booting, GPU modes, networking, backups, automation) was run on one machine: an Intel Core i9-14900K, 32 GB RAM, an NVIDIA RTX 4080 SUPER plus the Intel iGPU, Ubuntu 26.04, kernel 7.0, Docker 29, Python 3.14, Secure Boot on. Other kernels, Docker versions older than 29 and Android versions other than 13 are not verified.

## Requirements

| What | Requirement |
| --- | --- |
| CPU | 64-bit x86 (`x86_64`). The installer stops on anything else. Four or more threads; the doctor warns below that. |
| OS | Linux with systemd, one of the families in the [distribution sections](#distribution-notes). |
| Kernel | 5.0 or newer (the doctor fails below that). The Android images are tested on 5.10 and newer. The kernel must provide binder (see [Binder](#binder)), cgroup v2 and the usual container networking modules. |
| Docker | Docker Engine running, and your user allowed to use it. Docker 29 is what was tested. |
| RAM | The doctor fails below `resources.memory_mb` plus half of `performance.reserve_host_mb` (3072 MB, which is 3 GB, with the defaults) and warns below 8 GB. Plan on 0.9 to 1.1 GB per running Android in use. See [Scaling](SCALING.md). |
| Disk | The doctor warns below 5 GB free for the farm home and 20 GB for Docker's data. Each Android needs 2 to 6 GB once apps are installed. |
| Python | 3.10 or newer with the `venv` module. The installer installs it when missing. |
| Other tools | `adb`, `ffmpeg`, `curl`, `kmod`, `ip` are required. `scrcpy`, `zstd`, `pigz`, `patchelf`, `git` and `lspci` are optional. The installer installs what it can. |

Put the farm home and Docker's data on a filesystem that keeps Unix permissions and extended attributes (ext4, xfs, btrfs). The doctor fails or warns for NTFS, exFAT, FAT, and network filesystems, because the farm home holds 0600 secrets and sockets and Android's `/data` holds numeric owners and SELinux labels.

## Install from a fresh machine

### 1. Get the code

```bash
git clone https://github.com/hamzaahmadaslam/super-android-farm.git
cd super-android-farm
```

### 2. Look at what the installer will do

`--dry-run` prints every command and every file the installer would write, and changes nothing. Read it before the real run.

```bash
./install.sh --dry-run
```

On the Ubuntu test machine it starts like this (shortened):

```text
== Distribution
Ubuntu 26.04.1 LTS (family: debian, kernel 7.0.0-38-generic)

== Python
Python 3.14.4 with venv support

== Virtual environment: /home/<you>/super-android-farm/.venv
...
== Host setup (needs root: packages, kernel modules, limits, groups, boot unit)
+ sudo env SUPERFARM_HOME=... SUPERFARM_USER=<you> .../.venv/bin/python -m superfarm.host.setup apply
  which would do the following:
  [dry-run] Install packages
  [dry-run] Enable Docker
  [dry-run] Add <you> to groups
  [dry-run] Load kernel modules
  [dry-run] Raise kernel limits
  [dry-run] Create runtime folders
  [dry-run] Install the boot unit
```

On a machine that already has Docker, adb and ffmpeg, the package step lists only what is missing. After the virtual environment exists you can also run `sf host plan`, which prints the same host steps.

### 3. Run the installer

```bash
./install.sh
```

Run it as your normal desktop user. It refuses to run as root, because the daemon must never run as root, and it asks for `sudo` itself where needed. It is safe to run again: every step checks what is already there.

The installer does five things:

1. Reads `/etc/os-release` to find the package family (apt, dnf, pacman or zypper).
2. Makes sure Python 3.10 or newer with `venv` exists. This is a `sudo` package install only if Python is missing.
3. Creates a virtual environment in `.venv` inside the checkout and installs the project into it in editable mode (`pip install -e`).
4. Runs host setup, the one step that needs root (below).
5. Prints the next steps.

Options: `--dry-run`, `--home DIR` (farm home, default `~/.local/share/superfarm`), `--venv DIR`, `--no-host` (skip host setup and run it later with `sf host apply`), `--dev` (also install the test tools) and `--container-test` (for the distribution test only, never on a real machine). Paths for `--home` and `--venv` must not contain spaces, because systemd units cannot name them.

The installer adds no optional extras. Add one later from the checkout, for example the MCP server (see [MCP.md](MCP.md)): `.venv/bin/python -m pip install -e ".[mcp]"`.

### 4. The one sudo step

Host setup is the same code as `sf host apply`. It runs as root through `sudo`, with the fixed list of actions you saw in the dry run:

| Step | What it does |
| --- | --- |
| `packages` | Installs the required packages (Docker, adb, ffmpeg, kmod, iproute, curl) and the optional ones one by one, so a package missing from your distribution (scrcpy on some) does not stop the rest. |
| `docker-service` | `systemctl enable --now docker.service`. |
| `groups` | Adds your user to the `video` and `render` groups, and to `docker`, when those groups exist. |
| `modules` | Writes `/etc/modules-load.d/superfarm.conf` and `/etc/modprobe.d/superfarm.conf`, and loads the kernel modules by name. The farm never relies on module auto-loading. |
| `sysctl` | Writes `/etc/sysctl.d/90-superfarm.conf` with kernel limits sized for many containers (inotify, `pid_max`, `file-max`, neighbour table sizes, IP forwarding). Values are floors: a machine that already has more keeps its value. |
| `run-dirs` | Creates `/run/superfarm` (and the NVIDIA socket folder below it) owned by the `docker` group, and `/etc/tmpfiles.d/superfarm.conf` to recreate them at boot. |
| `zram`, `ksm`, `camera` | Only when `performance.zram.enabled`, `performance.ksm` or `media.camera.enabled` is on. |
| `systemd` | Installs `/etc/systemd/system/superfarm-host.service`, a oneshot unit that repeats the boot-time part of host setup (modules, zram, camera devices, `/run` folders) as root at every boot. |

Root only creates folders below `/run/superfarm`, and everything it acts on is a fixed command with validated values. `sf host apply --boot` runs only the part that has to be redone after a reboot.

If the `groups` step reported nothing on a fresh machine, run `sf host apply` once more after Docker is installed. Reading the code, the step builds its group list before the package step runs, so the `docker` group does not exist yet on a machine that had no Docker. This path was not run end to end.

### 5. Log out and in

If host setup added you to `docker`, `video` or `render`, the groups apply only to new logins. Log out and in again (or reboot). `sf doctor` reports a missing Docker permission under "Docker access".

### 6. Put `sf` on your PATH

The command is in the virtual environment. Either add `<checkout>/.venv/bin` to your `PATH` or link it:

```bash
ln -s "$PWD/.venv/bin/sf" ~/.local/bin/sf
```

The rest of this guide writes `sf`.

### 7. Check the machine

```bash
sf doctor
```

The doctor runs 46 read-only checks (CPU, kernel, binder, Docker, kernel limits, memory, disk, GPUs, tools, ports, security settings) and prints a table. For every `fail` or `warn` it prints the exact fix for your distribution. It never changes anything and never needs root. The exit code is 1 when any check fails.

Fix every `fail` before you go on. Read the warnings; many are optional (for example scrcpy, compressed swap, a missing display for native windows). The same checks are in the panel under System, and [Troubleshooting](TROUBLESHOOTING.md) explains the common ones.

### 8. Start the daemon

The daemon serves the panel and the API on `127.0.0.1:7700` (`server.host`, `server.port`) and runs as your user.

In the foreground, for a first look:

```bash
sf serve
```

Stop it with Ctrl-C. Androids keep running when the daemon stops.

As a user service, which is how you run it day to day:

```bash
sf service install     # writes ~/.config/systemd/user/superfarm.service and enables it
sf service start
sf service status
sf service logs -f
```

The service starts at your first login and stops at your last logout. On a headless machine, or to keep the farm up through logouts, enable lingering once:

```bash
loginctl enable-linger "$USER"
```

`sf service status` shows whether lingering is on. Androids are Docker containers and keep running either way. What stops with the user manager is the panel, running automation and the NVIDIA relay (NVIDIA Androids lose their picture until they restart). The service is a user unit, not a system unit, because native device windows and "listen to one Android" need your desktop session's display and sound. Details are in the comments of `deploy/systemd/superfarm.service`.

If `sf service install` is run from a session without a desktop, device windows need the service to be restarted once from inside the desktop (`systemctl --user restart superfarm`). The panel, the API and the Androids are unaffected.

### 9. Log in to the panel

```bash
sf open     # prints the panel address
sf token    # prints the API token
```

Open `http://127.0.0.1:7700` in a browser on the same machine and paste the token into the login form. The token is created on first use in `<home>/secrets/api-token` and is never put in a link.

Treat the token like a root password. Whoever holds it can make the daemon run containers that are privileged on this machine. `sf token --rotate` replaces it and signs every browser session out. See [Security model](SECURITY_MODEL.md) before you change `server.host`.

The dashboard shows a short first-run checklist: host checks, the Android image, creating an Android, starting it and adding an app. The [Quickstart](QUICKSTART.md) walks through the same path.

## Settings: farm defaults and per-Android overrides

[Configuration reference](CONFIGURATION.md) lists every setting. It is generated from the code, so it holds no explanation; this section does.

Settings live in `config.yaml` in the farm home (`sf config path` prints the location). The file holds only what differs from the defaults.

```bash
sf config show               # everything in effect
sf config show --changed     # only what config.yaml holds
sf config get android.fps
sf config set android.fps 25
sf config unset android.fps  # back to the default
```

Keys use dots: `network.vpn.selection` is `selection` in `vpn` in `network`. Lists are comma separated (`sf config set network.dns 1.1.1.1,9.9.9.9`). A value is validated before it is saved. `sf config set` edits the file; a running daemon reads it at its next start (`sf service stop && sf service start`). The panel's Settings page and `PATCH /api/config` apply a change to the running daemon.

Most settings are farm-wide defaults. An Android follows them until you override one for that Android:

```bash
sf set 1-5 fps=30 network.mode=vpn       # per-Android overrides
sf set 3 gpu_mode=default                 # follow the farm default again
```

`sf set` keys are fields of the Android's own settings, not `config.yaml` keys. They include `android_version`, `width`, `height`, `dpi`, `fps`, `gpu_mode`, `gpu_node`, `resources.cpus`, `resources.memory_mb`, `resources.memory_swap_mb`, `resources.cpuset`, `network.mode`, `network.vpn_profile`, `network.gateway`, `network.proxy`, `network.killswitch`, `device.*`, `camera`, `autostart`, `tags`, `extra_boot_args` and `extra_props`. An Android with no override follows the farm value and keeps following it when the farm value changes.

What needs a restart after a change:

| Setting group | Takes effect |
| --- | --- |
| `android.*`, `resources.*`, `gpu.*`, `network.*`, `device.*`, `media.camera.*`, `docker.log_*`, `docker.name_prefix`, `performance.restart_policy`, `performance.trim_*`, `performance.disable_animations` | At each Android's next restart. The change is saved at once. Some changes recreate the container; data is kept. |
| `server.host`, `server.port`, `server.tls_cert`, `server.tls_key`, `server.allowed_origins`, `server.trusted_proxies`, `logging.*` | At the next daemon restart. |
| Everything else | Immediately, when set through the panel or the API. |

`PATCH /api/config` returns `changed`, `restart_android` and `restart_daemon` lists so a client can say which applies.

Three settings cannot be changed through the API, because they decide which program the daemon runs: `docker.binary`, `gpu.nvidia.relay_dir` and `gpu.nvidia.icd`. Change them in `config.yaml` or with `sf config set`, then restart the daemon.

Two settings are refused when they conflict with each other: `server.auth_required` cannot be off when `server.host` is not a loopback address, and `network.gateway` must name a gateway in `network.gateways`.

## Distribution notes

The installer detects the family from `/etc/os-release`, including derivatives named in `ID_LIKE`. The package names for each family are in `superfarm/host/distro.py`, and the distribution test checked each name against the real package index of one release per family.

| Family | Distributions | Package manager | Docker package |
| --- | --- | --- | --- |
| debian | Debian, Ubuntu, Mint, Pop!_OS, Raspberry Pi OS and similar | `apt-get` | `docker.io` |
| fedora | Fedora, RHEL, CentOS Stream, Rocky, Alma | `dnf` | `moby-engine` |
| arch | Arch, Manjaro, EndeavourOS, CachyOS | `pacman` | `docker` |
| suse | openSUSE Leap and Tumbleweed, SLES | `zypper` | `docker` |

### Binder

Android needs the kernel's binder driver, loaded with `devices=binder,hwbinder,vndbinder`. Host setup loads `binder_linux` by name when the module exists. When the kernel has no binder at all, the doctor prints what to do for your family:

- Debian and Ubuntu: install `linux-modules-extra-<kernel>` on releases that still split it out, then load the module.
- Arch: a kernel flavour that includes binder, such as `linux-zen`, with its headers, then reboot into it. Manjaro uses `linuxXY-headers`.
- Fedora, RHEL family and openSUSE: the doctor says to boot a kernel built with `CONFIG_ANDROID_BINDER_IPC` and `CONFIG_ANDROID_BINDERFS`. No such kernel was tested.

Binder was available on the Ubuntu 26.04 test machine and was not checked on any other real kernel. On the other families the doctor's advice comes from the code and was not followed on real hardware.

### Ubuntu and Debian

- Packages: `docker.io`, `adb`, `ffmpeg`, `scrcpy` where the release has it.
- Debian 12 has no `scrcpy` package, and Ubuntu 24.04's package (1.25) is older than the 2.1 that the H.264 live view needs. See [scrcpy](#scrcpy).
- The `python3-venv` and `python3-dev` packages are installed together with Python, because Debian splits `venv` out.
- AppArmor is active by default. The doctor notes that privileged Android containers run unconfined and the network sidecars run under `docker-default`.

### Fedora and the RHEL family

- Fedora uses `moby-engine`, `android-tools` and `ffmpeg-free`. `scrcpy` is not in the Fedora repositories.
- On RHEL, CentOS Stream, Rocky and Alma the base repositories ship podman, not Docker, and no `adb` or `ffmpeg`. The installer prints these hints and does not set the repositories up for you: add Docker's own repository (`https://download.docker.com/linux/rhel/docker-ce.repo`, then install `docker-ce docker-ce-cli containerd.io`), enable EPEL for `android-tools`, and RPM Fusion for `ffmpeg`. The RHEL-family hints were not covered by the distribution test.
- SELinux enforcing: Androids run privileged and are not confined. The network sidecars are, and may be denied their bind-mounted VPN profiles. If a VPN sidecar cannot read its profile, the doctor suggests `sudo chcon -R -t container_file_t <home>/vpn <home>/secrets` (it prints the real paths).

### Arch and Manjaro

- Packages: `docker`, `android-tools`, `ffmpeg`, `scrcpy`.
- The installer installs Python with `pacman -S --needed` and does not refresh the package database on a real system, to avoid a partial upgrade. If a package is not found, run a full system upgrade (`sudo pacman -Syu`) and run the installer again.
- Kernel headers (needed only to build the virtual camera module) are named after the kernel flavour. The doctor and `distro.py` handle `linux-lts`, `linux-zen`, `linux-hardened`, `linux-rt` and Manjaro's `linuxXY` naming.

### openSUSE

- Packages: `docker`, `android-tools`, `ffmpeg`, `scrcpy`. Kernel headers are `kernel-default-devel` (or the flavour's `-devel`).
- Binder: see above. This was not tried on a real openSUSE kernel.

### scrcpy

`scrcpy` powers native device windows, "listen to one Android", audio capture and the H.264 live view in the panel. It is optional: the panel's screenshot-based live view works without it. The doctor reports per feature what the code enforces: native windows and listening need a working `scrcpy` and check no version; the H.264 live view needs 2.1 up to 3.x (tested: 3.3.x, other versions need `stream.h264` set to `on`). The doctor warns when the H.264 view cannot be used with the installed version.

Where to get it when your distribution has none or an old one:

- Download `scrcpy-linux-x86_64-*.tar.gz` from `https://github.com/Genymobile/scrcpy/releases`, unpack it, and put its folder on `PATH` ahead of any packaged copy. The test machine runs scrcpy 3.3.4.
- Fedora also has a community COPR: `dnf copr enable zeno/scrcpy && dnf install scrcpy`. That COPR is a third-party source.

A systemd user service does not read your shell profile. If you unpacked scrcpy into `~/.local/bin`, add that folder for all user services:

```bash
mkdir -p ~/.config/environment.d
echo 'PATH=$HOME/.local/bin:$PATH' > ~/.config/environment.d/60-path.conf
```

Then log out and in.

## Secure Boot and module signing

With Secure Boot on, the kernel refuses modules that are not signed by a key the firmware trusts. Distribution kernels and their modules are signed. The only module Super Farm can ask you to build yourself is the virtual camera module, `v4l2loopback`. Everything else in the module list is stock.

If you do not use virtual cameras, Secure Boot needs no action. The doctor's "Secure Boot" row says so.

If you do, sign the module once per kernel build. The signing script reports the state without root:

```bash
sf host sign-module
```

With no arguments this prints the Secure Boot state, the signing key it found and whether it is enrolled. Arguments after `--` go to `scripts/host/sign-module.sh`, and anything but a status report runs under `sudo`:

```bash
sf host sign-module -- --create-key --enroll    # only when no key exists yet; then reboot and confirm in the firmware
sf host sign-module -- v4l2loopback             # sign the module; repeat after every kernel update
```

The script looks for a key in, in order: `/var/lib/shim-signed/mok/MOK.{priv,der}` (Debian and Ubuntu), `/var/lib/dkms/mok.{key,pub}` (DKMS), `/etc/pki/akmods/...` (Fedora and RHEL) and `/var/lib/superfarm/mok/` (created by `--create-key`). The firmware enrolment asks for a one-time password and takes effect at the next reboot.

Module signing was not run for real (it needs root and a firmware enrolment). The DKMS path (`sudo native/v4l2loopback/build.sh dkms`) rebuilds and, where DKMS has a key, signs the module at every kernel update.

## NVIDIA mode

The default rendering mode `auto` works without anything in this section. It picks the best path the machine offers, and on an NVIDIA-only machine falls back to CPU rendering until the NVIDIA runtime exists.

NVIDIA cards cannot use Mesa, so Androids draw through a Vulkan relay and a Venus driver. The runtime is built from source by you. Prerequisites:

- The NVIDIA kernel driver (proprietary or open module) loaded, and the Vulkan ICD file present (`gpu.nvidia.icd`, default `/usr/share/vulkan/icd.d/nvidia_icd.json`). The doctor's rows "NVIDIA driver" and "NVIDIA Vulkan driver" check both and print the install command per family.
- Docker, about 6 GB of free disk for the build folder, network access for three pinned source archives, and the Android NDK r27c.
- Android 13. The runtime is built for Android 13 x86_64 only.

Build it as your normal user (no root):

```bash
native/venus/build.sh all --ndk /path/to/android-ndk-r27c      # or: --fetch-ndk (about 650 MB download)
```

The build folder defaults to `~/.cache/superfarm/venus-build` (`--work DIR` moves it). The outputs go to `<home>/overlays/venus/` (the driver, the allocator, `gpu_config.sh`) and `<home>/overlays/venus-relay/` (the relay program the daemon runs). `native/venus/build.sh --help` lists the single steps.

The build script itself has not been run end to end. The test machine used a prebuilt runtime and relay built the same way; `fetch` and `check-patches` have run from the script. Expect to debug the build on a different host. The relay must be built against a C library no newer than the host's, so on a distribution other than Ubuntu 26.04 pass `--base-image` or `--native`. [native/venus/README.md](../native/venus/README.md) explains the design and the rules for running the relay. [GPU](GPU.md) covers how to use the mode once it is built.

## Virtual camera and microphone

This feature has the most steps and is Android 13 only. Nothing is distributed: you build the pieces on your machine. In order:

1. Build the kernel module: install kernel headers and a compiler (`sf doctor` prints the package names), then
   ```bash
   native/v4l2loopback/build.sh build
   sudo native/v4l2loopback/build.sh install      # or: sudo native/v4l2loopback/build.sh dkms
   ```
2. Under Secure Boot, sign the module (previous section).
3. Download a BlissOS 16.x (Android 13) x86_64 ISO yourself and extract the camera HAL overlay from it into `<home>/overlays/camera/13`:
   ```bash
   native/camera/extract_hal.sh --image redroid/redroid:13.0.0-latest ~/Downloads/Bliss-v16.9.7-x86_64.iso
   ```
   An ISO needs about 8 GB of scratch space (`--work DIR` chooses where).
4. Put the overlay into the Android image (for good) or into a running Android, as [native/camera/README.md](../native/camera/README.md) describes.
5. `sf config set media.camera.enabled true`, then `sf host apply` (creates the loopback devices), then recreate the Androids that should have a camera or microphone.

Only the microphone path and the in-container steps were run end to end. The camera picture needs a loopback device created by root, which was not done on the test machine. [Media](MEDIA.md) covers using sources once the pieces exist.

## Update

The installer installs the project in editable mode, so an update is a `git pull`:

```bash
cd super-android-farm
git pull
./install.sh                          # safe to repeat; refreshes the environment and host setup
sf service stop && sf service start   # load the new code
```

After an update, the System page in the panel marks an Android image as stale when it no longer matches its base image or overlay files; `sf image build 13` rebuilds it. Running Androids keep running while the daemon restarts. There is no separate settings migration: `config.yaml` is validated when it loads, and a key the new version does not know is rejected by name.

## Uninstall

Removing the farm has three parts. Do them in this order.

1. Decide what happens to the Androids' data. Back up what you want to keep ([Backup](BACKUP.md)). `sf rm all` deletes every Android and its data volume, after taking a safety backup first; `sf rm all --keep-data` deletes the containers and keeps the data volumes (`sf-data-<id>`).

2. Stop and remove the services:
   ```bash
   sf service stop
   systemctl --user disable superfarm
   rm ~/.config/systemd/user/superfarm.service
   sudo systemctl disable --now superfarm-host.service
   sudo rm /etc/systemd/system/superfarm-host.service
   sudo systemctl daemon-reload
   ```

3. Remove the files host setup wrote, and the data:
   ```bash
   sudo rm -f /etc/modules-load.d/superfarm.conf /etc/modprobe.d/superfarm.conf \
              /etc/sysctl.d/90-superfarm.conf /etc/tmpfiles.d/superfarm.conf
   sudo rm -rf /run/superfarm
   ```

Then, if you want everything gone:

- Docker objects the farm made carry the prefix `sf` (`docker.name_prefix`): containers `sf-android-<id>` and `sf-net-<id>`, volumes `sf-data-<id>`, gateway containers `sf-gw-<name>` and networks `sf-gwnet-<name>`. List them with `docker ps -a --filter name=sf-` and `docker volume ls --filter name=sf-` before you delete anything, because they are the Androids' data. The images are `superfarm/android:<version>`, `superfarm/tools:1` and `superfarm/net`.
- The farm home (`~/.local/share/superfarm` by default) holds `config.yaml`, secrets including the API token and VPN credentials, the app library, backups and automation data. Delete it only when you no longer need any of that.
- The checkout and its `.venv`.

Uninstalling leaves packages, your group memberships and Docker itself in place. The sysctl values stay loaded until the next reboot. Removing the files above does not unload kernel modules.

## Next

- [Quickstart](QUICKSTART.md): create an Android, start it, install an app, run a flow and back it up.
- [Configuration reference](CONFIGURATION.md): every setting and its default.
- [GPU](GPU.md) and [Networking](NETWORKING.md): the two choices that matter most for how a farm behaves.
- [Troubleshooting](TROUBLESHOOTING.md): what the doctor's warnings mean.

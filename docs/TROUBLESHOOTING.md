# Troubleshooting

For operators whose farm, or one Android in it, does not do what it should.

Each row gives a symptom, what to check and the command that fixes it. The symptoms are the doctor's own check names and the error messages the code prints. Commands are written as `sf ...`; use `.venv/bin/sf` if `sf` is not on your `PATH`.

## Start here

Four commands answer most questions:

```bash
sf doctor            # 46 read-only checks of the machine, each with its fix
sf ls                # every Android and its state
sf info 3            # one Android in detail, including its last error
sf tasks             # background tasks and their per-device results
```

Where the logs are:

| What | Command or file |
| --- | --- |
| The daemon | `sf service logs -f`, or `<home>/logs/daemon.log` (`sf serve` also prints it) |
| One Android's containers | `sf logs 3` |
| Android's own log | `sf logs 3 --logcat` |
| One Android's network sidecar | `docker logs sf-net-3` |
| Tunnel state and kill switch | `sf vpn status` |
| Who changed what | `<home>/logs/audit.log` |
| The NVIDIA relay | `<home>/logs/venus-relay-*.log` |

`<home>` is the farm home: `sf config path` prints the location of `config.yaml` inside it.

`sf config set` saves to `config.yaml`, and a running daemon reads the file at its next start. After a fix that uses it, restart the daemon:

```bash
sf service stop && sf service start      # or stop and rerun `sf serve`
```

Settings under `android`, `resources`, `gpu`, `network` and `device` reach an Android at its next restart (`sf restart <selector>`).

## Install

| Symptom | Check | Fix |
| --- | --- | --- |
| `error: run this as your normal desktop user, not as root or with sudo` | You started `install.sh` with `sudo`. | `./install.sh` |
| `error: Super Farm needs an x86_64 machine` | `uname -m` | None. The Android images are x86_64 only. |
| `error: paths with spaces are not supported for --venv / --home` | The checkout or the farm home has a space in its path. | `./install.sh --venv ~/sf-venv --home ~/.local/share/superfarm` |
| `warning: host setup reported problems` | Read the steps printed above the warning. | `sf host apply` |
| You want to see what host setup would do | | `sf host apply --dry-run` |
| `sf: command not found` | The virtual environment is not on your `PATH`. | `ln -s "$PWD/.venv/bin/sf" ~/.local/bin/sf` |
| Doctor: `Android binder` fails with "exists for this kernel but is not loaded" | `lsmod \| grep binder` | `sudo modprobe binder_linux devices=binder,hwbinder,vndbinder`<br>`sf host apply` |
| Doctor: `Android binder` fails with "this kernel has no binder support" | Your kernel was built without binder. | Debian and Ubuntu releases that split it out: `sudo apt-get install -y linux-modules-extra-$(uname -r)`<br>Arch: `sudo pacman -S linux-zen linux-zen-headers`, then reboot into it<br>Otherwise boot a kernel built with `CONFIG_ANDROID_BINDER_IPC` and `CONFIG_ANDROID_BINDERFS` |
| Doctor: `Android binder` warns "binderfs is not registered" | | `sudo mkdir -p /dev/binderfs && sudo mount -t binder binder /dev/binderfs` |
| Doctor: `Container networking` or `Netfilter` fails with "installed but not loaded" | Module auto-loading is off on this host. | `sf host apply` |
| Doctor: `Kernel module auto-loading` warns | `cat /proc/sys/kernel/modprobe` prints nothing. | `sf host apply` |
| Doctor: `IP forwarding` fails | | `sudo sysctl -w net.ipv4.ip_forward=1`<br>`sf host apply` |
| Doctor: `Docker installed` fails | | The doctor prints the install command for your distribution. |
| Doctor: `Docker daemon` fails | `systemctl status docker` | `sudo systemctl enable --now docker.service` |
| Doctor: `Docker usable by this user` fails | `id -nG` does not list `docker`. | `sudo usermod -aG docker "$USER"`, then log out and in |
| Doctor: `Docker storage driver` fails with `vfs` | `docker info --format '{{.Driver}}'` | Set `{"storage-driver": "overlay2"}` in `/etc/docker/daemon.json`, then `sudo systemctl restart docker` |
| Doctor: `inotify limits` or `ARP / neighbour table size` warns | | `sf host apply` |
| Doctor: `Farm home filesystem` fails with "no Unix permissions" | The farm home is on NTFS, exFAT or FAT. | `export SUPERFARM_HOME=~/.local/share/superfarm` |
| Doctor: `Host setup applied` warns | Host setup has not run, or only partly. | `sf host apply` |
| Doctor: `scrcpy` warns that the live H.264 view is not supported | `scrcpy --version`. Windows and listening need no particular version; the H.264 view needs 2.1 up to 3.x. | Download the upstream build from <https://github.com/Genymobile/scrcpy/releases> and put its folder first on your `PATH`. |
| After a reboot, Androids no longer start | The boot unit did not run. | `systemctl status superfarm-host.service`<br>`sf host apply --boot` |

`sf host apply`, the boot unit and `sf service install` have not been run for real during testing; see [VERIFICATION.md](VERIFICATION.md). If one of them fails, `sf host apply --dry-run` prints every command so that you can run the failing one by hand.

## An Android will not boot

| Symptom | Check | Fix |
| --- | --- | --- |
| `Android N stopped while booting. Last output: ...` | `sf doctor`<br>`sf logs N` | Fix what the doctor reports. To rule out the GPU: `sf gpu set N software`, then `sf start N` |
| `Android N did not finish booting within 180 s. It is left running` | `sf top` (is the machine overloaded?)<br>`docker logs sf-android-N` | `sf config set performance.boot_timeout 300`<br>`sf config set performance.start_concurrency 2` |
| `Android N was started but cannot be reached: the adb program is not installed` | `adb version` | Install your distribution's ADB package; `sf doctor` prints the command. |
| `Android N: network (vpn) is not ready: ...` | `docker logs sf-net-N`<br>`sf vpn status N` | See [VPN and kill switch](#vpn-and-kill-switch). |
| `a container named sf-android-N exists that this farm did not create; it is left alone` | `docker ps -a --filter name=sf-` | Remove or rename that container yourself, or use another prefix: `sf config set docker.name_prefix farm2` |
| `Docker did not answer (...)` | `docker info` | `sudo systemctl enable --now docker.service` |
| `could not pull <image>: ...` | The host's internet connection; the image name in `sf image ls`. | `sf image pull 13` |
| `Android N: its data was written by Android X and it is now set to Android Y` | `sf info N` | Set the version back: `sf set N android_version=X`. To go ahead and risk the data: `sf set N android_version=Y --force` |
| `Android N uses the adopted volume ..., which no longer exists` | `docker volume ls` | Restore the volume, or give the Android a fresh one: `sf set N data_volume=default` |
| `Android N is busy (...)` or HTTP `409 busy` | `sf tasks --active` | Wait for the task, or cancel it: `sf tasks t12 --cancel` |
| An Android boots, then apps crash at start after a rendering change | The start result's warnings in `sf tasks <id>`. | `sf recreate N`. If one app still crashes: `sf app clear N <package>` |

Only Android 13 has been booted in testing. An Android of another version that does not boot may be hitting something nobody has seen yet; `sf logs N --logcat` is the place to look.

## No GPU, or a black screen

| Symptom | Check | Fix |
| --- | --- | --- |
| You do not know what an Android draws with | `sf gpu ls`<br>`sf info N` | |
| Doctor: `GPUs` warns "no DRM render node found" | `ls /dev/dri/` | Install the GPU's kernel driver. Without one, Androids draw on the CPU. |
| `gpu mode '<mode>' is not available: ...` | `sf gpu ls` lists every choice on this machine and why one is unavailable. | Pick an available one: `sf gpu set N igpu` |
| `render node /dev/dri/renderD... does not exist on this machine` | `sf host gpus` | `sf gpu set N auto --node ""` |
| Doctor: `NVIDIA runtime for Android (Venus)` warns "not built" | `ls <home>/overlays/venus <home>/overlays/venus-relay` | `native/venus/build.sh all --fetch-ndk` (not verified end to end, see [GPU.md](GPU.md)) |
| `NVIDIA Vulkan driver manifest ... not found` | `find /usr/share /etc -name nvidia_icd.json` | `sf config set gpu.nvidia.icd <that path>` |
| Doctor: `NVIDIA driver` fails | `cat /proc/driver/nvidia/version` | Install NVIDIA's own driver; the doctor prints the command for your distribution. |
| `no access to /dev/dri/renderD129 for this user` | `ls -l /dev/dri/` | `sudo usermod -aG render,video "$USER"`, then log out and in |
| `cannot create /run/superfarm/venus/...` | `ls -ld /run/superfarm` | `sf host apply` |
| `relay for <node> exited with code ...` | The log named in the message, under `<home>/logs/` | Fix what the log says, then `sf start N` |
| `gpu mode 'nvidia' is ...` on an Android that is not version 13 | `sf info N` | The NVIDIA runtime is built for Android 13 only: `sf gpu set N igpu` or `sf gpu set N software` |
| Every NVIDIA Android lost its picture at once | Is the relay still running? `pgrep -x virgl_test_serv` | `sf restart <selector of the NVIDIA Androids>` |
| NVIDIA Androids lose their picture when you log out | The relay runs in your user session. | `loginctl enable-linger "$USER"` |
| The screen is black but the Android is `booted` | `sf screenshot N -o shot.png`<br>`sf logs N --logcat` | `sf gpu set N software`, then `sf restart N`, to see whether the GPU mode is the cause |
| CPU use is high | `sf gpu ls` shows `software` in use. Software rendering costs about five times the CPU of a GPU mode. | `sf gpu set all igpu`, then `sf restart all` |

Never restart the relay while NVIDIA Androids run. Each of them holds connections to it and loses its graphics until it is restarted.

## VPN and kill switch

`sf vpn status` shows the tunnel state of each Android. The states below are the ones the farm reports.

| Symptom | Check | Fix |
| --- | --- | --- |
| State `auth_failed` | The VPN server rejected the username or password. | `sf vpn credentials default`, then `sf restart N` |
| `VPN profile ... asks for a username and password, and none is stored` | `sf vpn credentials` lists the stored names. | `sf vpn credentials default` |
| State `tls_error` | The certificate or key in the profile is wrong or expired, or something on the path blocks the handshake. Doctor: `Clock synchronised`. | Download the profile again and import it: `sf vpn import <file>` |
| State `dns_error` | `sf config get network.dns`; the host's connection. | `sf config set network.dns 1.1.1.1,9.9.9.9` |
| State `unreachable` | No server in the profile answered. | Try another profile: `sf set N network.vpn_profile=<name>`, then `sf restart N` |
| State `config_error` | `docker logs sf-net-N` shows the option OpenVPN refused. | Import a clean copy of the profile: `sf vpn import <file>` |
| State `tun_error`, or `/dev/net/tun does not exist on this host` | `ls -l /dev/net/tun` | `sudo modprobe tun`<br>`sf host apply` |
| `pinned VPN profile ... does not exist` | `sf vpn profiles` | `sf vpn import <file>`, or `sf set N network.vpn_profile=default` |
| A directive is missing from an imported profile | The first lines of the stored file name what was removed. Directives that can run programs are always dropped. | None; this is by design. |
| `network.vpn.extra_args: ... is not allowed` | The option could run a program or read files in the sidecar. | `sf config unset network.vpn.extra_args` |
| An Android behind a VPN has no connectivity for up to two minutes | The server went away. It is noticed after `network.vpn.ping_restart_seconds` (120). Traffic is dropped meanwhile; it does not leak. | Wait, or `sf restart N` |
| Kill switch not shown as active on a `vpn` or `gateway` Android | `sf vpn status N`<br>`docker exec sf-net-N /sf/killswitch.sh check` | `sf recreate N` |
| Traffic leaves with the host's address | `sf info N`: is the mode `proxy` or `direct`? Is `network.killswitch` off? | `proxy` mode has no kill switch. Use `sf set N network.mode=vpn`. To switch the kill switch back on: `sf config unset network.killswitch` |
| `gateway '<name>' is not defined` | `sf gateway ls` | `sf gateway add <name> --profile <profile>` |
| `gateway ... still serves N Android(s)` | `sf gateway ls` | Stop its Androids first, or `sf gateway down <name> --force` |
| `docker.subnet_pool ... has no free /24 left` | `sf gateway ls` | `sf gateway rm <unused name>` |
| A VPN sidecar cannot read its profile on a host with SELinux | Doctor: `SELinux / AppArmor` | `sudo chcon -R -t container_file_t <home>/vpn <home>/secrets` |
| An Android cannot reach a device on your LAN | By default private addresses are blocked. | `sf config set network.allow_lan true`, then `sf recreate N` |

WireGuard tunnels have not been connected in testing. If one does not come up, `docker logs sf-net-N` is the only evidence there is.

## ADB

| Symptom | Check | Fix |
| --- | --- | --- |
| You do not know an Android's ADB address | `sf info N` | Android N is on `127.0.0.1:(5555 + N)` by default: `adb connect 127.0.0.1:5558` for Android 3 |
| A start fails because the ADB port is taken | `ss -ltnp 'sport = :5558'` | `sf config set network.adb_port_base 15555`, then `sf recreate all` |
| Doctor: `Ports` warns that ADB ports are inside the kernel's outgoing port range | `cat /proc/sys/net/ipv4/ip_local_port_range` | `sf config set network.adb_port_base 5555` |
| Doctor: `ADB exposure` or `Published ADB ports` warns | `sf config get network.adb_bind` | `sf config set network.adb_bind 127.0.0.1`, then `sf recreate all` |
| You need ADB from another machine | ADB has no authentication. Do not publish it. | On the other machine: `ssh -L 5558:127.0.0.1:5558 <farm host>`, then `adb connect 127.0.0.1:5558` |
| `sf shell` refuses a command | The denylist refuses wipes, power-off and firewall or route changes. | If you mean it: `sf shell N --force -- <command>` |

## Panel and login

| Symptom | Check | Fix |
| --- | --- | --- |
| `sf: the daemon is not running` | `sf service status` | `sf serve`, or `sf service start` |
| The browser cannot connect | `sf open` prints the address. | `sf serve` |
| The daemon exits at start because the port is in use | `ss -ltnp 'sport = :7700'` | `sf serve --port 7701`, or `sf config set server.port 7701` |
| `the token is not valid` (401) | You pasted an old token. | `sf token` prints the current one. |
| `too many failed attempts; try again shortly` (429) | Several wrong tokens came from your address. | Wait a minute and use the token from `sf token`. |
| You are signed out after a daemon restart | Sessions are kept in memory. | Log in again. |
| `this host name is not one the daemon answers to` (403) | You opened the panel under a name the daemon does not know. | Use `http://127.0.0.1:7700`, or `sf config set server.allowed_origins https://farm.example.com` |
| `missing or wrong X-SF-CSRF header` (403) | A script is using the browser's cookie. | Send `Authorization: Bearer <token>` from scripts. |
| `refusing to listen on <address> with server.auth_required off` | | `sf config unset server.auth_required` |
| You want the panel on another machine | Doctor: `Panel exposure` | `ssh -L 7700:127.0.0.1:7700 <farm host>`, then open `http://127.0.0.1:7700` |
| The token may have leaked | | `sf token --rotate` |
| `sf service install` fails or the service does not start | `systemctl --user status superfarm` | `sf serve` in a terminal works without systemd. |

The panel has been tested in Chrome only.

## Live view

| Symptom | Check | Fix |
| --- | --- | --- |
| A thumbnail shows "offline" | `sf ls`: is the Android running? ADB cannot reach it. | `sf start N` |
| A thumbnail shows "booting" for a long time | `sf logs N` | `sf restart N` |
| The live view is slow (a few frames per second) | It is using the screenshot stream. | Install scrcpy for the H.264 view, or raise the rate: `sf config set stream.live_fps 12` |
| The H.264 view is not offered: "scrcpy-server was not found" | `scrcpy --version` | Install scrcpy, or `export SCRCPY_SERVER_PATH=<path to scrcpy-server>` before starting the daemon |
| The H.264 view is not offered: "scrcpy X is untested with this release" | Tested: 3.3.x. Supported: 2.1 up to 3.x. | `sf config set stream.h264 on` |
| A live view answers 503 | `stream.max_viewers` (64) are open. | Close other views, or `sf config set stream.max_viewers 128` |
| Typing text with accents or other scripts fails | That path needs scrcpy on the host. | Install scrcpy. |
| Native windows do not open | Doctor: `Desktop session (windows and sound)` | From a terminal in your desktop session: `sf service install`<br>`systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY`<br>`systemctl --user restart superfarm` |

## Automation

| Symptom | Check | Fix |
| --- | --- | --- |
| A device shows "needs attention" | `sf auto runs t12` shows the device and the reason. | Fix the screen by hand in the panel, then `sf auto resolve t12 4 resume`. Or `sf auto resolve t12 4 skip`, or `abort`. |
| `could not read the screen: ...` | The screen never stops moving (video, animation). | `.venv/bin/python -m pip install -e ".[u2]"`<br>`sf config set automation.hierarchy_reader u2` |
| A trained program stops at its first tap | The app was not on its first screen. | `sf app stop <selector> <package>`, then run it again |
| A step does not find its element | `sf auto run <kind> <name> N --dry-run` finds every element and taps nothing. | Record the task again and retrain: `sf auto train <dataset>` |
| Training left steps out | The warnings in the training task say which and why. | Record more runs of the same task; three or more give a consensus. |
| A flow is refused when saved | | `sf auto flows validate <file>` prints the error with its position. |
| A flow refuses to type a text | Flows type printable ASCII only. | Type other text from the panel's live view, which pastes through the clipboard. |
| A value named like a password is refused | Secret-looking names are never stored. | Pass it for one run: `sf auto run flow <name> N --secret password` |
| A pop-up is not dismissed | A pop-up rule fires only when its guard and its dismiss control are both on screen. | Teach the rule again from the live screen in the panel. |
| The list of runs is empty after a daemon restart | Run status is kept in memory. | None. Programs, flows, recordings and schedules are on disk. |

## Camera and audio

`sf camera status` lists every reason the camera is not ready. The virtual camera picture has not been verified end to end; the microphone path has.

| Symptom | Check | Fix |
| --- | --- | --- |
| `media.camera.enabled is off` | `sf config get media.camera.enabled` | `sf config set media.camera.enabled true`<br>`sf host apply` |
| `the v4l2loopback kernel module is not loaded` | `lsmod \| grep v4l2loopback` | `native/v4l2loopback/build.sh build`<br>`sudo native/v4l2loopback/build.sh install`<br>`sf host apply` |
| Doctor: `Virtual camera module` fails with "unsigned, and Secure Boot is on" | `sf host sign-module` reports the Secure Boot state and the key. | `sf host sign-module -- v4l2loopback`<br>`sf host apply` |
| `N of 32 camera devices do not exist` | `ls /dev/video*` | `sf host apply` |
| `/dev/videoN is not a Super Farm camera ... refusing to write to it` | Another program owns that device number. | `sf config set media.camera.device_base 150`<br>`sf host apply` |
| `no permission for N camera devices` | `id -nG` | `sf host apply`, then log out and in |
| `the camera HAL overlay for Android 13 is missing` | `ls <home>/overlays/camera/13` | `native/camera/extract_hal.sh --image <your Android 13 image> <BlissOS ISO>` (see `native/camera/README.md`) |
| `ffmpeg is not installed` | `ffmpeg -version` | Install the `ffmpeg` package. |
| The camera works until the container is recreated | The installed HAL lives in the container. | Bake the overlay into the image: copy it to `<home>/overlays/android/13/vendor/`, then `sf image build 13` |
| A test tone is silent in a voice app | Apps remove steady tones as noise. | `python3 scripts/camera/make_test_clip.py test.mp4` makes a clip with beeps; add it to the media library and play it. |
| The picture is mirrored | `sf config get media.camera.mirror` | `sf config set media.camera.mirror false` |
| `sf listen N`: `scrcpy is not installed` | `scrcpy --version` | Install scrcpy. |
| `sf listen N`: `Android N is not reachable over ADB` | `sf ls` | `sf start N` |
| `sf listen N` runs but nothing is heard | Doctor: `Desktop session (windows and sound)`. The daemon needs your session's sound server. | `systemctl --user restart superfarm` from a terminal in your desktop session |
| The RTMP ingest is refused on another address | It has no authentication. | Keep `127.0.0.1` and tunnel: `ssh -L 1935:127.0.0.1:1935 <farm host>` |

## AI and MCP

[MCP.md](MCP.md#troubleshooting) and [AI.md](AI.md#troubleshooting) have their own tables. The most common cases:

| Symptom | Check | Fix |
| --- | --- | --- |
| `the MCP SDK is not installed` | | From the checkout: `.venv/bin/python -m pip install -e ".[mcp]"` |
| `MCP access is switched off` | `sf config get ai.mcp.enabled` | `sf config set ai.mcp.enabled true` |
| An assistant's action never runs | `sf approvals ls --state pending` | `sf approvals watch`, and answer Yes or No |
| A request expired before you answered | `sf config get ai.approval_timeout_seconds` | `sf config set ai.approval_timeout_seconds 900` |
| A tool is missing from the client's list | `sf mcp tools` shows what the policy does with each tool. | `sf config set ai.policy ask`, or remove the tool from `ai.deny` |
| The MCP client cannot start the server | `sf mcp config` prints the entry with the paths of this install. | Paste that entry into the client's configuration unchanged. |
| A model profile does not answer | `sf ai models test <profile>` reports each check. | Fix what it reports; a missing key: `sf ai models key <profile>` |
| A Pilot run stops with "no progress" | `sf ai pilot show <session> --steps` | Run it again with more runs: `--runs 5` |

## Performance and capacity

| Symptom | Check | Fix |
| --- | --- | --- |
| `not starting Android N: over capacity (...)` | `sf host capacity` | Stop some Androids, or `sf start N --force`. To be warned only: `sf config set performance.enforce_capacity false` |
| A start warns "over capacity ... Started anyway" | `sf host capacity` | None needed. The estimate is advisory. |
| `N MB of RAM is available; below performance.memory_guard_mb` | `free -m` | `sf stop <selector>`, or lower the guard: `sf config set performance.memory_guard_mb 1000` |
| Androids are killed when memory runs out | Doctor: `Swap / zram` warns "no swap at all". | `sf config set performance.zram.enabled true`<br>`sf host apply` |
| The machine is slow with many Androids | `sf top` | `sf gpu set all igpu`; `sf config set android.fps 15`; then `sf restart all` |
| A bulk start overloads the machine | Booting is the heaviest moment. | `sf config set performance.start_concurrency 2` |
| Containers lose connectivity at random with many Androids | `dmesg \| grep -i 'neighbour table overflow'` | `sf host apply` |
| Doctor: `Docker address space at scale` warns | `docker network ls` | Add larger pools to `/etc/docker/daemon.json`, as the doctor prints, then `sudo systemctl restart docker` |
| Doctor: `Disk space` warns | `docker system df` | `sf backup prune` |

The capacity figures were measured on one machine. [SCALING.md](SCALING.md) explains how to read them on another.

## Backups

| Symptom | Check | Fix |
| --- | --- | --- |
| `Android N has no data yet (volume ... does not exist); start it once first` | | `sf start N`, `sf stop N`, then `sf backup create N` |
| `Android N is running; stop it first` when restoring over it | | `sf stop N`, then `sf backup restore <name> --over N` |
| `bad backup name ...` | `sf backup list` shows the names. | `sf backup restore android-3/20250101-040000` |
| `... does not match its recorded checksums; it is damaged or incomplete` | `sf backup verify <name> --deep` | Restore an older backup of that Android. |
| A restored Android has no network | A copy restored next to its original is created with network mode `none`, so one identity is never online twice. | After deleting or stopping the original: `sf set <new id> network.mode=default`, then `sf restart <new id>`. Or restore with `--keep-network`. |
| A delete is refused because the safety backup failed | `sf backup list`; free disk space. | Fix the cause, or delete without the backup: `sf rm N --force` |
| A farm backup from another machine restored only part of its content | The output lists what was not taken and why. | Only for a backup you made yourself: `sf backup restore farm/<time> --trust`, with `--restore-config` or `--restore-secrets` as needed |
| Backups fill the disk | `sf backup list` | `sf backup prune` keeps the newest `backup.keep` (7) of each Android. |
| `tar failed (exit ...)` | `docker info`; free disk space under the backup folder. | Free space, then run the backup again. |

## Still stuck

1. Run `sf doctor --json > doctor.json` and read every line that is not `ok`.
2. Collect `sf info N --json`, `sf logs N -n 200` and the last lines of `<home>/logs/daemon.log`.
3. Check [VERIFICATION.md](VERIFICATION.md): what you are doing may be on the "not verified" list.
4. Open an issue with those files. Remove tokens, VPN credentials and addresses first.

## Next

- [FAQ.md](FAQ.md): shorter answers to common questions
- [INSTALL.md](INSTALL.md): the install steps in order
- [NETWORKING.md](NETWORKING.md) and [GPU.md](GPU.md): the two areas with the most moving parts
- [SECURITY_MODEL.md](SECURITY_MODEL.md): what is exposed and what is not
- [VERIFICATION.md](VERIFICATION.md): what was tested

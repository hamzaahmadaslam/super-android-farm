# Frequently asked questions

For anyone with a quick question about what Super Farm is, what it needs, and what it does and does not do. Each answer is short and links to the page that has the detail.

## Basics

### What is Super Farm?

It runs many Android instances as Docker containers on one Linux machine and controls them from a web panel, a command line (`sf`) and an HTTP API. See the [README](../README.md) and the [Quickstart](QUICKSTART.md).

### What does it run on?

An x86-64 Linux machine with systemd, a kernel that provides binder, and Docker. The installer stops on any other CPU. ARM machines, Windows and macOS are not supported, because the Android images are x86-64. See [Requirements](INSTALL.md#requirements).

### Which Android versions work?

Images are configured for Android 9 to 15 and a farm can mix versions, but only Android 13 was booted in testing. The virtual camera and the NVIDIA runtime are built for Android 13 only. See [Verification](VERIFICATION.md).

### Do I need root?

Only for host setup: packages, kernel modules, kernel limits, groups and the boot unit. `sf host apply` asks for `sudo` itself. The daemon, the panel and every other command run as your normal user, and the installer refuses to run as root. See [the one sudo step](INSTALL.md#4-the-one-sudo-step).

### Do I need a GPU?

No. The `software` mode draws on the CPU and always works. It costs roughly five times the CPU of a GPU mode on plain UI screens. Intel and AMD GPUs work with Mesa out of the box, and NVIDIA cards need a runtime that you build. See [GPU](GPU.md).

### How many Androids can my machine hold?

Run `sf host capacity`. It estimates from measured figures and says what runs out first. On the test machine (32 threads, 32 GB) the default was about 33, limited by RAM. Budget about 0.9 to 1.1 GB of RAM per Android in use. See [Scaling](SCALING.md).

### Does it supply apps, VPN accounts or accounts on any service?

No. You bring the app files, the VPN profiles and logins, and any accounts. Read [Acceptable use](../ACCEPTABLE_USE.md) before you decide what to run.

### Is there a demo?

Yes, a static copy of the panel with a fake backend. See [the demo notes](DEMO.md).

## Installing and running

### The installer says binder is missing. What now?

Android needs the kernel's binder driver. `sf doctor` prints what to do for your distribution: load `binder_linux`, install a kernel package that has it, or boot a kernel built with binder. See [Binder](INSTALL.md#binder).

### scrcpy is missing or too old. Is that a problem?

Only for native windows, listening to an Android, and the H.264 live view. The panel's screenshot-based live view works without it. Debian 12 and Fedora have no package, and Ubuntu 24.04's is older than the 2.1 that the H.264 view needs. See [scrcpy](INSTALL.md#scrcpy).

### How do I log in to the panel?

Open `http://127.0.0.1:7700` and paste the token that `sf token` prints. The token is equivalent to root on the host, so treat it like a root password. `sf token --rotate` replaces it. See [Log in to the panel](INSTALL.md#9-log-in-to-the-panel).

### Can I reach the panel from another computer?

Keep it on loopback and use an SSH tunnel, or put a TLS reverse proxy in front. `server.host` on another address exposes farm control to the network, and the doctor warns about it. See [Security model](SECURITY_MODEL.md).

### Does stopping the daemon stop my Androids?

No. They are Docker containers and keep running. What stops with the daemon is the panel, running automation and the NVIDIA relay. See [Start the daemon](INSTALL.md#8-start-the-daemon).

### Do Androids start by themselves after a reboot?

Not by default. `performance.restart_policy` is `no`, and nothing is started at boot. To start some, set `performance.autostart` to a selector such as `all` or `tag:always`, or turn on `autostart` for an Android, and keep the daemon running from boot with `loginctl enable-linger "$USER"`. See [Settings](INSTALL.md#settings-farm-defaults-and-per-android-overrides).

### Where are my files, logs and data?

The farm home (`~/.local/share/superfarm` by default; `sf config path` shows it) holds settings, secrets, the app library, backups, automation data and logs. The daemon writes `logs/daemon.log` and an audit log of every change. Each Android's `/data` is a Docker volume named `sf-data-<id>`. `sf logs 3` shows an Android's container log and `sf service logs -f` the daemon's.

### Can I run two farms on one machine?

Yes, with a separate farm home, a different `docker.name_prefix`, and different ports (`server.port`, `network.adb_port_base`). A second home is a second, fully separate farm.

### How do I update or remove it?

Update with `git pull` and `./install.sh`, then restart the service. Uninstalling removes the services and the files host setup wrote, and keeps your data until you delete it. See [Update](INSTALL.md#update) and [Uninstall](INSTALL.md#uninstall).

## Apps, network and media

### An app will not install. Why?

The result line for each Android says why. Common causes are a newer version already installed, a different signing key, storage full, a missing split (add the whole `.xapk`, `.apks` or `.apkm`), or an ARM-only app on an image without ARM translation. See [Apps](APPS.md#install-ensure-and-update).

### How do I give each Android its own IP address?

Use the `vpn` mode, where each Android runs its own tunnel and gets a profile by `network.vpn.selection`. Or attach Androids to a shared gateway, which gives them one address together. See [Networking](NETWORKING.md#per-android-vpn-or-shared-gateway).

### What happens if the VPN drops?

With the kill switch on (the default) nothing leaves outside the tunnel. A dead VPN server is noticed after `network.vpn.ping_restart_seconds` (120 s), and traffic is dropped in the meantime. `proxy` mode has no kill switch. See [The kill switch](NETWORKING.md#the-kill-switch).

### Is ADB exposed to my network?

Not by default. ADB has no authentication, so `network.adb_bind` is `127.0.0.1`. Keep it there and use an SSH tunnel for remote ADB. See [ADB exposure](NETWORKING.md#adb-exposure).

### Why does the virtual camera not work?

It needs pieces you build yourself (a kernel module, signed under Secure Boot, and a camera HAL extracted from a BlissOS image), it supports Android 13 only, and its picture path was not verified end to end. `sf camera status` lists exactly what is missing. See [Media](MEDIA.md#virtual-camera-and-microphone).

### Why does the RTMP ingest have no password?

The ingest cannot tell your OBS from anyone else's, so it listens on loopback only and is protected by the address it listens on. For OBS on another PC, use an SSH tunnel. See [RTMP ingest](MEDIA.md#rtmp-ingest-live-sources).

## Automation and AI

### Does automation work with any app?

Yes, in the sense that no code is specific to an app. Elements are found through Android's view hierarchy. A screen that exposes no elements, such as a custom-drawn view, can only be matched by position in a trained program, and training warns about those steps. See [Automation](AUTOMATION.md#how-elements-are-found).

### What happens when a run meets a screen it does not know?

That Android stops with `needs_attention` and waits for you to resume, skip the step or stop it. The other Androids carry on. See [Runs](AUTOMATION.md#runs).

### Can an AI assistant operate the farm?

Yes, through the MCP server (`sf mcp`) or the built-in Assistant (`sf ask`). Under the default policy every action that changes the farm waits for your Yes or No. See [AI assistants](AI.md) and [MCP server](MCP.md).

### Can an assistant delete my Androids without asking?

Not under the default policy (`ai.policy: ask`): deletion, uninstalling, erasing data, restoring and shell commands are destructive and always wait for you, and `run_shell` is denied outright. A process that holds the API token can call the API directly, so protect the token. See [Security model](SECURITY_MODEL.md).

## Backups and maintenance

### How do I back up and move an Android?

`sf backup create 3` backs up its data, and `sf backup restore android-3/<time>` restores it as a new Android. To move a whole farm, back up every Android and the farm, copy the backup folder across, and restore. See [Backup](BACKUP.md).

### What has been tested?

One machine, Android 13, and parts of the system in containers. [Verification](VERIFICATION.md) lists what was run, with the test behind each line, and what was not.

### How do I report a problem or contribute?

Report security problems as [SECURITY.md](../SECURITY.md) describes. For anything else see [Contributing](../CONTRIBUTING.md) and [Troubleshooting](TROUBLESHOOTING.md).

## Next

- [Quickstart](QUICKSTART.md): create an Android and run something on it.
- [Troubleshooting](TROUBLESHOOTING.md): when something does not work.
- [Documentation index](README.md): every document.

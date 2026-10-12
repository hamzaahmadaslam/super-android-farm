# Changelog

For anyone who wants to know what a version of Super Farm contains. The newest version is first.

Versions follow `MAJOR.MINOR.PATCH`. While the major version is 0, a minor version may change settings, routes or commands.

## 0.1.0

The first version. [docs/VERIFICATION.md](docs/VERIFICATION.md) says which of the items below were run on real hardware and which were not.

### Farm

- Android instances as pairs of Docker containers: a network sidecar and the Android, with one data volume each
- A different Android version per instance (images configured for Android 9 to 15)
- Rendering per Android: CPU, integrated GPU, discrete GPU, by vendor, or one pinned card; NVIDIA through a Venus relay built from `native/venus/`
- Device presets and a stable identity per Android
- An advisory capacity estimate, with optional enforcement
- `sf doctor`: 46 read-only host checks, each with its fix
- Host setup (`sf host apply`) for apt, dnf, pacman and zypper distributions, and an installer (`install.sh`)

### Networking

- Network modes `direct`, `vpn` (OpenVPN or WireGuard per Android), `gateway` (one shared VPN gateway), `proxy` and `none`
- A kill switch for `vpn` and `gateway` mode, loaded before the tunnel starts and verified after
- VPN profile import with sanitising; write-only credentials
- ADB published on loopback by default

### Control

- App library for APK, XAPK, APKS and APKM files; install, ensure, uninstall, launch, stop and clear across a selection; a version matrix
- Files, device settings and shell commands across a selection
- Thumbnails, a live view (screenshot stream, or H.264 relayed from scrcpy-server), and broadcast input
- Native scrcpy windows, and listening to one Android on the host's speakers
- A virtual camera and microphone fed from a file or an RTMP stream

### Automation and AI

- Recording on the live view, consensus training into programs, replay on any selection
- YAML flows, app profiles with pop-up rules and a never-tap list, values and pools, schedules
- A registry of 39 tools (17 read, 17 change, 5 destructive) served to MCP clients (`sf mcp`) and used by a built-in Assistant (`sf ask`)
- A policy for AI assistants and an approvals inbox in the panel and the terminal
- The Pilot: a model operates an app once and the verified runs become a program

### Interfaces

- The `sf` command line, with shell completion
- An HTTP API with token and session authentication, an audit log and WebSocket events
- A web panel served by the daemon, with no build step and no external requests
- The daemon as a systemd user service

### Operations

- Android backups (tar with a checksum manifest), restore as new or over an existing Android, scheduled backups, safety backups before risky operations
- Farm backups of settings and definitions, sealed so that a backup from elsewhere is restored in a safe mode
- A local quality gate (`scripts/check.sh`), live tests against real containers, and an installer test on five distributions

## Next

- [README.md](README.md): what Super Farm is
- [docs/VERIFICATION.md](docs/VERIFICATION.md): what was tested
- [docs/README.md](docs/README.md): every document

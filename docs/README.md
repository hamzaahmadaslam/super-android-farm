# Documentation

For anyone looking for the right page. Every document of the project is listed here with one line.

If you are new, read [INSTALL.md](INSTALL.md), then [QUICKSTART.md](QUICKSTART.md). If something is broken, go to [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Start

| Document | What it covers |
| --- | --- |
| [../README.md](../README.md) | What Super Farm is, its features, its status and a five-step quick start |
| [INSTALL.md](INSTALL.md) | Requirements, the installer, host setup, notes per distribution, update and uninstall |
| [QUICKSTART.md](QUICKSTART.md) | From a running daemon to a first Android with an app, a flow and a backup |
| [DEMO.md](DEMO.md) | The live demo: a static copy of the panel with a fake backend |
| [FAQ.md](FAQ.md) | Short answers to common questions, each linking to the page with the detail |

## Guides

| Document | What it covers |
| --- | --- |
| [GPU.md](GPU.md) | Rendering modes: CPU, integrated GPU, discrete GPU, and NVIDIA through the relay |
| [NETWORKING.md](NETWORKING.md) | Network modes, VPN profiles and credentials, shared gateways, the kill switch |
| [APPS.md](APPS.md) | The app library, and installing, updating and removing apps across a selection |
| [AUTOMATION.md](AUTOMATION.md) | Recording, training and replaying tasks; YAML flows; values, pools and schedules |
| [MEDIA.md](MEDIA.md) | The virtual camera and microphone, listening to one Android, native windows |
| [BACKUP.md](BACKUP.md) | Android and farm backups, verify, restore, schedules, moving a farm |
| [SCALING.md](SCALING.md) | The capacity estimate, what limits it, and the settings that change it |
| [AI.md](AI.md) | Model profiles, the built-in Assistant, the Pilot, and the panel pages for them |
| [MCP.md](MCP.md) | Connecting an MCP client, the policy, the approvals inbox, the tool reference |

## Reference

| Document | What it covers |
| --- | --- |
| [CLI.md](CLI.md) | Every `sf` command with its options and an example; generated from the parser |
| [API.md](API.md) | The HTTP API: authentication, errors, tasks, and every route; the route list is generated |
| [CONFIGURATION.md](CONFIGURATION.md) | Every setting with its type, default and meaning; generated from the code |

## How it works

| Document | What it covers |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The pieces, how a start works end to end, where state lives, the module map |
| [DESIGN_NOTES.md](DESIGN_NOTES.md) | The problems behind the design: what went wrong, why, and what the project does about it |
| [SECURITY_MODEL.md](SECURITY_MODEL.md) | Trust boundaries, every listener, secrets, the kill switch, approvals, audit logs, hardening |
| [VERIFICATION.md](VERIFICATION.md) | What was tested, on what, what was not, and how to run the checks again |

## Operate and fix

| Document | What it covers |
| --- | --- |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Symptom, check and fix for install, boot, GPU, VPN, ADB, panel, automation, media, AI and backups |
| [../AGENTS.md](../AGENTS.md) | A runbook for AI assistants and maintainers: project map, routine commands, safety rules, a decision tree |

## Contribute

| Document | What it covers |
| --- | --- |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Setting up a checkout, the gates and tests, and how to add a setting, route, command or tool |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | What the project asks of a contribution |
| [../CHANGELOG.md](../CHANGELOG.md) | What each version contains |

## Policies and licences

| Document | What it covers |
| --- | --- |
| [../DISCLAIMER.md](../DISCLAIMER.md) | Purpose, your responsibility, what each feature is for, warranty and risks |
| [../ACCEPTABLE_USE.md](../ACCEPTABLE_USE.md) | The intended uses and the uses that are not permitted |
| [../SECURITY.md](../SECURITY.md) | Supported versions and how to report a vulnerability privately |
| [../LICENSE](../LICENSE) | The Apache License 2.0 |
| [../NOTICE](../NOTICE) | The copyright notice and the third-party material the repository contains |

## Native components

These live next to the sources they describe.

| Document | What it covers |
| --- | --- |
| [../native/venus/README.md](../native/venus/README.md) | NVIDIA mode: the relay, the driver and allocator, how to build them, the NVIDIA-specific findings |
| [../native/camera/README.md](../native/camera/README.md) | The camera and microphone overlay for Android 13, extracted from an image you obtain yourself |
| [../native/v4l2loopback/README.md](../native/v4l2loopback/README.md) | The virtual camera kernel module, its patch, DKMS and Secure Boot signing (GPL-2.0-or-later) |

## Next

- New here: [INSTALL.md](INSTALL.md), then [QUICKSTART.md](QUICKSTART.md)
- Deciding whether to trust it: [VERIFICATION.md](VERIFICATION.md) and [SECURITY_MODEL.md](SECURITY_MODEL.md)
- Changing the code: [DEVELOPMENT.md](DEVELOPMENT.md) and [ARCHITECTURE.md](ARCHITECTURE.md)

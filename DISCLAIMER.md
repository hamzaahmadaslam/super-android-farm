# Disclaimer

For everyone who installs, runs or contributes to Super Farm. Read it before you start a farm.

This page says what the software is for, what you are responsible for, how it treats other software, and which risks come with running it. It is not legal advice. If you need to know whether a particular use is lawful where you are, ask a lawyer.

## Purpose

Super Farm is published for these purposes:

- education: learning how Android runs in containers, how GPU rendering, VPN routing and UI automation work
- research, including accessibility and automation research
- app development, QA and compatibility testing across Android versions, screen sizes and device configurations
- operating apps and services that you own or are authorised to test

It is not published for doing harm, and it must not be used to cause damage, loss or disruption to any person, app, service, network or other software.

## Your responsibility

You decide what a farm does, and you answer for it.

- Comply with the law that applies to you and to the systems you connect to.
- Comply with the terms of service of every app, service, network and VPN provider you use. Many services forbid automated access, multiple accounts or emulated devices. Read their terms before you point a farm at them.
- Obtain authorisation before you test anything you do not own. Written permission from the owner is the safe form.
- Keep the data you handle (accounts, credentials, recordings, screenshots, backups) lawful to hold and properly protected.

The authors and contributors do not monitor, control or have access to any installation, and the software reports nothing to them.

## What is not permitted

[ACCEPTABLE_USE.md](ACCEPTABLE_USE.md) lists the uses that are not permitted, among them fraud, spam, fake engagement, evading bans or detection, and unauthorised access. A use on that list is not permitted even when it is technically possible.

## What the features are for

Some features could be turned to misuse. This table says what each one exists for.

| Feature | It exists so that | It does not exist to |
| --- | --- | --- |
| Device profiles and identity settings (model name, `android_id`, locale, time zone, mock location) | an app can be tested across device configurations, and each test instance has a stable identity of its own | impersonate a real device or a real person, or defeat a service's device checks |
| VPN profiles, shared gateways and the kill switch | network behaviour can be tested from other network paths, and a test instance's traffic stays private and never leaves by the wrong route | evade geographic restrictions, legal restrictions or a service's blocks |
| Recording, trained programs, flows and broadcast input | you can test and operate your own apps repeatedly and on many instances at once | automate accounts or activity on services that forbid it |
| App library and bulk install | one build can be rolled out to every test instance | distribute apps you have no right to distribute |
| Virtual camera and microphone | camera and audio features of an app can be tested with a known input | present a recording as a live person where that deceives someone |
| Assistant, Pilot and MCP server | an AI assistant can help you inspect and operate your farm | act without you: under the default policy every action that changes the farm waits for the operator's approval |

The operator can loosen the AI policy (`ai.policy: allow`) and is then responsible for everything an assistant does.

## How it interacts with other software

- Super Farm does not modify, patch, crack or redistribute third-party apps. Apps you install run unmodified.
- The repository ships no third-party binaries. Android images are pulled from their upstream publisher by Docker when you ask for them. The optional camera HAL must be extracted, by you, from an Android image that you obtain yourself; the result stays on your machine.
- The repository does contain source patches for three open-source components (Mesa, virglrenderer and the v4l2loopback kernel module), each under that component's own licence, and a script that adapts local copies of system libraries for the virtual camera. You build these yourself. [NOTICE](NOTICE) lists them.
- Super Farm is not affiliated with, endorsed by or sponsored by Google, Android, the redroid project, NVIDIA, Intel, AMD, Docker, any VPN provider or any app vendor. Product names and trademarks belong to their owners and are used here only to say what the software works with.

## No warranty and limitation of liability

Super Farm is licensed under the Apache License 2.0. The licence governs; nothing on this page changes it.

In the words of the licence, the software is provided on an "AS IS" basis, without warranties or conditions of any kind (section 7), and contributors are not liable for damages arising from its use (section 8). Read the full text in [LICENSE](LICENSE). The one folder under a different licence, `native/v4l2loopback/`, carries its own warranty disclaimer in its `COPYING` file.

## Risks you accept by running it

- Every Android runs as a privileged Docker container. A process that breaks out of Android has root on the host. Run only apps you are prepared to trust that far, on a machine that holds nothing you cannot afford to lose.
- The API token is equivalent to root on the host. The daemon controls Docker, so whoever holds the token controls the machine. Treat it like a root password and keep the panel on `127.0.0.1` unless you have put TLS in front of it.
- ADB has no authentication. Keep `network.adb_bind` on `127.0.0.1`.
- Automation and bulk commands can lose data. A delete, a cleared app, a restore over an existing Android or a shell command sent to `all` cannot be undone. Take a backup first (`sf backup create <selector>`).
- The kill switch has limits. It covers `vpn` and `gateway` mode. `proxy` mode has none. [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) states what it guarantees and what it does not.
- Much of the software has been tested on one machine only. [docs/VERIFICATION.md](docs/VERIFICATION.md) lists what has not been verified.

## Misuse

A disclaimer does not make misuse acceptable: using Super Farm for something this page or [ACCEPTABLE_USE.md](ACCEPTABLE_USE.md) rules out remains your act and your liability.

## Reporting

Report a vulnerability in Super Farm privately, as described in [SECURITY.md](SECURITY.md). If you see the software being used against a service, report it to that service; the authors cannot see or stop what an installation does.

## Next

- [ACCEPTABLE_USE.md](ACCEPTABLE_USE.md): the permitted and not permitted uses
- [SECURITY.md](SECURITY.md): how to report a vulnerability
- [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md): trust boundaries and hardening
- [LICENSE](LICENSE) and [NOTICE](NOTICE)

# Security policy

For anyone who has found, or thinks they have found, a vulnerability in Super Farm.

## Supported versions

| Version | Security fixes |
| --- | --- |
| 0.1.x (the latest release and the `main` branch) | yes |
| anything older | no |

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting on the repository:

1. Open <https://github.com/hamzaahmadaslam/super-android-farm/security/advisories/new>, or go to the repository's **Security** tab and choose **Report a vulnerability**.
2. Describe what an attacker can do, what they need to start with (network access, a browser tab, the API token, a file you would import), and how to reproduce it. A failing test or a short script helps most.
3. Say which version or commit you tested, and your distribution, kernel and Docker version.

Do not open a public issue for a vulnerability, and do not include real credentials, tokens or VPN profiles in a report. Please give the fix time to land before you publish details.

## In scope

- Getting past authentication, CSRF protection, the Host and Origin checks, or the login rate limit of the daemon.
- Reading or writing files outside the folders a request is meant to reach (path traversal in ids, names, uploads, backups, flows).
- Running a command or loading code on the host through a value the API, a VPN profile, a flow, an app file or a backup archive carries.
- Secrets leaving the farm: the API token, VPN credentials or model keys appearing in a log, an API answer, a backup made without `--include-secrets`, or a message sent to a model.
- Traffic leaving outside the tunnel in `vpn` or `gateway` mode with the kill switch on.
- An AI assistant changing the farm through the tools without the approval the policy requires, or wording its own approval request.
- A farm backup from elsewhere deciding what runs on this machine without `--trust`.
- Host setup (`sf host apply`) doing something as root that a farm setting should not be able to cause.

## Out of scope

- Anything that needs the API token. The token is equivalent to root on the host by design.
- An app inside an Android gaining root on the host through the container being privileged. This is a documented property of the design, see [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).
- Deployments that the documentation warns against: the panel on a public address without TLS, ADB published beyond loopback, the RTMP ingest on a network you do not trust.
- Vulnerabilities in software Super Farm uses but does not contain: Android images, Docker, the kernel, Mesa, scrcpy, OpenVPN, WireGuard. Report those to their projects.
- Misuse of the software against other services. See [ACCEPTABLE_USE.md](ACCEPTABLE_USE.md).

## Threat model in short

- The operator is trusted. So is every process running as the operator's user: it can read the token file.
- The API token gives control of Docker and therefore root on the host. Listeners bind to `127.0.0.1` by default.
- Androids are privileged containers. Apps inside them are untrusted, but the container is not a security boundary against them.
- ADB and the RTMP camera ingest have no authentication and stay on loopback by default.
- Text read from a device screen is untrusted input for a model. It is passed as marked data, and the approvals step is what stops an assistant that was talked into something.
- The approvals step guards AI assistants that work through the farm's tools. It does not guard against a process that holds the token and calls the API directly.

[docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) has the full model: every listener, where secrets are stored, what the kill switch guarantees, what the audit logs record, and a hardening checklist.

## Next

- [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md)
- [DISCLAIMER.md](DISCLAIMER.md)
- [ACCEPTABLE_USE.md](ACCEPTABLE_USE.md)

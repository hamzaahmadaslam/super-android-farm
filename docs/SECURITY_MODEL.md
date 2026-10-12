# Security model

For operators who decide where a farm may run and what it may be exposed to, and for reviewers of the code.

This page states who and what Super Farm trusts, every network listener it opens, where secrets are kept, and what the kill switch, the approvals step and the audit logs do and do not cover. Each statement is taken from the code or from a test that ran; [VERIFICATION.md](VERIFICATION.md) says which tests those were. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## The short version

- Whoever holds the API token has root on the host. Treat the token like a root password.
- Everything listens on `127.0.0.1` by default. Leave it there unless you add TLS.
- An Android is a privileged container. It is not a security boundary against a hostile app.
- ADB and the RTMP camera ingest have no authentication.
- The kill switch stops leaks when a tunnel is down. It does not stop root inside an Android.
- The approvals step stops an AI assistant that works through the farm's tools. It does not stop a program that holds the token.

## Trust boundaries

| Party | Trusted? | What it can do | What holds it back |
| --- | --- | --- | --- |
| The operator, and every process running as the operator's user | Yes | Read the token file, call the API, run `docker` | Nothing. The daemon runs as this user. |
| A holder of the API token | Equivalent to root on the host | Everything the API offers: create privileged containers, choose images, run shell commands in Androids, restore backups | Three settings that name a program the daemon runs cannot be changed through the API (see [The API token](#the-api-token)) |
| A browser page on another origin | No | Send requests to `127.0.0.1` | Host header allow-list, Origin check, `SameSite=Strict` cookie, CSRF header |
| An Android container | Privileged; not a boundary | Root inside it can reach host devices and the shared network namespace | Sidecar firewall for the network; nothing for the host |
| An app inside an Android | No | Whatever Android allows it; anything, if it gains root | Android's own sandbox, then the row above |
| Text on a device screen, app names, command output | No | Reach a model's context as text | Marked data blocks, redaction, the approvals step |
| An MCP client or a model | Limited by policy | Call the farm's tools | `ai.policy`, `ai.deny`, the approvals step |
| A VPN profile file | No | Carry directives that run programs | Sanitised at import and again at every start |
| A farm backup made elsewhere | No | Name images, volumes, boot arguments, settings | Restored in a safe mode unless you pass `--trust` |

### The API token

The token is 32 random bytes, created on first run and stored in `<home>/secrets/api-token` with mode 0600 inside a 0700 folder. `sf token` prints it; `sf token --rotate` replaces it.

The daemon controls Docker and starts Androids with `--privileged`, so the token is equivalent to root on the host. The code treats it that way:

- It is compared in constant time and never written to a URL, a log line or an error message.
- WebSockets do not accept it in the query string. A socket authenticates with the session cookie, a bearer header, or a first message `{"auth": "<token>"}`.
- Replacing the token ends every browser session on the next request and closes every open WebSocket within about 5 seconds.
- Wrong tokens are counted per client address (`server.login_attempts_per_minute`, default 10). Further wrong ones from that address are refused for a minute. The right token is always accepted.

Three settings decide which program the daemon runs or which library that program loads: `docker.binary`, `gpu.nvidia.relay_dir` and `gpu.nvidia.icd`. `PATCH /api/config` refuses to change them. They are changed in `config.yaml` or with `sf config set`, by someone who already has a shell on the machine. This narrows what a stolen browser session can do; it does not make the token less than root, because the API can still create privileged containers.

### The daemon and root

The daemon runs as your desktop user and never as root. That user must be able to use Docker, and membership of the `docker` group already equals root on the machine.

Root is used in two places only: `sf host apply` (once, through `sudo`) and `superfarm-host.service` (at every boot). Both run fixed commands with validated values: no value from the farm's YAML is passed to a shell. The boot unit runs Python from the farm user's virtual environment as root and reads that user's `config.yaml`. This grants nothing the `docker` group has not already granted. Neither path has been run for real; see [VERIFICATION.md](VERIFICATION.md).

### Androids and the apps inside them

Each Android is two containers: a network sidecar and the Android itself, which runs with `--privileged` in the sidecar's network namespace. A privileged container can reach host devices. Treat any app that can gain root inside Android as able to gain root on the host, and run a farm on a machine that holds nothing else of value.

What the farm does to contain Androids at the network level:

- The sidecar's firewall drops inbound connections by default. With `network.adb_bind` on loopback, only the host can connect to an Android's ADB port; other containers on the bridge, including other Androids, cannot.
- In `direct` mode, private (RFC 1918), link-local and carrier-grade NAT destinations are blocked unless `network.allow_lan` is on, so an Android cannot reach your LAN or the host's services.
- A data volume is always a named Docker volume. A name that Docker would read as a host path is refused, so an Android is never handed a host folder as `/data`.
- The farm only touches containers that carry its labels (`io.superfarm.farm=<prefix>`). A container with a matching name and no label is left alone and the start fails with a message.

### Device content and AI models

What an Android shows, the names of its apps and the output of commands were written by apps and web pages. A screen can contain a sentence addressed to a model.

- Device text reaches a model only inside a marked block (`<<<DEVICE-CONTENT <nonce>: ...>>>`) with a random marker the text cannot forge.
- The farm never takes tool arguments, approval summaries or decisions from device text.
- The contents of text fields and password fields, and labels whose names match `automation.forbidden_field_words`, are withheld.
- Before any message is sent to a model, the contents of every file under `<home>/secrets` are masked wherever they appear in it.

These measures lower the risk of prompt injection. They do not remove it. The approvals step is what holds if a model is talked into something.

### VPN profiles

A profile is somebody else's file. OpenVPN can run programs named in a profile (`up`, `down`, `route-up`, `plugin`), and WireGuard's `PostUp` does the same through `wg-quick`.

- At import a profile is rewritten to contain only directives from an allow-list of plain settings. Everything else is dropped, and the dropped names are listed in the file's header.
- At every start the sidecar receives a freshly sanitised copy, so a file placed in `<home>/vpn/` by hand gets the same treatment.
- `network.vpn.extra_args` is checked against a list of options that could run a program, load code or write files; those are refused.
- The sidecar image does not contain `wg-quick`.

### Farm backups from elsewhere

A farm backup decides which images run on this machine, with which volumes and boot arguments. `sf backup restore farm/<time>` therefore distinguishes two cases:

- A backup this farm made carries a seal: the archive's checksum signed (HMAC-SHA256) with `<home>/secrets/farm-key`. It is restored in full.
- Any other backup is restored in a safe mode. An Android definition loses an image that is not already on this machine, an adopted data volume, extra boot arguments and properties, and autostart. Saved runtime state, schedules, `config.yaml`, VPN profiles and secrets are not taken. `--trust`, `--restore-config` and `--restore-secrets` opt in to each part.

A backup made before the seal existed, or restored into a new farm home, counts as "elsewhere".

## Network listeners

| Listener | Default bind | Settings | Authentication | Open when |
| --- | --- | --- | --- | --- |
| Panel, HTTP API and WebSockets | `127.0.0.1:7700` | `server.host`, `server.port`, `server.tls_cert`, `server.tls_key` | API token, or session cookie plus CSRF header | the daemon runs |
| ADB of Android N | `127.0.0.1:(5555 + N)` | `network.adb_bind`, `network.adb_port_base` | none | that Android's sidecar runs |
| MCP over HTTP | `127.0.0.1:7711` | `ai.mcp.http_port` (the address is fixed) | API token as bearer header; Host and Origin must be loopback names | `sf mcp --http` runs |
| RTMP camera ingest | `127.0.0.1:1935` | `media.camera.rtmp_bind`, `media.camera.rtmp_port`, `media.camera.rtmp_allow_remote` | none; the stream key is not checked | a live `rtmp` source is playing |
| NVIDIA relay | Unix socket `/run/superfarm/venus/<node>/vtest.sock`, mode 0666 | `gpu.nvidia.socket_dir` | none | an NVIDIA Android has started |
| scrcpy tunnels (native windows, listening, H.264 view) | a free loopback port chosen at start | none | none | that view or window is open |

Notes on the table:

- `sf mcp` without `--http` opens no listener. It speaks over the standard input and output of the client that launched it.
- The relay socket is world-writable because Android processes connect under many user ids. It is bind-mounted into NVIDIA Androids only.
- `server.auth_required: false` is refused when the daemon is bound to an address that is not loopback. The decision is made from the address the listening socket is bound to, captured once at start, so a saved setting, a restored config or `sf serve --host` cannot open an unauthenticated listener on the network.
- A non-loopback `media.camera.rtmp_bind` is refused unless `media.camera.rtmp_allow_remote` is on.
- `sf doctor` warns when the panel or ADB is exposed (checks "Panel exposure", "ADB exposure" and "Published ADB ports").

### What the farm connects to

The farm reports nothing to its authors. It makes these outbound connections:

- Docker pulls the Android base images and the small Alpine base of the sidecar and helper images.
- A VPN sidecar connects to the servers in its profile.
- `sf vpn status --refresh` and the panel's public address check ask `network.vpn.public_ip_url` (default `https://api.ipify.org`) from inside the Android's network namespace.
- The Assistant, the Pilot and automation's optional model call the model servers you configure, and send them tool results, screen elements and, for a vision profile, screenshots.

## Protections of the HTTP API

| Protection | What it does |
| --- | --- |
| Host header allow-list | Requests whose `Host` is not a loopback name, `server.host`, the bound address or a configured origin are refused. This is the defence against DNS rebinding. |
| Origin check | A state-changing request or WebSocket handshake that names a foreign `Origin` is refused in every authentication mode. |
| Session cookie | `HttpOnly`, `SameSite=Strict`, and `Secure` when the daemon serves TLS. A cookie-authenticated request that changes state must also send the session's CSRF value in `X-SF-CSRF`. |
| Session lifetime | Sessions last `server.session_hours` (default 12) and live in memory, so a daemon restart signs every browser out. |
| Response headers | A Content-Security-Policy of `default-src 'self'` with no inline script, `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`. |
| Body limits | JSON bodies are capped at 4 MiB before authentication. Uploads stream to disk under `server.max_upload_mb` and are cut off at the limit. |
| No public schema | `/api/openapi.json` needs authentication and no documentation page is served. `/api/health` and the login route are the only routes that need none. |
| Forwarded headers | They are believed only from addresses in `server.trusted_proxies`, which is empty by default. |

## Where secrets are stored

| Secret | Location | Mode | Readable through the API |
| --- | --- | --- | --- |
| API token | `<home>/secrets/api-token` | 0600 | no |
| VPN username and password | `<home>/secrets/vpn-<name>.auth` | 0600 | no; names only |
| Model API keys | `<home>/secrets/<name>`, named by `api_key_secret` | 0600 | no; only whether one is stored |
| Farm backup seal key | `<home>/secrets/farm-key` | 0600 | no; never put in or taken from a backup |
| VPN profiles (may hold private keys) | `<home>/vpn/` | owner-only | no |

The `secrets` folder is 0700. `config.yaml` is written with mode 0640 and holds no credentials: a model profile names its key file, never the key.

What keeps secrets from spreading:

- The audit log never records request bodies or query strings.
- A farm backup leaves out `secrets/` and `vpn/` unless you pass `--include-secrets`. A backup made with secrets cannot be downloaded through the API.
- Automation refuses to store a value whose name looks like a secret (`automation.forbidden_field_words`). Such values are given when a run starts and live in that run's memory.
- The recorder never stores what was typed into a field.
- App-data backups and value pools are owner-only.

An Android data backup is a copy of `/data`. It contains whatever the apps stored, including their logins. Keep backups as private as the farm home.

## The kill switch

In `vpn` and `gateway` mode with `network.killswitch` on (the default), the sidecar's firewall has this property:

> Nothing leaves through the uplink interface except the VPN transport to the pinned server addresses and the answers of an ADB session.

How it is established:

1. The default-deny rule set for IPv4 and IPv6 is loaded before anything else runs in the sidecar.
2. The VPN server names are resolved while only DNS to the configured resolvers is open. The addresses are pinned in the firewall, in the routing table and in `/etc/hosts`, so a reconnect needs no DNS.
3. DNS on the uplink is closed again.
4. The tunnel starts.

How it is kept:

- A start is not reported ready until the live rules have been read back and verified.
- The sidecar checks its own rules about every 30 seconds and loads them again when they are missing. The Docker health check reports the container as unhealthy while they are missing.
- If the rules cannot be loaded, the sidecar reports an error instead of starting without them.
- IPv6 is default-drop even when IPv6 is disabled by sysctl, so re-enabling it inside the namespace meets a closed firewall.
- `none` mode drops all outbound traffic whatever the kill switch setting says.
- A shared gateway forwards its members only into its tunnel interface, also when the kill switch setting is off.

Verified live: with the VPN server killed, nothing left by the uplink and ADB kept working; a wiped firewall was detected and restored; a control run with the kill switch off did leak, which shows the test can fail.

### Limits of the kill switch

- `proxy` mode has no kill switch. An app that ignores Android's HTTP proxy setting connects directly.
- `network.killswitch: false` turns it off, and traffic then leaves by the uplink whenever the tunnel is down.
- Root inside an Android shares the sidecar's network namespace and can change its firewall. The sidecar restores its rules within about 30 seconds, and the shell denylist refuses firewall and route commands sent through `sf shell`, but neither is a barrier against a hostile root process. The kill switch protects against tunnel failures and ordinary apps.
- A dead VPN server is noticed after `network.vpn.ping_restart_seconds` (default 120). Traffic is dropped in the meantime; it does not leak.
- When every pinned server address fails, DNS to the configured resolvers opens on the uplink for the few seconds of each new lookup of the server names.
- With `network.dns_leak_protection` off, DNS to the configured resolvers stays open on the uplink.
- The gateway network is an ordinary bridge without NAT and without a host address, because Docker's `--internal` networks drop forwarded packets. This is weaker than an internal network.
- With `network.ipv6` on, IPv6 is allowed only inside the tunnel. That rule set is unit-tested and has not been run live.
- WireGuard tunnels have not been connected in testing; only the OpenVPN path has.

## The approvals step

`ai.policy` decides what an AI assistant may do through the farm's tools: `read-only`, `ask` (the default) or `allow`. `ai.deny` (default: `run_shell`) removes tools under every policy.

Under `ask`:

- The daemon makes the decision for every call that changes the farm, and creates the approval request itself.
- The line you approve is written by the daemon from the tool's validated arguments and the Androids the selector matches at that moment. The caller sends a tool name and arguments and cannot word the request.
- The action runs on exactly the Androids listed, even if the selector would match others by the time you answer.
- A request expires after `ai.approval_timeout_seconds` (default 300). Nothing runs.
- "Approve and remember" covers the same tool on the same Androids for the rest of that assistant session. It is kept in memory, is never offered for destructive tools, and ends after 8 hours or at a daemon restart.
- A daemon restart approves nothing: requests still pending are expired at start.
- `ai.ask_for` must contain `destructive`. A setting that removes it is refused.

### What the approvals step does not cover

- A process that holds the API token can call the API directly. The step guards assistants that work through `sf mcp` or the built-in Assistant.
- Read tools run without asking. They include screenshots and screen contents, which are sent to the model server you configured.
- Under `ai.policy: allow`, and for tools named in `ai.auto_approve`, nothing waits.
- A Pilot session that you start is your own action. Its taps are bounded by the Pilot's limits, the allowed packages and the app's never-tap list, and are not approved one by one.
- Automation's optional recovery model (`automation.llm`) proposes actions for a stuck step without an approval. It can only point at elements it was shown, and every tap passes the never-tap list.
- `ai.mcp.elicitation` (off by default) also shows the Yes/No through the MCP client. A client could answer such a question without showing it to you, which is why it is off.
- `ai.mcp.enabled` is a switch for the operator and not a boundary: `sf mcp` needs the API token either way.

## Audit logs

| File | Records | Kept |
| --- | --- | --- |
| `<home>/logs/audit.log` | One JSON line per request from an authenticated caller that changed, or tried to change, something: time, client address, how it authenticated, method, path, status, outcome, duration, and a few facts the route adds (selector, ids, task, archive, package) | owner-only, rotated by `logging.max_mb` and `logging.keep` |
| `<home>/logs/audit-anonymous.log` | Requests nobody authenticated: refused logins, wrong tokens | same, with a size budget of its own |
| `<home>/logs/approvals.jsonl` | Every approval request and every decision | owner-only, the last 500 requests |
| `<home>/logs/daemon.log` | The daemon's own log, including a bounded number of warnings about wrong tokens | rotated by the same settings |

Properties and limits:

- Request bodies and query strings are never written.
- The path is cut at 200 characters.
- Unauthenticated requests go to their own file, so someone who can reach the port cannot push the authenticated history out by flooding it.
- An AI bridge names itself in the `X-SF-Caller` header and that name appears as `caller` on its audit lines. It is a label supplied by the caller, like a User-Agent, and proves nothing.
- `server.audit_log: false` switches the log off. The request that does so is the log's last line.
- Commands that work without the daemon (`sf config set`, `sf token --rotate`, `sf host apply`) are not HTTP requests and do not appear in the audit log.
- The logs are plain files owned by the daemon's user. Anyone with that user's rights can edit them.

## Hardening checklist

1. Run the farm on a machine that holds nothing else you need to protect.
2. Keep `server.host` on `127.0.0.1`. To reach the panel from another machine, forward the port: `ssh -L 7700:127.0.0.1:7700 <farm host>`.
3. If the panel must listen on the network, set `server.tls_cert` and `server.tls_key`, or put a TLS reverse proxy in front and list its address in `server.trusted_proxies`. Firewall the port.
4. Keep `server.auth_required` on.
5. Keep `network.adb_bind` on `127.0.0.1`. After changing it back, run `sf recreate all`.
6. Keep `media.camera.rtmp_bind` on `127.0.0.1` and reach it through an SSH tunnel.
7. Rotate the token when it may have been seen: `sf token --rotate`.
8. Keep `network.killswitch` on and `network.ipv6` off. Do not rely on `proxy` mode when a leak matters.
9. Keep `ai.policy` at `ask`, keep `run_shell` in `ai.deny`, and keep `ai.mcp.elicitation` off.
10. Keep `control.shell_denylist` on.
11. Keep the farm home on a native Linux filesystem, readable by your user only. `sf doctor` fails on filesystems without Unix permissions.
12. Make farm backups without `--include-secrets` unless the archive stays private, and restore backups from elsewhere without `--trust`.
13. Run `sf doctor` after every change to the listeners. It checks the three exposure points.
14. Read `logs/audit.log` and `sf approvals ls` when something looks wrong.

## Next

- [SECURITY.md](../SECURITY.md): reporting a vulnerability
- [NETWORKING.md](NETWORKING.md): network modes, VPN profiles and gateways
- [MCP.md](MCP.md): the policy and the approvals inbox in use
- [AI.md](AI.md): what a model is shown and what it can never do
- [BACKUP.md](BACKUP.md): backups and restores
- [CONFIGURATION.md](CONFIGURATION.md): every setting named on this page
- [ARCHITECTURE.md](ARCHITECTURE.md): how the pieces fit together

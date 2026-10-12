# Networking

For anyone who decides how Androids reach the internet: through the host's connection, a VPN of their own, a shared VPN gateway, an HTTP proxy, or not at all.

Every Android shares the network namespace of one small helper container, the network sidecar (`sf-net-<id>`). The sidecar owns the firewall, the VPN client, the routes and the published ADB port. Android and sidecar are created and removed together. This is why a kill switch can exist at all: the rules are loaded in the sidecar before the Android starts.

## Modes

Set the farm default with `network.mode`, or override it per Android (`sf create --net vpn`, `sf set 3 network.mode=vpn`, or the Network group of the Android's settings in the panel). A mode takes effect when the Android is next restarted.

| Mode | What it does |
| --- | --- |
| `direct` | The Android uses the host's connection through Docker's bridge. Private address ranges (RFC 1918, link-local, `100.64.0.0/10`) are blocked unless `network.allow_lan` is on. This is the default. |
| `vpn` | One tunnel per Android, run by that Android's sidecar. |
| `gateway` | The Android shares a named VPN gateway with other Androids. See [Per-Android VPN or shared gateway](#per-android-vpn-or-shared-gateway). |
| `proxy` | The Android's global HTTP proxy (`http_proxy` in Android's global settings) is set to `network.proxy` (`host:port`) after boot. The sidecar is a direct one, and there is no kill switch. An app that ignores Android's global proxy connects directly (general Android behaviour; not tested here). |
| `none` | No network. Everything outbound is dropped. ADB still works. |

In every mode the sidecar's firewall denies inbound and forwarded traffic by default. ADB is the only inbound port.

## Add VPN profiles and credentials

Super Farm runs OpenVPN. WireGuard profiles are accepted and sanitised, but the WireGuard connect path is not verified (see [Not verified](#not-verified)).

Import profiles:

```bash
sf vpn import ~/vpn/nl-ams.ovpn ~/vpn/de-fra.ovpn
sf vpn profiles
```

In the panel: Network, VPN profiles, choose files, Import. Profiles are stored owner-only in `<home>/vpn/`, because they usually carry private keys. A file ending in `.ovpn` is OpenVPN and `.conf` is WireGuard.

A profile is somebody else's file, so it is treated as untrusted twice, at import and again at every start:

- Only directives known to be inert are kept. Anything that can run a program or load a library (`up`, `down`, `route-up`, `plugin`, `script-security` and similar, and `PostUp` and friends in WireGuard) is dropped. A directive added by a future OpenVPN release is dropped until someone has looked at it.
- File references must be inline (`<ca> ... </ca>`), so a profile is one self-contained file and nothing outside it is opened.
- The list of dropped directives is shown for each profile.
- IPv6 server addresses are refused, because the kill switch pins IPv4 server addresses. Use the server's name or IPv4 address.
- A profile larger than 256 KiB is refused.

If a profile asks for a username and password that are not embedded, store them under a name:

```bash
sf vpn credentials default
printf '%s' "$VPN_PASSWORD" | sf vpn credentials default --username myaccount --password-stdin
```

The password is asked for without echo or read from stdin and is never accepted on the command line, so it stays out of the process list and shell history. It is written to `<home>/secrets/vpn-<name>.auth` with mode 0600. There is no way to read a stored login back, only to replace or delete it (`sf vpn credentials default --delete`). `network.vpn.credentials` (default `default`) says which name profiles use. Credentials are excluded from farm backups unless you ask ([Backup](BACKUP.md)).

Tunnel settings: `network.vpn.provider` (`openvpn`), `ping_seconds` (10), `ping_restart_seconds` (120), `connect_retry_max` (5) and `extra_args`. Arguments in `extra_args` that could run a program, load code, write files or change routing are refused.

## Profile selection

For `vpn` mode, an Android that is not pinned to a profile gets one by `network.vpn.selection`:

| Policy | Behaviour |
| --- | --- |
| `round-robin` (default) | The least-used profile, so Androids are spread evenly. It needs no counter and stays balanced when Androids are deleted. |
| `random` | A random profile. |
| `fixed` | `network.vpn.profile`. It must be set, and must exist. |
| `unique` | Never the same profile for two running Androids. A start fails when profiles run out; import more or stop an Android. |

The choice is remembered. An Android keeps its profile, and so its exit address, across restarts until you change it. In a bulk start, profiles chosen moments earlier by other starts count as taken, so `unique` never hands out one twice.

Pin an Android to a profile: `sf create --vpn nl-ams.ovpn` (implies `--net vpn`) or `sf set 3 network.vpn_profile=nl-ams.ovpn`.

## Per-Android VPN or shared gateway

Per-Android VPN (`vpn` mode): each Android has its own tunnel and can have its own exit address. A failing tunnel affects one Android. The cost is one VPN client process per Android. The per-Android cost was not measured.

Shared gateway (`gateway` mode): one tunnel serves every Android attached to it. They share one exit address, and if the gateway stops, all its members lose connectivity (nothing leaks; see below). It needs Docker 25 or newer, because members join two networks. A gateway holds at most 126 Androids.

A gateway is a container (`sf-gw-<name>`) running the sidecar image as VPN client and NAT. Members sit on a bridge network of its own (`sf-gwnet-<name>`) that has no address on the host and no NAT, so the gateway container is the only router on it. The gateway forwards members into the tunnel and nowhere else, whatever the kill switch setting. Each gateway gets a private /24 from `docker.subnet_pool` (default `10.213.0.0/16`); the gateway is `.2` and members take addresses from the upper half.

Define a gateway for the whole farm or for a group:

```bash
sf gateway add eu --profile nl-ams.ovpn
sf gateway add us --profile us-nyc.ovpn --credentials main
sf gateway up eu
sf gateway ls
```

A group is a gateway plus the Androids you attach to it. One farm can have several gateways:

```bash
sf create -n 10 --gateway eu --tag eu          # ten new Androids behind "eu"
sf set tag:shop network.mode=gateway network.gateway=eu
sf config set network.mode gateway             # make it the farm default
sf config set network.gateway eu
```

Starting an Android that uses a gateway brings the gateway up. `sf gateway down eu` stops it; `--force` stops it while Androids are attached. `sf gateway rm eu` refuses while any Android is set to use that gateway; change their network first. In the panel: Network, Shared gateways, Add gateway.

## The kill switch

`network.killswitch` (default on) makes the sidecar drop everything that would leave outside the tunnel. It can be switched per Android (`sf set 3 network.killswitch=false`).

What it guarantees, when it is on and the mode is `vpn` or `gateway`:

- The default-deny firewall for IPv4 and IPv6 is loaded before anything else runs in the sidecar, so it holds from the first packet, before the VPN connects.
- Names of the VPN servers are resolved once, while only DNS to your configured resolvers is open. The resulting addresses are pinned in the firewall, the routes and `/etc/hosts`, then DNS on the uplink is closed again. Afterwards the only things allowed out of the uplink interface are the VPN transport to those pinned addresses and the answers of an ADB session.
- The sidecar never exits because the tunnel failed. It restarts the tunnel in place behind the firewall that stays up. The Android shares the sidecar's namespace, and a restarted sidecar would leave it attached to a dead one.
- The firewall is checked, not only set. The status shows whether the live rules have the property above, and a missing or wiped rule set is detected and restored.
- IPv6 is dropped by default. Most VPN profiles carry no IPv6, so it would bypass the tunnel.
- With `none` mode, outbound is dropped whatever the kill switch setting says.

Verified live (14 of 14 tests, against a throwaway OpenVPN server that the test created): all traffic went through the tunnel; with the server killed, nothing left by the plain interface and ADB still worked; the tunnel reconnected; a wrong password was reported as an authentication failure; a wiped firewall was detected and restored; the IPv6 default drop held when IPv6 was re-enabled inside the namespace; a shared gateway with two members worked; one Android behind its own VPN and one behind a gateway ran together. A control run with the kill switch off did leak.

What it does not guarantee:

- `proxy` mode has no kill switch.
- With `network.killswitch` off, a `vpn` Android is allowed out of the uplink whenever the tunnel is down. That is what the control run measured.
- A dead VPN server is noticed after `ping_restart_seconds` (120 s by default). Traffic is dropped in the meantime, not leaked, so apps see a stalled connection.
- With `network.allow_lan` on, private address ranges stay reachable on the uplink on purpose.
- The kill switch is about the network path. It does not hide other things an app can learn about the device. Device identity is a separate setting under `device.*`.
- A shell command inside an Android that edits the firewall could open the switch. `control.shell_denylist` (on by default) refuses obvious cases unless forced; it is not a security boundary against someone with a shell on the host.
- IPv6 enabled (`network.ipv6`) and WireGuard are not verified.

## DNS

`network.dns` lists the resolvers Androids use (default `1.1.1.1`, `8.8.8.8`). With a VPN, at least one IPv4 resolver is required, because the sidecar resolves the VPN server names over IPv4 before the tunnel exists. `network.dns_leak_protection` (default on) closes DNS on the uplink after that first resolution, so all later lookups go through the tunnel. A private resolver you configure on purpose stays reachable in `direct` mode even with `allow_lan` off.

## IPv6

`network.ipv6` (default off) leaves IPv6 enabled inside Androids. It is off by default because it would bypass the tunnel of most profiles. When on in `vpn` mode, IPv6 is allowed only inside the tunnel. Only the default drop was verified, not an IPv6-enabled tunnel.

## ADB exposure

ADB has no authentication. Anyone who reaches an Android's ADB port gets a root shell on that Android. `network.adb_bind` (default `127.0.0.1`) decides where ports are published; Android N is on `network.adb_port_base + N` (5555 + N by default). With a loopback bind, the sidecar also refuses everyone but the host on its bridge, so other containers cannot connect. Keep it on loopback. For remote ADB use an SSH tunnel. `sf doctor` warns about a non-loopback bind in the "ADB exposure" row, and about running Androids that publish ADB on all interfaces. A change needs `sf recreate` of the affected Androids.

## Check an Android's network and public address

```bash
sf vpn status                  # running Androids: mode, tunnel state, kill switch
sf vpn status 3 --refresh      # also ask the public address from inside Android 3's network
```

In the panel: Network, Status per Android, then "Check public addresses". An Android's own page has a Network tab.

The public address is read by fetching `network.vpn.public_ip_url` (default `https://api.ipify.org`) from inside the Android's network, so it shows the exit address and the service sees that address. Point the setting at a service you trust if you do not want to use the default. A tunnel in trouble shows a state (`auth_failed`, `tls_error`, `dns_error`, `unreachable`, `config_error`, `tun_error`) with a suggested fix.

## Not verified

- WireGuard. The kernel module was not loaded on the test machine, so the connect path never ran. Profiles import and sanitise; host setup loads the `wireguard` module when a provider is set to it.
- IPv6 enabled (`network.ipv6`).
- Docker older than 29.
- Sidecars under SELinux enforcing: the doctor warns that the sidecars are confined and may be denied their bind-mounted profiles, and prints a `chcon` fix. That was not run.
- The per-Android VPN's resource cost, and more than two Androids behind VPNs at once through Super Farm itself.

## Next

- [Security model](SECURITY_MODEL.md): what the kill switch and ADB bind do and do not protect.
- [Scaling](SCALING.md): the cost of many sidecars and tunnels.
- [Backup](BACKUP.md): VPN credentials stay out of farm backups unless you opt in.
- [Troubleshooting](TROUBLESHOOTING.md): tunnel states and what they mean.
- [Command reference](CLI.md#sf-vpn): `sf vpn` and `sf gateway`.

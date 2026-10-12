"""VPN profiles and credentials.

Profiles live in `<home>/vpn/` (`*.ovpn` for OpenVPN, `*.conf` for WireGuard) and are stored
owner-only, because they routinely carry private keys. Usernames and passwords live in
`<home>/secrets/` and can be written but never read back through this module.

A profile is somebody else's file, so it is treated as untrusted input twice:

  * at import: it is rewritten to contain only directives known to be inert;
  * at every start: the sidecar is handed a freshly sanitised copy, never the stored file,
    so a profile dropped into the folder by hand gets exactly the same treatment.

OpenVPN can run programs named in a profile (`up`, `down`, `route-up`, `plugin`, ...) and
wg-quick does the same for `PostUp` and friends. None of that survives sanitising: the only
script the sidecar ever runs is its own route hook.
"""
from __future__ import annotations

import ipaddress
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from collections.abc import Callable, Collection, Mapping, Sequence

from ..paths import Paths
from ..util.files import atomic_write, inside, safe_filename, write_secret

OPENVPN_EXT = ".ovpn"
WIREGUARD_EXT = ".conf"
MAX_PROFILE_BYTES = 256 * 1024
HEADER = "# sanitised by superfarm"

_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
_HOST = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")
_CRED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_WG_KEY = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")

# Directives that are plain settings: they cannot name a program, a library or a file.
# Anything not listed here is dropped, so a directive added by a future OpenVPN release is
# unsafe until someone has looked at it.
_OVPN_SAFE = frozenset("""
client tls-client pull nobind bind float lport rport port proto proto-force
remote remote-random remote-random-hostname resolv-retry
connect-retry connect-retry-max connect-timeout server-poll-timeout explicit-exit-notify
dev dev-type topology tun-mtu tun-mtu-extra link-mtu mtu-disc mtu-test fragment mssfix
sndbuf rcvbuf txqueuelen fast-io persist-key persist-tun persist-local-ip persist-remote-ip
cipher data-ciphers data-ciphers-fallback ncp-ciphers ncp-disable auth auth-nocache auth-retry
auth-user-pass keysize key-direction key-method
tls-version-min tls-version-max tls-cipher tls-ciphersuites tls-groups tls-cert-profile
tls-timeout tls-exit remote-cert-tls remote-cert-ku remote-cert-eku ns-cert-type
verify-x509-name verify-hash peer-fingerprint
reneg-sec reneg-bytes reneg-pkts hand-window tran-window replay-window mute-replay-warnings
comp-lzo compress allow-compression comp-noadapt
ping ping-exit ping-restart ping-timer-rem keepalive inactive session-timeout
redirect-gateway redirect-private route route-gateway route-metric route-delay route-nopull
route-ipv6 ifconfig ifconfig-ipv6 ifconfig-nowarn tun-ipv6 block-ipv6
allow-pull-fqdn pull-filter dhcp-option block-outside-dns register-dns
ignore-unknown-option verb mute disable-dco allow-recursive-routing
""".split())

# Directives whose argument is a file. Only the inline form (<ca> ... </ca>) is accepted,
# so a profile is one self-contained file and nothing outside it is ever opened.
_OVPN_FILE = frozenset(
    "ca cert key dh pkcs12 tls-auth tls-crypt tls-crypt-v2 extra-certs crl-verify secret".split())
_OVPN_BLOCKS = _OVPN_FILE | {"connection", "auth-user-pass", "peer-fingerprint", "http-proxy-user-pass"}
_OVPN_PROTO = {"udp": "udp", "udp4": "udp", "tcp": "tcp", "tcp4": "tcp", "tcp-client": "tcp", "tcp4-client": "tcp"}

_WG_INTERFACE = {"privatekey": "PrivateKey", "address": "Address", "dns": "DNS", "mtu": "MTU",
                 "listenport": "ListenPort"}
_WG_PEER = {"publickey": "PublicKey", "presharedkey": "PresharedKey", "endpoint": "Endpoint",
            "allowedips": "AllowedIPs", "persistentkeepalive": "PersistentKeepalive"}


class Remote(NamedTuple):
    host: str
    port: int
    proto: str          # udp | tcp

    def __str__(self) -> str:
        return f"{self.host}:{self.port}/{self.proto}"


@dataclass
class Profile:
    """A sanitised profile: the text that may be handed to a VPN client, and what was learnt."""
    provider: str                       # openvpn | wireguard
    text: str
    remotes: list[Remote] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)      # directive names dropped
    needs_credentials: bool = False     # asks for a username/password that is not embedded
    name: str = ""


# --------------------------------------------------------------------------- helpers

def provider_of(name: str) -> str:
    low = name.lower()
    if low.endswith(OPENVPN_EXT):
        return "openvpn"
    if low.endswith(WIREGUARD_EXT):
        return "wireguard"
    raise ValueError(f"{name!r}: a VPN profile must be an OpenVPN file (*{OPENVPN_EXT}) or a WireGuard file (*{WIREGUARD_EXT})")


def _check_host(host: str, where: str) -> str:
    if ":" in host or host.startswith("["):
        # The kill switch pins IPv4 server addresses; an IPv6 server would be unreachable
        # behind it, so say so now instead of failing silently at connect time.
        raise ValueError(f"{where}: IPv6 server address {host!r} is not supported; use the server's name or IPv4 address")
    try:
        ipaddress.IPv4Address(host)
        return host
    except ValueError:
        pass
    if not _HOST.match(host) or host.replace(".", "").isdigit():
        raise ValueError(f"{where}: {host!r} is not a valid server name or IPv4 address")
    return host


def _check_port(value: str, where: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError(f"{where}: {value!r} is not a valid port (1-65535)")
    return int(value)


def _decode(data: bytes | str) -> str:
    if isinstance(data, bytes):
        if len(data) > MAX_PROFILE_BYTES:
            raise ValueError(f"profile is larger than {MAX_PROFILE_BYTES // 1024} KiB; that is not a VPN profile")
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("profile is not a text file (invalid UTF-8)") from None
    if "\x00" in data:
        raise ValueError("profile is not a text file (contains NUL bytes)")
    if len(data) > MAX_PROFILE_BYTES:
        raise ValueError(f"profile is larger than {MAX_PROFILE_BYTES // 1024} KiB; that is not a VPN profile")
    return data.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------- OpenVPN

def sanitise_openvpn(data: bytes | str, *, read_file: Callable[[str], str | None] | None = None) -> Profile:
    """Rewrite an OpenVPN profile so that it can only configure a tunnel.

    read_file(name) may supply the text of a file the profile refers to by a plain relative
    name (`ca ca.crt`); it is then embedded. Without it such a reference is an error, with
    the exact fix in the message. Raises ValueError for anything that cannot be made safe.
    """
    lines = _decode(data).split("\n")
    out: list[str] = []
    removed: list[str] = []
    remotes: list[tuple[str, int | None, str | None]] = []
    protos: list[str] = []
    state = {"port": None, "needs_auth": False, "inline_auth": False}

    def drop(name: str) -> None:
        if name not in removed:
            removed.append(name)

    def directive(raw: str, lineno: int) -> list[str]:
        """One directive line -> the lines to keep for it (possibly none)."""
        words = raw.split()
        name = words[0][2:] if words[0].startswith("--") else words[0]
        args = words[1:]
        where = f"line {lineno}"
        if name in _OVPN_FILE:
            if args and args[0] == "[inline]":
                return [raw]
            if not args:
                raise ValueError(f"{where}: `{name}` has no value")
            body = read_file(args[0]) if read_file and "/" not in args[0] and "\\" not in args[0] else None
            if body is None:
                raise ValueError(
                    f"{where}: `{name} {args[0]}` refers to a separate file. Paste that file's contents into "
                    f"the profile between <{name}> and </{name}> lines, remove this line, and import again.")
            extra = [f"key-direction {args[1]}"] if name == "tls-auth" and len(args) > 1 and args[1] in ("0", "1") else []
            return [*extra, f"<{name}>", *_decode(body).strip("\n").split("\n"), f"</{name}>"]
        if name not in _OVPN_SAFE:
            if name in ("http-proxy", "socks-proxy"):
                raise ValueError(f"{where}: profiles that connect through a proxy (`{name}`) are not supported; "
                                 "the kill switch only lets the VPN server itself through")
            drop(name)
            return []
        if name == "dev" or name == "dev-type":
            if not args or not args[0].startswith("tun"):
                raise ValueError(f"{where}: only tun (routed) tunnels are supported, not `{raw.strip()}`")
            # A fixed or odd device name would escape the firewall's `tun+` match.
            return [f"{name} tun"]
        if name == "proto":
            if not args or args[0] not in _OVPN_PROTO:
                raise ValueError(f"{where}: unsupported transport `{raw.strip()}` (use udp or tcp over IPv4)")
            protos.append(_OVPN_PROTO[args[0]])
            return [raw]
        if name in ("port", "rport"):
            state["port"] = _check_port(args[0] if args else "", where)
            return [raw]
        if name == "remote":
            if not args:
                raise ValueError(f"{where}: `remote` needs a server name")
            host = _check_host(args[0], where)
            port = _check_port(args[1], where) if len(args) > 1 else None
            proto = None
            if len(args) > 2:
                if args[2] not in _OVPN_PROTO:
                    raise ValueError(f"{where}: unsupported transport {args[2]!r} (use udp or tcp over IPv4)")
                proto = _OVPN_PROTO[args[2]]
            remotes.append((host, port, proto))
            return [raw]
        if name == "auth-user-pass":
            # A path here would point at a file on the importing machine; the farm supplies
            # the credentials from its own secret store instead.
            state["needs_auth"] = True
            if args:
                drop("auth-user-pass <file>")
            return ["auth-user-pass"]
        if name == "auth-retry" and args[:1] == ["interact"]:
            drop("auth-retry interact")      # would wait for a keyboard that does not exist
            return []
        return [raw]

    block: str | None = None
    block_lines: list[str] = []
    block_start = 0
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if block is not None:
            if line == f"</{block}>":
                if block == "connection":
                    kept: list[str] = []
                    for off, inner in enumerate(block_lines, 1):
                        s = inner.strip()
                        if s and not s.startswith(("#", ";")):
                            if s.startswith("<"):
                                raise ValueError(f"line {block_start + off}: blocks cannot be nested inside <connection>")
                            kept += directive(s, block_start + off)
                    out += ["<connection>", *kept, "</connection>"]
                elif block in _OVPN_BLOCKS:
                    if block == "auth-user-pass":
                        state["inline_auth"] = True
                    out += [f"<{block}>", *block_lines, f"</{block}>"]
                else:
                    drop(f"<{block}>")
                block = None
            else:
                block_lines.append(raw.rstrip())
            continue
        if not line or line.startswith(("#", ";")):
            if not line.startswith(HEADER):
                out.append(raw.rstrip())
            continue
        m = re.fullmatch(r"<([a-z0-9-]+)>", line)
        if m:
            block, block_lines, block_start = m.group(1), [], lineno
            continue
        if line.startswith("<"):
            raise ValueError(f"line {lineno}: unexpected `{line[:40]}`")
        out += directive(line, lineno)
    if block is not None:
        raise ValueError(f"line {block_start}: <{block}> is never closed")
    if not remotes:
        raise ValueError("the profile names no server: it needs at least one `remote <host> <port>` line")

    default_protos = list(dict.fromkeys(protos)) or ["udp"]
    resolved: list[Remote] = []
    for host, port, proto in remotes:
        # A remote without its own transport may be dialled with any transport the profile
        # mentions (connection blocks), so every one of them is listed.
        for p in ([proto] if proto else default_protos):
            r = Remote(host, port or state["port"] or 1194, p)
            if r not in resolved:
                resolved.append(r)

    while out and not out[-1]:
        out.pop()
    header = HEADER + (": removed " + ", ".join(sorted(removed)) if removed else "")
    return Profile(provider="openvpn", text="\n".join([header, *out]) + "\n", remotes=resolved, removed=sorted(removed),
                   needs_credentials=bool(state["needs_auth"]) and not state["inline_auth"])


# --------------------------------------------------------------------------- WireGuard

def sanitise_wireguard(data: bytes | str) -> Profile:
    """Rewrite a wg-quick style profile, keeping only keys that describe the tunnel.

    PreUp/PostUp/PreDown/PostDown are shell commands, `Table` and `SaveConfig` change how
    routes and files are handled; all are dropped. Raises ValueError when what is left
    cannot describe a tunnel.
    """
    out: list[str] = []
    removed: list[str] = []
    remotes: list[Remote] = []
    section = ""
    seen = {"privatekey": False, "address": False, "peers": 0, "publickey": 0, "endpoint": 0}
    for lineno, raw in enumerate(_decode(data).split("\n"), 1):
        line = raw.strip()
        where = f"line {lineno}"
        if not line or line.startswith(("#", ";")):
            if not line.startswith(HEADER):
                out.append(raw.rstrip())
            continue
        m = re.fullmatch(r"\[\s*([A-Za-z]+)\s*\]", line)
        if m:
            section = m.group(1).lower()
            if section not in ("interface", "peer"):
                raise ValueError(f"{where}: unknown section [{m.group(1)}]")
            if section == "peer":
                seen["peers"] += 1
            out.append("[Interface]" if section == "interface" else "[Peer]")
            continue
        if "=" not in line or not section:
            raise ValueError(f"{where}: expected `Key = value` inside [Interface] or [Peer]")
        key, value = (p.strip() for p in line.split("=", 1))
        table = _WG_INTERFACE if section == "interface" else _WG_PEER
        canon = table.get(key.lower())
        if canon is None:
            if key not in removed:
                removed.append(key)
            continue
        if canon in ("PrivateKey", "PublicKey", "PresharedKey") and not _WG_KEY.match(value):
            raise ValueError(f"{where}: {canon} is not a WireGuard key (44 base64 characters)")
        if canon == "Endpoint":
            host, sep, port = value.rpartition(":")
            if not sep:
                raise ValueError(f"{where}: Endpoint must be <server>:<port>")
            remotes.append(Remote(_check_host(host, where), _check_port(port, where), "udp"))
            seen["endpoint"] += 1
        elif canon in ("Address", "AllowedIPs"):
            for part in value.split(","):
                try:
                    ipaddress.ip_interface(part.strip())
                except ValueError:
                    raise ValueError(f"{where}: {part.strip()!r} in {canon} is not an address") from None
        elif canon in ("MTU", "ListenPort", "PersistentKeepalive"):
            if not value.isdigit() or int(value) > 65535:
                raise ValueError(f"{where}: {canon} must be a number")
        if canon == "PrivateKey":
            seen["privatekey"] = True
        elif canon == "Address":
            seen["address"] = True
        elif canon == "PublicKey":
            seen["publickey"] += 1
        out.append(f"{canon} = {value}")
    if not seen["privatekey"] or not seen["address"]:
        raise ValueError("the profile's [Interface] section needs both PrivateKey and Address")
    if not seen["peers"] or seen["publickey"] != seen["peers"] or seen["endpoint"] != seen["peers"]:
        raise ValueError("every [Peer] section needs a PublicKey and an Endpoint (<server>:<port>)")
    while out and not out[-1]:
        out.pop()
    header = HEADER + (": removed " + ", ".join(sorted(removed)) if removed else "")
    return Profile(provider="wireguard", text="\n".join([header, *out]) + "\n", remotes=remotes, removed=sorted(removed))


def sanitise(name: str, data: bytes | str, *, read_file: Callable[[str], str | None] | None = None) -> Profile:
    kind = provider_of(name)
    profile = sanitise_openvpn(data, read_file=read_file) if kind == "openvpn" else sanitise_wireguard(data)
    profile.name = name
    return profile


# --------------------------------------------------------------------------- the profile folder

def _profile_path(paths: Paths, name: str) -> Path:
    if not name or name != safe_filename(name, "") or name.startswith("."):
        raise ValueError(f"bad VPN profile name {name!r}")
    provider_of(name)
    return inside(paths.vpn, paths.vpn / name)


def _sibling_reader(paths: Paths) -> Callable[[str], str | None]:
    """Lets a hand-copied profile use `ca ca.crt` when ca.crt sits next to it in the vpn folder."""
    def read(name: str) -> str | None:
        if name != safe_filename(name, ""):
            return None
        try:
            p = inside(paths.vpn, paths.vpn / name)
            if not p.is_file() or p.stat().st_size > MAX_PROFILE_BYTES:
                return None
            return p.read_text(encoding="utf-8", errors="strict")
        except (OSError, ValueError):
            return None
    return read


def import_profile(paths: Paths, filename: str, data: bytes) -> str:
    """Validate, sanitise and store an uploaded profile. Returns the stored file name."""
    name = safe_filename(filename, "")
    if not name:
        raise ValueError("the profile needs a file name")
    stem, dot, ext = name.rpartition(".")
    name = f"{stem}{dot}{ext.lower()}"
    profile = sanitise(name, data)
    paths.vpn.mkdir(parents=True, exist_ok=True)
    # Owner-only: profiles carry client keys, WireGuard ones always do.
    atomic_write(_profile_path(paths, name), profile.text, mode=0o600)
    return name


def load_profile(paths: Paths, name: str) -> Profile:
    """Read a stored profile and sanitise it again (it may have been put there by hand)."""
    path = _profile_path(paths, name)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise KeyError(f"no VPN profile named {name!r}; see `sf vpn profiles`") from None
    return sanitise(name, data, read_file=_sibling_reader(paths))


def write_runtime_copy(paths: Paths, name: str, dest: Path) -> Profile:
    """Write the sanitised form of a stored profile to dest (0600): the file a sidecar mounts."""
    profile = load_profile(paths, name)
    current = None
    try:
        current = dest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    if current != profile.text:      # leave the file alone when unchanged: it is bind-mounted
        atomic_write(dest, profile.text, mode=0o600)
    return profile


def delete_profile(paths: Paths, name: str) -> None:
    path = _profile_path(paths, name)
    try:
        path.unlink()
    except FileNotFoundError:
        raise KeyError(f"no VPN profile named {name!r}") from None


def profile_names(paths: Paths, provider: str | None = None) -> list[str]:
    if not paths.vpn.is_dir():
        return []
    out = []
    for p in paths.vpn.iterdir():
        if not p.is_file() or p.name.startswith(".") or p.name != safe_filename(p.name, ""):
            continue
        try:
            kind = provider_of(p.name)
        except ValueError:
            continue
        if provider is None or kind == provider:
            out.append(p.name)
    return sorted(out)


def list_profiles(paths: Paths) -> list[dict]:
    """Every profile with what the panel needs to show. Never includes keys or credentials."""
    out = []
    for name in profile_names(paths):
        path = paths.vpn / name
        st = path.stat()
        row = {"name": name, "provider": provider_of(name), "size": st.st_size, "modified": st.st_mtime,
               "valid": True, "error": "", "servers": [], "removed": [], "needs_credentials": False}
        try:
            profile = load_profile(paths, name)
            row.update(servers=[str(r) for r in profile.remotes], removed=profile.removed,
                       needs_credentials=profile.needs_credentials)
        except (ValueError, KeyError, OSError) as e:
            row.update(valid=False, error=str(e))
        out.append(row)
    return out


# --------------------------------------------------------------------------- credentials

def credentials_path(paths: Paths, name: str) -> Path:
    if not _CRED_NAME.match(name or ""):
        raise ValueError(f"bad credentials name {name!r}: use letters, digits, _ . - (max 64)")
    return paths.secrets / f"vpn-{name}.auth"


def set_credentials(paths: Paths, name: str, username: str, password: str) -> None:
    """Store a username/password pair (owner-only file). There is deliberately no getter."""
    for label, value in (("username", username), ("password", password)):
        if not value:
            raise ValueError(f"the {label} is empty")
        if any(c in value for c in "\n\r\x00"):
            raise ValueError(f"the {label} contains a line break")     # the file format is one line each
    write_secret(credentials_path(paths, name), f"{username}\n{password}\n")


def has_credentials(paths: Paths, name: str) -> bool:
    try:
        return credentials_path(paths, name).stat().st_size > 2
    except (OSError, ValueError):
        return False


def delete_credentials(paths: Paths, name: str) -> bool:
    try:
        credentials_path(paths, name).unlink()
        return True
    except FileNotFoundError:
        return False


# --------------------------------------------------------------------------- choosing a profile

def choose_profile(selection: str, available: Sequence[str], *, fixed: str = "", current: str = "",
                   assigned: Mapping[int, str] | None = None, in_use: Collection[str] = (),
                   rng: random.Random | None = None) -> str:
    """Pick the profile for one Android. Pure: every input is passed in.

    available   candidate profile names
    fixed       network.vpn.profile (used by `fixed`)
    current     this Android's previous choice; kept whenever it is still valid, so an
                Android keeps its exit address across restarts until someone changes it
    assigned    other Androids' choices (instance id -> profile), running or not
    in_use      profiles held by other Androids that are running right now
    """
    names = sorted(set(available))
    if not names:
        raise ValueError("there are no VPN profiles of the configured kind; add one with `sf vpn import <file>`")
    others = list((assigned or {}).values())
    if selection == "fixed":
        if not fixed:
            raise ValueError("network.vpn.selection is `fixed` but network.vpn.profile is empty")
        if fixed not in names:
            raise ValueError(f"network.vpn.profile {fixed!r} is not among the stored profiles ({', '.join(names)})")
        return fixed
    if selection == "unique":
        busy = set(in_use)
        if current in names and current not in busy:
            return current
        free = [n for n in names if n not in busy]
        if not free:
            raise ValueError(f"all {len(names)} VPN profiles are in use by running Androids (selection is `unique`); "
                             "import more profiles or stop an Android")
        # Prefer a profile nobody has claimed, so a stopped Android finds its own one free later.
        unclaimed = [n for n in free if n not in others]
        return (unclaimed or free)[0]
    if current in names:
        return current
    if selection == "random":
        return (rng or random).choice(names)
    if selection == "round-robin":
        # Dealing in turn = always handing out the least-used profile; unlike a counter this
        # needs no extra state and stays balanced when Androids are deleted.
        return min(names, key=lambda n: (others.count(n), n))
    raise ValueError(f"unknown network.vpn.selection {selection!r}")


class Reservations:
    """Profiles chosen moments ago by starts that have no running container yet.

    A bulk start launches several Androids at once; without this, two of them would see the
    same profile as free and `unique` would hand it out twice.
    """
    def __init__(self, ttl: float = 180.0):
        self.ttl = ttl
        self._held: dict[int, tuple[str, float]] = {}

    def hold(self, instance_id: int, profile: str, now: float | None = None) -> None:
        self._held[instance_id] = (profile, time.monotonic() if now is None else now)

    def held_by_others(self, instance_id: int, now: float | None = None) -> set[str]:
        now = time.monotonic() if now is None else now
        self._held = {i: (p, t) for i, (p, t) in self._held.items() if now - t < self.ttl}
        return {p for i, (p, _) in self._held.items() if i != instance_id}

"""Stand-ins shared by the network unit tests: a scripted Docker CLI and sample profiles.

FakeDocker subclasses the real Docker class and replaces only the one method that starts a
process, so the tests exercise the real argument building and output parsing.
"""
from __future__ import annotations

import json

from superfarm.config import Config
from superfarm.core.docker import Docker
from superfarm.core.models import InstanceSpec, Resolved, resolve
from superfarm.util.proc import Result

PREFIX = "sfx"

# Not real key material: the shape is all the parser looks at.
PEM = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUZXhhbXBsZS1ub3QtYS1yZWFsLWNlcnQwCgYIKoZIzj0E\n-----END CERTIFICATE-----"
KEY = "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgZXhhbXBsZQ==\n-----END PRIVATE KEY-----"
WG_PRIVATE = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="
WG_PUBLIC = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="

OVPN = f"""client
dev tun
proto udp
remote vpn1.example.net 1194
remote 198.51.100.20 443 tcp
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
cipher AES-256-GCM
auth-user-pass
verb 3
<ca>
{PEM}
</ca>
<cert>
{PEM}
</cert>
<key>
{KEY}
</key>
"""

WG = f"""[Interface]
PrivateKey = {WG_PRIVATE}
Address = 10.66.0.2/32
DNS = 10.66.0.1
MTU = 1380

[Peer]
PublicKey = {WG_PUBLIC}
Endpoint = wg.example.net:51820
AllowedIPs = 0.0.0.0/0
"""


def make_config(**network) -> Config:
    data: dict = {"docker": {"name_prefix": PREFIX}, "network": {"adb_port_base": 17100, **network}}
    return Config.model_validate(data)


def make_resolved(config: Config, instance_id: int = 1, **network) -> Resolved:
    return resolve(InstanceSpec.model_validate({"id": instance_id, "network": network}), config)


def option_values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


def env_of(args: list[str]) -> dict[str, str]:
    return dict(v.split("=", 1) for v in option_values(args, "-e"))


class FakeDocker(Docker):
    """Records every CLI call and answers from simple in-memory state."""

    def __init__(self, *, version: str = "29.1.3") -> None:
        super().__init__("docker")
        self.version = version
        self.calls: list[list[str]] = []
        self.images: set[str] = set()
        self.containers: dict[str, dict] = {}      # name -> {"labels", "running", "args"}
        self.networks: dict[str, dict] = {}        # name -> inspect-style dict
        self.exec_output: dict[str, Result] = {}   # container -> answer of any `docker exec`
        self.build_fails = False

    def run(self, argv, *, timeout=120, check=True, input=None, binary=False) -> Result:  # noqa: A002
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        res = self._answer(argv)
        if check and not res.ok:
            from superfarm.util.proc import CommandError
            raise CommandError(["docker", *argv], res.code, res.stdout, res.stderr)
        return res

    def called(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    def _answer(self, a: list[str]) -> Result:
        if a[0] == "version":
            return Result(0, self.version + "\n", "")
        if a[:2] == ["image", "inspect"]:
            return Result(0, "1\n", "") if a[-1] in self.images else Result(1, "", "No such image")
        if a[0] == "build":
            if self.build_fails:
                return Result(1, "", "fetch https://dl-cdn.example/alpine: temporary error (try again later)")
            self.images.add(a[a.index("-t") + 1])
            return Result(0, "", "")
        if a[0] == "tag":
            self.images.add(a[2])
            return Result(0, "", "")
        if a[:2] == ["network", "inspect"]:
            if a[2] == "--format" and "Subnet" in a[3]:
                return Result(0, " ".join(n["IPAM"]["Config"][0]["Subnet"] for n in self.networks.values()) + "\n", "")
            if a[2] == "--format":
                return Result(0, "1\n", "") if a[-1] in self.networks else Result(1, "", "not found")
            net = self.networks.get(a[-1])
            return Result(0, json.dumps([net]), "") if net else Result(1, "[]", "not found")
        if a[:2] == ["network", "ls"]:
            return Result(0, "\n".join(self.networks) + "\n", "")
        if a[:2] == ["network", "create"]:
            labels = dict(v.split("=", 1) for v in option_values(a, "--label"))
            subnet = a[a.index("--subnet") + 1] if "--subnet" in a else f"172.{18 + len(self.networks)}.0.0/16"
            bridge = a[a.index("--gateway") + 1] if "--gateway" in a else subnet.rsplit(".", 2)[0] + ".0.1"
            self.networks[a[-1]] = {"Name": a[-1], "Internal": "--internal" in a, "Labels": labels, "Containers": {},
                                    "Options": dict(v.split("=", 1) for v in option_values(a, "-o")),
                                    "IPAM": {"Config": [{"Subnet": subnet, "Gateway": bridge}]}}
            return Result(0, "id\n", "")
        if a[:2] == ["network", "rm"]:
            return Result(0, "", "") if self.networks.pop(a[-1], None) else Result(1, "", "not found")
        if a[:2] == ["container", "inspect"]:
            c = self.containers.get(a[-1])
            if c is None:
                return Result(1, "", "No such container")
            fmt = a[3]
            if "Running" in fmt:
                return Result(0, "true\n" if c["running"] else "false\n", "")
            if "Labels" in fmt:
                return Result(0, c["labels"].get(fmt.split('"')[1], "") + "\n", "")
            return Result(0, "1\n", "")
        if a[0] == "inspect":
            c = self.containers.get(a[-1])
            if c is None:
                return Result(1, "[]", "No such object")
            return Result(0, json.dumps([{"State": {"Status": "running" if c["running"] else "exited", "ExitCode": c.get("exit", 0), "Error": ""}}]), "")
        if a[:2] == ["run", "-d"]:
            name = a[a.index("--name") + 1]
            labels = dict(v.split("=", 1) for v in option_values(a, "--label"))
            self.containers[name] = {"labels": labels, "running": True, "args": a[2:]}
            return Result(0, "cid\n", "")
        if a[0] == "start":
            self.containers[a[-1]]["running"] = True
            return Result(0, "", "")
        if a[0] == "rm":
            for name in a[1:]:
                self.containers.pop(name, None)
            return Result(0, "", "")
        if a[0] == "ps":
            rows = []
            for name, c in self.containers.items():
                if "-a" not in a and not c["running"]:
                    continue
                want = dict(f.split("=", 1)[1].split("=", 1) for f in option_values(a, "--filter"))
                if all(c["labels"].get(k) == v for k, v in want.items()):
                    rows.append(json.dumps({"Names": name, "State": "running" if c["running"] else "exited", "Status": "",
                                            "Image": "", "Labels": ",".join(f"{k}={v}" for k, v in c["labels"].items())}))
            return Result(0, "\n".join(rows) + "\n", "")
        if a[0] == "exec":
            name = next(x for x in a[1:] if not x.startswith("-"))
            c = self.containers.get(name)
            if c is None or not c["running"]:
                return Result(1, "", "container is not running")
            return self.exec_output.get(name, Result(0, "", ""))
        if a[0] == "logs":
            return Result(0, "", "")
        raise AssertionError(f"FakeDocker: unexpected docker {' '.join(a)}")


def report(status: dict[str, str], *, rules4: str = "", rules6: str = "", log: str = "", pins: str = "",
           check: str = "killswitch_check=ok\nheartbeat_age=1", ipv6: str = "") -> str:
    """Text in the format of `healthcheck.sh report`."""
    st = "\n".join(f"{k}={v}" for k, v in status.items())
    return (f"== status\n{st}\n== check\n{check}\n== rules4\n{rules4}\n== rules6\n{rules6}\n"
            f"== ipv6\n{ipv6}\n== pins\n{pins}\n== log\n{log}\n")


RULES_VPN = """-P INPUT DROP
-P FORWARD DROP
-P OUTPUT DROP
-N SF-DNS
-N SF-PIN
-A INPUT -i lo -j ACCEPT
-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A INPUT -s 172.17.0.1/32 -i eth0 -p tcp -m tcp --dport 5555 -j ACCEPT
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -o eth0 -p tcp -m tcp --sport 5555 -m conntrack --ctstate ESTABLISHED -j ACCEPT
-A OUTPUT -o tun+ -j ACCEPT
-A OUTPUT -o eth0 -j SF-PIN
-A OUTPUT -o eth0 -j SF-DNS
-A SF-PIN -d 198.51.100.20/32 -p udp -m udp --dport 1194 -j ACCEPT"""

RULES6_CLOSED = """-P INPUT DROP
-P FORWARD DROP
-P OUTPUT DROP
-A INPUT -i lo -j ACCEPT
-A OUTPUT -o lo -j ACCEPT"""

STATUS_CONNECTED = {"mode": "vpn", "provider": "openvpn", "profile": "a.ovpn", "state": "connected", "ready": "1",
                    "since": "1700000100", "started": "1700000000", "tunnel_dev": "tun0", "tunnel_ip": "10.8.0.6",
                    "killswitch": "active", "uplink": "eth0", "gateway_if": "", "restarts": "0", "last_error": ""}

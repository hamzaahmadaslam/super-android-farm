"""A thin, typed layer over the Docker CLI.

The CLI (not the SDK) is used on purpose: it is what every guide and bug report speaks,
it needs no extra dependency, and each call is one argv list that can be logged and
replayed by hand.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence

from ..util.proc import CommandError, Result, run


class Docker:
    def __init__(self, binary: str = "docker"):
        self.binary = binary

    # ---------------------------------------------------------------- plumbing
    def run(self, argv: Sequence[str], *, timeout: float = 120, check: bool = True,
            input: str | bytes | None = None, binary: bool = False) -> Result:
        return run([self.binary, *argv], timeout=timeout, check=check, input=input, binary=binary)

    def available(self) -> tuple[bool, str]:
        try:
            r = self.run(["version", "--format", "{{.Server.Version}}"], timeout=15, check=False)
        except CommandError as e:
            return False, str(e)
        return (True, r.out) if r.ok else (False, (r.stderr or r.stdout).strip()[:300])

    # -------------------------------------------------------------- containers
    def ps(self, *, all: bool = True, labels: Mapping[str, str] | None = None, name_prefix: str = "") -> list[dict]:
        """Containers as dicts: name, state (running/exited/...), status text, image, labels."""
        argv = ["ps", "--no-trunc", "--format", "{{json .}}"]
        if all:
            argv.append("-a")
        for k, v in (labels or {}).items():
            argv += ["--filter", f"label={k}={v}" if v else f"label={k}"]
        out = []
        for line in self.run(argv, timeout=30).stdout.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            name = row.get("Names", "")
            if name_prefix and not name.startswith(name_prefix):
                continue
            labels_d = dict(p.split("=", 1) for p in row.get("Labels", "").split(",") if "=" in p)
            out.append({"name": name, "state": row.get("State", ""), "status": row.get("Status", ""),
                        "image": row.get("Image", ""), "labels": labels_d})
        return out

    def inspect(self, name: str) -> dict | None:
        r = self.run(["inspect", name], timeout=30, check=False)
        if not r.ok:
            return None
        data = json.loads(r.stdout or "[]")
        return data[0] if data else None

    def exists(self, name: str) -> bool:
        return self.run(["container", "inspect", "--format", "1", name], timeout=30, check=False).ok

    def running(self, name: str) -> bool:
        r = self.run(["container", "inspect", "--format", "{{.State.Running}}", name], timeout=30, check=False)
        return r.ok and r.out == "true"

    def pid(self, name: str) -> int:
        r = self.run(["container", "inspect", "--format", "{{.State.Pid}}", name], timeout=30, check=False)
        return int(r.out) if r.ok and r.out.isdigit() else 0

    def label(self, name: str, key: str) -> str:
        r = self.run(["container", "inspect", "--format", f'{{{{index .Config.Labels "{key}"}}}}', name],
                     timeout=30, check=False)
        return r.out if r.ok else ""

    def create_and_start(self, run_args: Sequence[str], *, timeout: float = 180) -> str:
        """`docker run -d ...`; run_args is everything after `run`. Returns the container id."""
        return self.run(["run", "-d", *run_args], timeout=timeout).out

    def start(self, *names: str, timeout: float = 120) -> None:
        self.run(["start", *names], timeout=timeout)

    def stop(self, *names: str, grace: int = 10, check: bool = False) -> None:
        if names:
            self.run(["stop", "-t", str(grace), *names], timeout=grace * len(names) + 60, check=check)

    def restart(self, *names: str, grace: int = 10) -> None:
        self.run(["restart", "-t", str(grace), *names], timeout=grace * len(names) + 120)

    def rm(self, *names: str, force: bool = True) -> None:
        if names:
            self.run(["rm", *(["-f"] if force else []), *names], timeout=120, check=False)

    def exec(self, name: str, argv: Sequence[str], *, timeout: float = 60, check: bool = True,
             input: str | bytes | None = None, user: str | None = None, binary: bool = False) -> Result:
        head = ["exec", *(["-i"] if input is not None else []), *(["-u", user] if user else []), name]
        return self.run([*head, *argv], timeout=timeout, check=check, input=input, binary=binary)

    def cp(self, src: str, dst: str, *, timeout: float = 300) -> None:
        self.run(["cp", src, dst], timeout=timeout)

    def logs(self, name: str, tail: int = 100) -> str:
        r = self.run(["logs", "--tail", str(int(tail)), name], timeout=30, check=False)
        return (r.stdout + r.stderr) if r.ok else ""

    def stats(self, names: Iterable[str]) -> dict[str, dict]:
        """One sample per running container: cpu (% of one core), mem_mb, mem_limit_mb, pids."""
        names = list(names)
        if not names:
            return {}
        r = self.run(["stats", "--no-stream", "--format", "{{json .}}", *names], timeout=60, check=False)
        out: dict[str, dict] = {}
        for line in r.stdout.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            used, _, limit = row.get("MemUsage", "").partition("/")
            out[row.get("Name", "")] = {
                "cpu": _num(row.get("CPUPerc", "0").rstrip("%")),
                "mem_mb": _to_mb(used), "mem_limit_mb": _to_mb(limit), "pids": int(_num(row.get("PIDs", "0"))),
            }
        return out

    # ------------------------------------------------- volumes, images, networks
    def volume_exists(self, name: str) -> bool:
        return self.run(["volume", "inspect", "--format", "1", name], timeout=30, check=False).ok

    def volume_create(self, name: str, labels: Mapping[str, str] | None = None) -> None:
        argv = ["volume", "create"]
        for k, v in (labels or {}).items():
            argv += ["--label", f"{k}={v}"]
        self.run([*argv, name], timeout=60)

    def volume_rm(self, name: str) -> bool:
        return self.run(["volume", "rm", name], timeout=120, check=False).ok

    def volumes(self, name_prefix: str = "") -> list[str]:
        out = self.run(["volume", "ls", "-q"], timeout=30).stdout.split()
        return [v for v in out if v.startswith(name_prefix)]

    def image_exists(self, ref: str) -> bool:
        return self.run(["image", "inspect", "--format", "1", ref], timeout=30, check=False).ok

    def image_id(self, ref: str) -> str:
        r = self.run(["image", "inspect", "--format", "{{.Id}}", ref], timeout=30, check=False)
        return r.out if r.ok else ""

    def pull(self, ref: str, *, timeout: float = 3600) -> None:
        self.run(["pull", ref], timeout=timeout)

    def build(self, tag: str, context: str, *, dockerfile: str | None = None,
              build_args: Mapping[str, str] | None = None, timeout: float = 3600) -> None:
        argv = ["build", "-t", tag]
        if dockerfile:
            argv += ["-f", dockerfile]
        for k, v in (build_args or {}).items():
            argv += ["--build-arg", f"{k}={v}"]
        self.run([*argv, context], timeout=timeout)

    def network_exists(self, name: str) -> bool:
        return self.run(["network", "inspect", "--format", "1", name], timeout=30, check=False).ok

    def network_create(self, name: str, *, subnet: str = "", labels: Mapping[str, str] | None = None,
                       internal: bool = False) -> None:
        argv = ["network", "create", "--driver", "bridge"]
        if subnet:
            argv += ["--subnet", subnet]
        if internal:
            argv.append("--internal")
        for k, v in (labels or {}).items():
            argv += ["--label", f"{k}={v}"]
        self.run([*argv, name], timeout=60)

    def network_rm(self, name: str) -> bool:
        return self.run(["network", "rm", name], timeout=60, check=False).ok

    def network_subnets(self) -> list[str]:
        """Subnets of every Docker network, to avoid handing out one that is in use."""
        ids = self.run(["network", "ls", "-q"], timeout=30).stdout.split()
        if not ids:
            return []
        r = self.run(["network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *ids],
                     timeout=30, check=False)
        return r.stdout.split()


def _num(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _to_mb(text: str) -> float:
    text = text.strip()
    for unit, factor in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("GB", 953.67), ("MB", 0.9537), ("kB", 0.00095), ("B", 1 / 1048576)):
        if text.endswith(unit):
            return round(_num(text[: -len(unit)]) * factor, 1)
    return 0.0

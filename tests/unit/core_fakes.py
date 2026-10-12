"""In-memory stand-ins for Docker and ADB, shared by the core unit tests.

They model just enough behaviour for the instance lifecycle to be exercised end to end
without a daemon: containers with labels and a running flag, labelled volumes, images.
Every call is recorded in `calls` so tests can assert on order.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from superfarm.config import Config
from superfarm.core.instances import InstanceManager
from superfarm.core.planner import Core, CpuTopology
from superfarm.core.store import Store
from superfarm.core.tasks import TaskManager
from superfarm.paths import Paths
from superfarm.util.proc import CommandError, Result

PREFIX = "sfx"


def option(args: list[str], flag: str) -> str | None:
    """Value following `flag` in an argv list."""
    return args[args.index(flag) + 1] if flag in args else None


def options(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


def labels_of(args: list[str]) -> dict[str, str]:
    return dict(v.split("=", 1) for v in options(args, "--label"))


class FakeDocker:
    # Not the real CLI: code that starts a child process itself (the backup helper) must fail
    # here instead of reaching the Docker daemon of the machine the tests run on.
    binary = "docker-is-not-used-in-unit-tests"

    def __init__(self) -> None:
        self.containers: dict[str, dict] = {}
        self.volume_labels: dict[str, dict[str, str]] = {}
        self.images: dict[str, dict] = {"superfarm/net": {"id": "sha256:net", "labels": {}},
                                        "alpine:latest": {"id": "sha256:alpine", "labels": {}}}
        self.calls: list[tuple] = []
        self.helper_runs: list[list[str]] = []
        self.helper_output = "cleared"
        self.fail_run_of: set[str] = set()          # container names whose `docker run` exits at once
        self.pull_fails = False
        self.build_fails = False

    # ------------------------------------------------------------------ plumbing
    def run(self, argv, *, timeout=120, check=True, input=None, binary=False) -> Result:
        argv = [str(a) for a in argv]
        self.calls.append(("run", *argv))
        if argv[:3] == ["container", "inspect", "--format"]:
            fmt, name = argv[3], argv[4]
            c = self.containers.get(name)
            if c is None:
                return Result(1, "", "no such container")
            if "StartedAt" in fmt:
                return Result(0, "2024-01-01T00:00:00.000000000Z\n", "")
            key = fmt.split('"')[1]
            return Result(0, c["labels"].get(key, "") + "\n", "")
        if argv[:3] == ["volume", "inspect", "--format"]:
            labels = self.volume_labels.get(argv[4])
            if labels is None:
                return Result(1, "", "no such volume")
            return Result(0, f"{labels.get('io.superfarm.farm', '')}/{labels.get('io.superfarm.instance', '')}\n", "")
        if argv[:3] == ["image", "inspect", "--format"]:
            image = self.images.get(argv[4])
            return Result(0, json.dumps(image["labels"]), "") if image else Result(1, "", "no such image")
        if argv[:2] == ["run", "--rm"]:
            self.helper_runs.append(argv)
            return Result(0, self.helper_output + "\n", "")
        raise AssertionError(f"FakeDocker.run: unexpected {argv}")

    def available(self):
        return True, "fake"

    # ---------------------------------------------------------------- containers
    def ps(self, *, all=True, labels=None, name_prefix=""):
        self.calls.append(("ps",))
        out = []
        for name, c in self.containers.items():
            if labels and any(c["labels"].get(k) != v for k, v in labels.items()):
                continue
            if not all and not c["running"]:
                continue
            out.append({"name": name, "state": "running" if c["running"] else c.get("state", "exited"),
                        "status": "Up" if c["running"] else "Exited", "image": c["image"], "labels": dict(c["labels"])})
        return out

    def exists(self, name):
        return name in self.containers

    def running(self, name):
        return bool(self.containers.get(name, {}).get("running"))

    def label(self, name, key):
        return self.containers.get(name, {}).get("labels", {}).get(key, "")

    def create_and_start(self, run_args, *, timeout=180):
        args = [str(a) for a in run_args]
        name = option(args, "--name")
        self.calls.append(("create", name))
        if name in self.containers:
            raise CommandError(["docker", "run"], 125, "", f"name {name} is already in use")
        self.containers[name] = {"running": name not in self.fail_run_of, "labels": labels_of(args), "args": args,
                                 "image": "", "state": "exited"}
        return "id-" + name

    def start(self, *names, timeout=120):
        for n in names:
            self.calls.append(("start", n))
            self.containers[n]["running"] = True

    def stop(self, *names, grace=10, check=False):
        for n in names:
            self.calls.append(("stop", n))
            if n in self.containers:
                self.containers[n]["running"] = False

    def rm(self, *names, force=True):
        for n in names:
            self.calls.append(("rm", n))
            self.containers.pop(n, None)

    def exec(self, name, argv, *, timeout=60, check=True, input=None, user=None, binary=False):
        self.calls.append(("exec", name, *argv))
        return Result(0, "", "")

    def logs(self, name, tail=100):
        return "init: cannot find binder\n"

    def stats(self, names):
        return {n: {"cpu": 10.0, "mem_mb": 900.0, "mem_limit_mb": 1536.0, "pids": 80} for n in names}

    # ------------------------------------------------------------------- volumes
    def volume_exists(self, name):
        return name in self.volume_labels

    def volume_create(self, name, labels=None):
        self.calls.append(("volume_create", name))
        self.volume_labels[name] = dict(labels or {})

    def volume_rm(self, name):
        self.calls.append(("volume_rm", name))
        return self.volume_labels.pop(name, None) is not None

    def volumes(self, name_prefix=""):
        return [v for v in self.volume_labels if v.startswith(name_prefix)]

    # -------------------------------------------------------------------- images
    def image_exists(self, ref):
        return ref in self.images

    def image_id(self, ref):
        return self.images.get(ref, {}).get("id", "")

    def pull(self, ref, *, timeout=3600):
        self.calls.append(("pull", ref))
        if self.pull_fails:
            raise CommandError(["docker", "pull", ref], 1, "", "pull access denied")
        self.images[ref] = {"id": f"sha256:{ref}", "labels": {}}

    def build(self, tag, context, *, dockerfile=None, build_args=None, timeout=3600):
        self.calls.append(("build", tag, dict(build_args or {})))
        if self.build_fails:
            raise CommandError(["docker", "build"], 1, "", "network unreachable")
        args = dict(build_args or {})
        self.images[tag] = {"id": f"sha256:built-{tag}-{len(self.calls)}",
                            "labels": {"io.superfarm.overlay": args.get("OVERLAY_HASH", "none"),
                                       "io.superfarm.base-id": args.get("BASE_ID", "")}}

    def names_called(self, kind: str) -> list[str]:
        return [c[1] for c in self.calls if c[0] == kind]


class FakeAdb:
    """One device. `registry` keeps devices by serial so settings survive across Adb objects."""
    registry: dict[str, FakeAdb] = {}

    def __init__(self, serial: str, binary: str | None = None):
        self.serial = serial
        known = FakeAdb.registry.setdefault(serial, self)
        self.dev = known.__dict__.setdefault("dev", {
            "props": {"ro.product.locale": "en-US", "persist.sys.timezone": "GMT"},
            "settings": {}, "files": {}, "shell": [], "boots": True, "connects": True,
            "enabled": {"com.android.egg", "com.android.bluetooth", "com.android.deskclock", "com.android.systemui",
                        "com.example.app", "com.android.calendar"},
        })

    def connect(self, attempts=5, delay=1.0):
        return self.dev["connects"]

    def disconnect(self):
        self.dev["shell"].append("<disconnect>")

    def state(self):
        return "device" if self.dev["connects"] else "offline"

    def booted(self):
        return self.dev["boots"]

    def getprop(self, name):
        return self.dev["props"].get(name, "")

    def setprop(self, name, value):
        self.dev["props"][name] = value

    def setting_get(self, namespace, key):
        return self.dev["settings"].get(f"{namespace}/{key}", "null")

    def setting_put(self, namespace, key, value):
        self.dev["settings"][f"{namespace}/{key}"] = value

    def shell(self, command, *, timeout=30, check=True):
        dev = self.dev
        dev["shell"].append(command)
        if isinstance(command, str):
            if command.startswith("pm list packages -e"):
                return "\n".join(f"package:{p}" for p in sorted(dev["enabled"]))
            return ""
        if command[:1] == ["cat"]:
            return dev["files"].get(command[1], "")
        if command[:2] == ["rm", "-f"]:
            dev["files"].pop(command[2], None)
            return ""
        if command[:2] == ["sh", "-c"]:
            script, args = command[2], command[4:]
            if "disable-user" in script:
                dev["enabled"] -= set(args)
            elif "pm enable" in script:
                dev["enabled"] |= set(args)
            elif "printf" in script:
                dev["files"][script.rsplit("> ", 1)[1]] = "\n".join(args)
            return ""
        if command[:3] == ["cmd", "alarm", "set-timezone"]:
            dev["props"]["persist.sys.timezone"] = command[3]
        return ""


@dataclass
class FakeGpu:
    mode: str = "software"
    node: str = ""
    run_args: list[str] = field(default_factory=list)
    boot_args: list[str] = field(default_factory=lambda: ["androidboot.redroid_gpu_mode=guest"])
    reason: str = "test"


def uniform_topology(cores: int = 4, threads: int = 2) -> CpuTopology:
    return CpuTopology(cores=tuple(Core(id=i, cpus=tuple(range(i * threads, (i + 1) * threads))) for i in range(cores)))


def fake_sidecar_args(r, config, paths, store, docker) -> list[str]:
    labels = [x for k, v in r.names.labels("net").items() for x in ("--label", f"{k}={v}")]
    return ["--name", r.names.net, *labels, "-p", f"{r.adb_bind}:{r.adb_port}:5555", "superfarm/net", r.net_mode]


class Farm:
    """A manager wired to fakes, plus handles on everything a test wants to look at."""

    def __init__(self, tmp_path, config: dict | None = None):
        FakeAdb.registry = {}
        base = {"docker": {"name_prefix": PREFIX}, "network": {"adb_port_base": 17000},
                "performance": {"start_stagger_seconds": 0},
                # Off unless a test is about it: a safety backup runs a helper container.
                "backup": {"auto_before_risky": False},
                "android": {"images": {"13": {"base": "example/android:13", "tag": "local/android:13"}}}}
        self.config = Config.model_validate(_merge(base, config or {}))
        self.paths = Paths(tmp_path / "home").ensure()
        self.docker = FakeDocker()
        self.docker.images["local/android:13"] = {"id": "sha256:android13", "labels": {}}
        self.store = Store(self.paths)
        self.tasks = TaskManager(workers=4)
        self.mgr = InstanceManager(self.config, self.paths, self.docker, self.store, self.tasks)
        self.gpu = FakeGpu()
        self.net_ready = {"ready": True, "state": "connected", "detail": ""}
        self.mem_mb = 16000
        self.capacity_max = 20                     # what the fake estimate says this machine holds
        self.events: list[str] = []
        b = self.mgr.backends
        b.gpu_choose = self._choose
        b.capacity_estimate = lambda config, paths, running: {
            "max_instances": self.capacity_max, "binding": "ram", "advice": ["add RAM"], "running_seen": running}
        b.sidecar_run_args = fake_sidecar_args
        b.net_wait_ready = lambda docker, r, timeout=90: (self.events.append("net-wait"), self.net_ready)[1]
        b.net_status = lambda docker, r: {"mode": r.net_mode, "state": "connected", "public_ip": "198.51.100.7"}
        b.adb = FakeAdb
        b.mem_available_mb = lambda: self.mem_mb
        b.cpu_topology = uniform_topology
        b.sleep = lambda seconds: None
        self.mgr.lock_wait = 0.2

    def _choose(self, r, config, paths, *, counts=None):
        self.events.append(f"gpu:{counts}")
        return self.gpu

    def device(self, id: int) -> dict:
        return FakeAdb(f"127.0.0.1:{17000 + id}").dev

    def names(self, id: int):
        return self.mgr.names(id)

    def close(self) -> None:
        self.tasks.shutdown()


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out

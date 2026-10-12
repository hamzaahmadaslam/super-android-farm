"""Androids and the farm at a glance: ls, create, start/stop, rm, set, info, logs, gpu, image, tasks, top, open."""
from __future__ import annotations

import argparse
import time
from typing import Any, get_args

from ...config import GpuMode, NetMode
from ...core.models import InstanceSpec
from .. import values
from ..client import CliError, base_url
from ..common import EXIT_OK, EXIT_USAGE, Ctx, confirm, each, follow, one_id, report
from ..render import duration, when

GPU_MODES = tuple(get_args(GpuMode))
NET_MODES = tuple(get_args(NetMode))
READ_ONLY_KEYS = ("id", "created")


# ------------------------------------------------------------------ ls

def _net(row: dict) -> str:
    net = row.get("net") or {}
    extra = net.get("profile") or net.get("gateway") or net.get("proxy") or ""
    return f"{net.get('mode', '-')}:{extra}" if extra else str(net.get("mode", "-"))


def _gpu(row: dict) -> str:
    gpu = row.get("gpu") or {}
    return str(gpu.get("mode") or gpu.get("requested") or "-")


def _instance_rows(rows: list[dict]) -> list[tuple]:
    return [(r.get("id"), r.get("name"), r.get("state"), r.get("android_version", "-"), _gpu(r), _net(r),
             r.get("serial"), ",".join(r.get("tags") or []) or "-") for r in rows]


LS_HEADERS = ("ID", "NAME", "STATE", "ANDROID", "GPU", "NET", "ADB", "TAGS")


def ls(ctx: Ctx) -> int:
    def show() -> None:
        response = ctx.api.get("/api/instances")
        if ctx.out.json_mode:
            ctx.out.json(response)
            return
        ctx.out.table(LS_HEADERS, _instance_rows(response.get("instances") or []), right=(0,), state_columns=(2,),
                      empty="No Androids yet. Create some with: sf create -n 3")

    if not ctx.args.watch:
        show()
        return EXIT_OK
    try:
        while True:
            if ctx.out.tty:
                ctx.out.stdout.write("\033[H\033[J")       # home + clear: redraw in place
            show()
            ctx.out.stdout.flush()
            ctx.sleep(ctx.args.interval)
    except KeyboardInterrupt:
        return EXIT_OK      # leaving a watch with Ctrl-C is the normal way out


# ------------------------------------------------------------------ create

def create_template(args: argparse.Namespace) -> dict:
    """The InstanceSpec fields `sf create` was given, as the API's `template`."""
    template: dict[str, Any] = {}
    network: dict[str, Any] = {}
    resources: dict[str, Any] = {}
    for option, key in (("android", "android_version"), ("gpu", "gpu_mode"), ("image", "image"),
                        ("data_volume", "data_volume"), ("width", "width"), ("height", "height"),
                        ("dpi", "dpi"), ("fps", "fps")):
        if getattr(args, option) is not None:
            template[key] = getattr(args, option)
    if args.tag:
        template["tags"] = list(args.tag)
    if args.net:
        network["mode"] = args.net
    if args.vpn:
        # Naming a profile or a gateway says which mode is meant.
        network["vpn_profile"] = args.vpn
        network.setdefault("mode", "vpn")
    if args.gateway:
        network["gateway"] = args.gateway
        network.setdefault("mode", "gateway")
    if args.vpn and network["mode"] != "vpn":
        raise CliError(f"--vpn needs network mode vpn, not {network['mode']}", EXIT_USAGE)
    if args.gateway and network["mode"] != "gateway":
        raise CliError(f"--gateway needs network mode gateway, not {network['mode']}", EXIT_USAGE)
    if args.cpus is not None:
        resources["cpus"] = args.cpus
    if args.memory is not None:
        resources["memory_mb"] = args.memory
    if network:
        template["network"] = network
    if resources:
        template["resources"] = resources
    return template


def create(ctx: Ctx) -> int:
    args = ctx.args
    response = ctx.api.post("/api/instances", {"count": args.count, "name": args.name or "",
                                               "template": create_template(args)})
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    made = response.get("instances") or []
    ctx.out.table(("ID", "NAME", "ANDROID", "DEVICE"),
                  [(s.get("id"), s.get("name") or f"Android {s.get('id')}", s.get("android_version") or "default",
                    (s.get("device") or {}).get("profile", "-")) for s in made], right=(0,))
    ids = [int(s["id"]) for s in made]
    if ids:
        span = str(ids[0]) if len(ids) == 1 else f"{ids[0]}-{ids[-1]}" if ids == list(range(ids[0], ids[-1] + 1)) \
            else ",".join(map(str, ids))
        ctx.out.note(f"created, not started. Start with: sf start {span}")
    return EXIT_OK


# ------------------------------------------------------------------ lifecycle

def lifecycle(action: str):
    def run(ctx: Ctx) -> int:
        body = {"action": action, "selector": ctx.args.selector, "force": bool(getattr(ctx.args, "force", False))}
        return ctx.finish(ctx.api.post("/api/instances/bulk", body))
    return run


def rm(ctx: Ctx) -> int:
    rows = ctx.instances()
    ids = ctx.select(ctx.args.selector, rows=rows)
    names = {int(r["id"]): r.get("name", "") for r in rows}
    listing = ", ".join(f"{i} ({names.get(i)})" if names.get(i) else str(i) for i in ids[:12])
    listing += f" and {len(ids) - 12} more" if len(ids) > 12 else ""
    what = "The data volumes are kept." if ctx.args.keep_data else "Their data is deleted with them."
    if not confirm(ctx, f"Delete {len(ids)} Android(s): {listing}? {what}"):
        ctx.out.note("nothing deleted")
        return EXIT_OK
    query = {"keep_data": ctx.args.keep_data, "force": ctx.args.force}

    def call(instance_id: int) -> Any:
        # A safety backup may run first: allow it the time a large /data takes.
        return ctx.api.request("DELETE", f"/api/instances/{instance_id}", query=query, timeout=3600)

    return each(ctx, ids, call, verb="deleting",
                detail=lambda r: "deleted, data kept" if (r or {}).get("kept_data") else "deleted")


# ------------------------------------------------------------------ set

def spec_patch(assignments: list[str]) -> dict:
    """`key=value ...` -> a patch for PATCH /api/instances/{id}."""
    try:
        patch = values.parse_assignments(InstanceSpec, assignments)
    except values.KeyValueError as e:
        raise CliError(f"{e}\n    settable keys: {', '.join(k for k in values.keys_of(InstanceSpec) if k not in READ_ONLY_KEYS)}",
                       EXIT_USAGE) from None
    for key in READ_ONLY_KEYS:
        if key in patch:
            raise CliError(f"{key} cannot be changed" + ("; clone the Android instead (sf does not renumber)"
                                                           if key == "id" else ""), EXIT_USAGE)
    return patch


def _patch_each(ctx: Ctx, ids: list[int], patch: dict) -> int:
    body = {"patch": patch, "force": bool(getattr(ctx.args, "force", False))}
    return each(ctx, ids, lambda i: ctx.api.patch(f"/api/instances/{i}", body), verb="updating",
                detail=lambda r: "saved; applies at its next (re)start")


def set_(ctx: Ctx) -> int:
    patch = spec_patch(ctx.args.assignments)
    return _patch_each(ctx, ctx.select(ctx.args.selector), patch)


# ------------------------------------------------------------------ info / logs

def info(ctx: Ctx) -> int:
    instance_id = one_id(ctx.args.id)
    status = ctx.api.get(f"/api/instances/{instance_id}")
    device = ctx.api.get(f"/api/instances/{instance_id}/info") if ctx.args.device else None
    if ctx.out.json_mode:
        ctx.out.json({"status": status, "device": device} if ctx.args.device else status)
        return EXIT_OK
    screen = status.get("screen") or {}
    gpu, net = status.get("gpu") or {}, status.get("net") or {}
    items: list[tuple[str, Any]] = [
        ("id", status.get("id")), ("name", status.get("name")), ("state", status.get("state")),
        ("busy", status.get("busy") or "-"),
        ("android", f"{status.get('android_version', '-')}  ({status.get('image', '-')})"),
        ("screen", f"{screen.get('width')}x{screen.get('height')} @{screen.get('dpi')}dpi, {screen.get('fps')} fps"
         if screen else "-"),
        ("rendering", f"{gpu.get('mode') or 'not started yet'} (asked for: {gpu.get('requested', '-')})"
         + (f" on {gpu['node']}" if gpu.get("node") else "")),
        ("network", _net(status) + ("" if net.get("killswitch", True) else "  kill switch OFF")),
        ("adb", status.get("serial")), ("data volume", status.get("data_volume")),
        ("tags", status.get("tags") or "-"), ("uptime", duration(status.get("uptime"))),
        ("last start", when(status.get("last_start"))),
    ]
    for key in ("last_error", "config_error"):
        if status.get(key):
            items.append((key.replace("_", " "), status[key]))
    for warning in status.get("warnings") or []:
        items.append(("warning", warning))
    ctx.out.pairs(items)
    if isinstance(device, dict):
        ctx.out.print()
        ctx.out.pairs((str(k), v) for k, v in device.items())
    return EXIT_OK


def logs(ctx: Ctx) -> int:
    instance_id = one_id(ctx.args.id)
    if ctx.args.logcat:
        response = ctx.api.get(f"/api/instances/{instance_id}/logcat", lines=ctx.args.lines)
    else:
        response = ctx.api.get(f"/api/instances/{instance_id}/logs", tail=ctx.args.lines)
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    if ctx.args.logcat:
        ctx.out.print(str(response.get("text", "")).rstrip("\n"))
        return EXIT_OK
    for part in ("net", "android"):
        ctx.out.print(f"==> {part} container <==")
        ctx.out.print(str(response.get(part) or "(no output)").rstrip("\n"))
    return EXIT_OK


# ------------------------------------------------------------------ gpu / image

def gpu_ls(ctx: Ctx) -> int:
    from ..local import GPU_HEADERS, gpu_rows
    response = ctx.api.get("/api/system/gpus")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK
    ctx.out.table(GPU_HEADERS, gpu_rows(response.get("gpus") or []),
                  empty="No GPU with a render node was found: Androids draw on the CPU (software mode).")
    options = response.get("options") or []
    if options:
        ctx.out.print("\nRendering choices (sf gpu set <selector> <mode>):")
        ctx.out.table(("MODE", "CARD", "AVAILABLE", "WHAT", "WHY NOT"),
                      [(o.get("value"), o.get("node") or "any", bool(o.get("available")), o.get("label"),
                        "" if o.get("available") else o.get("reason", "")) for o in options])
    return EXIT_OK


def gpu_set(ctx: Ctx) -> int:
    mode = None if ctx.args.mode == "default" else ctx.args.mode
    patch: dict[str, Any] = {"gpu_mode": mode}
    if ctx.args.node is not None:
        patch["gpu_node"] = ctx.args.node or None
    return _patch_each(ctx, ctx.select(ctx.args.selector), patch)


def image_ls(ctx: Ctx) -> int:
    response = ctx.api.get("/api/images")
    if ctx.out.json_mode:
        ctx.out.json(response)
        return EXIT_OK

    def feature(row: dict, name: str) -> str:
        entry = (row.get("features") or {}).get(name) or {}
        return "yes" if entry.get("available") else "no"

    ctx.out.table(("ANDROID", "BASE IMAGE", "PULLED", "RUNS AS", "BUILT", "STALE", "CAMERA", "NVIDIA"),
                  [(r.get("version"), r.get("base"), bool(r.get("base_present")), r.get("tag"), bool(r.get("built")),
                    bool(r.get("stale")), feature(r, "camera"), feature(r, "nvidia"))
                   for r in response.get("images") or []])
    return EXIT_OK


def image_task(action: str):
    def run(ctx: Ctx) -> int:
        return ctx.finish(ctx.api.post(f"/api/images/{ctx.args.version}/{action}"))
    return run


# ------------------------------------------------------------------ tasks / top / open

def tasks(ctx: Ctx) -> int:
    task_id = ctx.args.id
    if not task_id:
        response = ctx.api.get("/api/tasks", active=ctx.args.active or None)
        if ctx.out.json_mode:
            ctx.out.json(response)
            return EXIT_OK
        now = time.time()
        ctx.out.table(("TASK", "STATE", "PROGRESS", "FAILED", "AGE", "WHAT"),
                      [(t.get("id"), t.get("state"), f"{t.get('current', 0)}/{t.get('total', 0)}" if t.get("total") else "-",
                        t.get("failed", 0), duration(now - t["created"]) if t.get("created") else "-", t.get("title"))
                       for t in response.get("tasks") or []],
                      right=(3,), state_columns=(1,), empty="No tasks.")
        return EXIT_OK
    if ctx.args.cancel:
        response = ctx.api.post(f"/api/tasks/{task_id}/cancel")
        if ctx.out.json_mode:
            ctx.out.json(response)
        else:
            ctx.out.note(f"asked task {task_id} to stop; work already running on a device finishes first")
        return EXIT_OK
    task = ctx.api.get(f"/api/tasks/{task_id}")["task"]
    if task.get("state") in ("queued", "running") and ctx.wait:
        return follow(ctx, task)
    if not ctx.out.json_mode:
        ctx.out.pairs([("task", task.get("id")), ("what", task.get("title")), ("state", task.get("state")),
                       ("progress", f"{task.get('current', 0)}/{task.get('total', 0)}"),
                       ("message", task.get("message") or "-")])
    code = report(ctx, task)
    # Looking at a task that is still running is not a failure.
    return EXIT_OK if task.get("state") in ("queued", "running") else code


def _top_frame(ctx: Ctx, snapshot: dict) -> None:
    host = snapshot.get("host") or {}
    memory = host.get("memory") or {}
    totals = snapshot.get("totals") or {}
    load = " ".join(str(x) for x in host.get("load") or []) or "-"
    ctx.out.print(f"Androids: {totals.get('running', 0)} running of {totals.get('defined', 0)}    "
                  f"host CPU {host.get('cpu_percent', 0)}% of {host.get('cpu_count', 0)} threads, load {load}")
    ctx.out.print(f"RAM {memory.get('used_mb', 0)} of {memory.get('total_mb', 0)} MB used "
                  f"({memory.get('used_percent', 0)}%), {memory.get('available_mb', 0)} MB available; "
                  f"swap {memory.get('swap_total_mb', 0) - memory.get('swap_free_mb', 0)} of "
                  f"{memory.get('swap_total_mb', 0)} MB")
    for gpu in snapshot.get("gpus") or []:
        util = "-" if gpu.get("util_percent") is None else f"{gpu['util_percent']}%"
        vram = "-" if gpu.get("vram_used_mb") is None else f"{gpu['vram_used_mb']} of {gpu.get('vram_total_mb', '?')} MB"
        ctx.out.print(f"GPU {gpu.get('name', '?')}: {util} busy, VRAM {vram}")
    instances = snapshot.get("instances") or {}
    rows = sorted(((key, s.get("cpu", 0), s.get("mem_mb", 0), s.get("mem_limit_mb", 0), s.get("pids", 0))
                   for key, s in instances.items()), key=lambda r: -float(r[1] or 0))
    ctx.out.print()
    ctx.out.table(("ID", "CPU %", "MEM MB", "LIMIT MB", "PIDS"), rows, right=(0, 1, 2, 3, 4),
                  empty="No Android is running.")


def top(ctx: Ctx) -> int:
    if ctx.out.json_mode:
        ctx.out.json(ctx.api.get("/api/system/metrics"))
        return EXIT_OK
    live = ctx.out.tty and not ctx.args.once
    try:
        while True:
            snapshot = ctx.api.get("/api/system/metrics")
            if live:
                ctx.out.stdout.write("\033[H\033[J")
            _top_frame(ctx, snapshot)
            ctx.out.stdout.flush()
            if not live:
                return EXIT_OK
            ctx.sleep(ctx.args.interval)
    except KeyboardInterrupt:
        return EXIT_OK


def open_(ctx: Ctx) -> int:
    url = base_url(ctx.config)
    if ctx.out.json_mode:
        ctx.out.json({"url": url})
        return EXIT_OK
    ctx.out.print(url)
    ctx.out.note("Open it in a browser on this machine and log in with the API token (print it with: sf token).\n"
                 "The token is never put in a link.")
    return EXIT_OK


# ------------------------------------------------------------------ parser

def register(b: Any, sub: argparse._SubParsersAction) -> None:
    p = b.add(sub, "ls", ls, "list the Androids and their state", "sf ls\nsf ls -w", aliases=("list",))
    p.add_argument("-w", "--watch", action="store_true", help="keep the list on screen and refresh it")
    p.add_argument("--interval", type=float, default=2.0, metavar="SECONDS", help="refresh interval for -w (default: 2)")

    p = b.add(sub, "create", create, "add Androids (they are created stopped)",
              "sf create -n 5 --android 13 --gpu igpu --tag eu\nsf create --name shop --net vpn --vpn nl-ams.ovpn",
              description="Add one or more Androids. Anything not given follows the farm's settings "
                          "(sf config show) and keeps following them when they change.")
    p.add_argument("-n", "--count", type=int, default=1, help="how many (default: 1)")
    p.add_argument("--name", help="display name (numbered when several are created)")
    p.add_argument("--android", metavar="VERSION", help="Android version, e.g. 13 (default: android.default_version)")
    p.add_argument("--gpu", choices=GPU_MODES, help="rendering: CPU only (software), integrated or discrete GPU, a vendor, or auto")
    p.add_argument("--net", choices=NET_MODES, help="network mode")
    p.add_argument("--vpn", metavar="PROFILE", help="pin this VPN profile (implies --net vpn)")
    p.add_argument("--gateway", metavar="NAME", help="shared VPN gateway to use (implies --net gateway)")
    p.add_argument("--cpus", type=float, help="CPU cores each may use")
    p.add_argument("--memory", type=int, metavar="MIB", help="RAM limit each, in MiB")
    p.add_argument("--width", type=int, help="screen width in pixels")
    p.add_argument("--height", type=int, help="screen height in pixels")
    p.add_argument("--dpi", type=int, help="screen density")
    p.add_argument("--fps", type=int, help="frames per second")
    p.add_argument("--tag", action="append", metavar="TAG", help="tag for selectors (repeatable)")
    p.add_argument("--data-volume", metavar="VOLUME", help="adopt an existing Docker volume as /data (one Android only)")
    p.add_argument("--image", metavar="IMAGE", help="run this exact image instead of the one for the Android version")

    for action, summary, example in (
            ("start", "start Androids", "sf start 1-10"),
            ("restart", "restart Androids", "sf restart tag:eu"),
            ("recreate", "rebuild the containers of Androids (data is kept)", "sf recreate 3")):
        p = b.add(sub, action, lifecycle(action), summary, example)
        p.add_argument("selector", help="which Androids")
        p.add_argument("--force", action="store_true", help="start even when the farm is over its capacity estimate")
    p = b.add(sub, "stop", lifecycle("stop"), "stop Androids", "sf stop all")
    p.add_argument("selector", help="which Androids")

    p = b.add(sub, "rm", rm, "delete Androids (asks first)", "sf rm 7\nsf rm tag:test --keep-data --yes",
              description="Delete Androids and, unless --keep-data, their data. With backup.auto_before_risky "
                          "(the default) a backup is taken first.", aliases=("delete",))
    p.add_argument("selector", help="which Androids")
    p.add_argument("--keep-data", action="store_true", help="keep the /data volume (it can be adopted again later)")
    p.add_argument("--force", action="store_true", help="delete even if the safety backup cannot be taken")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    p = b.add(sub, "set", set_, "change settings of Androids (applied at their next restart)",
              "sf set 1-5 fps=30 network.mode=vpn tags=eu,shop\nsf set 3 gpu_mode=default",
              description="Change per-Android settings. Keys are InstanceSpec fields, nested ones with a dot "
                          "(network.mode, resources.cpus, device.locale). Lists are comma separated. "
                          "`default` puts an optional setting back to following the farm's value.")
    p.add_argument("selector", help="which Androids")
    p.add_argument("assignments", nargs="+", metavar="key=value", help="one or more settings")
    p.add_argument("--force", action="store_true",
                   help="allow a change that can lose data (another Android version on existing data)")

    p = b.add(sub, "info", info, "show one Android in detail", "sf info 3")
    p.add_argument("id", help="Android id")
    p.add_argument("--device", action="store_true", help="also ask the running Android about itself (model, build, battery)")

    p = b.add(sub, "logs", logs, "show the container logs of one Android", "sf logs 3 -n 50\nsf logs 3 --logcat")
    p.add_argument("id", help="Android id")
    p.add_argument("-n", "--lines", type=int, default=200, help="lines to show (default: 200)")
    p.add_argument("--logcat", action="store_true", help="Android's own log (logcat) instead of the containers' output")

    gpu = b.group(sub, "gpu", "GPUs and the rendering mode of Androids")
    b.add(gpu, "ls", gpu_ls, "list GPUs and the rendering choices on this machine", "sf gpu ls")
    p = b.add(gpu, "set", gpu_set, "choose how Androids draw (applied at their next restart)",
              "sf gpu set 1-8 igpu\nsf gpu set 9 nvidia --node /dev/dri/renderD129")
    p.add_argument("selector", help="which Androids")
    p.add_argument("mode", choices=(*GPU_MODES, "default"), help="`default` follows the farm's gpu.mode again")
    p.add_argument("--node", metavar="DEVICE", help="pin one card, e.g. /dev/dri/renderD129 (empty string: unpin)")

    image = b.group(sub, "image", "Android images, one per Android version")
    b.add(image, "ls", image_ls, "list the image of every configured Android version", "sf image ls")
    p = b.add(image, "pull", image_task("ensure"), "download and prepare the image of one Android version",
              "sf image pull 13")
    p.add_argument("version", help="Android version, e.g. 13")
    p = b.add(image, "build", image_task("build"), "rebuild an image with its overlays (camera, GPU runtime)",
              "sf image build 13")
    p.add_argument("version", help="Android version, e.g. 13")

    p = b.add(sub, "tasks", tasks, "list background tasks, or show, follow and cancel one",
              "sf tasks\nsf tasks t12 --wait\nsf tasks t12 --cancel")
    p.add_argument("id", nargs="?", help="task id, e.g. t12")
    p.add_argument("--active", action="store_true", help="only tasks still running")
    p.add_argument("--cancel", action="store_true", help="ask the task to stop")

    p = b.add(sub, "top", top, "live load of the host, the GPUs and every running Android", "sf top")
    p.add_argument("--interval", type=float, default=3.0, metavar="SECONDS", help="refresh interval (default: 3)")
    p.add_argument("--once", action="store_true", help="print one snapshot and exit")

    b.add(sub, "open", open_, "print the panel's address and how to log in", "sf open")

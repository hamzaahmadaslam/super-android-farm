"""The NVIDIA relay: file checks, process detection and the never-restart rule."""
import os
import socket
import stat
import sys

import pytest

from superfarm.config import Config
from superfarm.host import venus
from superfarm.paths import get_paths

NODE = "/dev/dri/renderD129"


@pytest.fixture
def env(tmp_path):
    paths = get_paths(tmp_path / "home").ensure()
    cfg = Config.model_validate({"gpu": {"nvidia": {"socket_dir": str(tmp_path / "sock"), "icd": str(tmp_path / "icd.json")}}})
    return cfg, paths, tmp_path


def build_runtime(paths):
    rt = paths.overlays / "venus"
    rt.mkdir(parents=True)
    for name in venus.RUNTIME_FILES:
        (rt / name).write_text("x")


def build_relay(paths, script, libdir="lib"):
    rl = paths.overlays / "venus-relay"
    for sub in ("bin", "libexec", libdir):
        (rl / sub).mkdir(parents=True, exist_ok=True)
    (rl / venus.RELAY_BINARY).write_text(script)
    (rl / venus.RELAY_HELPER).write_text("#!/bin/sh\n")
    os.chmod(rl / venus.RELAY_BINARY, 0o755)
    os.chmod(rl / venus.RELAY_HELPER, 0o755)
    (rl / libdir / venus.RELAY_LIBRARY).write_text("")
    return rl


def fake_proc(tmp_path, procs):
    """procs: {pid: (comm, argv)}"""
    root = tmp_path / "proc"
    root.mkdir(exist_ok=True)
    (root / "self").mkdir(exist_ok=True)
    for pid, (comm, argv) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
    return root


# A stand-in relay: creates the socket it is asked for and stays alive, like the real one.
FAKE_RELAY = f"""#!{sys.executable}
import os, socket, sys, time
path = sys.argv[sys.argv.index("--socket-path") + 1]
open(os.path.join(os.path.dirname(path), "env.txt"), "w").write(
    "\\n".join(f"{{k}}={{os.environ.get(k, '')}}" for k in ("VK_ICD_FILENAMES", "RENDER_SERVER_EXEC_PATH", "LD_LIBRARY_PATH")))
s = socket.socket(socket.AF_UNIX); s.bind(path); s.listen(1)
time.sleep(30)
"""


def test_runtime_status_names_every_missing_file(env):
    cfg, paths, _ = env
    st = venus.runtime_status(cfg, paths)
    assert not st["ready"] and not st["runtime_ready"] and not st["relay_ready"] and not st["icd"]
    assert len(st["missing"]) == 4 + 3 and st["missing"][0].endswith("overlays/venus/gpu_config.sh")
    build_runtime(paths)
    assert venus.runtime_status(cfg, paths)["runtime_ready"] and not venus.runtime_status(cfg, paths)["ready"]
    build_relay(paths, "#!/bin/sh\n", libdir="lib/x86_64-linux-gnu")
    st = venus.runtime_status(cfg, paths)
    assert st["ready"] and st["missing"] == [] and st["relay"]["library"].endswith("lib/x86_64-linux-gnu")
    assert "Android 13" in st["built_for"]


def test_configured_dirs_override_the_defaults(env, tmp_path):
    _, paths, _ = env
    cfg = Config.model_validate({"gpu": {"nvidia": {"runtime_dir": "/opt/a", "relay_dir": "/opt/b"}}})
    assert str(venus.runtime_dir(cfg, paths)) == "/opt/a" and str(venus.relay_dir(cfg, paths)) == "/opt/b"
    assert venus.runtime_dir(Config(), paths) == paths.overlays / "venus"


@pytest.mark.parametrize("bad", ["", "renderD129", "/dev/dri/card0", "/dev/dri/renderD129/../x", "/dev/dri/renderD12 9"])
def test_bad_render_nodes_are_refused(env, bad):
    cfg, paths, _ = env
    with pytest.raises(ValueError):
        venus.socket_dir(cfg, bad)
    with pytest.raises(ValueError):
        venus.ensure_relay(cfg, paths, bad)


def test_one_socket_folder_per_card(env):
    cfg, _, tmp = env
    assert venus.socket_dir(cfg, NODE) == tmp / "sock" / "renderD129"
    assert venus.socket_dir(Config(), "/dev/dri/renderD128") == venus.Path("/run/superfarm/venus/renderD128")


def test_relays_are_found_by_exact_process_name_only(tmp_path):
    proc_root = fake_proc(tmp_path, {
        100: ("virgl_test_serv", ["/x/bin/virgl_test_server", "--venus", "--rendernode", NODE, "--socket-path", "/run/a/vtest.sock"]),
        # A shell whose command line merely mentions the server (what `pkill -f` would wrongly hit).
        101: ("bash", ["bash", "-c", "pgrep -f virgl_test_server"]),
        102: ("virgl_test_serv", ["virgl_test_server", "--socket-path=/run/b/vtest.sock"]),
        103: ("virgl_render_se", ["virgl_render_server"]),
    })
    found = venus.find_relays(proc_root)
    assert [(p["pid"], p["socket"], p["node"]) for p in found] == [
        (100, "/run/a/vtest.sock", NODE), (102, "/run/b/vtest.sock", "")]
    assert venus.find_relays(tmp_path / "no-proc") == []


def test_status_ignores_relays_that_serve_another_socket(env):
    cfg, _, tmp = env
    foreign = fake_proc(tmp, {7: ("virgl_test_serv", ["virgl_test_server", "--socket-path", "/run/other/vtest.sock"])})
    row = venus.relay_status(cfg, nodes=[NODE], proc_root=foreign)[0]
    assert row == {"node": NODE, "socket_dir": str(tmp / "sock/renderD129"), "socket": str(tmp / "sock/renderD129/vtest.sock"),
                   "pid": 0, "socket_present": False, "running": False}


def test_status_running_needs_both_process_and_socket(env):
    cfg, _, tmp = env
    sdir = tmp / "sock" / "renderD129"
    sdir.mkdir(parents=True)
    sock = sdir / "vtest.sock"
    mine = fake_proc(tmp, {9: ("virgl_test_serv", ["virgl_test_server", "--socket-path", str(sock)])})
    assert venus.relay_status(cfg, nodes=[NODE], proc_root=mine)[0]["running"] is False       # process, no socket
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock))
    try:
        row = venus.relay_status(cfg, nodes=[NODE], proc_root=mine)[0]
        assert row["running"] and row["pid"] == 9
        assert venus.relay_status(cfg, nodes=[NODE], proc_root=tmp / "empty")[0]["running"] is False   # socket, no process
    finally:
        s.close()


def test_ensure_relay_explains_what_is_missing(env, monkeypatch):
    cfg, paths, tmp = env
    with pytest.raises(venus.RelayError, match="not built.*build.sh"):
        venus.ensure_relay(cfg, paths, NODE)
    build_relay(paths, "#!/bin/sh\n")
    with pytest.raises(venus.RelayError, match="manifest"):
        venus.ensure_relay(cfg, paths, NODE)
    (tmp / "icd.json").write_text("{}")
    real_access = os.access
    monkeypatch.setattr(venus.os, "access", lambda p, m: False if str(p) == NODE else real_access(p, m))
    with pytest.raises(venus.RelayError, match="usermod -aG render,video"):
        venus.ensure_relay(cfg, paths, NODE)


def test_ensure_relay_reports_unwritable_socket_dir(env, monkeypatch):
    _, paths, tmp = env
    build_relay(paths, "#!/bin/sh\n")
    (tmp / "icd.json").write_text("{}")
    (tmp / "blocker").write_text("a file where a folder is needed")
    cfg = Config.model_validate({"gpu": {"nvidia": {"socket_dir": str(tmp / "blocker" / "venus"), "icd": str(tmp / "icd.json")}}})
    monkeypatch.setattr(venus.os, "access", lambda p, m: True)
    with pytest.raises(venus.RelayError, match="run `sf host apply` once"):
        venus.ensure_relay(cfg, paths, NODE)


@pytest.fixture
def startable(env, monkeypatch):
    cfg, paths, tmp = env
    (tmp / "icd.json").write_text("{}")
    real_access = os.access
    monkeypatch.setattr(venus.os, "access", lambda p, m: True if str(p) == NODE else real_access(p, m))
    started = []
    yield cfg, paths, tmp, started
    for child in list(venus._children.values()) + started:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    venus._children.clear()


def test_ensure_relay_starts_once_and_then_leaves_it_alone(startable):
    cfg, paths, tmp, started = startable
    rl = build_relay(paths, FAKE_RELAY)
    sdir = venus.ensure_relay(cfg, paths, NODE, timeout=10, proc_root=tmp / "noproc")
    child = venus._children["renderD129"]
    started.append(child)
    sock = tmp / "sock" / "renderD129" / "vtest.sock"
    assert sdir == str(tmp / "sock" / "renderD129") and stat.S_ISSOCK(sock.stat().st_mode)
    assert stat.S_IMODE(sock.stat().st_mode) == 0o666           # every Android uid must be able to connect
    assert os.getsid(child.pid) == child.pid                    # own session: outlives the daemon
    envtxt = dict(line.split("=", 1) for line in (sock.parent / "env.txt").read_text().splitlines())
    assert envtxt["VK_ICD_FILENAMES"] == str(tmp / "icd.json")
    assert envtxt["RENDER_SERVER_EXEC_PATH"] == str(rl / venus.RELAY_HELPER)
    assert envtxt["LD_LIBRARY_PATH"].split(os.pathsep)[0] == str(rl / "lib")
    assert "starting relay for /dev/dri/renderD129" in (paths.logs / "venus-relay-renderD129.log").read_text()

    # Second call with the relay visible in /proc: nothing is started, nothing is signalled.
    proc_root = fake_proc(tmp, {child.pid: ("virgl_test_serv", ["virgl_test_server", "--socket-path", str(sock)])})
    inode = sock.stat().st_ino
    assert venus.ensure_relay(cfg, paths, NODE, proc_root=proc_root) == sdir
    assert venus._children["renderD129"] is child and child.poll() is None and sock.stat().st_ino == inode


def test_stale_socket_is_replaced(startable):
    cfg, paths, tmp, started = startable
    build_relay(paths, FAKE_RELAY)
    sdir = tmp / "sock" / "renderD129"
    sdir.mkdir(parents=True)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(sdir / "vtest.sock"))
    stale.close()                                                # file stays, nobody listens: left by a crash
    venus.ensure_relay(cfg, paths, NODE, timeout=10, proc_root=tmp / "noproc")
    started.append(venus._children["renderD129"])
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(5)
    probe.connect(str(sdir / "vtest.sock"))                      # a live server answers now
    probe.close()


def test_relay_that_exits_reports_its_log(startable):
    cfg, paths, tmp, _ = startable
    build_relay(paths, "#!/bin/sh\necho 'libEGL: no surfaceless platform' >&2\nexit 3\n")
    with pytest.raises(venus.RelayError, match="exited with code 3.*no surfaceless platform"):
        venus.ensure_relay(cfg, paths, NODE, timeout=10, proc_root=tmp / "noproc")
    assert "renderD129" not in venus._children


def test_relay_that_never_creates_its_socket_times_out_and_is_stopped(startable):
    cfg, paths, tmp, _ = startable
    build_relay(paths, "#!/bin/sh\nexec sleep 30\n")
    with pytest.raises(venus.RelayError, match="did not create"):
        venus.ensure_relay(cfg, paths, NODE, timeout=0.5, proc_root=tmp / "noproc")
    assert "renderD129" not in venus._children


def test_relay_command_line(env):
    cfg, paths, tmp = env
    argv, extra = venus.relay_command(cfg, paths, NODE)
    assert argv[1:] == ["--venus", "--use-egl-surfaceless", "--rendernode", NODE,
                        "--socket-path", str(tmp / "sock/renderD129/vtest.sock")]
    assert argv[0].endswith("overlays/venus-relay/bin/virgl_test_server") and "LD_LIBRARY_PATH" not in extra

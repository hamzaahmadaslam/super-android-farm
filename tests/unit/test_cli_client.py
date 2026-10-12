"""The HTTP client and task following, against an in-process stub of the daemon."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest
from cli_fakes import TOKEN, Stub, instance, make_home, run_sf, task

from superfarm.cli import client as client_mod
from superfarm.cli.client import Api, ApiError, CliError, DaemonDown
from superfarm.cli.common import Ctx
from superfarm.config import Config


@pytest.fixture
def stub():
    server = Stub()
    yield server
    server.close()


@pytest.fixture
def home(tmp_path, monkeypatch, stub):
    # No test waits for real: every poll interval of the CLI goes through Ctx.sleep.
    monkeypatch.setattr(Ctx, "sleep", lambda self, seconds: None)
    return make_home(tmp_path, monkeypatch, port=stub.port)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ plain calls

def test_json_round_trip_with_bearer_token(stub):
    stub.on("POST", "/api/instances", lambda r: (201, {"echo": r.json}))
    api = Api(stub.url, TOKEN)
    assert api.post("/api/instances", {"count": 2}) == {"echo": {"count": 2}}
    sent = stub.requests[-1]
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["accept"] == "application/json"


def test_query_values_are_encoded_and_none_is_left_out(stub):
    stub.on("GET", "/api/x", {})
    Api(stub.url, TOKEN).get("/api/x", selector="1-3,tag:a b", refresh=True, missing=None)
    assert stub.requests[-1].query == {"selector": "1-3,tag:a b", "refresh": "true"}


def test_path_segments_are_escaped(stub):
    stub.on("GET", "/api/automation/flows/examples/open app", {"ok": True})
    assert Api(stub.url, TOKEN).get("/api/automation/flows/examples/open app") == {"ok": True}
    stub.on("DELETE", "/api/net/profiles/a?b#c", {"deleted": True})
    assert Api(stub.url, TOKEN).delete("/api/net/profiles/a?b#c") == {"deleted": True}
    assert stub.requests[-1].query == {}                # `?` in a name is data, not a query string
    with pytest.raises(ValueError):
        Api(stub.url, TOKEN).get("api/x")


def test_the_token_never_appears_in_a_url_or_repr(stub):
    stub.on("GET", "/api/x", {})
    api = Api(stub.url, TOKEN)
    api.get("/api/x", a=1)
    assert TOKEN not in repr(api) and TOKEN not in str(api.__dict__.get("url"))
    assert all(TOKEN not in r.path and TOKEN not in json.dumps(r.query) for r in stub.requests)


def test_empty_body_and_non_json_answers(stub):
    stub.on("DELETE", "/api/listen", (200, None))
    assert Api(stub.url, TOKEN).delete("/api/listen") is None
    stub.on("GET", "/api/x", (200, b"<html>hello</html>", "text/html"))
    with pytest.raises(CliError, match="did not answer with JSON"):
        Api(stub.url, TOKEN).get("/api/x")


def test_error_envelope_becomes_an_api_error(stub):
    stub.on("GET", "/api/instances/9", (404, {"error": {"code": "not_found", "message": "there is no Android 9"}}))
    with pytest.raises(ApiError) as e:
        Api(stub.url, TOKEN).get("/api/instances/9")
    assert (e.value.status, e.value.api_code, str(e.value)) == (404, "not_found", "there is no Android 9")
    stub.on("GET", "/api/boom", (500, b"Internal Server Error", "text/plain"))
    with pytest.raises(ApiError) as e:
        Api(stub.url, TOKEN).get("/api/boom")
    assert e.value.status == 500 and "HTTP 500" in str(e.value)


def test_401_explains_where_the_token_comes_from_without_printing_it(stub):
    stub.on("GET", "/api/instances", {"instances": []})
    with pytest.raises(ApiError) as e:
        Api(stub.url, "wrong-token").get("/api/instances")
    assert e.value.status == 401 and "rejected the API token" in str(e.value) and "wrong-token" not in str(e.value)
    with pytest.raises(ApiError, match="none was found"):
        Api(stub.url, "").get("/api/instances")
    assert "authorization" not in stub.requests[-1].headers


def test_connection_refused_says_how_to_start_the_daemon():
    api = Api(f"http://127.0.0.1:{free_port()}", TOKEN, timeout=5)
    with pytest.raises(DaemonDown) as e:
        api.get("/api/health")
    assert "the daemon is not running (start it with: sf serve  or  sf service start)" in str(e.value)
    assert TOKEN not in str(e.value)


def test_no_answer_is_reported_as_a_timeout_not_as_down():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)                 # accepts the connection, never answers
    try:
        with pytest.raises(CliError, match="no answer from the daemon") as e:
            Api(f"http://127.0.0.1:{listener.getsockname()[1]}", TOKEN, timeout=0.3).get("/api/health")
        assert not isinstance(e.value, DaemonDown)
    finally:
        listener.close()


def test_redirects_are_not_followed_so_the_token_cannot_leak(stub):
    other = Stub()
    try:
        other.on("GET", "/steal", {"stolen": True})
        stub.on("GET", "/api/x", lambda r: (302, b"", "text/plain"))
        # A 302 without following surfaces as an HTTP error; nothing reaches the other server.
        with pytest.raises(ApiError):
            Api(stub.url, TOKEN).get("/api/x")
        assert other.requests == []
    finally:
        other.close()


def test_environment_proxies_are_ignored(stub, monkeypatch):
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{free_port()}")
    stub.on("GET", "/api/health", {"ok": True})
    assert Api(stub.url, TOKEN).get("/api/health") == {"ok": True}


# ------------------------------------------------------------------ where the daemon is

def test_base_url_follows_the_config():
    def url(**server):
        return client_mod.base_url(Config.model_validate({"server": server}))
    assert url() == "http://127.0.0.1:7700"
    assert url(port=7701) == "http://127.0.0.1:7701"
    assert url(host="0.0.0.0") == "http://127.0.0.1:7700"           # a wildcard is dialled through loopback
    assert url(host="::") == "http://[::1]:7700"
    assert url(host="::1") == "http://[::1]:7700"
    assert url(host="farm.lan") == "http://farm.lan:7700"
    assert url(tls_cert="/c.pem", tls_key="/k.pem") == "https://127.0.0.1:7700"
    assert url(tls_cert="/c.pem") == "http://127.0.0.1:7700"         # half a pair is not TLS


def test_tls_context_only_with_a_certificate(tmp_path):
    assert client_mod.tls_context(Config()) is None
    config = Config.model_validate({"server": {"tls_cert": str(tmp_path / "missing.pem"), "tls_key": "k"}})
    with pytest.raises(CliError, match="cannot read server.tls_cert"):
        client_mod.tls_context(config)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_tls_context_trusts_exactly_the_configured_self_signed_certificate(tmp_path):
    import ssl
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    made = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                           str(cert), "-days", "2", "-subj", "/CN=localhost", "-addext",
                           "subjectAltName=DNS:localhost,IP:127.0.0.1"], capture_output=True, timeout=60)
    if made.returncode != 0:
        pytest.skip("openssl could not create a test certificate")
    context = client_mod.tls_context(Config.model_validate({"server": {"tls_cert": str(cert), "tls_key": str(key)}}))
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True


def test_token_is_read_from_the_farm_home(tmp_path, monkeypatch):
    paths = make_home(tmp_path, monkeypatch, token="abc123")
    assert client_mod.read_token(paths) == "abc123"
    client_mod.token_path(paths).unlink()
    assert client_mod.read_token(paths) == ""


def test_cli_and_api_agree_on_the_token_file():
    auth = pytest.importorskip("superfarm.api.auth")
    assert client_mod.TOKEN_FILE == auth.TOKEN_FILE


# ------------------------------------------------------------------ files

def test_upload_streams_multipart_with_fields(stub, tmp_path):
    payload = os.urandom(700_000)                       # larger than one chunk
    file = tmp_path / "my app (1).apk"
    file.write_bytes(payload)
    stub.on("POST", "/api/files/push", lambda r: (202, {"task": task(state="done")}))
    seen = []
    Api(stub.url, TOKEN).upload("/api/files/push", file, fields={"selector": "1-3", "remote": "/sdcard/", "skip": None},
                                progress=lambda sent, total: seen.append((sent, total)))
    sent = stub.requests[-1]
    kind, _, boundary = sent.headers["content-type"].partition("; boundary=")
    assert kind == "multipart/form-data" and int(sent.headers["content-length"]) == len(sent.body)
    parts = sent.body.split(b"--" + boundary.encode())
    assert parts[0] == b"" and parts[-1] == b"--\r\n"
    fields = {}
    for part in parts[1:-1]:
        head, _, body = part.partition(b"\r\n\r\n")
        fields[head.decode()] = body[:-2]
    assert fields['\r\nContent-Disposition: form-data; name="selector"'] == b"1-3"
    assert fields['\r\nContent-Disposition: form-data; name="remote"'] == b"/sdcard/"
    file_head = next(h for h in fields if 'name="file"' in h)
    assert 'filename="my app (1).apk"' in file_head and fields[file_head] == payload
    assert not any("skip" in h for h in fields)
    assert seen[-1] == (len(payload), len(payload)) and len(seen) >= 2


def test_upload_file_name_cannot_inject_headers(stub, tmp_path):
    file = tmp_path / 'evil"\r\nX-Injected: 1.apk'
    try:
        file.write_bytes(b"x")
    except OSError:
        pytest.skip("this filesystem refuses such a file name")
    stub.on("POST", "/api/apps/library", (201, {"app": {}}))
    Api(stub.url, TOKEN).upload("/api/apps/library", file)
    assert b"X-Injected" not in stub.requests[-1].body.split(b"\r\n\r\n")[0].replace(b"X-Injected_ 1.apk", b"")
    assert "x-injected" not in stub.requests[-1].headers


def test_upload_of_a_missing_file_is_a_clear_error(stub, tmp_path):
    with pytest.raises(CliError, match="cannot read"):
        Api(stub.url, TOKEN).upload("/api/apps/library", tmp_path / "nope.apk")
    assert stub.requests == []


def test_download_writes_the_file_and_leaves_nothing_behind_on_error(stub, tmp_path):
    data = os.urandom(300_000)
    stub.on("GET", "/api/instances/1/screenshot", (200, data, "image/png"))
    dest = tmp_path / "shot.png"
    assert Api(stub.url, TOKEN).download("/api/instances/1/screenshot", dest) == len(data)
    assert dest.read_bytes() == data and not (tmp_path / "shot.png.part").exists()

    stub.on("GET", "/api/instances/2/screenshot", (409, {"error": {"code": "device_unavailable", "message": "stopped"}}))
    with pytest.raises(ApiError, match="stopped"):
        Api(stub.url, TOKEN).download("/api/instances/2/screenshot", tmp_path / "no.png")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["shot.png"]


# ------------------------------------------------------------------ through the CLI

def test_daemon_down_message_and_exit_code(tmp_path, monkeypatch, capsys):
    make_home(tmp_path, monkeypatch, port=free_port())
    run = run_sf(capsys, "ls")
    assert run.code == 1 and run.out == ""
    assert "sf: the daemon is not running (start it with: sf serve  or  sf service start)" in run.err
    assert run.err.count("not running") == 1                     # one clear message, no traceback
    assert "Traceback" not in run.err


def test_wrong_token_through_the_cli(tmp_path, monkeypatch, capsys, stub):
    make_home(tmp_path, monkeypatch, port=stub.port, token="some-other-token")
    stub.on("GET", "/api/instances", {"instances": []})
    run = run_sf(capsys, "ls")
    assert run.code == 1 and "rejected the API token" in run.err and "some-other-token" not in run.err + run.out


def test_json_mode_prints_exactly_the_api_json(home, stub, capsys):
    payload = {"instances": [instance(1, state="booted", tags=("eu",)), instance(2)], "extra": {"k": [1, None, "ü"]}}
    stub.on("GET", "/api/instances", payload)
    run = run_sf(capsys, "--json", "ls")
    assert run.code == 0 and run.json == payload and run.err == ""


def test_validation_errors_list_each_field(home, stub, capsys):
    stub.on("POST", "/api/instances", (422, {"error": {
        "code": "validation_error", "message": "count: too big (and 1 more)",
        "detail": [{"field": "count", "message": "too big"}, {"field": "name", "message": "too long"}]}}))
    run = run_sf(capsys, "create", "-n", "900")
    assert run.code == 1
    assert "sf: count: too big (and 1 more)" in run.err and "    name: too long" in run.err


OVER = {"error": {"code": "over_capacity", "message": "starting 3 more Android(s) goes over this machine's estimated capacity",
                  "detail": "starting 3 more", "capacity": {
                      "estimate": {"max_instances": 24, "binding": "ram"}, "running": 24, "headroom": 0, "over": True,
                      "reasons": ["24 Android(s) are running and this machine is estimated to hold about 24"],
                      "memory_available_mb": 900, "enforced": True,
                      "advice": ["Enable compressed swap", "More RAM", "Mix modes", "a fourth line"]}}}


def test_over_capacity_explains_and_points_at_force(home, stub, capsys):
    stub.on("POST", "/api/instances/bulk", (409, OVER))
    run = run_sf(capsys, "start", "1-3")
    assert run.code == 1 and run.out == ""
    assert "over capacity: starting 3 more Android(s)" in run.err
    assert "24 running, about 24 estimated to fit (limited by RAM), 900 MB RAM available" in run.err
    assert "24 Android(s) are running" in run.err
    assert "- Enable compressed swap" in run.err and "a fourth line" not in run.err
    assert "repeat the command with --force" in run.err
    assert stub.requests[-1].json == {"action": "start", "selector": "1-3", "force": False}

    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="done", results={"1": {"ok": True, "detail": "booted"}})}))
    run = run_sf(capsys, "start", "1-3", "--force", "--wait")
    assert run.code == 0 and stub.requests[-1].json["force"] is True


def test_another_409_is_not_dressed_up_as_capacity(home, stub, capsys):
    stub.on("POST", "/api/instances/bulk", (409, {"error": {"code": "busy", "message": "Android 1 is being restarted"}}))
    run = run_sf(capsys, "start", "1")
    assert run.code == 1 and run.err == "sf: Android 1 is being restarted\n"


# ------------------------------------------------------------------ following tasks

def test_wait_follows_the_task_and_reports_per_device_results(home, stub, capsys):
    final = task(state="done", current=3, total=3, results={
        "1": {"ok": True, "detail": "booted in 21 s"}, "2": {"ok": False, "detail": "boot timed out"},
        "10": {"ok": True, "detail": "already running"}})
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="queued")}))
    stub.on("GET", "/api/tasks/t1", [{"task": task(state="running", current=1, total=3)},
                                     {"task": task(state="running", current=2, total=3)}, {"task": final}])
    run = run_sf(capsys, "start", "all", "--wait")
    assert run.code == 1                                            # one device failed
    assert run.out.splitlines() == ["ANDROID  RESULT  DETAIL",
                                    "      1  ok      booted in 21 s",
                                    "      2  failed  boot timed out",
                                    "     10  ok      already running",
                                    "2 ok, 1 failed"]
    assert len(stub.calls("GET", "/api/tasks/t1")) == 3


def test_all_devices_ok_exits_zero(home, stub, capsys):
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="running")}))
    stub.on("GET", "/api/tasks/t1", {"task": task(state="done", results={"1": {"ok": True, "detail": "stopped"}})})
    run = run_sf(capsys, "stop", "1", "--wait")
    assert run.code == 0 and "1 ok, 0 failed" in run.out


@pytest.mark.parametrize("state, message", [("failed", "task t1 failed: RuntimeError: docker is gone"),
                                            ("cancelled", "task t1 was cancelled")])
def test_failed_or_cancelled_task_exits_one(home, stub, capsys, state, message):
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="running")}))
    stub.on("GET", "/api/tasks/t1", {"task": task(state=state, error="RuntimeError: docker is gone")})
    run = run_sf(capsys, "restart", "1", "--wait")
    assert run.code == 1 and message in run.err


def test_no_wait_prints_the_task_id_and_returns_at_once(home, stub, capsys):
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="queued")}))
    run = run_sf(capsys, "start", "all", "--no-wait")
    assert run.code == 0 and run.out == "t1\n" and "sf tasks t1 --wait" in run.err
    assert stub.calls("GET") == []


def test_wait_defaults_to_no_when_stdout_is_not_a_terminal(home, stub, capsys):
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="queued")}))
    run = run_sf(capsys, "start", "all")          # captured stdout is not a tty
    assert run.code == 0 and run.out == "t1\n"


def test_json_with_wait_prints_the_final_task_json(home, stub, capsys):
    final = {"task": task(state="done", results={"1": {"ok": False, "detail": "no"}})}
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="queued")}))
    stub.on("GET", "/api/tasks/t1", final)
    run = run_sf(capsys, "--json", "start", "1", "--wait")
    assert run.code == 1 and run.json == final
    queued = {"task": task(state="queued")}
    run = run_sf(capsys, "--json", "start", "1", "--no-wait")
    assert run.code == 0 and run.json == queued


def test_ctrl_c_asks_the_daemon_to_cancel_and_exits_130(home, stub, capsys, monkeypatch):
    def interrupted(self, seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(Ctx, "sleep", interrupted)
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="running")}))
    stub.on("POST", "/api/tasks/t1/cancel", {"cancelled": True})
    run = run_sf(capsys, "start", "all", "--wait")
    assert run.code == 130
    assert len(stub.calls("POST", "/api/tasks/t1/cancel")) == 1
    assert "asked the daemon to cancel task t1" in run.err


def test_progress_line_shape():
    from superfarm.cli.common import progress_line
    line = progress_line(task(state="running", current=3, total=10, title="Start Androids", started=100.0), now=165.0)
    assert line == "[###.......] 3/10  Start Androids  1m05s"
    line = progress_line({"title": "Build image", "message": "step 4/9\n pulling", "state": "running", "started": 0}, now=5)
    assert line == "Build image: step 4/9 pulling"


def test_daemon_dying_mid_follow_is_one_clear_error(home, stub, capsys):
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="running")}))
    stub.on("GET", "/api/tasks/t1", (500, {"error": {"code": "internal", "message": "internal error"}}))
    run = run_sf(capsys, "start", "1", "--wait")
    assert run.code == 1 and run.err == "sf: internal error\n"


@pytest.mark.skipif(sys.platform != "linux", reason="signals to a child process")
def test_real_sigint_cancels_the_task_and_exits_130(tmp_path, monkeypatch, stub):
    """The whole path with a real signal: a separate `sf` process following a task that never ends."""
    make_home(tmp_path, monkeypatch, port=stub.port)
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="running")}))
    stub.on("GET", "/api/tasks/t1", {"task": task(state="running", current=1, total=5)})
    stub.on("POST", "/api/tasks/t1/cancel", {"cancelled": True})
    child = subprocess.Popen([sys.executable, "-m", "superfarm.cli.main", "start", "all", "--wait"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        deadline = time.monotonic() + 20
        while not stub.calls("GET", "/api/tasks/t1") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stub.calls("GET", "/api/tasks/t1"), "the CLI never started following"
        child.send_signal(signal.SIGINT)
        out, err = child.communicate(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 130
    assert len(stub.calls("POST", "/api/tasks/t1/cancel")) == 1
    assert "asked the daemon to cancel task t1" in err and "Traceback" not in err


def test_wait_default_follows_the_terminal():
    import io

    from superfarm.cli.render import Output

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    def ctx(stdout, wait):
        return Ctx(type("Args", (), {"wait": wait})(), Output(stdout=stdout, stderr=io.StringIO(), env={}))

    assert ctx(Tty(), None).wait is True and ctx(io.StringIO(), None).wait is False
    assert ctx(Tty(), False).wait is False and ctx(io.StringIO(), True).wait is True


def run_on_a_terminal(*argv: str) -> tuple[int, str]:
    """Run `sf` with a pseudo-terminal as its stdin, stdout and stderr; return (exit code, what was shown)."""
    import pty
    master, slave = pty.openpty()
    child = subprocess.Popen([sys.executable, "-m", "superfarm.cli.main", *argv], stdin=slave, stdout=slave,
                             stderr=slave, close_fds=True, env=dict(os.environ))
    os.close(slave)
    seen = b""
    try:
        while True:
            try:
                block = os.read(master, 65536)
            except OSError:                              # EIO: the child closed its side
                break
            if not block:
                break
            seen += block
    finally:
        os.close(master)
        code = child.wait(timeout=30)
    return code, seen.decode(errors="replace")


@pytest.mark.skipif(sys.platform != "linux", reason="needs a pseudo-terminal")
def test_on_a_real_terminal_a_task_is_followed_with_one_updating_line(tmp_path, monkeypatch, stub):
    """No --wait given: on a terminal `sf start` follows the task by default, redraws one
    progress line in place, then prints the result table in colour."""
    make_home(tmp_path, monkeypatch, port=stub.port)
    monkeypatch.setenv("TERM", "xterm")
    final = task(state="done", current=2, total=2, results={"1": {"ok": True, "detail": "booted"},
                                                            "2": {"ok": False, "detail": "boot timed out"}})
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="running", current=0, total=2)}))
    stub.on("GET", "/api/tasks/t1", [{"task": task(state="running", current=1, total=2)}, {"task": final}])
    code, text = run_on_a_terminal("start", "all")
    assert code == 1                                     # one device failed
    assert text.count("\r\x1b[K") == 3                  # drawn, redrawn in place, cleared: never a second line
    assert "[..........] 0/2 Start Androids" in text and "[#####.....] 1/2 Start Androids" in text
    assert "\n" not in text.split("ANDR")[0]             # nothing but that one line before the table
    assert "\x1b[32mok" in text and "\x1b[31mfailed" in text and "1 ok, 1 failed" in text
    assert "Traceback" not in text


@pytest.mark.skipif(sys.platform != "linux", reason="needs a pseudo-terminal")
def test_no_color_is_honoured_on_a_real_terminal(tmp_path, monkeypatch, stub):
    make_home(tmp_path, monkeypatch, port=stub.port)
    monkeypatch.setenv("TERM", "xterm")
    stub.on("GET", "/api/instances", {"instances": [instance(1, state="booted")]})
    code, text = run_on_a_terminal("ls")
    assert code == 0 and "\x1b[32mbooted" in text
    monkeypatch.setenv("NO_COLOR", "1")
    code, text = run_on_a_terminal("ls")
    assert code == 0 and "booted" in text and "\x1b[" not in text

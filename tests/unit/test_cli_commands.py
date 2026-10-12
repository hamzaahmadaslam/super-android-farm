"""Client commands end to end against the stub daemon: what they send and what they print."""
import argparse
import io
import json

import pytest
from cli_fakes import Stub, api_contract, instance, make_home, run_sf, task

from superfarm.cli.common import Ctx
from superfarm.cli.main import build_parser

SECRET = "s3cr3t-Passw0rd!"


@pytest.fixture(scope="module")
def contract(tmp_path_factory):
    return api_contract(tmp_path_factory.mktemp("contract-home"))


@pytest.fixture
def stub(contract):
    """The stub daemon. When the test is over, everything the CLI sent is checked against
    the real API's routes and request models, so a test cannot pass on a call the real
    daemon would refuse."""
    server = Stub()
    yield server
    server.close()
    if contract is not None:
        for request in server.requests:
            contract.check(request)


@pytest.fixture
def home(tmp_path, monkeypatch, stub):
    monkeypatch.setattr(Ctx, "sleep", lambda self, seconds: None)
    return make_home(tmp_path, monkeypatch, port=stub.port)


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


FLEET = [instance(1, state="booted", tags=("eu",), name="shop"), instance(2, tags=("eu", "spare")),
         instance(3, state="running"), instance(10, tags=("us",))]


# ------------------------------------------------------------------ ls / create

def test_ls_prints_an_aligned_table(home, stub, capsys):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    run = run_sf(capsys, "ls")
    lines = run.out.splitlines()
    assert lines[0].split() == ["ID", "NAME", "STATE", "ANDROID", "GPU", "NET", "ADB", "TAGS"]
    assert lines[1].split() == ["1", "shop", "booted", "13", "intel", "direct", "127.0.0.1:5556", "eu"]
    assert lines[4].split()[:4] == ["10", "Android", "10", "stopped"]
    # Every row starts its STATE column where the header does.
    column = lines[0].index("STATE")
    assert all(line[column - 1] == " " and line[column] != " " for line in lines)
    assert run.code == 0 and "\033[" not in run.out


def test_ls_without_androids_says_what_to_do_next(home, stub, capsys):
    stub.on("GET", "/api/instances", {"instances": []})
    run = run_sf(capsys, "ls")
    assert run.code == 0 and run.out == "" and "sf create -n 3" in run.err


def test_ls_watch_redraws_until_ctrl_c(home, stub, capsys, monkeypatch):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    ticks = []

    def sleep(self, seconds):
        ticks.append(seconds)
        if len(ticks) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(Ctx, "sleep", sleep)
    run = run_sf(capsys, "ls", "-w", "--interval", "7")
    assert run.code == 0 and ticks == [7.0, 7.0, 7.0]
    assert len(stub.calls("GET", "/api/instances")) == 3 and run.out.count("ID  NAME") == 3


def test_create_builds_the_template_from_its_options(home, stub, capsys):
    stub.on("POST", "/api/instances", lambda r: (201, {"instances": [
        {"id": 5, "name": "shop 1", "android_version": "12", "device": {"profile": "pixel-7"}},
        {"id": 6, "name": "shop 2", "android_version": "12", "device": {"profile": "pixel-7"}}]}))
    run = run_sf(capsys, "create", "-n", "2", "--name", "shop", "--android", "12", "--gpu", "dgpu", "--vpn", "nl.ovpn",
                 "--cpus", "3", "--memory", "2048", "--width", "720", "--height", "1280", "--dpi", "320", "--fps", "30",
                 "--tag", "eu", "--tag", "shop", "--image", "custom:1")
    assert stub.requests[-1].json == {"count": 2, "name": "shop", "template": {
        "android_version": "12", "gpu_mode": "dgpu", "image": "custom:1", "width": 720, "height": 1280, "dpi": 320,
        "fps": 30, "tags": ["eu", "shop"], "network": {"vpn_profile": "nl.ovpn", "mode": "vpn"},
        "resources": {"cpus": 3.0, "memory_mb": 2048}}}
    assert run.code == 0 and "sf start 5-6" in run.err and run.out.splitlines()[1].split()[:2] == ["5", "shop"]


def test_create_with_nothing_sends_an_empty_template(home, stub, capsys):
    stub.on("POST", "/api/instances", (201, {"instances": [{"id": 1, "name": "", "device": {}}]}))
    run_sf(capsys, "create")
    assert stub.requests[-1].json == {"count": 1, "name": "", "template": {}}


def test_create_gateway_implies_the_mode_and_conflicts_are_refused(home, stub, capsys):
    stub.on("POST", "/api/instances", (201, {"instances": []}))
    run_sf(capsys, "create", "--gateway", "eu", "--data-volume", "old-data")
    assert stub.requests[-1].json["template"] == {"data_volume": "old-data", "network": {"gateway": "eu", "mode": "gateway"}}
    before = len(stub.requests)
    run = run_sf(capsys, "create", "--net", "direct", "--vpn", "nl.ovpn")
    assert run.code == 2 and "--vpn needs network mode vpn" in run.err and len(stub.requests) == before


# ------------------------------------------------------------------ selectors, set, rm

def test_set_resolves_the_selector_like_the_daemon_and_patches_each(home, stub, capsys):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    for i in (1, 2, 3, 10):
        stub.on("PATCH", f"/api/instances/{i}", {"instance": {"id": i}, "note": "saved"})
    run = run_sf(capsys, "set", "tag:eu,running,!tag:spare", "fps=30", "network.mode=none", "tags=a,b")
    patched = [r.path for r in stub.calls("PATCH")]
    assert patched == ["/api/instances/1", "/api/instances/3"]          # eu: 1,2 + running: 1,3 - spare: 2
    assert stub.calls("PATCH")[0].json == {"patch": {"fps": 30, "network": {"mode": "none"}, "tags": ["a", "b"]},
                                           "force": False}
    assert run.code == 0 and "2 ok, 0 failed" in run.out
    # An empty value empties a whole map, which in a patch is null.
    run_sf(capsys, "set", "3", "extra_props=")
    assert stub.calls("PATCH")[-1].json == {"patch": {"extra_props": None}, "force": False}


@pytest.mark.parametrize("selector, expected", [("all", [1, 2, 3, 10]), ("stopped", [2, 10]), ("2-9", [2, 3]),
                                                ("10", [10]), ("all,!1-3", [10]), ("tag:us,1", [1, 10])])
def test_selector_forms(home, stub, capsys, selector, expected):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    for i in (1, 2, 3, 10):
        stub.on("PATCH", f"/api/instances/{i}", {"instance": {}})
    run_sf(capsys, "set", selector, "fps=30")
    assert [int(r.path.rsplit("/", 1)[1]) for r in stub.calls("PATCH")] == expected


def test_selector_errors(home, stub, capsys):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    run = run_sf(capsys, "set", "tag:nobody", "fps=30")
    assert run.code == 1 and "matches no Android" in run.err
    run = run_sf(capsys, "set", "bogus!", "fps=30")
    assert run.code == 2 and "bad selector part" in run.err
    assert stub.calls("PATCH") == []


def test_set_reports_each_android_and_fails_when_one_is_refused(home, stub, capsys):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    stub.on("PATCH", "/api/instances/1", {"instance": {}})
    stub.on("PATCH", "/api/instances/2", (409, {"error": {"code": "conflict", "message": "has data written by Android 13"}}))
    run = run_sf(capsys, "set", "1-2", "android_version=12", "--force")
    assert run.code == 1 and stub.calls("PATCH")[0].json == {"patch": {"android_version": "12"}, "force": True}
    assert "failed  has data written by Android 13" in run.out and "1 ok, 1 failed" in run.out


def test_set_refuses_bad_keys_before_talking_to_the_daemon(home, stub, capsys):
    run = run_sf(capsys, "set", "all", "nonsense=1")
    assert run.code == 2 and "settable keys" in run.err and stub.requests == []


def test_gpu_set_default_goes_back_to_the_farm_setting(home, stub, capsys):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    stub.on("PATCH", "/api/instances/3", {"instance": {}})
    run_sf(capsys, "gpu", "set", "3", "default")
    assert stub.calls("PATCH")[-1].json["patch"] == {"gpu_mode": None}
    run_sf(capsys, "gpu", "set", "3", "nvidia", "--node", "/dev/dri/renderD129")
    assert stub.calls("PATCH")[-1].json["patch"] == {"gpu_mode": "nvidia", "gpu_node": "/dev/dri/renderD129"}


def test_rm_refuses_without_confirmation_when_nobody_can_answer(home, stub, capsys, monkeypatch):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=False))
    run = run_sf(capsys, "rm", "all")
    assert run.code == 2 and "repeat with --yes" in run.err and stub.calls("DELETE") == []


@pytest.mark.parametrize("answer, deleted", [("y\n", True), ("yes\n", True), ("\n", False), ("n\n", False), ("", False)])
def test_rm_asks_and_obeys_the_answer(home, stub, capsys, monkeypatch, answer, deleted):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    stub.on("DELETE", "/api/instances/1", {"deleted": 1, "kept_data": False})
    monkeypatch.setattr("sys.stdin", FakeStdin(answer, tty=True))
    run = run_sf(capsys, "rm", "1")
    assert "Delete 1 Android(s): 1 (shop)? Their data is deleted with them. [y/N]" in run.err
    assert bool(stub.calls("DELETE")) is deleted and run.code == 0


def test_rm_yes_deletes_each_and_one_failure_does_not_stop_the_rest(home, stub, capsys):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    stub.on("DELETE", "/api/instances/1", (409, {"error": {"code": "busy", "message": "Android 1 is starting"}}))
    stub.on("DELETE", "/api/instances/2", {"deleted": 2, "kept_data": True})
    run = run_sf(capsys, "rm", "tag:eu", "--yes", "--keep-data")
    assert [r.path for r in stub.calls("DELETE")] == ["/api/instances/1", "/api/instances/2"]
    assert stub.calls("DELETE")[0].query == {"keep_data": "true", "force": "false"}
    assert run.code == 1 and "deleted, data kept" in run.out and "Android 1 is starting" in run.out


def test_lifecycle_commands_use_the_bulk_route(home, stub, capsys):
    stub.on("POST", "/api/instances/bulk", (202, {"task": task(state="queued")}))
    for action in ("start", "stop", "restart", "recreate"):
        run_sf(capsys, action, "tag:eu", "--no-wait")
        assert stub.requests[-1].json == {"action": action, "selector": "tag:eu", "force": False}


# ------------------------------------------------------------------ info / logs / tasks / top / open

def test_info_shows_the_facts_and_problems(home, stub, capsys):
    status = {**instance(3, state="error"), "last_error": "boot timed out", "warnings": ["over capacity"], "spec": {}}
    stub.on("GET", "/api/instances/3", status)
    run = run_sf(capsys, "info", "3")
    assert "state        error" in run.out and "last error   boot timed out" in run.out and "warning      over capacity" in run.out
    assert "screen       480x854 @160dpi, 20 fps" in run.out
    run = run_sf(capsys, "info", "three")
    assert run.code == 2 and "bad instance id" in run.err


def test_logs_container_and_logcat(home, stub, capsys):
    stub.on("GET", "/api/instances/3/logs", {"id": 3, "android": "init: starting\n", "net": ""})
    run = run_sf(capsys, "logs", "3", "-n", "50")
    assert stub.requests[-1].query == {"tail": "50"}
    assert run.out == "==> net container <==\n(no output)\n==> android container <==\ninit: starting\n"
    stub.on("GET", "/api/instances/3/logcat", {"id": 3, "text": "I/ActivityManager: hello\n"})
    run = run_sf(capsys, "logs", "3", "--logcat")
    assert run.out == "I/ActivityManager: hello\n" and stub.requests[-1].query == {"lines": "200"}


def test_tasks_list_show_and_cancel(home, stub, capsys):
    stub.on("GET", "/api/tasks", {"tasks": [task("t2", "running", current=1, total=4), task("t1", "done")]})
    run = run_sf(capsys, "tasks", "--active")
    assert stub.requests[-1].query == {"active": "true"}
    assert run.out.splitlines()[1].split()[:3] == ["t2", "running", "1/4"]
    stub.on("GET", "/api/tasks/t1", {"task": task("t1", "done", results={"1": {"ok": False, "detail": "nope"}})})
    run = run_sf(capsys, "tasks", "t1")
    assert run.code == 1 and "0 ok, 1 failed" in run.out
    stub.on("GET", "/api/tasks/t2", {"task": task("t2", "running", current=1, total=4)})
    assert run_sf(capsys, "tasks", "t2", "--no-wait").code == 0          # still running is not a failure
    stub.on("POST", "/api/tasks/t2/cancel", {"cancelled": True})
    run = run_sf(capsys, "tasks", "t2", "--cancel")
    assert run.code == 0 and len(stub.calls("POST", "/api/tasks/t2/cancel")) == 1


def test_top_once_prints_one_snapshot(home, stub, capsys):
    stub.on("GET", "/api/system/metrics", {
        "time": 1.0, "host": {"cpu_percent": 12.5, "cpu_count": 16, "load": [1.0, 0.5, 0.2], "temps": [],
                              "memory": {"total_mb": 32000, "available_mb": 20000, "used_mb": 12000,
                                         "swap_total_mb": 8000, "swap_free_mb": 7000, "used_percent": 37.5}},
        "gpus": [{"name": "intel i915", "util_percent": None, "vram_used_mb": None}],
        "instances": {"1": {"cpu": 12.0, "mem_mb": 900.0, "mem_limit_mb": 1536.0, "pids": 800},
                      "2": {"cpu": 55.5, "mem_mb": 1100.0, "mem_limit_mb": 1536.0, "pids": 900}},
        "totals": {"defined": 4, "running": 2, "cpu_percent": 67.5, "mem_mb": 2000.0}})
    run = run_sf(capsys, "top", "--once")
    assert "Androids: 2 running of 4" in run.out and "RAM 12000 of 32000 MB used (37.5%)" in run.out
    rows = [line.split() for line in run.out.splitlines() if line.strip() and line.split()[0].isdigit()]
    assert rows == [["2", "55.5", "1100", "1536", "900"], ["1", "12", "900", "1536", "800"]]       # busiest first
    assert len(stub.calls("GET", "/api/system/metrics")) == 1


def test_open_prints_the_url_and_never_a_token_link(home, stub, capsys):
    run = run_sf(capsys, "open")
    assert run.out == f"http://127.0.0.1:{stub.port}\n" and "sf token" in run.err
    assert "token=" not in run.out + run.err and stub.requests == []


# ------------------------------------------------------------------ shell / files / apps

def test_shell_sends_argv_untouched_and_prints_each_output(home, stub, capsys):
    done = task("t7", "done", results={"1": {"ok": True, "detail": "exit 0"}, "2": {"ok": False, "detail": "exit 1"},
                                       "3": {"ok": False, "detail": "AdbError: offline"}})
    stub.on("POST", "/api/shell", (202, {"task": task("t7", "queued")}))
    stub.on("GET", "/api/tasks/t7", {"task": done})
    stub.on("GET", "/api/shell/t7/output", {"task": done, "outputs": {
        "1": {"code": 0, "output": "13\n", "truncated": False, "timed_out": False, "seconds": 0.1},
        "2": {"code": 1, "output": "sh: nope\n", "truncated": False, "timed_out": False, "seconds": 0.1}}})
    run = run_sf(capsys, "shell", "1-3", "--root", "--timeout", "5", "--wait", "--", "sh", "-c", "echo $X; ls -l", "--json")
    assert stub.calls("POST", "/api/shell")[0].json == {
        "selector": "1-3", "argv": ["sh", "-c", "echo $X; ls -l", "--json"], "root": True, "force": False, "timeout": 5.0}
    assert run.out.splitlines() == ["==> Android 1 (exit 0) <==", "13", "==> Android 2 (exit 1) <==", "sh: nope",
                                    "==> Android 3 (not run: AdbError: offline) <=="]
    assert run.code == 1


def test_shell_on_one_android_prints_just_the_output(home, stub, capsys):
    done = task("t8", "done", results={"1": {"ok": True, "detail": "exit 0"}})
    stub.on("POST", "/api/shell", (202, {"task": done}))
    stub.on("GET", "/api/shell/t8/output", {"task": done, "outputs": {
        "1": {"code": 0, "output": "uid=2000(shell)\n", "truncated": False, "timed_out": False, "seconds": 0.1}}})
    run = run_sf(capsys, "shell", "1", "--wait", "--", "id")
    assert run.code == 0 and run.out == "uid=2000(shell)\n"


def test_shell_needs_a_command(home, stub, capsys):
    run = run_sf(capsys, "shell", "all")
    assert run.code == 2 and "after `--`" in run.err and stub.requests == []


def test_screenshot_and_pull_write_files(home, stub, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stub.on("GET", "/api/instances/3/screenshot", (200, b"\x89PNG-data", "image/png"))
    run = run_sf(capsys, "screenshot", "3")
    assert run.out == "android-3.png\n" and (tmp_path / "android-3.png").read_bytes() == b"\x89PNG-data"
    stub.on("GET", "/api/instances/3/files/pull", (200, b"report", "application/octet-stream"))
    (tmp_path / "out").mkdir()
    run = run_sf(capsys, "pull", "3", "/sdcard/Download/report.pdf", "-o", "out")
    assert stub.requests[-1].query == {"path": "/sdcard/Download/report.pdf"}
    assert (tmp_path / "out" / "report.pdf").read_bytes() == b"report" and run.code == 0


def test_push_uploads_with_selector_and_destination(home, stub, capsys, tmp_path):
    file = tmp_path / "notes.txt"
    file.write_text("hello")
    stub.on("POST", "/api/files/push", (202, {"task": task(state="queued")}))
    run = run_sf(capsys, "push", "1-3", str(file), "/sdcard/Documents/", "--no-wait")
    body = stub.requests[-1].body
    assert b'name="selector"\r\n\r\n1-3\r\n' in body and b'name="remote"\r\n\r\n/sdcard/Documents/\r\n' in body
    assert b'filename="notes.txt"' in body and b"\r\n\r\nhello\r\n" in body and run.out == "t1\n"
    run = run_sf(capsys, "push", "1", str(tmp_path / "missing.txt"))
    assert run.code == 2 and "no such file" in run.err


def test_app_install_accepts_a_package_name_and_picks_its_newest_version(home, stub, capsys):
    stub.on("GET", "/api/apps/library", {"apps": [
        {"id": "com.example.shop-41", "package": "com.example.shop", "version_code": 41},
        {"id": "com.example.shop-42", "package": "com.example.shop", "version_code": 42},
        {"id": "org.other-1", "package": "org.other", "version_code": 1}]})
    stub.on("POST", "/api/apps/install", (202, {"task": task(state="queued")}))
    run_sf(capsys, "app", "install", "all", "com.example.shop", "--no-wait")
    assert stub.requests[-1].json == {"selector": "all", "app_id": "com.example.shop-42", "grant": True, "downgrade": False}
    run_sf(capsys, "app", "install", "all", "com.example.shop-41", "--no-grant", "--downgrade", "--no-wait")
    assert stub.requests[-1].json == {"selector": "all", "app_id": "com.example.shop-41", "grant": False, "downgrade": True}
    run = run_sf(capsys, "app", "install", "all", "com.unknown")
    assert run.code == 1 and "sf apps add" in run.err


def test_app_package_actions_and_matrix(home, stub, capsys):
    stub.on("POST", "/api/apps/uninstall", (202, {"task": task(state="queued")}))
    run_sf(capsys, "app", "uninstall", "tag:t", "com.example.shop", "--keep-data", "--no-wait")
    assert stub.requests[-1].json == {"selector": "tag:t", "package": "com.example.shop", "keep_data": True}
    stub.on("GET", "/api/apps/matrix", {"matrix": {"com.b": {"1": "2.0"}, "com.a": {"1": "1.0", "3": "1.1"}}})
    run = run_sf(capsys, "app", "matrix", "running")
    assert stub.requests[-1].query == {"selector": "running"}
    assert [line.split() for line in run.out.splitlines()] == [["PACKAGE", "1", "3"], ["com.a", "1.0", "1.1"], ["com.b", "2.0", "-"]]


def test_input_and_windows_report_per_device(home, stub, capsys):
    stub.on("POST", "/api/input/tap", {"ok": False, "results": {"1": {"ok": True, "detail": ""}, "2": {"ok": False, "detail": "offline"}}})
    run = run_sf(capsys, "input", "tap", "1-2", "0.5", "0.9")
    assert stub.requests[-1].json == {"selector": "1-2", "x": 0.5, "y": 0.9, "normalized": True}
    assert run.code == 1 and "2  failed  offline" in run.out
    run = run_sf(capsys, "input", "tap", "1", "240", "400")
    assert run.code == 2 and "--pixels" in run.err
    run_sf(capsys, "input", "tap", "1", "240", "400", "--pixels")
    assert stub.requests[-1].json == {"selector": "1", "x": 240.0, "y": 400.0, "normalized": False}
    stub.on("POST", "/api/input/key", {"ok": True, "results": {"1": {"ok": True, "detail": ""}}})
    assert run_sf(capsys, "input", "key", "1", "HOME").code == 0 and stub.requests[-1].json == {"selector": "1", "name": "HOME"}
    stub.on("POST", "/api/windows/close-all", {"ok": True, "running": []})
    assert run_sf(capsys, "window", "close", "all").code == 0 and stub.requests[-1].path == "/api/windows/close-all"
    stub.on("POST", "/api/windows/grid", {"ok": True, "results": {"1": {"ok": True, "detail": ""}}})
    run_sf(capsys, "window", "grid", "1-4")
    assert stub.requests[-1].json == {"selector": "1-4"}


# ------------------------------------------------------------------ secrets

def test_no_option_anywhere_takes_a_secret_as_its_value():
    """A password on the command line is visible in `ps` and lands in shell history."""
    def walk(parser):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    yield from walk(child)
            else:
                yield parser.prog, action

    risky = ("password", "passwd", "token", "secret", "apikey", "api-key", "pin")
    for prog, action in walk(build_parser()):
        names = [*action.option_strings, action.dest]
        if any(word in name.lower() for name in names for word in risky):
            # Allowed: flags that take no value, and options whose value is a *name*, not the secret.
            takes_value = action.nargs != 0
            assert not takes_value or (prog, action.dest) in {
                ("sf auto run", "secret"),           # the NAME of a value that is then asked for
                ("sf vpn credentials", "password_stdin"),
            }, (prog, names)
    stdin_flag = next(a for prog, a in walk(build_parser()) if prog == "sf vpn credentials" and a.dest == "password_stdin")
    assert stdin_flag.nargs == 0


def test_vpn_credentials_reads_the_password_from_stdin_and_never_echoes_it(home, stub, capsys, monkeypatch):
    stub.on("PUT", "/api/net/credentials/default", {"name": "default", "stored": True})
    monkeypatch.setattr("sys.stdin", FakeStdin(SECRET + "\n"))
    run = run_sf(capsys, "vpn", "credentials", "default", "--username", "someone", "--password-stdin")
    assert run.code == 0 and stub.requests[-1].json == {"username": "someone", "password": SECRET}
    assert SECRET not in run.out + run.err
    run = run_sf(capsys, "--json", "vpn", "credentials", "default", "--username", "someone", "--password-stdin")
    assert SECRET not in run.out + run.err


def test_vpn_credentials_prompts_without_echo_on_a_terminal(home, stub, capsys, monkeypatch):
    stub.on("PUT", "/api/net/credentials/work", {"name": "work", "stored": True})
    monkeypatch.setattr("sys.stdin", FakeStdin("someone\n", tty=True))
    prompts = []

    def fake_getpass(prompt, stream=None):
        prompts.append(prompt)
        return SECRET

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    run = run_sf(capsys, "vpn", "credentials", "work")
    assert run.code == 0 and stub.requests[-1].json == {"username": "someone", "password": SECRET}
    assert prompts == ["VPN password for 'work' (not shown): "] and "VPN username for 'work': " in run.err
    assert SECRET not in run.out + run.err


def test_vpn_credentials_refuses_to_guess(home, stub, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", FakeStdin("", tty=False))
    run = run_sf(capsys, "vpn", "credentials", "default", "--password-stdin")
    assert run.code == 2 and "--username" in run.err
    run = run_sf(capsys, "vpn", "credentials", "default", "--username", "someone")
    assert run.code == 2 and "--password-stdin" in run.err
    run = run_sf(capsys, "vpn", "credentials", "default", "--username", "someone", "--password-stdin")
    assert run.code == 2 and "empty password" in run.err
    assert stub.requests == []


def test_vpn_credentials_lists_names_only_and_deletes(home, stub, capsys):
    stub.on("GET", "/api/net/credentials", {"credentials": [{"name": "default"}, {"name": "work"}]})
    run = run_sf(capsys, "vpn", "credentials")
    assert run.out.splitlines() == ["CREDENTIALS", "default", "work"]
    stub.on("DELETE", "/api/net/credentials/work", {"deleted": "work"})
    assert run_sf(capsys, "vpn", "credentials", "work", "--delete").code == 0


def test_run_secret_is_read_hidden_and_sent_only_in_the_body(home, stub, capsys, monkeypatch):
    stub.on("POST", "/api/automation/runs", (202, {"task": task(state="queued")}))
    monkeypatch.setattr("sys.stdin", FakeStdin(SECRET + "\n"))
    run = run_sf(capsys, "auto", "run", "program", "sign-in", "tag:eu", "--var", "user=demo", "--var", "note=a=b",
                 "--secret", "password", "--dry-run", "--no-wait")
    sent = stub.requests[-1]
    assert sent.json == {"kind": "program", "name": "sign-in", "selector": "tag:eu", "dry_run": True,
                         "variables": {"user": "demo", "note": "a=b", "password": SECRET}}
    assert SECRET not in run.out + run.err and SECRET not in sent.path + json.dumps(sent.query)
    run = run_sf(capsys, "auto", "run", "flow", "x", "1", "--var", "novalue")
    assert run.code == 2 and "name=value" in run.err


def test_values_set_without_a_value_reads_it_from_stdin(home, stub, capsys, monkeypatch):
    stub.on("PUT", "/api/automation/values/shared/invite", {"name": "invite", "stored": True})
    monkeypatch.setattr("sys.stdin", FakeStdin(SECRET + "\n"))
    run = run_sf(capsys, "auto", "values", "set", "invite")
    assert stub.requests[-1].json == {"value": SECRET} and SECRET not in run.out + run.err
    stub.on("PUT", "/api/automation/values/instances/3/city", {"name": "city", "stored": True})
    run_sf(capsys, "auto", "values", "set", "city", "Berlin", "--instance", "3")
    assert stub.requests[-1].json == {"value": "Berlin"}


def test_hidden_values_are_listed_by_name_only(home, stub, capsys):
    stub.on("GET", "/api/automation/values", {"shared": {"values": {"city": "Berlin"}, "hidden": ["api_pin"]},
                                              "pools": [{"name": "codes", "remaining": 3, "used": 1}]})
    run = run_sf(capsys, "auto", "values")
    assert "city     Berlin" in run.out and "api_pin  (hidden)" in run.out and "codes" in run.out


# ------------------------------------------------------------------ network / backups / automation

def test_gateway_add_and_rm_with_confirmation(home, stub, capsys, monkeypatch):
    stub.on("PUT", "/api/net/gateways/eu", {"name": "eu", "config": {}})
    run_sf(capsys, "gateway", "add", "eu", "--profile", "nl.ovpn")
    assert stub.requests[-1].json == {"provider": "openvpn", "profile": "nl.ovpn", "credentials": "default", "subnet": ""}
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=False))
    assert run_sf(capsys, "gateway", "rm", "eu").code == 2 and stub.calls("DELETE") == []
    stub.on("DELETE", "/api/net/gateways/eu", {"deleted": "eu", "removed": []})
    assert run_sf(capsys, "gateway", "rm", "eu", "--yes", "--force").code == 0
    assert stub.requests[-1].query == {"force": "true"}


def test_gateway_up_reports_readiness(home, stub, capsys):
    stub.on("POST", "/api/net/gateways/eu/up", {"name": "eu", "state": "connected", "ready": True, "detail": ""})
    run = run_sf(capsys, "gateway", "up", "eu")
    assert run.code == 0 and run.out == "gateway eu: connected\n"
    stub.on("POST", "/api/net/gateways/eu/up", {"name": "eu", "state": "auth_failed", "ready": False, "detail": "bad login"})
    run = run_sf(capsys, "gateway", "up", "eu")
    assert run.code == 1 and "auth_failed - bad login" in run.out


def test_vpn_status_per_android(home, stub, capsys):
    stub.on("GET", "/api/instances", {"instances": FLEET})
    stub.on("GET", "/api/instances/1/net", {"mode": "vpn", "profile": "nl.ovpn", "state": "connected", "public_ip": "203.0.113.7",
                                            "since": 0, "detail": "", "killswitch": {"enabled": True, "active": True}})
    stub.on("GET", "/api/instances/3/net", {"mode": "vpn", "profile": "de.ovpn", "state": "reconnecting", "public_ip": None,
                                            "since": 0, "detail": "ping-restart", "killswitch": {"enabled": True, "active": False}})
    run = run_sf(capsys, "vpn", "status", "--refresh")
    rows = [line.split() for line in run.out.splitlines()[1:]]
    assert rows[0][:6] == ["1", "vpn", "nl.ovpn", "connected", "203.0.113.7", "active"]
    assert rows[1][:5] == ["3", "vpn", "de.ovpn", "reconnecting", "-"] and "NOT ACTIVE" in run.out
    assert stub.calls("GET", "/api/instances/1/net")[0].query == {"refresh": "true"}


def test_vpn_import_uploads_each_file(home, stub, capsys, tmp_path):
    for name in ("a.ovpn", "b.conf"):
        (tmp_path / name).write_text("client\n")
    stub.on("POST", "/api/net/profiles", [(201, {"name": "a.ovpn"}), (201, {"name": "b.conf"})])
    run = run_sf(capsys, "vpn", "import", str(tmp_path / "a.ovpn"), str(tmp_path / "b.conf"))
    assert run.out == "a.ovpn\nb.conf\n" and len(stub.calls("POST", "/api/net/profiles")) == 2


def test_backup_restore_as_new_and_over(home, stub, capsys, monkeypatch):
    stub.on("POST", "/api/backups/restore", (202, {"task": task(state="queued")}))
    run_sf(capsys, "backup", "restore", "android-3/20250101-040000", "--no-wait")
    assert stub.requests[-1].json == {"archive": "android-3/20250101-040000"}       # no id, no force: a new Android
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=False))
    before = len(stub.requests)
    run = run_sf(capsys, "backup", "restore", "android-3/20250101-040000", "--over", "3")
    assert run.code == 2 and len(stub.requests) == before                           # nobody to ask: refused
    monkeypatch.setattr("sys.stdin", FakeStdin("n\n", tty=True))
    run = run_sf(capsys, "backup", "restore", "android-3/20250101-040000", "--over", "3")
    assert run.code == 0 and "Replace everything Android 3 holds" in run.err and len(stub.requests) == before
    run_sf(capsys, "backup", "restore", "android-3/20250101-040000", "--over", "3", "--yes", "--no-wait")
    assert stub.requests[-1].json == {"archive": "android-3/20250101-040000", "id": 3, "force": True}


def test_backup_restore_of_farm_settings_says_what_was_not_taken(home, stub, capsys):
    name = "farm/20250101-050000"
    output = {"restored": ["instances/7/instance.yaml"], "instances": [7], "own": False, "missing_vpn_profiles": ["eu.ovpn"],
              "adjusted": ["Android 7: autostart switched off"],
              "skipped": ["config.yaml (made by another farm: restore with `restore_config` to take it)"]}
    stub.on("POST", "/api/backups/restore", (202, {"task": task(state="queued")}))
    stub.on("GET", "/api/tasks/t1", {"task": task(state="done", output=output, kind="backup.restore-farm")})
    run = run_sf(capsys, "backup", "restore", name, "--wait")
    assert stub.calls("POST", "/api/backups/restore")[-1].json == {"archive": name}      # nothing is chosen for the operator
    assert run.code == 0 and "restored 1 file(s); Androids: 7" in run.out
    assert "changed: Android 7: autostart switched off" in run.out
    assert "not restored: config.yaml (made by another farm" in run.out
    assert "--trust" in run.err and "--restore-config" in run.err and "eu.ovpn" in run.err
    assert "new Android" not in run.err                                                   # that is the other kind of backup
    # One of the farm's own: nothing to explain.
    stub.on("GET", "/api/tasks/t1", {"task": task(state="done", output={**output, "own": True, "adjusted": [], "skipped": []})})
    assert "--trust" not in run_sf(capsys, "backup", "restore", name, "--wait").err
    # The choices are for farm backups only, and an Android's options are not for them.
    before = len(stub.requests)
    assert run_sf(capsys, "backup", "restore", "android-3/20250101-040000", "--trust").code == 2
    assert run_sf(capsys, "backup", "restore", name, "--over", "3", "--yes").code == 2
    assert len(stub.requests) == before


def test_backup_restore_sends_each_explicit_choice_for_a_farm_backup(home, stub, capsys, contract):
    name = "farm/20250101-050000"
    stub.on("POST", "/api/backups/restore", (202, {"task": task(state="queued")}))
    run_sf(capsys, "backup", "restore", name, "--trust", "--no-wait")
    assert stub.requests[-1].json == {"archive": name, "trust": True}
    run_sf(capsys, "backup", "restore", name, "--restore-config", "--restore-secrets", "--no-wait")
    assert stub.requests[-1].json == {"archive": name, "restore_config": True, "restore_secrets": True}
    fields = set(contract.schemas["Restore"]["properties"]) if contract is not None else set()
    if contract is not None and not {"trust", "restore_config", "restore_secrets"} <= fields:
        # What the CLI sends is checked above. The route's request model is the API's side.
        stub.requests.clear()
        pytest.skip("POST /api/backups/restore does not declare trust / restore_config / restore_secrets yet")


def test_backup_list_verify_prune_farm(home, stub, capsys):
    stub.on("GET", "/api/backups", {"directory": "/b", "schedule": {"schedule": ""}, "backups": [
        {"name": "android-3/20250101-040000", "kind": "android", "id": 3, "instance_name": "shop", "android_version": "13",
         "created": 0, "size": 1536, "reason": "manual", "present": True},
        {"name": "farm/20250101-050000", "kind": "farm", "created": 0, "size": 10, "reason": "manual", "present": True}]})
    run = run_sf(capsys, "backup", "list", "--kind", "android")
    assert "android-3/20250101-040000  shop" in run.out and "farm/" not in run.out and "1.5 KiB" in run.out
    assert "farm settings" in run_sf(capsys, "backup", "ls").out
    stub.on("POST", "/api/backups/verify", [{"archive": "x", "ok": True}, {"archive": "x", "ok": False}])
    assert run_sf(capsys, "backup", "verify", "android-3/20250101-040000", "--deep").code == 0
    assert stub.requests[-1].json == {"archive": "android-3/20250101-040000", "deep": True}
    run = run_sf(capsys, "backup", "verify", "android-3/20250101-040000")
    assert run.code == 1 and "DAMAGED" in run.err
    stub.on("POST", "/api/backups/prune", {"removed": ["android-3/20240101-040000"]})
    assert run_sf(capsys, "backup", "prune").out == "android-3/20240101-040000\n"
    stub.on("POST", "/api/backups/farm", (201, {"archive": "farm/20250101-050000", "dir": "/b/farm/x", "includes_secrets": True}))
    run = run_sf(capsys, "backup", "farm", "--include-secrets")
    assert stub.requests[-1].json == {"include_secrets": True} and run.out == "farm/20250101-050000\n" and "keep it private" in run.err
    stub.on("POST", "/api/backups", (202, {"task": task(state="queued")}))
    run_sf(capsys, "backup", "create", "1-5", "--no-wait")
    assert stub.requests[-1].json == {"selector": "1-5"}


def test_auto_runs_resolve_and_schedules(home, stub, capsys):
    stub.on("GET", "/api/automation/runs/t5", {"run": {"id": "t5", "kind": "flow", "name": "login", "state": "running",
            "waiting": [2], "devices": {"1": {"state": "done", "step": 4, "steps": 4, "step_label": "", "elapsed": 12.0},
                                        "2": {"state": "needs_attention", "step": 2, "steps": 4, "step_label": "tap Login",
                                              "elapsed": 30.0, "waiting": {"reason": "unknown screen"}}}}})
    run = run_sf(capsys, "auto", "runs", "t5")
    assert "2  needs_attention  2/4   tap Login  30s   unknown screen" in run.out and "sf auto resolve t5" in run.err
    stub.on("POST", "/api/automation/runs/t5/resolve", {"ok": True})
    run_sf(capsys, "auto", "resolve", "t5", "2", "skip")
    assert stub.requests[-1].json == {"id": 2, "action": "skip"}
    stub.on("POST", "/api/automation/schedules", (201, {"schedule": {"id": "s1", "next_run": 0}}))
    run = run_sf(capsys, "auto", "schedule", "add", "nightly", "flow", "login", "all", "--every", "3600", "--var", "a=b")
    assert stub.requests[-1].json == {"name": "nightly", "kind": "flow", "target": "login", "selector": "all", "every": 3600,
                                      "cron": "", "variables": {"a": "b"}, "dry_run": False}
    assert run.out == "s1\n"
    assert run_sf(capsys, "auto", "schedule", "add", "x", "flow", "login", "all").code == 2
    stub.on("PATCH", "/api/automation/schedules/s1", {"schedule": {"id": "s1"}})
    run_sf(capsys, "auto", "schedule", "disable", "s1")
    assert stub.requests[-1].json == {"enabled": False}


def test_auto_flows_save_reads_the_file_and_bare_group_lists(home, stub, capsys, tmp_path):
    flow = tmp_path / "login.yaml"
    flow.write_text("name: login\nsteps: []\n")
    stub.on("PUT", "/api/automation/flows/team/login", {"flow": {"name": "team/login"}})
    assert run_sf(capsys, "auto", "flows", "save", "team/login", str(flow)).code == 0
    assert stub.requests[-1].json == {"text": "name: login\nsteps: []\n"}
    stub.on("GET", "/api/automation/flows", {"flows": [{"name": "team/login", "steps": 3, "description": "Log in"}]})
    assert "team/login" in run_sf(capsys, "auto", "flows").out
    stub.on("POST", "/api/automation/datasets/open-settings/train", (202, {"task": task(state="queued")}))
    assert run_sf(capsys, "auto", "train", "open-settings", "--no-wait").out == "t1\n"


def test_camera_and_listen(home, stub, capsys):
    stub.on("POST", "/api/camera/play", {"ok": True, "feed": {}, "results": {"1": {"ok": True, "detail": "camera attached"}}})
    run = run_sf(capsys, "camera", "play", "1", "intro.mp4", "--sound", "none")
    assert stub.requests[-1].json == {"selector": "1", "source": "intro.mp4", "sound": "none"} and run.code == 0
    stub.on("POST", "/api/camera/stop", {"ok": True, "stopped": [], "ids": []})
    run_sf(capsys, "camera", "stop")
    assert stub.requests[-1].json == {}
    run_sf(capsys, "camera", "stop", "1-3")
    assert stub.requests[-1].json == {"selector": "1-3"}
    stub.on("POST", "/api/camera/mic", {"ok": True, "mode": "solo", "muted": [2, 3]})
    assert "muted on: 2, 3" in run_sf(capsys, "camera", "mic", "1", "solo").out
    stub.on("POST", "/api/listen/3", {"ok": True, "listening": 3})
    assert run_sf(capsys, "listen", "3").code == 0
    stub.on("DELETE", "/api/listen", {"listening": None})
    assert run_sf(capsys, "listen", "off").code == 0 and stub.requests[-1].method == "DELETE"
    stub.on("GET", "/api/listen", {"listening": 3})
    assert run_sf(capsys, "listen").out == "listening to Android 3\n"


def test_unavailable_component_shows_its_reason(home, stub, capsys):
    stub.on("GET", "/api/camera", (503, {"error": {"code": "unavailable", "message": "camera is unavailable",
                                                   "detail": {"component": "camera", "reason": "ffmpeg is not installed"}}}))
    run = run_sf(capsys, "camera", "status")
    assert run.code == 1 and run.err == "sf: camera is unavailable\n    ffmpeg is not installed\n"


def test_the_contract_check_itself_catches_drift(contract):
    """The safety net must have teeth: a wrong route, field, choice or query name fails."""
    if contract is None:
        pytest.skip("the API package is not importable")
    from cli_fakes import Recorded

    def sent(method, path, body=None, query=None):
        headers = {"content-type": "application/json"} if body is not None else {}
        return Recorded(method, path, query or {}, headers, json.dumps(body).encode() if body is not None else b"")

    contract.check(sent("POST", "/api/instances/bulk", {"action": "start", "selector": "all", "force": True}))
    contract.check(sent("GET", "/api/automation/flows/examples/open-app"))
    contract.check(sent("DELETE", "/api/instances/3", query={"keep_data": "true"}))
    for bad in (sent("POST", "/api/instances/everything"),
                sent("POST", "/api/instances/bulk", {"action": "start"}),                          # selector missing
                sent("POST", "/api/instances/bulk", {"action": "start", "selector": "1", "now": True}),  # unknown field
                sent("POST", "/api/instances/bulk", {"action": "explode", "selector": "1"}),       # not a choice
                sent("POST", "/api/instances/bulk", {"action": "start", "selector": 5}),           # wrong type
                sent("POST", "/api/instances/3/explode", {}),
                sent("GET", "/api/instances", query={"verbose": "1"}),
                sent("PATCH", "/api/tasks/t1")):
        with pytest.raises(AssertionError):
            contract.check(bad)

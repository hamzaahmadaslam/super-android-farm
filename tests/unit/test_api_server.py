"""The daemon: logging, the ticker, what it refuses to start with, and one real run on a
loopback port (start, serve, tick, stop, close)."""
from __future__ import annotations

import logging
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import httpx
import pytest

from superfarm.api import server as server_mod
from superfarm.api.auth import read_token
from superfarm.api.server import ConfigError, Daemon, Ticker, check_listen, serve, setup_logging, tls_files
from superfarm.config import Config
from superfarm.paths import get_paths


@pytest.fixture
def clean_logging():
    """The daemon configures the root logger; put it back the way pytest had it."""
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield
    for handler in [h for h in root.handlers if h not in before]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)


def free_port() -> int:
    for port in range(17750, 17800):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 17750-17799")


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ------------------------------------------------------------------------------ logging

def test_logging_goes_to_a_rotating_file_at_the_configured_level(tmp_path, clean_logging):
    config = Config.model_validate({"logging": {"level": "warning", "max_mb": 3, "keep": 4}})
    setup_logging(config, tmp_path / "logs", stderr=False)
    logging.getLogger("superfarm.test").info("not this one")
    logging.getLogger("superfarm.test").warning("disk is nearly full")
    text = (tmp_path / "logs" / "daemon.log").read_text()
    assert "disk is nearly full" in text and "not this one" not in text and "WARNING" in text
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_superfarm_daemon", False)]
    assert len(ours) == 1 and isinstance(ours[0], RotatingFileHandler)
    assert ours[0].maxBytes == 3 * 1024 * 1024 and ours[0].backupCount == 4


def test_setting_up_logging_twice_does_not_double_the_lines(tmp_path, clean_logging):
    for _ in range(3):
        setup_logging(Config(), tmp_path / "logs", stderr=False)
    logging.getLogger("superfarm.test").error("once")
    assert (tmp_path / "logs" / "daemon.log").read_text().count("once") == 1
    assert sum(1 for h in logging.getLogger().handlers if getattr(h, "_superfarm_daemon", False)) == 1


# ------------------------------------------------------------------------------- ticker

def test_ticker_keeps_ticking_after_a_failure(caplog):
    calls: list[str] = []

    def tick():
        calls.append("tick")
        if len(calls) == 2:
            raise RuntimeError("schedules file is corrupt")

    ticker = Ticker(tick, 0.01, first=lambda: calls.append("first"))
    ticker.start()
    ticker.start()                                   # idempotent
    assert wait_for(lambda: len(calls) >= 6)
    ticker.stop()
    assert calls[0] == "first" and calls.count("first") == 1 and calls.count("tick") >= 5
    assert "tick failed" in caplog.text and "schedules file is corrupt" in caplog.text
    seen = len(calls)
    time.sleep(0.05)
    assert len(calls) == seen                        # stopped means stopped


def test_a_failing_startup_job_does_not_stop_the_ticker(caplog):
    ticks: list[int] = []
    ticker = Ticker(lambda: ticks.append(1), 0.01, first=lambda: 1 / 0)
    ticker.start()
    assert wait_for(lambda: len(ticks) >= 2)
    ticker.stop()
    assert "startup job failed" in caplog.text


def test_tick_interval_is_thirty_seconds():
    assert server_mod.TICK_SECONDS == 30.0


def test_the_daemon_starts_what_is_marked_autostart_once_at_start(tmp_path, clean_logging, caplog):
    daemon = Daemon(tmp_path / "home", port=free_port(), log_to_stderr=False)
    assert daemon.ticker.fn == daemon.farm.tick and daemon.ticker.interval == 30.0
    calls: list[str] = []

    class Manager:
        def autostart(self):
            calls.append("autostart")
            return None

    daemon.farm.use("instances", Manager())
    daemon.ticker.first()
    assert calls == ["autostart"]
    daemon.farm.close()


# ----------------------------------------------------------------------- what is refused

def test_tls_needs_both_files_and_both_must_exist(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    assert tls_files(Config()) is None
    with pytest.raises(ConfigError, match="must be set together"):
        tls_files(Config.model_validate({"server": {"tls_cert": str(cert)}}))
    both = Config.model_validate({"server": {"tls_cert": str(cert), "tls_key": str(key)}})
    with pytest.raises(ConfigError, match="not a readable file"):
        tls_files(both)
    cert.write_text("x")
    key.write_text("x")
    assert tls_files(both) == (str(cert), str(key))


def test_a_host_override_cannot_get_around_the_auth_rule():
    open_farm = Config.model_validate({"server": {"auth_required": False}})
    check_listen(open_farm, "127.0.0.1")
    check_listen(open_farm, "localhost")
    for host in ("0.0.0.0", "192.168.1.20", "::", "farm.lan"):
        with pytest.raises(ConfigError, match="auth_required"):
            check_listen(open_farm, host)
        check_listen(Config(), host)                 # with authentication on, any address may be chosen


def test_serve_refuses_an_exposed_farm_without_auth_and_binds_nothing(tmp_path, capsys, monkeypatch):
    home = get_paths(tmp_path / "home").ensure()
    home.config.write_text("server:\n  auth_required: false\n")
    monkeypatch.setattr(server_mod.uvicorn, "Server", lambda config: pytest.fail("a server was created"))
    assert serve(host="0.0.0.0", port=17799, home=home.home) == 2
    assert "refusing to listen on 0.0.0.0" in capsys.readouterr().err


def test_serve_reports_an_invalid_config_instead_of_a_traceback(tmp_path, capsys):
    home = get_paths(tmp_path / "home").ensure()
    home.config.write_text("server:\n  host: 0.0.0.0\n  auth_required: false\n")
    assert serve(home=home.home) == 2
    assert "configuration is not valid" in capsys.readouterr().err


def test_serve_reports_half_configured_tls(tmp_path, capsys):
    home = get_paths(tmp_path / "home").ensure()
    home.config.write_text("server:\n  tls_cert: /nonexistent/cert.pem\n")
    assert serve(home=home.home) == 2
    assert "tls_cert and server.tls_key must be set together" in capsys.readouterr().err


# ------------------------------------------------------------------------------ real run

def test_daemon_serves_ticks_and_shuts_down_cleanly(tmp_path, clean_logging):
    port = free_port()
    daemon = Daemon(tmp_path / "home", port=port, tick_seconds=0.05, log_to_stderr=False)
    ticks: list[float] = []

    def tick():
        ticks.append(time.monotonic())
        if len(ticks) == 1:
            raise RuntimeError("first tick breaks")

    daemon.ticker.fn = tick
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(daemon.run()))
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert wait_for(lambda: daemon.server.started), "the server did not start"
        health = httpx.get(f"{base}/api/health")
        assert health.status_code == 200 and health.json()["ok"] is True
        assert "server" not in health.headers                         # no software banner
        assert health.headers["x-content-type-options"] == "nosniff"
        assert httpx.get(f"{base}/api/instances").status_code == 401
        token = read_token(daemon.farm.paths)
        listed = httpx.get(f"{base}/api/instances", headers={"Authorization": f"Bearer {token}"})
        assert listed.status_code == 200 and listed.json() == {"instances": []}
        assert httpx.get(f"{base}/api/health", headers={"Host": "evil.example"}).status_code == 403
        assert httpx.get(f"{base}/").status_code == 200
        assert wait_for(lambda: len(ticks) >= 3)                      # the ticker survived its first failure
    finally:
        daemon.stop()
        thread.join(15)
    assert not thread.is_alive() and result == [0]
    assert daemon.farm._closed
    log = (daemon.farm.paths.logs / "daemon.log").read_text()
    assert "daemon starting" in log and "daemon stopped" in log and "tick failed" in log
    assert token not in log
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2)


def test_a_port_that_is_taken_is_a_failed_start_not_a_hang(tmp_path, clean_logging):
    port = free_port()
    with socket.socket() as taken:
        taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        taken.bind(("127.0.0.1", port))
        taken.listen(1)
        daemon = Daemon(tmp_path / "home", port=port, log_to_stderr=False)
        result: list[int] = []
        thread = threading.Thread(target=lambda: result.append(daemon.run()))
        thread.start()
        thread.join(15)
    assert not thread.is_alive() and result == [1]
    assert daemon.farm._closed


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_the_real_process_in_order_and_closes_the_farm(tmp_path, sig):
    """In its own process, as `sf serve` runs it: a thread cannot receive signals, and the
    point is what happens after uvicorn hands the signal back."""
    port = free_port()
    home = tmp_path / "home"
    program = (
        "import sys\n"
        "from superfarm.api import server\n"
        "from superfarm.farm import Farm\n"
        "close = Farm.close\n"
        "def closing(self):\n"
        "    close(self)\n"
        "    (self.paths.home / 'closed').write_text('yes')\n"
        "Farm.close = closing\n"
        f"sys.exit(server.serve(port={port}, home={str(home)!r}))\n")
    process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def up() -> bool:
            try:
                return httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200
            except httpx.TransportError:
                return False
        assert wait_for(up, 20), "the daemon did not start"
        process.send_signal(sig)
        assert process.wait(20) == 0                         # a clean stop, not death by signal
    finally:
        if process.poll() is None:
            process.kill()
    assert (home / "closed").read_text() == "yes"
    assert "daemon stopped" in (home / "logs" / "daemon.log").read_text()


def test_daemon_speaks_https_when_a_certificate_is_configured(tmp_path, clean_logging):
    if not shutil.which("openssl"):
        pytest.skip("needs the openssl program to make a throwaway certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    home = get_paths(tmp_path / "home").ensure()
    home.config.write_text(f"server:\n  tls_cert: {cert}\n  tls_key: {key}\n")
    port = free_port()
    daemon = Daemon(home.home, port=port, log_to_stderr=False)
    assert daemon.scheme == "https"
    thread = threading.Thread(target=daemon.run)
    thread.start()
    try:
        assert wait_for(lambda: daemon.server.started), "the server did not start"
        trusted = ssl.create_default_context(cafile=str(cert))          # the certificate is checked, not waved through
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=trusted) as client:
            assert client.get("/api/health").json()["ok"] is True
            login = client.post("/api/auth/login", json={"token": read_token(daemon.farm.paths)})
            cookies = login.headers.get_list("set-cookie")
            assert login.status_code == 200 and len(cookies) == 2 and all("; Secure" in c for c in cookies)
            assert client.get("/api/auth/me").json()["method"] == "session"
        with pytest.raises(httpx.TransportError):                     # and plain HTTP is not spoken on that port
            httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2)
    finally:
        daemon.stop()
        thread.join(15)
    assert not thread.is_alive()

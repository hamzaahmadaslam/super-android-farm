"""The daemon: `sf serve` runs this in the foreground.

    serve(host, port, home)  ->  exit code

One process, run as the desktop user: the HTTP API and panel (uvicorn), and a ticker that
calls `Farm.tick()` every 30 seconds for schedules, scheduled backups and camera upkeep.
Stopping it (Ctrl-C, SIGTERM, `sf service stop`) closes what the daemon itself started on
the host. Androids keep running: the daemon manages them, it does not keep them alive.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from ..config import Config
from ..farm import Farm
from .app import create_app
from .auth import LOOPBACK_NAMES

log = logging.getLogger("superfarm.daemon")

TICK_SECONDS = 30.0
SHUTDOWN_GRACE = 5         # seconds open live views and sockets get to end before they are cut
EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_HANDLER_MARK = "_superfarm_daemon"


def setup_logging(config: Config, logs: Path, *, stderr: bool = True) -> None:
    """Log to logs/daemon.log, rotated by size (logging.max_mb, logging.keep), and to stderr
    (which is the journal when run as a service). Safe to call again: it replaces its own
    handlers and leaves any others alone."""
    logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(handler)
        handler.close()
    handlers: list[logging.Handler] = [RotatingFileHandler(
        logs / "daemon.log", maxBytes=config.logging.max_mb * 1024 * 1024, backupCount=config.logging.keep,
        encoding="utf-8")]
    if stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(config.logging.level.upper())
    try:
        os.chmod(logs / "daemon.log", 0o640)
    except OSError:
        pass


class Ticker:
    """Calls `fn` every `interval` seconds on its own thread. A call that raises is logged
    and the next one still happens; `first` runs once before the first wait."""

    def __init__(self, fn, interval: float = TICK_SECONDS, *, first=None):
        self.fn, self.interval, self.first = fn, float(interval), first
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sf-ticker", daemon=True)
            self._thread.start()

    def _call(self, fn, what: str) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - housekeeping must never end the daemon
            log.exception("%s failed", what)

    def _run(self) -> None:
        if self.first is not None:
            self._call(self.first, "startup job")
        while not self._stop.wait(self.interval):
            self._call(self.fn, "tick")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


class ConfigError(Exception):
    pass


def tls_files(config: Config) -> tuple[str, str] | None:
    """(certificate, key) when TLS is configured, None when it is not. Half a pair is an error."""
    cert, key = config.server.tls_cert.strip(), config.server.tls_key.strip()
    if not cert and not key:
        return None
    if not (cert and key):
        raise ConfigError("server.tls_cert and server.tls_key must be set together")
    for label, value in (("server.tls_cert", cert), ("server.tls_key", key)):
        if not Path(value).expanduser().is_file():
            raise ConfigError(f"{label}: {value} is not a readable file")
    return str(Path(cert).expanduser()), str(Path(key).expanduser())


def check_listen(config: Config, host: str) -> None:
    """The config refuses `auth_required: false` on a non-loopback host; a --host given on
    the command line must not get around that."""
    if host not in LOOPBACK_NAMES and not config.server.auth_required:
        raise ConfigError(f"refusing to listen on {host} with server.auth_required off: anyone who can reach "
                          "that address could control the farm. Turn authentication on, or listen on 127.0.0.1")


def _hold_signals():
    """Keep SIGINT / SIGTERM from ending the process before the farm is closed.

    uvicorn handles both itself and, once the server has shut down, raises the signal again
    for whoever was listening before it. Left at the default that means "terminate now",
    which skips everything after server.run(): no Farm.close(), child processes left behind.
    With a listener of our own that does nothing, the second delivery is a plain return and
    run() finishes in order. Returns the function that puts the previous handlers back."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None                  # signals are delivered to the main thread only
    previous = {sig: signal.signal(sig, lambda *_: None) for sig in (signal.SIGINT, signal.SIGTERM)}

    def restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return restore


class Daemon:
    def __init__(self, home: str | os.PathLike | None = None, host: str | None = None, port: int | None = None, *,
                 tick_seconds: float = TICK_SECONDS, log_to_stderr: bool = True):
        self.farm = Farm(home)
        config = self.farm.config
        self.host = host or config.server.host
        self.port = int(port or config.server.port)
        check_listen(config, self.host)
        tls = tls_files(config)
        self.scheme = "https" if tls else "http"
        setup_logging(config, self.farm.paths.logs, stderr=log_to_stderr)
        self.app = create_app(self.farm)
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, host=self.host, port=self.port,
            ssl_certfile=tls[0] if tls else None, ssl_keyfile=tls[1] if tls else None,
            log_config=None,                 # our handlers, set up above
            access_log=False,                # the audit log records what matters, without query strings
            server_header=False, timeout_graceful_shutdown=SHUTDOWN_GRACE))
        self.ticker = Ticker(self.farm.tick, tick_seconds, first=self._autostart)

    def _autostart(self) -> None:
        task = self.farm.instances.autostart()
        if task is not None:
            log.info("autostart: task %s (%s)", task.id, task.title)

    def run(self) -> int:
        """Serve until stopped. Returns the process exit code."""
        log.info("Super Farm daemon starting: %s://%s:%s, home %s", self.scheme,
                 f"[{self.host}]" if ":" in self.host else self.host, self.port, self.farm.paths.home)
        code = EXIT_OK
        restore_signals = _hold_signals()
        self.ticker.start()
        try:
            self.server.run()
            if not self.server.started:
                code = EXIT_FAILED
        except SystemExit:                   # uvicorn exits this way when it cannot bind the port
            code = EXIT_FAILED
        finally:
            self.ticker.stop()
            self.farm.close()
            log.info("Super Farm daemon stopped")
            restore_signals()
        return code

    def stop(self) -> None:
        """Ask a running daemon to shut down (what SIGTERM does)."""
        self.server.should_exit = True


def serve(host: str | None = None, port: int | None = None, home: str | os.PathLike | None = None) -> int:
    """Run the daemon in the foreground. `host` and `port` override server.host / server.port."""
    try:
        daemon = Daemon(home, host, port)
    except ConfigError as e:
        print(f"sf serve: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except ValueError as e:                  # config.yaml does not validate
        print(f"sf serve: the configuration is not valid: {e}", file=sys.stderr)
        return EXIT_CONFIG
    return daemon.run()

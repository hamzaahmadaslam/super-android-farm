"""Talking to one Android over ADB.

Every instance is reached at <adb_bind>:<adb_port> (TCP). All calls are bounded by a
timeout: a busy Android can stall any ADB command, and one stalled device must never
hold up a fleet operation.
"""
from __future__ import annotations

import re
import shlex
import time
from collections.abc import Sequence

from ..util.proc import CommandError, Result, run, which

_SERIAL = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")


class AdbError(RuntimeError):
    pass


def adb_binary() -> str:
    return which("adb") or "adb"


def quote(value: str) -> str:
    """Quote one argument for the *device's* shell (`adb shell` joins its arguments into one line)."""
    return shlex.quote(str(value))


class Adb:
    def __init__(self, serial: str, binary: str | None = None):
        if not _SERIAL.match(serial):
            raise ValueError(f"bad adb serial {serial!r}")
        self.serial = serial
        self.binary = binary or adb_binary()

    @classmethod
    def for_port(cls, port: int, host: str = "127.0.0.1") -> Adb:
        return cls(f"{host}:{int(port)}")

    # ---------------------------------------------------------------- plumbing
    def raw(self, argv: Sequence[str], *, timeout: float = 30, check: bool = True,
            binary: bool = False, input: bytes | None = None) -> Result:
        try:
            return run([self.binary, "-s", self.serial, *argv], timeout=timeout, check=check, binary=binary, input=input)
        except CommandError as e:
            raise AdbError(f"{self.serial}: adb {argv[0]} failed: {e}") from None

    def shell(self, command: str | Sequence[str], *, timeout: float = 30, check: bool = True) -> str:
        """Run a command on the device and return its stdout, stripped.

        Pass a list to have each argument quoted for the device shell. Pass a string only
        for fixed command lines written in this code base, never for anything built from
        user or device input.
        """
        line = command if isinstance(command, str) else " ".join(quote(a) for a in command)
        return self.raw(["shell", line], timeout=timeout, check=check).stdout.replace("\r\n", "\n").strip()

    def exec_out(self, command: Sequence[str], *, timeout: float = 30) -> bytes:
        """Binary-safe stdout of a device command (screenshots, file contents)."""
        line = " ".join(quote(a) for a in command)
        return self.raw(["exec-out", line], timeout=timeout, binary=True).stdout  # type: ignore[return-value]

    # -------------------------------------------------------------- connection
    def connect(self, attempts: int = 5, delay: float = 1.0) -> bool:
        """`adb connect` until the device answers as `device` (not offline / unauthorized)."""
        for i in range(attempts):
            run([self.binary, "connect", self.serial], timeout=15, check=False)
            if self.state() == "device":
                return True
            if i < attempts - 1:
                time.sleep(delay)
        return False

    def disconnect(self) -> None:
        run([self.binary, "disconnect", self.serial], timeout=15, check=False)

    def state(self) -> str:
        r = run([self.binary, "-s", self.serial, "get-state"], timeout=10, check=False)
        return r.out if r.ok else "offline"

    def booted(self) -> bool:
        try:
            return self.shell("getprop sys.boot_completed", timeout=10, check=False) == "1"
        except AdbError:
            return False

    def wait_boot(self, timeout: float = 180, poll: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connect(attempts=1) and self.booted():
                return True
            time.sleep(poll)
        return False

    # ------------------------------------------------------------------ basics
    def getprop(self, name: str) -> str:
        return self.shell(["getprop", name], timeout=10, check=False)

    def setprop(self, name: str, value: str) -> None:
        self.shell(["setprop", name, value], timeout=10)

    def setting_get(self, namespace: str, key: str) -> str:
        return self.shell(["settings", "get", namespace, key], timeout=10, check=False)

    def setting_put(self, namespace: str, key: str, value: str) -> None:
        self.shell(["settings", "put", namespace, key, value], timeout=10)

    def size(self) -> tuple[int, int]:
        m = re.search(r"(\d+)x(\d+)", self.shell("wm size", timeout=10))
        if not m:
            raise AdbError(f"{self.serial}: could not read screen size")
        return int(m.group(1)), int(m.group(2))

    def screencap_png(self, timeout: float = 20) -> bytes:
        data = self.exec_out(["screencap", "-p"], timeout=timeout)
        if not data.startswith(b"\x89PNG"):
            raise AdbError(f"{self.serial}: screencap returned no picture")
        return data

    def current_app(self) -> tuple[str, str]:
        """(package, activity) in the foreground, or ("", "").

        The whole dump is read and searched here rather than piped into `grep -m1` on the
        device: closing a pipe early sends SIGPIPE into the service that is still writing
        its state, which has killed Android services before.
        """
        out = self.shell("dumpsys activity activities", timeout=20, check=False)
        m = re.search(r"(?:topResumedActivity|mResumedActivity)[^\n]*?\s([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", out)
        if not m:
            return "", ""
        pkg, act = m.group(1), m.group(2)
        return pkg, (pkg + act if act.startswith(".") else act)

    # ------------------------------------------------------------------- input
    def tap(self, x: int, y: int) -> None:
        self.shell(["input", "tap", str(int(x)), str(int(y))], timeout=15)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.shell(["input", "swipe", *(str(int(v)) for v in (x1, y1, x2, y2, ms))], timeout=15 + ms / 1000)

    def key(self, name: str | int) -> None:
        code = str(name).upper()
        if not code.isdigit() and not code.startswith("KEYCODE_"):
            code = "KEYCODE_" + code
        if not re.fullmatch(r"[A-Z0-9_]+", code):
            raise ValueError(f"bad key {name!r}")
        self.shell(["input", "keyevent", code], timeout=15)

    def text(self, value: str) -> None:
        """Type printable ASCII into the focused field.

        `input text` has two traps: a space must be written `%s`, and a literal `%`
        directly followed by `s` would be read as that escape, so the text is split
        between those two characters. Newlines become ENTER. Anything outside ASCII
        cannot be typed this way at all; superfarm.stream.input.Input.text handles that
        (clipboard + paste) and is what the panel and automation use.
        """
        if any(not (" " <= c <= "~" or c == "\n") for c in value):
            raise AdbError(f"{self.serial}: only printable ASCII can be typed with `input text`")
        for i, line in enumerate(value.split("\n")):
            if i:
                self.key("ENTER")
            # Cut after every '%' that is followed by 's' so the pair never reaches the device together.
            for chunk in re.split(r"(?<=%)(?=s)", line):
                if chunk:
                    self.shell(["input", "text", chunk.replace(" ", "%s")], timeout=30)

    # ------------------------------------------------------------------- files
    def push(self, local: str, remote: str, timeout: float = 600) -> None:
        self.raw(["push", local, remote], timeout=timeout)

    def pull(self, remote: str, local: str, timeout: float = 600) -> None:
        self.raw(["pull", remote, local], timeout=timeout)

    def install(self, apks: Sequence[str], *, replace: bool = True, grant: bool = True,
                downgrade: bool = False, timeout: float = 900) -> None:
        """Install one APK or a set of split APKs (base + config splits) atomically."""
        flags = [*(["-r"] if replace else []), *(["-g"] if grant else []), *(["-d"] if downgrade else [])]
        argv = ["install-multiple", *flags, *apks] if len(apks) > 1 else ["install", *flags, apks[0]]
        r = self.raw(argv, timeout=timeout, check=False)
        text = (r.stdout + r.stderr).strip()
        if not r.ok or "Success" not in text:
            raise AdbError(f"{self.serial}: install failed: {text.splitlines()[-1][:300] if text else 'no output'}")

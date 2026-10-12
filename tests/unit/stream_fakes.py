"""Stand-ins for ADB and the instance manager, shared by the stream unit tests.

FakeAdb answers the handful of calls the stream modules make (screenshots, `input`,
size queries, connection state) from memory, records every device command, and can be
told to be slow, offline, booting or broken.
"""
from __future__ import annotations

import io
import struct
import threading
import time
from types import SimpleNamespace

from PIL import Image

from superfarm.core.adb import AdbError


class FakeAdb:
    binary = "adb"

    def __init__(self, width: int = 48, height: int = 80, *, raw_delay: float = 0.0, png_delay: float = 0.0,
                 delay: float = 0.0):
        self.serial = "127.0.0.1:1"
        self.width, self.height = width, height
        self.raw_delay, self.png_delay, self.delay = raw_delay, png_delay, delay
        self.adb_state = "device"       # what `adb get-state` says
        self.is_booted = True
        self.fail: Exception | None = None      # raised by every screenshot / shell call
        self.raw_garbage = False                # plain `screencap` returns something unreadable
        self.raw_header = 16
        self.raw_calls = self.png_calls = 0
        self.shell_calls: list[list[str] | str] = []
        self.in_flight = self.max_in_flight = 0
        self._lock = threading.Lock()

    # ----------------------------------------------------------- screenshots
    def _enter(self) -> None:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def _leave(self) -> None:
        with self._lock:
            self.in_flight -= 1

    def _pixels(self) -> bytes:
        return bytes([200, 100, 50, 255]) * (self.width * self.height)

    def exec_out(self, command, *, timeout: float = 30) -> bytes:
        assert list(command) == ["screencap"], command
        self._enter()
        try:
            time.sleep(self.raw_delay)
            if self.fail:
                raise self.fail
            with self._lock:
                self.raw_calls += 1
            if self.raw_garbage:
                return b"\x00" * 64
            header = struct.pack("<III", self.width, self.height, 1) + (b"\x00" * 4 if self.raw_header == 16 else b"")
            return header + self._pixels()
        finally:
            self._leave()

    def screencap_png(self, timeout: float = 20) -> bytes:
        self._enter()
        try:
            time.sleep(self.png_delay)
            if self.fail:
                raise self.fail
            with self._lock:
                self.png_calls += 1
            out = io.BytesIO()
            Image.frombytes("RGBA", (self.width, self.height), self._pixels()).save(out, "PNG")
            return out.getvalue()
        finally:
            self._leave()

    @property
    def captures(self) -> int:
        return self.raw_calls + self.png_calls

    # ------------------------------------------------------------ connection
    def state(self) -> str:
        return self.adb_state

    def connect(self, attempts: int = 5, delay: float = 1.0) -> bool:
        return self.adb_state == "device"

    def booted(self) -> bool:
        return self.is_booted

    # ----------------------------------------------------------------- input
    def shell(self, command, *, timeout: float = 30, check: bool = True) -> str:
        time.sleep(self.delay)
        if self.fail:
            raise self.fail
        with self._lock:
            self.shell_calls.append(command if isinstance(command, str) else list(command))
        if command == "dumpsys window displays":
            return f"  init={self.width}x{self.height} 160dpi cur={self.width}x{self.height} app=1x1"
        if command == "wm size":
            return f"Physical size: {self.width}x{self.height}"
        return ""

    def tap(self, x: int, y: int) -> None:
        self.shell(["input", "tap", str(x), str(y)])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.shell(["input", "swipe", *(str(v) for v in (x1, y1, x2, y2, ms))])

    def inputs(self) -> list[list[str]]:
        """Only the `input ...` commands, in order."""
        return [c for c in self.shell_calls if isinstance(c, list) and c[:1] == ["input"]]


class FakeManager:
    """mgr.adb(id) / mgr.resolved(id) for a fixed set of fake devices."""

    def __init__(self, devices: dict[int, FakeAdb] | None = None):
        self.devices = devices if devices is not None else {1: FakeAdb()}

    def adb(self, id: int) -> FakeAdb:
        if id not in self.devices:
            raise KeyError(f"no instance {id}")
        return self.devices[id]

    def resolved(self, id: int) -> SimpleNamespace:
        adb = self.adb(id)
        return SimpleNamespace(id=id, name=f"Phone {id}", width=adb.width, height=adb.height)


def offline(adb: FakeAdb) -> None:
    adb.adb_state = "offline"
    adb.fail = AdbError("fake: device offline")


def online(adb: FakeAdb) -> None:
    adb.adb_state = "device"
    adb.is_booted = True
    adb.fail = None

"""Reading an Android's screen as a tree of elements, and acting on it.

Automation never works from fixed coordinates: it reads the live view hierarchy and
finds its target in it. Everything else depends on that read being *fresh* and
*bounded*, and both are harder than they look:

  * `uiautomator dump` waits for the screen to go idle. On a moving screen (video,
    an animation) it can sit for tens of seconds, so every read has a hard limit
    enforced from outside, and a cancelled run kills the read instead of waiting.
  * A dump that fails can exit 0 and leave an *older* file in place. Reading a fixed
    path then returns a screen from minutes ago and the run taps things that are no
    longer there. Every attempt therefore dumps to its own unique path and deletes
    it afterwards: a missing file is an honest failure, a stale file is a lie.
  * For screens that never go idle there is an optional persistent reader
    (uiautomator2) that asks with zero idle wait. It runs in a helper process so
    its own retries and socket hangs can be cut off by a deadline here.

Two readers, one interface: read(timeout=, cancel=) -> list of nodes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Protocol
from collections.abc import Sequence

from PIL import Image

from ..core.adb import Adb, AdbError, quote
from ..util.proc import kill_tree, spawn
from .memory import to_image

MAX_XML_BYTES = 8 * 1024 * 1024       # a real hierarchy is tens of kilobytes
REMOTE_DIR = "/data/local/tmp"        # writable by the adb shell on every Android version

# What counts as a text box. Plain EditText is only the common case: search bars are
# AutoCompleteTextView or SearchView$SearchAutoComplete, Material forms use
# TextInputEditText, and all of them take input the same way.
_EDITABLE_CLASS = re.compile(r"EditText|AutoComplete|TextInput|SearchAutoComplete", re.IGNORECASE)
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_COMPONENT = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*/[A-Za-z0-9_.$]+$")

_TEXT_CHUNK = 60                      # characters per `input text` command (see Device.text)
KEY_ALIASES = {"backspace": "DEL", "delete": "DEL", "escape": "ESCAPE", "esc": "ESCAPE",
               "return": "ENTER", "recent": "APP_SWITCH", "recents": "APP_SWITCH"}


class UiError(RuntimeError):
    """The screen could not be read (or an input could not be sent)."""


class Cancelled(Exception):
    """The run this read belonged to was cancelled."""


# ------------------------------------------------------------------ parsing
def is_editable(cls: str) -> bool:
    return bool(_EDITABLE_CLASS.search(cls or ""))


def parse_hierarchy(xml: str) -> list[dict]:
    """A uiautomator XML dump -> flat list of nodes, in document order.

    Besides id, text and description each node carries its class, its index among
    its siblings and the path of classes from the root. Those are what make a list
    row or an unlabelled icon addressable when it has no id of its own.

    Raises UiError for anything that is not a non-empty hierarchy, so a truncated or
    empty answer is retried instead of being acted on as "nothing is on screen".
    """
    if not isinstance(xml, str) or len(xml) > MAX_XML_BYTES:
        raise UiError("the view hierarchy is missing or implausibly large")
    start = xml.find("<?xml")
    if start < 0:
        start = xml.find("<hierarchy")
    if start < 0:
        raise UiError("the device returned no view hierarchy")
    body = xml[start:]
    # uiautomator never emits a DTD. Refusing one keeps entity-expansion tricks from a
    # hostile app's text out of the parser altogether.
    if "<!DOCTYPE" in body or "<!ENTITY" in body:
        raise UiError("the view hierarchy contains a document type declaration")
    try:
        root = ET.fromstring(body)  # noqa: S314 - DTDs are rejected above; no external entities
    except ET.ParseError as e:
        raise UiError(f"the view hierarchy is not complete XML ({e}); the screen was probably changing") from None
    if root.tag != "hierarchy":
        raise UiError("the device returned something that is not a view hierarchy")

    out: list[dict] = []

    def walk(element: ET.Element, path: str, depth: int) -> None:
        for position, child in enumerate(element):
            if child.tag != "node":
                continue
            cls = child.get("class") or ""
            short = cls.rsplit(".", 1)[-1]
            raw_index = child.get("index") or ""
            index = int(raw_index) if raw_index.isdigit() else position
            here = f"{path}/{short}[{index}]"
            bounds = [int(v) for v in re.findall(r"-?\d+", child.get("bounds", ""))]
            if len(bounds) == 4:
                resource = child.get("resource-id") or ""
                out.append({
                    "resource_id": resource,
                    "id": resource.rsplit("/", 1)[-1],
                    "text": (child.get("text") or "").strip(),
                    "desc": (child.get("content-desc") or "").strip(),
                    "cls": short,
                    "full_class": cls,
                    "package": child.get("package") or "",
                    "index": index,
                    "depth": depth,
                    "path": here,
                    "bounds": bounds,
                    "center": [(bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2],
                    "clickable": child.get("clickable") == "true",
                    "long_clickable": child.get("long-clickable") == "true",
                    "scrollable": child.get("scrollable") == "true",
                    "checkable": child.get("checkable") == "true",
                    "checked": child.get("checked") == "true",
                    "selected": child.get("selected") == "true",
                    "password": child.get("password") == "true",
                    "editable": is_editable(cls),
                    "focused": child.get("focused") == "true",
                    "enabled": child.get("enabled") != "false",
                })
            walk(child, here, depth + 1)

    walk(root, "", 0)
    if not out:
        raise UiError("the view hierarchy is empty; the screen was probably changing")
    return out


def visible(node: dict) -> bool:
    bounds = node.get("bounds") or []
    return len(bounds) == 4 and bounds[2] > bounds[0] and bounds[3] > bounds[1]


def _area(node: dict) -> int:
    left, top, right, bottom = node["bounds"]
    return max(1, (right - left) * (bottom - top))


def _named(node: dict) -> bool:
    return bool(node.get("id") or node.get("text") or node.get("desc"))


def _contains(node: dict, x: int, y: int) -> bool:
    left, top, right, bottom = node["bounds"]
    return left <= x <= right and top <= y <= bottom


def element_at(nodes: list[dict], x: int, y: int) -> dict | None:
    """The element a tap at (x, y) lands on, with everything that identifies it.

    Android hands the tap to the innermost clickable view, so that is the element -
    but its identity is usually spread over the views inside it: every row of a list
    shares one id, and only the label inside tells the rows apart. So the id comes
    from the tapped view and the text and description are filled in from the
    smallest named view within it under the point, then from the row's own label
    when the tap landed beside it, then from the nearest named view around it. With
    nothing clickable under the point, the smallest named node is used.
    """
    hits = [n for n in nodes if visible(n) and _contains(n, x, y) and n.get("enabled", True)]
    if not hits:
        return None
    clickable = sorted((n for n in hits if n["clickable"]), key=_area)
    named = sorted((n for n in hits if _named(n)), key=_area)
    target = clickable[0] if clickable else (named[0] if named else sorted(hits, key=_area)[0])
    inside = [n for n in named if n is not target and n["path"].startswith(target["path"] + "/")]
    if not inside and target["clickable"]:
        # Tapped on the row but beside its label. A row or button has a label or
        # two inside it; a big clickable container has many, and none of them says
        # what was meant, so only a small number counts.
        labelled = [n for n in nodes if visible(n) and (n["text"] or n["desc"])
                    and n["path"].startswith(target["path"] + "/")]
        if 0 < len(labelled) <= 3:
            inside = labelled[:1]
    outside = [n for n in named if n is not target and target["path"].startswith(n["path"] + "/")]
    source = (inside or outside or [target])[0]
    return {
        "id": target["id"] or source["id"],
        "resource_id": target["resource_id"] or source["resource_id"],
        "text": target["text"] or source["text"],
        "desc": target["desc"] or source["desc"],
        "cls": target["cls"],
        "path": target["path"],
        "index": target["index"],
        "package": target["package"],
        "clickable": target["clickable"],
        "editable": target["editable"] or source["editable"],
        "scrollable": target["scrollable"],
        "password": target["password"] or source["password"],
        "bounds": list(target["bounds"]),
    }


def candidates_at(nodes: list[dict], x: int, y: int, limit: int = 4) -> list[dict]:
    """Other named elements under the same point, smallest first.

    Kept with every recorded tap: when the chosen element turns out to be the wrong
    one (an anonymous wrapper, a label that changes), playback still has the
    neighbours to try before it falls back to coordinates.
    """
    out: list[dict] = []
    for node in sorted((n for n in nodes if visible(n) and _contains(n, x, y) and _named(n)), key=_area):
        entry = {k: node[k] for k in ("id", "text", "desc", "cls", "path") if node.get(k)}
        if entry not in out:
            out.append(entry)
        if len(out) >= limit:
            break
    return out


def interesting(nodes: list[dict], limit: int = 60) -> list[dict]:
    """The nodes worth showing a person or a model: visible, and labelled or actionable."""
    picked = [n for n in nodes if visible(n) and n.get("enabled", True)
              and (n["text"] or n["desc"] or n["clickable"] or n["editable"])]
    return picked[:limit]


def describe_node(node: dict | None) -> str:
    if not node:
        return "nothing"
    return str(node.get("text") or node.get("desc") or node.get("id") or node.get("cls") or "element")[:60]


def foreground_package(nodes: list[dict]) -> str:
    """The app most of the screen belongs to (dialogs of other packages sit on top
    of it and are usually smaller)."""
    areas: dict[str, int] = {}
    for node in nodes:
        if node.get("package") and visible(node) and node.get("depth") == 0:
            areas[node["package"]] = areas.get(node["package"], 0) + _area(node)
    if not areas:
        for node in nodes:
            if node.get("package"):
                areas[node["package"]] = areas.get(node["package"], 0) + 1
    return max(areas, key=lambda k: areas[k]) if areas else ""


# ------------------------------------------------------------ bounded adb
def _bounded(argv: Sequence[str], *, timeout: float, cancel: threading.Event | None = None) -> tuple[int, bytes, bytes]:
    """Run a command with a hard limit that also ends the moment the run is cancelled.

    A stalled device must not hold a worker for longer than the caller allowed, and
    pressing Stop must not have to wait out a 30 second read.
    """
    try:
        child = spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise UiError(f"{argv[0]} was not found; install the Android platform tools (adb)") from None
    deadline = time.monotonic() + max(0.1, timeout)
    try:
        while True:
            try:
                out, err = child.communicate(timeout=0.1)
                return child.returncode, out or b"", err or b""
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    raise Cancelled() from None
                if time.monotonic() >= deadline:
                    raise UiError(f"no answer within {timeout:.0f}s") from None
    finally:
        if child.poll() is None:
            kill_tree(child.pid, signal.SIGKILL)
            try:
                child.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass


def _device_argv(adb: Adb, verb: str, command: Sequence[str]) -> list[str]:
    # `adb shell` joins its arguments into one line for the device's shell, so each
    # argument is quoted for that shell here.
    return [adb.binary, "-s", adb.serial, verb, " ".join(quote(a) for a in command)]


_serial_locks: dict[str, threading.Lock] = {}
_serial_locks_guard = threading.Lock()


def _serial_lock(serial: str) -> threading.Lock:
    # uiautomator refuses to run twice at once on one device: a second dump started
    # while the first is running simply fails. Every read for a serial queues here.
    with _serial_locks_guard:
        return _serial_locks.setdefault(serial, threading.Lock())


def _acquire(lock: threading.Lock, deadline: float, cancel: threading.Event | None) -> bool:
    while True:
        if lock.acquire(timeout=0.1):
            return True
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if time.monotonic() >= deadline:
            return False


def _pause(seconds: float, cancel: threading.Event | None) -> None:
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise Cancelled()


# ------------------------------------------------------------------ readers
class HierarchyReader(Protocol):
    name: str

    def read(self, *, timeout: float, cancel: threading.Event | None = None) -> list[dict]: ...

    def close(self) -> None: ...


class DumpReader:
    """Android's built-in `uiautomator dump`. Needs nothing installed; waits for idle."""

    name = "uiautomator"

    def __init__(self, adb: Adb, *, attempts: int = 3):
        self.adb = adb
        self.attempts = max(1, attempts)
        self._device_timeout = True      # does the device have a `timeout` command?

    def read(self, *, timeout: float, cancel: threading.Event | None = None) -> list[dict]:
        deadline = time.monotonic() + max(1.0, timeout)
        lock = _serial_lock(self.adb.serial)
        if not _acquire(lock, deadline, cancel):
            raise UiError(f"{self.adb.serial}: another screen read did not finish within {timeout:.0f}s")
        try:
            problem = "no attempt could be made in the time allowed"
            for attempt in range(self.attempts):
                left = deadline - time.monotonic()
                if left < 1.0:
                    break
                # A fresh path per attempt: if the dump fails, `cat` finds nothing,
                # instead of finding the hierarchy of an earlier screen.
                remote = f"{REMOTE_DIR}/sf-ui-{uuid.uuid4().hex}.xml"
                finished = False
                try:
                    said = self._dump(remote, left, cancel)
                    finished = True
                    left = deadline - time.monotonic()
                    if left < 0.5:
                        problem = "the dump used up the time allowed"
                        break
                    code, out, err = _bounded(_device_argv(self.adb, "exec-out", ["cat", remote]),
                                              timeout=min(left, 15.0), cancel=cancel)
                    text = out.decode("utf-8", errors="replace")
                    if code != 0 or "<hierarchy" not in text:
                        raise UiError(said or err.decode("utf-8", errors="replace").strip()[:200]
                                      or "uiautomator wrote no hierarchy")
                    return parse_hierarchy(text)
                except UiError as e:
                    problem = str(e)
                finally:
                    self._remove(remote, interrupted=not finished)
                if attempt < self.attempts - 1 and deadline - time.monotonic() > 1.5:
                    _pause(0.35, cancel)             # usually mid-animation; let it settle
            raise UiError(
                f"{self.adb.serial}: could not read the screen: {problem}. If this screen never stops moving "
                "(video, animation), switch to the persistent reader: pip install 'superfarm[u2]' and set "
                "automation.hierarchy_reader to u2.")
        finally:
            lock.release()

    def _dump(self, remote: str, left: float, cancel: threading.Event | None) -> str:
        """Run the dump; return what uiautomator said when it did not report success."""
        on_device = max(2, int(left) - 1)
        command = ["uiautomator", "dump", remote]
        if self._device_timeout:
            # Bounded on the device too: killing the adb client does not always end
            # the uiautomator process, and a leftover one blocks the next dump.
            code, out, err = _bounded(_device_argv(self.adb, "shell", ["timeout", str(on_device), *command]),
                                      timeout=left, cancel=cancel)
            said = (out + err).decode("utf-8", errors="replace").strip()
            if code == 127 or ("timeout" in said and "not found" in said):
                self._device_timeout = False
            else:
                return "" if "dumped to" in said else said[:200]
        code, out, err = _bounded(_device_argv(self.adb, "shell", command), timeout=left, cancel=cancel)
        said = (out + err).decode("utf-8", errors="replace").strip()
        return "" if "dumped to" in said else said[:200]

    def _remove(self, remote: str, *, interrupted: bool = False) -> None:
        # Not cancellable on purpose: clean-up must happen for a cancelled read too.
        if interrupted:
            # Killing the adb client does not stop the dump on the device: it runs
            # on, blocks the next dump, and writes its file after we removed it.
            # Stop it by exact process name (never by pattern: a pattern also
            # matches the shell that carries it on its command line).
            try:
                _bounded(_device_argv(self.adb, "shell", ["pkill", "-x", "uiautomator"]), timeout=10)
            except UiError:
                pass
        try:
            _bounded(_device_argv(self.adb, "shell", ["rm", "-f", remote]), timeout=10)
        except UiError:
            pass

    def close(self) -> None:
        return None


_WORKER_BOOT = "from superfarm.automation.ui import u2_worker_main; u2_worker_main()"


def u2_available() -> bool:
    return importlib.util.find_spec("uiautomator2") is not None


class U2Reader:
    """A persistent uiautomator2 connection held by a helper process.

    The reader on the device answers at once (idle wait set to zero), which is what
    makes busy screens readable. The library has its own retries and can hang on a
    dead ADB socket, so it never runs in this process: the helper is killed when a
    read passes its deadline or the run is cancelled, and a fresh one is started for
    the next read.
    """

    name = "u2"
    CONNECT_TIMEOUT = 25.0
    _owners: dict[str, U2Reader] = {}
    _owners_guard = threading.Lock()

    def __init__(self, adb: Adb, *, worker_argv: Sequence[str] | None = None):
        self.adb = adb
        self._argv = list(worker_argv) if worker_argv else [sys.executable, "-c", _WORKER_BOOT, adb.serial]
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._stderr = None
        self._last_stderr = ""
        self._connected = False
        self._io = threading.Lock()

    # -- helper process ----------------------------------------------------
    def _spawn(self) -> None:
        # One persistent reader per device: two instrumentation sessions on one
        # Android evict each other and both runs see failed reads.
        with self._owners_guard:
            owner = self._owners.get(self.adb.serial)
            if owner is not None and owner is not self:
                raise UiError(f"{self.adb.serial}: another run is already reading this device's screen")
            self._owners[self.adb.serial] = self
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 - closed in _terminate
        try:
            self._proc = spawn(self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                               env={"ADBUTILS_ADB_PATH": self.adb.binary, "PYTHONIOENCODING": "utf-8"})
        except OSError as e:
            self._terminate()
            raise UiError(f"could not start the screen reader helper: {e}") from None
        lines: queue.Queue[bytes | None] = queue.Queue()
        self._lines, proc = lines, self._proc

        def pump() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    lines.put(line)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)

        threading.Thread(target=pump, name=f"sf-u2-{self.adb.serial}", daemon=True).start()

    def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        self._connected = False
        if proc is not None:
            if proc.poll() is None:
                kill_tree(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    kill_tree(proc.pid, signal.SIGKILL)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            for pipe in (proc.stdin, proc.stdout):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
        if self._stderr is not None:
            try:
                self._stderr.seek(0, os.SEEK_END)
                end = self._stderr.tell()
                self._stderr.seek(max(0, end - 2048))
                self._last_stderr = self._stderr.read().decode("utf-8", errors="replace").strip()
            except (OSError, ValueError):
                pass
            finally:
                self._stderr.close()
                self._stderr = None
        with self._owners_guard:
            if self._owners.get(self.adb.serial) is self:
                del self._owners[self.adb.serial]

    def _request(self, command: str, timeout: float, cancel: threading.Event | None) -> dict:
        if self._proc is None:
            self._spawn()
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps({"cmd": command}).encode() + b"\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            self._terminate()
            raise UiError(f"the screen reader helper exited: {e}; {self._last_stderr[-300:]}".rstrip("; ")) from None
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                left = deadline - time.monotonic()
                if left <= 0:
                    raise UiError(f"screen reader {command} gave no answer within {timeout:.0f}s")
                try:
                    line = self._lines.get(timeout=min(0.1, left))
                except queue.Empty:
                    continue
                if line is None:
                    raise UiError("the screen reader helper closed")
                try:
                    answer = json.loads(line)
                except json.JSONDecodeError:
                    raise UiError("the screen reader helper sent something that is not JSON") from None
                if not isinstance(answer, dict) or not answer.get("ok"):
                    detail = answer.get("error") if isinstance(answer, dict) else ""
                    raise UiError(str(detail or "the screen reader helper failed")[:300])
                return answer
        except (UiError, Cancelled) as e:
            # Whatever went wrong, the helper's state is unknown now: reap it, and let
            # the next read start a clean one.
            self._terminate()
            if isinstance(e, UiError) and self._last_stderr:
                raise UiError(f"{e}; helper said: {self._last_stderr[-300:]}") from None
            raise

    # -- interface ---------------------------------------------------------
    def read(self, *, timeout: float, cancel: threading.Event | None = None) -> list[dict]:
        deadline = time.monotonic() + max(1.0, timeout)
        with self._io:
            if not self._connected:
                self._request("connect", min(self.CONNECT_TIMEOUT, max(1.0, deadline - time.monotonic())), cancel)
                self._connected = True
            answer = self._request("dump", max(0.5, deadline - time.monotonic()), cancel)
        xml = answer.get("xml")
        if not isinstance(xml, str):
            raise UiError("the screen reader helper returned no hierarchy")
        return parse_hierarchy(xml)

    def close(self) -> None:
        with self._io:
            if self._proc is not None and self._connected:
                try:
                    self._request("close", 3.0, None)
                except (UiError, Cancelled):
                    pass
            self._terminate()


def u2_worker_main() -> None:
    """Entry point of the helper process: JSON lines in, JSON lines out.

    Only U2Reader starts this. It imports uiautomator2 here, never in the farm
    process, so the optional dependency stays optional.
    """
    import contextlib

    serial = sys.argv[1]
    wire = sys.stdout
    device = None
    for line in sys.stdin:
        request: dict = {}
        try:
            request = json.loads(line)
            command = request.get("cmd")
            # The library prints progress on stdout, which is this protocol's wire.
            with contextlib.redirect_stdout(sys.stderr):
                if command == "connect":
                    import uiautomator2 as u2
                    device = u2.connect(serial)
                    # Zero idle wait is the whole point: answer with whatever is on
                    # screen now instead of waiting for a video to stop moving.
                    device.jsonrpc.setConfigurator({"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0})
                    answer: dict = {"ok": True}
                elif command == "dump":
                    if device is None:
                        raise RuntimeError("not connected")
                    # One direct call gives one fresh answer. The high-level wrapper
                    # retries empty dumps and can outlast the caller's deadline; the
                    # parent does the bounded retrying.
                    xml = device.jsonrpc.dumpWindowHierarchy(False, 50)
                    if not isinstance(xml, str) or "<hierarchy" not in xml:
                        raise RuntimeError("no hierarchy returned")
                    answer = {"ok": True, "xml": xml}
                elif command == "close":
                    if device is not None:
                        with contextlib.suppress(Exception):
                            device.stop_uiautomator()
                    answer = {"ok": True}
                else:
                    raise ValueError("unknown command")
        except Exception as e:  # noqa: BLE001 - every failure is reported to the parent as data
            answer = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        wire.write(json.dumps(answer, ensure_ascii=True) + "\n")
        wire.flush()
        if request.get("cmd") == "close":
            break


class FallbackReader:
    """The persistent reader when it works, the built-in one when it does not.

    `auto` mode must never be worse than plain uiautomator: if the helper cannot
    connect (server not pushed yet, an Android version it does not support), the
    device quietly uses the built-in dump for the rest of its run.
    """

    def __init__(self, primary: HierarchyReader, secondary: HierarchyReader):
        self.primary, self.secondary = primary, secondary
        self.failures = 0
        self.name = primary.name

    def read(self, *, timeout: float, cancel: threading.Event | None = None) -> list[dict]:
        if self.failures < 2:
            started = time.monotonic()
            try:
                nodes = self.primary.read(timeout=timeout, cancel=cancel)
                self.failures = 0
                return nodes
            except UiError:
                self.failures += 1
                if self.failures >= 2:
                    self.name = self.secondary.name
                    self.primary.close()
                timeout = max(2.0, timeout - (time.monotonic() - started))
        return self.secondary.read(timeout=timeout, cancel=cancel)

    def close(self) -> None:
        self.primary.close()
        self.secondary.close()


def make_reader(adb: Adb, mode: str = "auto") -> HierarchyReader:
    """Pick the hierarchy reader for automation.hierarchy_reader (auto | uiautomator | u2)."""
    if mode == "uiautomator":
        return DumpReader(adb)
    if mode == "u2":
        if not u2_available():
            raise UiError("automation.hierarchy_reader is u2 but uiautomator2 is not installed. "
                          "Install it with: pip install 'superfarm[u2]'  (or set the reader to uiautomator)")
        return U2Reader(adb)
    if mode == "auto":
        return FallbackReader(U2Reader(adb), DumpReader(adb)) if u2_available() else DumpReader(adb)
    raise ValueError(f"unknown hierarchy reader {mode!r}: use auto, uiautomator or u2")


def read_nodes(adb: Adb, *, timeout: float = 30.0, cancel: threading.Event | None = None) -> list[dict]:
    """One fresh, bounded read of a device's screen with the built-in reader."""
    return DumpReader(adb).read(timeout=timeout, cancel=cancel)


# ------------------------------------------------------------------- device
class Device:
    """One Android as automation sees it: a screen to read and inputs to send.

    Everything a run does to a device goes through this class, which is also the
    seam the tests replace with a scripted fake.
    """

    def __init__(self, adb: Adb, *, reader: HierarchyReader | None = None, read_timeout: float = 30.0,
                 instance_id: int | None = None):
        self.adb = adb
        self.serial = adb.serial
        self.instance_id = instance_id
        self.reader: HierarchyReader = reader or DumpReader(adb)
        self.read_timeout = float(read_timeout)
        self._size: tuple[int, int] | None = None

    # -- reading -----------------------------------------------------------
    def nodes(self, *, timeout: float | None = None, cancel: threading.Event | None = None) -> list[dict]:
        limit = self.read_timeout if timeout is None else max(1.0, min(float(timeout), self.read_timeout))
        return self.reader.read(timeout=limit, cancel=cancel)

    def size(self) -> tuple[int, int]:
        if self._size is None:
            self._size = self._adb(self.adb.size)
        return self._size

    def current(self) -> tuple[str, str]:
        """(package, activity) in the foreground, or ("", "") when it cannot be read."""
        try:
            return self.adb.current_app()
        except AdbError:
            return "", ""

    def screenshot(self) -> Image.Image:
        return to_image(self._adb(self.adb.screencap_png))

    # -- input -------------------------------------------------------------
    def _adb(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AdbError as e:
            raise UiError(str(e)) from None

    def tap(self, x: int, y: int) -> None:
        self._adb(self.adb.tap, int(x), int(y))

    def long_press(self, x: int, y: int, ms: int = 700) -> None:
        # A swipe that does not move is how `input` expresses press-and-hold.
        self._adb(self.adb.swipe, int(x), int(y), int(x), int(y), max(300, min(int(ms), 10_000)))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._adb(self.adb.swipe, int(x1), int(y1), int(x2), int(y2), max(50, min(int(ms), 10_000)))

    def key(self, name: str) -> None:
        raw = str(name).strip()
        try:
            self.adb.key(KEY_ALIASES.get(raw.lower(), raw))
        except ValueError:
            raise UiError(f"unknown key {name!r}: use a name like back, home, enter, del or a KEYCODE_ name") from None
        except AdbError as e:
            raise UiError(str(e)) from None

    def text(self, value: str) -> None:
        """Type into the focused field. `input text` cannot type beyond printable
        ASCII, and silently typing something else would be worse than stopping."""
        if not value:
            return
        if not all(ch == "\n" or " " <= ch <= "~" for ch in value):
            raise UiError("this text has characters `input text` cannot type (only printable ASCII is supported)")
        for index, line in enumerate(value.split("\n")):
            if index:
                self.key("enter")
            # `input text` reads "%s" as a space and has no way to escape it. Spaces
            # are sent that way; a literal "%s" is typed by ending one command after
            # the "%" and starting the next with the "s". Long lines go in short
            # pieces: one 400-character command was seen to arrive with a third of
            # its characters missing.
            for part in re.split(r"(?<=%)(?=s)", line):
                for start in range(0, len(part), _TEXT_CHUNK):
                    piece = part[start:start + _TEXT_CHUNK]
                    self._adb(self.adb.shell, ["input", "text", piece.replace(" ", "%s")], timeout=30)

    def clear_focused(self, length_hint: int = 48) -> None:
        """Empty the focused field: jump to its end, then delete backwards."""
        count = max(4, min(int(length_hint) + 2, 5000))
        # Many key events per command: one adb round trip instead of one per
        # character, in batches small enough that none are dropped.
        self._adb(self.adb.shell, ["input", "keyevent", "KEYCODE_MOVE_END"], timeout=15)
        while count > 0:
            batch = min(count, 50)
            self._adb(self.adb.shell, ["input", "keyevent", *(["KEYCODE_DEL"] * batch)], timeout=30)
            count -= batch

    def launch(self, package: str, activity: str | None = None) -> str:
        """Start an app by its launcher activity (or a named one). Returns the component."""
        if not _PACKAGE.match(package or ""):
            raise UiError(f"bad package name {package!r}")
        if activity:
            component = activity if "/" in activity else f"{package}/{activity}"
        else:
            out = self._adb(self.adb.shell, ["cmd", "package", "resolve-activity", "--brief", package],
                            timeout=20, check=False)
            found = [line.strip() for line in out.splitlines() if line.strip().startswith(package + "/")]
            if not found:
                raise UiError(f"{package} has no launcher activity on {self.serial}; is the app installed?")
            component = found[-1]
        if not _COMPONENT.match(component):
            raise UiError(f"bad activity {component!r}")
        said = self._adb(self.adb.shell, ["am", "start", "-n", component], timeout=30, check=False)
        if "Error" in said or "Exception" in said:
            raise UiError(f"could not start {component}: {said.splitlines()[-1][:200]}")
        return component

    def stop_app(self, package: str) -> None:
        if not _PACKAGE.match(package or ""):
            raise UiError(f"bad package name {package!r}")
        self._adb(self.adb.shell, ["am", "force-stop", package], timeout=20)

    def close(self) -> None:
        self.reader.close()

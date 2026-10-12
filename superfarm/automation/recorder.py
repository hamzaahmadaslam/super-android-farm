"""Recording: one session per device, driven from the web panel.

The person clicks on the live view in the panel; the panel calls the methods here.
There is no desktop mouse hook and no screen sampling on a timer.

The order inside a recorded tap is the whole point:

    1. read the device's view hierarchy       <- the screen the tap is aimed at
    2. resolve the element under the point, and its neighbours
    3. save a picture of the area
    4. only then send the tap

Sampling the hierarchy on a timer and attributing the next tap to the newest sample
is a race: tap something right after a screen changes and the sample still describes
the screen that was left. Intercepting the tap removes the race - the app has not
seen the tap yet, so whatever is read describes exactly what it will hit.

Every action is saved as it happens, so a crash loses nothing that was recorded.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from ..config import AutomationConfig
from .memory import crop, crop_box, save_png
from .store import AutomationStore, Dataset, Recording, Step, check_package, now
from .ui import UiError, candidates_at, element_at
from .values import Resolver, Values, check_name, looks_secret

SHOT_SIZE = 180          # device pixels around the tap that get saved
MAX_STEPS = 500          # a recording is one goal, not an afternoon


class RecordingError(RuntimeError):
    pass


class RecordingSession:
    """One device being recorded into one dataset."""

    def __init__(self, device: Any, dataset: Dataset, config: AutomationConfig, *, values: Values | None = None,
                 instance_id: int | None = None):
        self.device = device
        self.dataset = dataset
        self.config = config
        self.values = values
        self.instance_id = instance_id
        self.resolver = Resolver(values, instance_id=instance_id, serial=getattr(device, "serial", ""))
        self.recording = Recording(id=dataset.new_recording_id(), created=now(),
                                   device=getattr(device, "serial", ""), instance=instance_id,
                                   package=dataset.package, screen=list(device.size()))
        self.active = True
        self.last_problem = ""
        self._started = time.monotonic()
        # Written at once, empty: that is what reserves the id. Two sessions started
        # in the same second would otherwise get the same folder, and cancelling
        # one would delete the other's recording.
        dataset.save_recording(self.recording)
        # One action at a time: the panel may send the next click before the last
        # one's hierarchy read has come back, and steps must stay in order.
        self._lock = threading.Lock()

    # -- plumbing ----------------------------------------------------------
    def _now(self) -> float:
        return round(time.monotonic() - self._started, 2)

    def _pixels(self, x: float, y: float, normalized: bool) -> tuple[int, int]:
        width, height = self.recording.screen
        if normalized:
            if not (0.0 <= float(x) <= 1.0 and 0.0 <= float(y) <= 1.0):
                raise RecordingError("coordinates must be between 0 and 1 (fractions of the screen)")
            return min(width - 1, int(float(x) * width)), min(height - 1, int(float(y) * height))
        px, py = int(x), int(y)
        if not (0 <= px < width and 0 <= py < height):
            raise RecordingError(f"({px}, {py}) is outside the {width}x{height} screen")
        return px, py

    def _read(self) -> list[dict]:
        """The hierarchy as it is now, or an empty list when it cannot be read.

        A failed read does not block the person: the tap still goes through and is
        recorded with its position and picture, and training says plainly that such
        a step can only be found by position.
        """
        try:
            self.last_problem = ""
            return self.device.nodes(timeout=float(self.config.read_timeout))
        except UiError as e:
            self.last_problem = str(e)
            return []

    def _activity(self) -> str:
        package, activity = self.device.current()
        return f"{package}/{activity}" if package else ""

    def _require_active(self) -> None:
        if not self.active:
            raise RecordingError("this recording has been stopped")
        if len(self.recording.steps) >= MAX_STEPS:
            raise RecordingError(f"a recording holds at most {MAX_STEPS} steps; stop and start a new one")

    def _add(self, step: Step) -> dict:
        step.id = f"s{len(self.recording.steps):04d}"
        self.recording.steps.append(step)
        self.recording.seconds = self._now()
        self.dataset.save_recording(self.recording)
        return {**step.to_dict(), "describe": step.describe(), "read_problem": self.last_problem}

    def _save_shot(self, step: Step, point: tuple[int, int]) -> None:
        try:
            image = self.device.screenshot()
        except UiError:
            return                    # the step is still worth having without its picture
        box = crop_box(point, SHOT_SIZE, (image.width, image.height))
        relative = f"shots/s{len(self.recording.steps):04d}.png"
        try:
            save_png(crop(image, box), self.dataset.shot_path(self.recording.id, relative))
        except OSError:
            return
        step.shot, step.shot_box = relative, box

    @staticmethod
    def _clean(element: dict | None) -> dict | None:
        # What a person typed into a field is content, not identity - and it may be
        # private. It never goes into a recording.
        if element and (element.get("editable") or element.get("password")):
            element = {**element, "text": ""}
        return element

    # -- pointing ----------------------------------------------------------
    def tap(self, x: float, y: float, *, normalized: bool = True, long_ms: int = 0) -> dict:
        with self._lock:
            self._require_active()
            point = self._pixels(x, y, normalized)
            nodes = self._read()                                   # 1. the screen, before the tap
            step = Step(kind="long_press" if long_ms else "tap", at=self._now(), device=list(point),
                        element=self._clean(element_at(nodes, *point)),          # 2. what is under it
                        candidates=candidates_at(nodes, *point), activity=self._activity(),
                        seconds=round(long_ms / 1000, 2) if long_ms else 0.0)
            if step.element is None:
                step.note = ("the screen could not be read" if not nodes else
                             "nothing identifiable was under this tap")
            self._save_shot(step, point)                           # 3. the picture
            if long_ms:                                            # 4. and only now, the tap
                self.device.long_press(*point, int(long_ms))
            else:
                self.device.tap(*point)
            return self._add(step)

    def long_press(self, x: float, y: float, *, ms: int = 700, normalized: bool = True) -> dict:
        return self.tap(x, y, normalized=normalized, long_ms=max(300, min(int(ms), 10_000)))

    def swipe(self, x1: float, y1: float, x2: float, y2: float, *, ms: int = 300, normalized: bool = True) -> dict:
        with self._lock:
            self._require_active()
            start, end = self._pixels(x1, y1, normalized), self._pixels(x2, y2, normalized)
            step = Step(kind="swipe", at=self._now(), device=list(start), to=list(end), activity=self._activity())
            self.device.swipe(*start, *end, max(50, min(int(ms), 10_000)))
            return self._add(step)

    def assert_visible(self, x: float, y: float, *, normalized: bool = True) -> dict:
        """Record "this element must be on screen here" without touching the device."""
        with self._lock:
            self._require_active()
            point = self._pixels(x, y, normalized)
            nodes = self._read()
            element = self._clean(element_at(nodes, *point))
            if element is None or not (element.get("id") or element.get("text") or element.get("desc")):
                raise RecordingError("there is nothing with an id, text or description at that point to expect"
                                     + (f" ({self.last_problem})" if self.last_problem else ""))
            return self._add(Step(kind="assert", at=self._now(), device=list(point), element=element,
                                  candidates=candidates_at(nodes, *point), activity=self._activity()))

    # -- typing ------------------------------------------------------------
    def text(self, value: str | None = None, *, name: str | None = None) -> dict:
        """Type into the focused field.

        `name` records the step as "type the value called <name>", resolved per
        device at run time (shared value, per-instance value, `pool.<name>`, or a
        variable passed to the run). With `value` as well, that text is typed now and
        only the name is saved - the way to record a password. Without a name the
        literal text is saved in the recording.
        """
        with self._lock:
            self._require_active()
            words = self.config.forbidden_field_words
            if name is not None:
                if not name.startswith("pool."):
                    check_name(name)
                typed = value if value is not None else self.resolver.get(name)
                if typed is None:
                    raise RecordingError(f"there is no value called {name!r}: add it to the shared values first, "
                                         "or send the text to type along with the name")
            else:
                if not value:
                    raise RecordingError("nothing to type")
                typed = value
            nodes = self._read()
            if name is None and not nodes:
                # The check below needs the focused field. Without it this may be a password,
                # and a literal is saved with the recording: refuse instead of guessing.
                raise RecordingError(
                    "the screen could not be read, so it cannot be told whether this is a password field"
                    + (f" ({self.last_problem})" if self.last_problem else "")
                    + ". Typed text is saved with the recording: try again, or give the value a name "
                    "(it is then typed now and only the name is saved).")
            # password="true" also on a class that is not a text field: toolkits draw their own.
            focused = next((n for n in nodes if n["focused"] and (n["editable"] or n["password"])), None)
            if name is None and focused is not None and (
                    focused.get("password") or looks_secret(f"{focused.get('id', '')} {focused.get('desc', '')}", words)):
                raise RecordingError(
                    "this looks like a password field, and typed text is saved with the recording. Give the "
                    "value a name (for example `password`): it is then typed now but only the name is saved, "
                    "and the value is passed when the program is run.")
            if focused is not None and focused.get("text") and not focused.get("password"):
                self.device.clear_focused(len(focused["text"]))
            self.device.text(typed)
            step = Step(kind="type", at=self._now(), text=None if name else typed, dynamic=name,
                        activity=self._activity())
            if focused is not None:
                step.element = {"id": focused["id"], "resource_id": focused["resource_id"], "text": "",
                                "desc": focused["desc"], "cls": focused["cls"], "path": focused["path"],
                                "index": focused["index"], "package": focused["package"],
                                "clickable": focused["clickable"], "editable": True,
                                "bounds": list(focused["bounds"])}
            return self._add(step)

    def key(self, name: str) -> dict:
        with self._lock:
            self._require_active()
            step = Step(kind="key", at=self._now(), key=str(name).lower(), activity=self._activity())
            self.device.key(name)
            return self._add(step)

    # -- the rest ----------------------------------------------------------
    def launch(self, package: str) -> dict:
        with self._lock:
            self._require_active()
            check_package(package)
            self.device.launch(package)
            if not self.recording.package:
                self.recording.package = package
            return self._add(Step(kind="launch", at=self._now(), package=package))

    def wait(self, seconds: float) -> dict:
        """Record "wait here" (the pause itself is the person's own)."""
        with self._lock:
            self._require_active()
            return self._add(Step(kind="wait", at=self._now(), seconds=max(0.1, min(float(seconds), 120.0))))

    def undo(self) -> bool:
        """Drop the last recorded step. The device is not rewound."""
        with self._lock:
            if not self.active or not self.recording.steps:
                return False
            step = self.recording.steps.pop()
            if step.shot:
                try:
                    self.dataset.shot_path(self.recording.id, step.shot).unlink()
                except (OSError, ValueError):
                    pass
            self.dataset.save_recording(self.recording)
            return True

    def status(self) -> dict:
        return {"recording": self.recording.id, "dataset": self.dataset.slug, "instance": self.instance_id,
                "device": self.recording.device, "active": self.active, "steps": len(self.recording.steps),
                "seconds": self._now() if self.active else self.recording.seconds,
                "screen": self.recording.screen, "last_problem": self.last_problem,
                "preview": [s.describe() for s in self.recording.steps[-8:]]}

    def stop(self) -> dict:
        with self._lock:
            if self.active:
                self.active = False
                self.recording.seconds = self._now()
                if self.recording.steps:
                    self.dataset.save_recording(self.recording)
                else:
                    self.dataset.delete_recording(self.recording.id)      # nothing to train on
                self._close()
            if not self.recording.steps:
                return {**self.status(), "taps": 0, "named_taps": 0,
                        "summary": "nothing was recorded, so nothing was saved"}
            taps = self.recording.taps
            named = sum(1 for s in self.recording.steps if s.kind in ("tap", "long_press")
                        and any((s.element or {}).get(k) for k in ("id", "text", "desc")))
            return {**self.status(), "taps": taps, "named_taps": named,
                    "summary": f"saved {self.recording.id}: {len(self.recording.steps)} step(s), {named} of {taps} "
                               "tap(s) landed on a named element"}

    def cancel(self) -> None:
        """Stop and throw the recording away."""
        with self._lock:
            self.active = False
            self.dataset.delete_recording(self.recording.id)
            self._close()

    def _close(self) -> None:
        try:
            self.device.close()
        except Exception:  # noqa: BLE001, S110 - closing the reader is best effort
            pass


class Recorder:
    """The recording sessions of a farm: at most one per device."""

    def __init__(self, store: AutomationStore, config: AutomationConfig, device_factory: Callable[[int], Any],
                 values: Values | None = None):
        self.store = store
        self.config = config
        self.values = values
        self._device_factory = device_factory
        self._sessions: dict[int, RecordingSession] = {}
        self._lock = threading.Lock()

    def adopt(self, previous: Recorder) -> None:
        """Keep the sessions of the recorder this one replaces (after a config change)."""
        with self._lock, previous._lock:
            self._sessions = previous._sessions

    def start(self, dataset: str, instance_id: int) -> dict:
        target = self.store.dataset(dataset)
        with self._lock:
            current = self._sessions.get(int(instance_id))
            if current is not None and current.active:
                raise RecordingError(f"Android {instance_id} is already being recorded into "
                                     f"{current.dataset.slug!r}; stop that recording first")
            device = self._device_factory(int(instance_id))
            try:
                session = RecordingSession(device, target, self.config, values=self.values, instance_id=int(instance_id))
            except Exception:
                device.close()
                raise
            self._sessions[int(instance_id)] = session
        return session.status()

    def session(self, instance_id: int) -> RecordingSession:
        session = self._sessions.get(int(instance_id))
        if session is None or not session.active:
            raise RecordingError(f"Android {instance_id} is not being recorded; start a recording first")
        return session

    def stop(self, instance_id: int) -> dict:
        with self._lock:
            session = self._sessions.pop(int(instance_id), None)
        if session is None:
            raise RecordingError(f"Android {instance_id} is not being recorded")
        return session.stop()

    def cancel(self, instance_id: int) -> bool:
        with self._lock:
            session = self._sessions.pop(int(instance_id), None)
        if session is None:
            return False
        session.cancel()
        return True

    def active(self) -> list[dict]:
        return [s.status() for s in self._sessions.values() if s.active]

    def busy(self) -> set[int]:
        return {i for i, s in self._sessions.items() if s.active}

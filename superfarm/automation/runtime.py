"""Running on one device: the session every run shares, and trained-program playback.

`Session` is one device doing one run. It owns the rules that hold for every kind of
automation, so that neither a flow, a trained program nor a model's suggestion can
get around them:

  * every tap passes the never-tap list, including taps a recovery proposes;
  * pop-ups are cleared only when a rule's guard *and* its dismiss control are both
    on screen, and only a couple of times per step;
  * every wait has a deadline, and so does the whole run;
  * a screen nothing covers stops *this* device with `needs_attention` and waits for
    a person (resume / skip / abort) while the other devices carry on;
  * `dry_run` reads and matches but never taps, types or presses anything.

`ProgramRun` plays a trained program. A step is resolved in this order:

  1. its element, by score (see selectors.py), then the alternatives the other
     recordings saw at that step
  2. the recorded coordinates, but only if the saved picture of that area still
     matches the live screen
  3. a fix remembered for this app and screen
  4. a model, if one is configured, for a bounded number of attempts
  5. a person
"""
from __future__ import annotations

import collections
import random
import threading
import time
from typing import Any
from collections.abc import Callable, Iterable

from ..config import AutomationConfig
from . import llm as llm_mod
from .memory import PATCH_MATCH, FixMemory, crop, dhash, load_image, patch_similarity, scale_box
from .profiles import OverlayRule
from .program import Program, ProgramStep
from .selectors import MissingVariable, Selector, find, find_scored, forbidden_at
from .ui import Cancelled, UiError, describe_node, foreground_package, interesting
from .values import PoolEmpty, Resolver

ATTENTION_TIMEOUT = 3600.0     # how long a device waits for a person before giving up
BACK_POLL = 4.0                # watch this long for a screen to close before a second Back
OPTIONAL_WAIT = 5.0            # an optional step's element gets this long to show up
MAX_RECOVERY_ROUNDS = 2        # recoveries per step before a person is asked
MAX_MODEL_ATTEMPTS = 2

__all__ = ["Aborted", "AttentionExpired", "Cancelled", "ForbiddenTap", "ProgramRun", "RunTimeout",
           "Session", "StepFailed", "StepTimeout", "bind_actions"]


class StepTimeout(Exception):
    """A wait ran its full time without seeing what it needs."""

    def __init__(self, what: str, detail: str = ""):
        super().__init__(f"timed out waiting for {what}" + (f" ({detail})" if detail else ""))
        self.what, self.detail = what, detail


class StepFailed(Exception):
    """A step cannot be completed and the run should end as failed."""


class ForbiddenTap(Exception):
    """A tap would have landed on a never-tap element."""


class RunTimeout(Exception):
    """The whole run passed its deadline."""


class Aborted(Exception):
    """A person chose to abort this device's run."""


class AttentionExpired(Exception):
    """Nobody answered a needs-attention request in time."""


class Session:
    """One device, one run."""

    poll = 0.4                     # pause between screen reads while waiting

    def __init__(self, device: Any, config: AutomationConfig, *, resolver: Resolver | None = None,
                 label: str = "", dry_run: bool = False, cancel: threading.Event | None = None,
                 log: Callable[[str], None] | None = None, overlays: Iterable[OverlayRule] = (),
                 never_tap: Iterable[Selector] = (), step_timeout: float | None = None,
                 run_timeout: float | None = None, rng: random.Random | None = None,
                 on_attention: Callable[[bool], None] | None = None):
        self.device = device
        self.config = config
        self.resolver = resolver or Resolver()
        self.label = label or getattr(device, "serial", "device")
        self.dry_run = dry_run
        self.cancel = cancel or threading.Event()
        self._log = log
        self.overlays = list(overlays)
        self.never_tap = list(never_tap)
        self.step_timeout = float(step_timeout if step_timeout is not None else config.step_timeout)
        self.run_timeout = float(run_timeout if run_timeout is not None else config.run_timeout)
        self.settle = float(config.settle_seconds)
        self.jitter = bool(config.human_jitter)
        self.rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
        self.on_attention = on_attention

        self.state = "queued"
        self.step_index = 0
        self.step_total = 0
        self.step_label = ""
        self.message = ""
        self.error = ""
        self.waiting: dict | None = None
        self.done = self.skipped = self.recovered = 0
        self.issues: list[str] = []
        self.lines: collections.deque[str] = collections.deque(maxlen=300)
        self.started = self.finished = 0.0
        self.nodes: list[dict] | None = None        # the most recent successful read
        self._deadline = float("inf")
        self._dismissed: dict[tuple[str, str], int] = {}
        self._attention = threading.Event()
        self._decision = ""

    # -- reporting ---------------------------------------------------------
    def log(self, message: str) -> None:
        line = self.resolver.redact(str(message))
        self.lines.append(line)
        if self._log is not None:
            self._log(f"[{self.label}] {line}")

    def snapshot(self) -> dict:
        elapsed = (self.finished or time.time()) - self.started if self.started else 0.0
        return {"label": self.label, "serial": getattr(self.device, "serial", ""), "state": self.state,
                "step": self.step_index, "steps": self.step_total, "step_label": self.step_label,
                "message": self.message, "error": self.error, "waiting": self.waiting, "done": self.done,
                "skipped": self.skipped, "recovered": self.recovered, "issues": list(self.issues),
                "dry_run": self.dry_run, "elapsed": round(elapsed, 1), "log": list(self.lines)[-40:]}

    # -- time and cancellation ---------------------------------------------
    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()
        if time.monotonic() >= self._deadline:
            raise RunTimeout(f"the run passed its limit of {self.run_timeout:.0f}s (automation.run_timeout)")

    def sleep(self, seconds: float) -> None:
        """Sleep that ends at once on cancel and never outlives the run's deadline."""
        end = min(time.monotonic() + max(0.0, seconds), self._deadline)
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            if self.cancel.wait(min(left, 0.25)):
                raise Cancelled()

    def pause(self, seconds: float | None = None) -> None:
        """The pause after an action. A person does not act on a metronome, and some
        apps notice one; with jitter on, the pause varies a little."""
        base = self.settle if seconds is None else seconds
        if self.dry_run or base <= 0:
            return
        self.sleep(base * self.rng.uniform(0.8, 1.4) if self.jitter else base)

    # -- reading the screen -------------------------------------------------
    def read(self) -> list[dict]:
        """A fresh hierarchy, bounded by the read limit and by what is left of the run."""
        self.check()
        left = self._deadline - time.monotonic()
        nodes = self.device.nodes(timeout=max(1.0, min(float(self.config.read_timeout), left)), cancel=self.cancel)
        self.nodes = nodes
        return nodes

    def lookup(self, name: str) -> str | None:
        return self.resolver.get(name)

    def render_selector(self, selector: Selector) -> Selector:
        return selector.rendered(self.lookup)

    def clear_overlays(self, nodes: list[dict], scope: str) -> bool:
        """Close one pop-up if a rule covers what is on screen. True when something
        was sent (the caller reads the screen again).

        A rule fires only when its guard and its dismiss control are both visible.
        After `max_dismissals` tries within one step the rule is left alone for that
        step: if "closing" it changes nothing, it is not the pop-up it was taken for,
        and tapping it forever helps nobody.
        """
        for rule in self.overlays:
            if find(nodes, self.render_selector(rule.guard)) is None:
                continue
            control = None
            if rule.dismiss is not None:
                control = find(nodes, self.render_selector(rule.dismiss))
                if control is None:
                    continue
            key = (scope, rule.name)
            count = self._dismissed.get(key, 0)
            if count >= rule.max_dismissals:
                if count == rule.max_dismissals:
                    self._dismissed[key] = count + 1
                    self.log(f"the pop-up {rule.name!r} is still showing after {count} attempt(s); leaving it alone")
                continue
            if self.dry_run:
                if count == 0:
                    self.log(f"[practice] would close the pop-up {rule.name!r}")
                    self._dismissed[key] = rule.max_dismissals + 1
                continue
            self._dismissed[key] = count + 1
            self.log(f"closing the pop-up {rule.name!r}")
            if control is not None:
                self.tap_node(control, nodes, what=f"dismiss {rule.name}")
            else:
                self.press_key(rule.key or "back")
            return True
        return False

    def settle_overlays(self, scope: str) -> None:
        """Look before a step that acts without looking.

        A key press, a swipe or typing into whatever has focus never reads the
        screen itself, so a pop-up would swallow it. Steps that wait for an element
        clear pop-ups on the way and do not need this.
        """
        if not self.overlays:
            return
        for _ in range(6):
            try:
                nodes = self.read()
            except UiError:
                return
            if not self.clear_overlays(nodes, scope):
                return
            self.pause()

    def wait_until(self, predicate: Callable[[list[dict]], Any], *, timeout: float, what: str,
                   scope: str | None = None) -> Any:
        """Read the screen until predicate(nodes) is truthy; return what it returned.

        Pop-ups are cleared on the way. A read that fails (screen mid-animation) is
        just another round. Raises StepTimeout when the time is up; in a dry run the
        screen is looked at exactly once.
        """
        deadline = min(time.monotonic() + max(0.0, timeout), self._deadline)
        problem = ""
        while True:
            self.check()
            try:
                nodes: list[dict] | None = self.read()
            except UiError as e:
                nodes, problem = None, str(e)
            if nodes is not None:
                if self.overlays and self.clear_overlays(nodes, scope or what):
                    self.pause()
                    continue
                result = predicate(nodes)
                if result:
                    return result
            if self.dry_run or time.monotonic() >= deadline:
                raise StepTimeout(what, problem)
            self.sleep(min(self.poll, max(0.05, deadline - time.monotonic())))

    # -- acting ------------------------------------------------------------
    def _point_in(self, node: dict) -> tuple[int, int]:
        left, top, right, bottom = node["bounds"]
        cx, cy = (left + right) // 2, (top + bottom) // 2
        if not self.jitter:
            return cx, cy
        # Stay well inside the element: a person rarely hits the exact centre, and
        # never the edge.
        dx, dy = (right - left) * 0.2, (bottom - top) * 0.2
        return (int(min(max(self.rng.gauss(cx, dx / 2), cx - dx), cx + dx)),
                int(min(max(self.rng.gauss(cy, dy / 2), cy - dy), cy + dy)))

    def tap_point(self, x: int, y: int, *, nodes: list[dict] | None, what: str = "", long_ms: int = 0) -> None:
        """The only way a run taps. Enforces the never-tap list on every tap."""
        self.check()
        if self.never_tap:
            if not nodes:
                # Without a hierarchy there is no telling what is under the point.
                raise ForbiddenTap("the screen could not be read, so the never-tap list cannot be checked "
                                   f"for a tap at ({x}, {y})")
            hit = forbidden_at(nodes, int(x), int(y), (self.render_selector(s) for s in self.never_tap))
            if hit is not None:
                raise ForbiddenTap(f"refused to tap {describe_node(hit)!r} at ({x}, {y}): it is on the never-tap list")
        verb = "long-press" if long_ms else "tap"
        if self.dry_run:
            self.log(f"[practice] would {verb} {what or ''} at ({x}, {y})".replace("  ", " "))
            return
        if long_ms:
            self.device.long_press(int(x), int(y), int(long_ms))
        else:
            self.device.tap(int(x), int(y))
        self.log(f"{verb} {what} at ({x}, {y})".replace("  ", " "))

    def tap_node(self, node: dict, nodes: list[dict], *, what: str = "", long_ms: int = 0) -> None:
        x, y = self._point_in(node)
        self.tap_point(x, y, nodes=nodes, what=what or repr(describe_node(node)), long_ms=long_ms)

    def press_key(self, name: str) -> None:
        self.check()
        if self.dry_run:
            self.log(f"[practice] would press {name}")
            return
        self.device.key(name)
        self.log(f"pressed {name}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.check()
        if self.dry_run:
            self.log(f"[practice] would swipe ({x1}, {y1}) -> ({x2}, {y2})")
            return
        if self.jitter:
            ms = int(ms * self.rng.uniform(0.85, 1.25))
        self.device.swipe(int(x1), int(y1), int(x2), int(y2), int(ms))
        self.log(f"swiped ({x1}, {y1}) -> ({x2}, {y2})")

    def type_text(self, value: str, *, shown: str = "", secret: bool = False) -> None:
        self.check()
        if secret:
            self.resolver.mark_secret(value)
        display = shown or ("***" if secret else repr(value[:40]))
        if self.dry_run:
            self.log(f"[practice] would type {display}")
            return
        self.device.text(value)
        self.log(f"typed {display}")

    def clear_field(self, length_hint: int) -> None:
        self.check()
        if self.dry_run:
            self.log("[practice] would clear the field")
            return
        self.device.clear_focused(length_hint)

    def launch(self, package: str, activity: str | None = None) -> None:
        self.check()
        if self.dry_run:
            self.log(f"[practice] would launch {package}")
            return
        component = self.device.launch(package, activity)
        self.log(f"launched {component}")

    def stop_app(self, package: str) -> None:
        self.check()
        if self.dry_run:
            self.log(f"[practice] would stop {package}")
            return
        self.device.stop_app(package)
        self.log(f"stopped {package}")

    def back(self, closed: Callable[[list[dict]], Any] | None = None, *, presses: int = 1) -> bool:
        """Press Back, and wait for the screen to close before pressing it again.

        A second Back sent while the first is still animating leaves the app
        altogether. So after each press the screen is watched; Back is pressed again
        only when `closed` stayed false for the whole watch period.
        """
        for attempt in range(max(1, presses)):
            self.press_key("back")
            if closed is None:
                self.pause()
                continue
            if self.dry_run:
                return False
            try:
                self.wait_until(closed, timeout=BACK_POLL, what="the screen to close", scope=f"back{attempt}")
                return True
            except StepTimeout:
                continue
        return closed is None

    # -- handing over to a person -------------------------------------------
    def resolve_attention(self, action: str) -> bool:
        """Called from the panel: resume (try the step again), skip (move on), abort."""
        if action not in ("resume", "skip", "abort"):
            raise ValueError("action must be resume, skip or abort")
        if self.waiting is None:
            return False
        self._decision = action
        self._attention.set()
        return True

    def ask_human(self, what: str, reason: str) -> str:
        """Stop this device and wait for a person. Returns 'resume' or 'skip'."""
        if not self.config.stop_on_unknown_screen:
            raise StepFailed(f"{what}: {reason} (automation.stop_on_unknown_screen is off, so nobody is asked)")
        self._decision = ""
        self._attention.clear()
        self.message = reason
        self.waiting = {"what": what, "reason": reason, "step": self.step_index, "since": time.time()}
        self.state = "needs_attention"
        self.log(f"needs attention: {what}: {reason}")
        began = time.monotonic()
        if self.on_attention is not None:
            self.on_attention(True)               # the runner lends this device's slot to another
        try:
            while not self._attention.wait(0.2):
                if self.cancel.is_set():
                    raise Cancelled()
                if time.monotonic() - began > ATTENTION_TIMEOUT:
                    raise AttentionExpired(f"{what}: {reason} (nobody answered within {ATTENTION_TIMEOUT / 60:.0f} minutes)")
        finally:
            # Time spent waiting for a person does not count against the run.
            self._deadline += time.monotonic() - began
        decision = self._decision
        if decision == "abort":
            raise Aborted(f"stopped by the operator at {what}")
        if self.on_attention is not None:
            self.on_attention(False)              # take a slot again before carrying on
        self.waiting = None
        self.state = "running"
        self.message = ""
        self.log(f"operator chose: {decision}")
        return decision

    def unknown(self, what: str, reason: str, *, optional: bool = False, policy: str = "stop") -> str:
        """A step cannot be done on this screen. Returns 'retry' or 'skip'."""
        if optional:
            self.skipped += 1
            self.log(f"skipped (optional): {what}: {reason}")
            return "skip"
        if self.dry_run:
            self.issues.append(f"{what}: {reason}")
            self.log(f"[practice] not possible on this screen: {what}: {reason}")
            return "skip"
        if policy == "continue":
            self.skipped += 1
            self.issues.append(f"{what}: {reason}")
            self.log(f"skipped: {what}: {reason}")
            return "skip"
        if self.ask_human(what, reason) == "resume":
            return "retry"
        self.skipped += 1
        return "skip"

    # -- the frame around a run ---------------------------------------------
    def execute(self, body: Callable[[], None]) -> dict:
        self.started = time.time()
        self._deadline = time.monotonic() + self.run_timeout
        self.state = "running"
        try:
            body()
            self.state = "done"
            if self.dry_run:
                self.message = ("practice run: nothing was sent"
                                + (f"; {len(self.issues)} step(s) not possible on the current screen" if self.issues else ""))
            elif self.issues:
                self.message = f"finished with {len(self.issues)} skipped step(s)"
        except Cancelled:
            self.state, self.message = "cancelled", "cancelled"
        except Aborted as e:
            self.state, self.error = "failed", str(e)
        except AttentionExpired as e:
            self.state, self.error = "needs_attention", str(e)
        except (RunTimeout, StepFailed, StepTimeout, ForbiddenTap, UiError, MissingVariable, PoolEmpty) as e:
            self.state, self.error = "failed", self.resolver.redact(str(e))
        except Exception as e:  # noqa: BLE001 - one device's crash stays that device's result
            self.state, self.error = "failed", self.resolver.redact(f"{type(e).__name__}: {e}")
        finally:
            self.finished = time.time()
            self.waiting = None
            if self.error:
                self.message = self.error
                self.log(f"{self.state}: {self.error}")
            try:
                self.device.close()
            except Exception:  # noqa: BLE001, S110 - closing the reader is best effort
                pass
        return self.snapshot()


# ------------------------------------------------------- recovery plumbing
def bind_actions(actions: list[dict], elements: list[dict]) -> list[dict]:
    """Turn a model's cleaned actions into concrete ones.

    A tap names an element by its number in the list the model was shown; here it
    becomes that element's identity (to find it again when the fix is replayed) and
    its centre. Numbers outside the list were already dropped by clean_recovery;
    anything unexpected that still arrives is dropped here.
    """
    out: list[dict] = []
    for action in actions:
        kind = action.get("type")
        if kind == "tap_element":
            index = action.get("element")
            if not isinstance(index, int) or not 0 <= index < len(elements):
                continue
            node = elements[index]
            selector = {k: node[k] for k in ("id", "text", "desc", "cls") if node.get(k)}
            if node.get("editable"):
                selector.pop("text", None)       # a field's content is not its identity
            out.append({"type": "tap", "selector": selector, "x": node["center"][0], "y": node["center"][1]})
        elif kind == "key" and action.get("name") in llm_mod.RECOVERY_KEYS:
            out.append({"type": "key", "name": action["name"]})
        elif kind == "swipe" and action.get("direction") in llm_mod.DIRECTIONS:
            out.append({"type": "swipe", "direction": action["direction"]})
        elif kind == "wait":
            out.append({"type": "wait", "seconds": max(0.2, min(float(action.get("seconds") or 1), 10.0))})
    return out


class ProgramRun(Session):
    """One device playing one trained program."""

    def __init__(self, program: Program, device: Any, config: AutomationConfig, *,
                 memory: FixMemory | None = None, model: llm_mod.LlmClient | None = None, **kwargs: Any):
        super().__init__(device, config, **kwargs)
        self.program = program
        self.memory = memory if config.learn_fixes else None
        self.model = model
        self.threshold = float(config.match_threshold)
        self.step_total = len(program.steps)
        self._rounds: dict[str, int] = {}
        self._screen: tuple[int, int] | None = None

    def run(self) -> dict:
        return self.execute(self._body)

    # -- the loop ----------------------------------------------------------
    def _body(self) -> None:
        for index, step in enumerate(self.program.steps):
            self.check()
            self.step_index, self.step_label = index, step.label()
            self.log(f"{step.id} {step.label()}")
            outcome = "retry"
            while outcome == "retry":
                try:
                    outcome = self._do(step)
                except ForbiddenTap as e:
                    outcome = self.unknown(step.label(), str(e), optional=step.optional)
                except (MissingVariable, PoolEmpty) as e:
                    outcome = self.unknown(step.label(), str(e))
            if outcome == "done":
                self.done += 1
                self.pause(step.wait_after or None)

    def _do(self, step: ProgramStep) -> str:
        kind = step.kind
        if kind == "wait":
            if not self.dry_run:
                self.sleep(min(step.seconds or 1.0, 120.0))
            return "done"
        if kind == "key":
            self.settle_overlays(step.id)
            self.press_key(step.key or "back")
            return "done"
        if kind == "launch":
            package = step.package or self.program.package
            if not package:
                return self.unknown(step.label(), "this step does not say which app to start")
            self.launch(package)
            return "done"
        if kind == "swipe":
            if not (step.device and step.to):
                self.log("swipe had no coordinates; skipped")
                return "skip"
            self.settle_overlays(step.id)
            self.swipe(*self._scaled(step.device), *self._scaled(step.to))
            return "done"
        if kind in ("tap", "long_press"):
            return self._do_tap(step)
        if kind == "type":
            return self._do_type(step)
        if kind == "assert":
            node, _, how = self._locate(step)
            if node is not None:
                self.log(f"present: {how}")
                return "done"
            return self._stuck(step, "the expected element is not on screen")
        self.log(f"unknown step kind {kind!r}; skipped")
        return "skip"

    # -- finding things ----------------------------------------------------
    def _device_size(self) -> tuple[int, int]:
        if self._screen is None:
            self._screen = tuple(self.device.size())  # type: ignore[assignment]
        return self._screen  # type: ignore[return-value]

    def _scaled(self, point: list) -> tuple[int, int]:
        """Recorded coordinates on this device's screen (resolutions may differ)."""
        recorded = self.program.screen
        if not recorded or len(recorded) != 2 or not all(recorded):
            return int(point[0]), int(point[1])
        try:
            width, height = self._device_size()
        except UiError:
            return int(point[0]), int(point[1])
        return int(point[0] * width / recorded[0]), int(point[1] * height / recorded[1])

    def _match(self, step: ProgramStep, nodes: list[dict]) -> tuple[dict | None, str]:
        near = self._scaled(step.device) if step.device else None
        return find_scored(nodes, step.candidates(), near=near, threshold=self.threshold)

    def _locate(self, step: ProgramStep) -> tuple[dict | None, list[dict] | None, str]:
        """Wait for the step's element. (node, the nodes it was found in, how)."""
        if not step.candidates():
            try:
                return None, self.read(), "nothing to look for"
            except UiError as e:
                return None, None, str(e)
        timeout = min(self.step_timeout, OPTIONAL_WAIT) if step.optional else self.step_timeout

        def look(nodes: list[dict]) -> Any:
            node, how = self._match(step, nodes)
            return (node, nodes, how) if node is not None else None

        try:
            return self.wait_until(look, timeout=timeout, what=step.label(), scope=step.id)
        except StepTimeout as e:
            return None, self.nodes, e.detail or "not found"

    def _area_matches(self, step: ProgramStep) -> tuple[bool, float]:
        """Whether the saved picture of the tapped area still matches the screen."""
        path = self.program.shot_path(step.shot or "")
        saved = load_image(path) if path else None
        if saved is None or not step.shot_box:
            return False, 0.0
        try:
            live = self.device.screenshot()
        except UiError:
            return False, 0.0
        box = scale_box(step.shot_box, self.program.screen, (live.width, live.height))
        value = patch_similarity(saved, crop(live, box))
        return value >= PATCH_MATCH, value

    # -- doing things ------------------------------------------------------
    def _do_tap(self, step: ProgramStep) -> str:
        long_ms = int((step.seconds or 0.7) * 1000) if step.kind == "long_press" else 0
        node, nodes, how = self._locate(step)
        if node is not None and nodes is not None:
            self.log(f"found by {how}")
            self.tap_node(node, nodes, long_ms=long_ms)
            return "done"
        if step.optional:
            return self.unknown(step.label(), "its element is not on screen", optional=True)
        reason = "the element is not on screen"
        if step.device:
            point = self._scaled(step.device)
            if step.shot:
                same, value = self._area_matches(step)
                if same:
                    self.log(f"no element match; the area still looks the same ({value:.2f}), using the recorded position")
                    self.tap_point(*point, nodes=nodes, what="the recorded position", long_ms=long_ms)
                    return "done"
                reason = f"the element is not on screen and the area looks different (similarity {value:.2f})"
            elif not step.candidates():
                # Nothing was identifiable when this was recorded and no picture was
                # kept: the position is all there is.
                self.log("this step has only a position; using it")
                self.tap_point(*point, nodes=nodes, what="the recorded position", long_ms=long_ms)
                return "done"
        return self._stuck(step, reason)

    def _value_for(self, step: ProgramStep) -> str:
        if step.dynamic:
            value = self.resolver.get(step.dynamic)
            if value is None:
                raise MissingVariable(step.dynamic)
            return value
        return step.text or ""

    def _do_type(self, step: ProgramStep) -> str:
        value = self._value_for(step)
        if not step.candidates():
            self.settle_overlays(step.id)
        node, nodes, how = self._locate(step) if step.candidates() else (None, None, "")
        if node is not None and nodes is not None:
            self.log(f"field found by {how}")
            self.tap_node(node, nodes)
            self.pause(0.4)
        elif step.candidates() and not self.dry_run:
            self.log("the field was not found; typing into whatever has focus")
        length = 0
        if not self.dry_run:
            try:
                focused = next((n for n in self.read() if n["focused"] and n["editable"]), None)
                length = len(focused["text"]) if focused else 0
            except UiError:
                length = 0
        if length:
            self.clear_field(length)
        secret = bool(step.dynamic and self.resolver.is_secret(step.dynamic))
        self.type_text(value, shown=f"{{{step.dynamic}}}" if step.dynamic else "", secret=secret)
        return "done"

    # -- getting unstuck ---------------------------------------------------
    def _stuck(self, step: ProgramStep, reason: str) -> str:
        if step.optional:
            return self.unknown(step.label(), reason, optional=True)
        if self.dry_run:
            return self.unknown(step.label(), reason)
        rounds = self._rounds.get(step.id, 0)
        if rounds < MAX_RECOVERY_ROUNDS:
            self._rounds[step.id] = rounds + 1
            if self._recover(step, reason):
                return "retry"
        if not self.config.stop_on_unknown_screen and step.device and step.kind in ("tap", "long_press"):
            # The operator chose guessing over stopping: use the recorded position.
            self.log(f"{reason}; guessing with the recorded position (automation.stop_on_unknown_screen is off)")
            self.tap_point(*self._scaled(step.device), nodes=self.nodes, what="the recorded position",
                           long_ms=int((step.seconds or 0.7) * 1000) if step.kind == "long_press" else 0)
            return "done"
        return self.unknown(step.label(), reason)

    def _possible(self, step: ProgramStep) -> bool:
        try:
            nodes = self.read()
        except UiError:
            return False
        if step.candidates():
            return self._match(step, nodes)[0] is not None
        return True

    def _fingerprint(self) -> tuple[str, int | None]:
        """(package, screen hash): what a remembered fix is keyed by. The hash is None
        when no screenshot could be taken (0 is a real hash: a blank screen)."""
        package = self.program.package or foreground_package(self.nodes or []) or self.device.current()[0]
        try:
            return package, dhash(self.device.screenshot())
        except UiError:
            return package, None

    def _recover(self, step: ProgramStep, reason: str) -> bool:
        self.log(f"stuck: {reason}")
        package, screen_hash = self._fingerprint()

        if self.memory is not None and screen_hash is not None:
            remembered = self.memory.lookup(package=package, dhash=screen_hash)
            if remembered is not None:
                self.log(f"trying the fix remembered as {remembered.id}")
                worked = self._apply(remembered.actions) and self._possible(step)
                self.memory.bump(remembered.id, worked)
                if worked:
                    self.recovered += 1
                    return True

        for _ in range(MAX_MODEL_ATTEMPTS if self.model is not None else 0):
            self.check()
            plan = self._ask_model(step, reason, package)
            if not plan:
                break
            analysis, actions = plan
            if self._apply(actions) and self._possible(step):
                self.recovered += 1
                if self.memory is not None and screen_hash is not None:
                    self.memory.remember(package=package, dhash=screen_hash, actions=actions, source="model",
                                         analysis=analysis, program=self.program.slug, step_id=step.id)
                    self.log("remembered this fix for next time")
                return True
        return False

    def _ask_model(self, step: ProgramStep, reason: str, package: str) -> tuple[str, list[dict]] | None:
        assert self.model is not None
        try:
            nodes = self.read()
        except UiError:
            return None
        elements = interesting(nodes)
        words = self.config.forbidden_field_words
        payload = {"goal": self.program.goal or self.program.name,
                   "stuck_step": {"id": step.id, "meant_to": step.label(),
                                  "looking_for": {k: v for k, v in step.selector.items() if k != "path"} or None},
                   "problem": reason,
                   "screen": {"app": package, "elements": llm_mod.elements_for_model(elements, words)},
                   "worked_before": self.memory.examples(package) if self.memory is not None else []}
        try:
            analysis, actions, needs_human = llm_mod.clean_recovery(llm_mod.recover(self.model, payload), len(elements))
        except llm_mod.LlmError as e:
            self.log(f"the model is unavailable: {e}")
            return None
        self.log(f"model: {analysis}")
        if needs_human or not actions:
            return None
        return analysis, bind_actions(actions, elements)

    def _apply(self, actions: list[dict]) -> bool:
        """Carry out bound recovery actions. Every tap is re-found on the live screen
        and passes the never-tap list like any other tap."""
        for action in actions:
            self.check()
            kind = action.get("type")
            try:
                if kind == "tap":
                    nodes = self.read()
                    node, _ = find_scored(nodes, [action.get("selector") or {}],
                                          near=(action.get("x", 0), action.get("y", 0)), threshold=self.threshold)
                    if node is None:
                        self.log("the element this fix taps is not on screen")
                        return False
                    self.tap_node(node, nodes)
                elif kind == "key":
                    if action.get("name") not in llm_mod.RECOVERY_KEYS:
                        return False
                    self.press_key(action["name"])
                elif kind == "swipe":
                    width, height = self._device_size()
                    span_x, span_y = int(width * 0.35), int(height * 0.35)
                    dx, dy = {"up": (0, -span_y), "down": (0, span_y), "left": (-span_x, 0),
                              "right": (span_x, 0)}.get(str(action.get("direction")), (0, -span_y))
                    self.swipe(width // 2, height // 2, width // 2 + dx, height // 2 + dy, 280)
                elif kind == "wait":
                    self.sleep(max(0.2, min(float(action.get("seconds") or 1), 10.0)))
                else:
                    return False
            except ForbiddenTap as e:
                self.log(f"recovery refused: {e}")
                return False
            except (UiError, ValueError, KeyError, TypeError) as e:
                self.log(f"that did not work: {e}")
                return False
            self.pause()
        return True

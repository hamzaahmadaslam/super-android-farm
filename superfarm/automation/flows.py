"""Hand-written flows: small YAML state machines for any app.

A flow says what to do in terms of what is *on screen*, never where it is:

    name: settings-search
    package: com.android.settings
    variables: {query: battery}          # null = must be supplied when the run starts
    on_unknown: stop                     # stop (ask a person) | continue (skip the step)
    timeouts: {step: 20, run: 300}
    overlays:                            # checked before every step and while waiting
      - name: rate-us
        guard: {id: rate_dialog_title}
        dismiss: {id: rate_dialog_close}     # or  key: back
    never_tap:                           # enforced on every tap of the run
      - {text: Erase all data}
    steps:
      - launch: com.android.settings
      - wait_for: {id: search_action_bar}
      - tap: {id: search_action_bar}
      - type: {text: "{query}", into: {editable: true}}
      - wait_for: {text_regex: "(?i).*{query}.*"}

Steps: launch, stop_app, tap, long_press, type, clear, key, back, home, swipe,
scroll_to, wait_for, wait_gone, assert, sleep, if/then/else, repeat/until, set,
screenshot, call, fail. Every step may carry `timeout`, `optional` and `label`.

A selector is a mapping ({id, text, text_regex, desc, cls, package, editable,
clickable, focused, checked, selected, index, within}), a bare string (exact text),
or `@role` from the app's profile. A condition is a selector (visible), or
{visible|gone: selector}, {activity: regex}, {app: package}, {equals: [a, b]},
{any|all: [...]}, {not: condition}.

Everything is validated when the flow is loaded: an unknown key or a step with two
actions is an error with its position, not a surprise half-way through a run on
forty devices.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..config import AutomationConfig
from ..paths import PACKAGE_DIR
from .memory import save_png
from .profiles import AppProfile, OverlayRule, Profiles
from .runtime import ForbiddenTap, Session, StepFailed, StepTimeout
from .selectors import MissingVariable, Selector, find, render, variables_in
from .store import AutomationStore, check_package, check_slug
from .ui import UiError, foreground_package
from .values import PoolEmpty, check_name, looks_secret

MAX_FLOW_BYTES = 256 * 1024
MAX_STEPS = 2000
MAX_NESTING = 12
MAX_CALL_DEPTH = 8
EXAMPLES_DIR = PACKAGE_DIR / "data" / "flows" / "examples"
_FLOW_NAME = re.compile(r"^(examples/)?[a-z0-9][a-z0-9-]{0,63}$")
_CONDITION_KEYS = {"visible", "gone", "activity", "app", "equals", "any", "all", "not"}
_COMMON = ("timeout", "optional", "label")
# Steps that act without reading the screen first; pop-up rules are checked before them.
_BLIND = {"key", "back", "home", "swipe", "type", "clear", "screenshot"}


class FlowError(ValueError):
    """A flow file is not valid. The message says where and how to fix it."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ----------------------------------------------------------------- conditions
class Condition(_Model):
    visible: Selector | None = None
    gone: Selector | None = None
    activity: str | None = Field(None, max_length=300, description="Regular expression searched in `package/activity` of the foreground.")
    app: str | None = Field(None, max_length=200, description="Package that must be in the foreground.")
    equals: tuple[str, str] | None = Field(None, description="Two texts (usually with {variables}) that must be the same.")
    any_of: list[Condition] | None = Field(None, alias="any", min_length=1, max_length=20)
    all_of: list[Condition] | None = Field(None, alias="all", min_length=1, max_length=20)
    negated: Condition | None = Field(None, alias="not")

    @model_validator(mode="before")
    @classmethod
    def _selector_means_visible(cls, value: Any) -> Any:
        if isinstance(value, str) or (isinstance(value, dict) and not (set(value) & _CONDITION_KEYS)):
            return {"visible": value}
        return value

    @model_validator(mode="after")
    def _exactly_one(self) -> Condition:
        given = [k for k in ("visible", "gone", "activity", "app", "equals", "any_of", "all_of", "negated")
                 if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError("a condition needs exactly one of: visible, gone, activity, app, equals, any, all, not")
        if self.activity is not None and not variables_in(self.activity):
            try:
                re.compile(self.activity)
            except re.error as e:
                raise ValueError(f"activity {self.activity!r} is not a valid regular expression: {e}") from None
        return self

    def describe(self) -> str:
        if self.visible is not None:
            return self.visible.describe()
        if self.gone is not None:
            return f"no {self.gone.describe()}"
        if self.activity is not None:
            return f"activity ~ {self.activity}"
        if self.app is not None:
            return f"app {self.app}"
        if self.equals is not None:
            return f"{self.equals[0]} = {self.equals[1]}"
        if self.any_of is not None:
            return " or ".join(c.describe() for c in self.any_of)
        if self.all_of is not None:
            return " and ".join(c.describe() for c in self.all_of)
        return f"not ({self.negated.describe()})" if self.negated else "?"


# ----------------------------------------------------------- step arguments
class LaunchArgs(_Model):
    package: str | None = Field(None, description="App to start. Default: the flow's package.")
    activity: str | None = Field(None, description="Activity to start instead of the launcher one.")
    fresh: bool = Field(False, description="Force-stop the app first, so it starts from its first screen.")
    wait: Condition | None = Field(None, description="What must be true before the next step.")


class StopArgs(_Model):
    package: str | None = None


class TargetArgs(_Model):
    target: Selector


class LongPressArgs(_Model):
    target: Selector
    ms: int = Field(700, ge=300, le=10_000)


class TypeArgs(_Model):
    text: str = Field(..., max_length=10_000)
    into: Selector | None = Field(None, description="Field to tap first. Default: whatever has focus.")
    clear: bool = Field(False, description="Empty the field before typing.")
    enter: bool = Field(False, description="Press Enter afterwards.")
    secret: bool = Field(False, description="Never show the typed text in logs.")


class ClearArgs(_Model):
    target: Selector | None = None


class KeyArgs(_Model):
    name: str = Field(..., pattern=r"^[A-Za-z0-9_]{1,40}$")


class BackArgs(_Model):
    until: Condition | None = Field(None, description="Press Back again (after watching the screen) until this holds.")
    presses: int = Field(1, ge=1, le=5)


class NoArgs(_Model):
    pass


class SwipeArgs(_Model):
    direction: Literal["up", "down", "left", "right"] | None = Field(None, description="Where the finger moves.")
    distance: float = Field(0.5, gt=0, le=0.9, description="Fraction of the screen (or of `in`) to travel.")
    start: tuple[float, float] | None = Field(None, alias="from", description="[x, y] as fractions 0..1 of the screen.")
    end: tuple[float, float] | None = Field(None, alias="to")
    ms: int = Field(300, ge=50, le=10_000)
    within: Selector | None = Field(None, alias="in", description="Swipe inside this element instead of the whole screen.")

    @model_validator(mode="after")
    def _shape(self) -> SwipeArgs:
        if (self.direction is None) == (self.start is None and self.end is None):
            raise ValueError("swipe needs either `direction` or both `from` and `to`")
        if self.direction is None and (self.start is None or self.end is None):
            raise ValueError("swipe needs both `from` and `to`")
        for point in (self.start, self.end):
            if point is not None and not all(0.0 <= v <= 1.0 for v in point):
                raise ValueError("swipe `from`/`to` are fractions of the screen between 0 and 1")
        return self


class ScrollToArgs(_Model):
    target: Selector
    direction: Literal["up", "down", "left", "right"] = Field("down", description="Which way to look: down = further down the list.")
    max_swipes: int = Field(10, ge=1, le=100)
    within: Selector | None = Field(None, alias="in")


class ConditionArgs(_Model):
    condition: Condition
    message: str = Field("", max_length=300)


class SleepArgs(_Model):
    seconds: float = Field(..., ge=0, le=600)


class RepeatArgs(_Model):
    times: int = Field(..., ge=1, le=10_000, description="Upper limit of rounds; there is no unbounded loop.")
    until: Condition | None = Field(None, description="Stop as soon as this holds. If it never does, the step fails.")


class SetArgs(_Model):
    var: str
    source: Selector | None = Field(None, alias="from", description="Element whose text is captured.")
    attr: Literal["text", "desc", "id"] = "text"
    value: str | None = Field(None, max_length=10_000, description="Literal (may contain {variables}).")
    pool: str | None = Field(None, description="Take a fresh line from this pool.")
    regex: str | None = Field(None, max_length=300, description="Keep only the first group (or the whole match).")

    @model_validator(mode="after")
    def _one_source(self) -> SetArgs:
        check_name(self.var)
        if sum(v is not None for v in (self.source, self.value, self.pool)) != 1:
            raise ValueError("set needs exactly one of `from` (a selector), `value` or `pool`")
        if self.pool is not None:
            check_name(self.pool)
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as e:
                raise ValueError(f"regex {self.regex!r} is not valid: {e}") from None
        return self


class ScreenshotArgs(_Model):
    name: str = Field("", pattern=r"^[A-Za-z0-9_-]{0,40}$")


class CallArgs(_Model):
    flow: str
    variables: dict[str, str] = Field(default_factory=dict, alias="with")

    @model_validator(mode="after")
    def _names(self) -> CallArgs:
        if not _FLOW_NAME.match(self.flow):
            raise ValueError(f"bad flow name {self.flow!r}")
        for name in self.variables:
            check_name(name)
        return self


class FailArgs(_Model):
    message: str = Field("the flow asked to stop", max_length=300)


# kind -> (argument model, name of the field the bare value goes into, extra option keys)
_KINDS: dict[str, tuple[type[_Model], str | None, tuple[str, ...]]] = {
    "launch": (LaunchArgs, "package", ()),
    "stop_app": (StopArgs, "package", ()),
    "tap": (TargetArgs, "target", ()),
    "long_press": (LongPressArgs, "target", ("ms",)),
    "type": (TypeArgs, "text", ()),
    "clear": (ClearArgs, "target", ()),
    "key": (KeyArgs, "name", ()),
    "back": (BackArgs, None, ()),
    "home": (NoArgs, None, ()),
    "swipe": (SwipeArgs, "direction", ()),
    "scroll_to": (ScrollToArgs, "target", ("direction", "max_swipes", "in")),
    "wait_for": (ConditionArgs, "condition", ()),
    "wait_gone": (TargetArgs, "target", ()),
    "assert": (ConditionArgs, "condition", ("message",)),
    "sleep": (SleepArgs, "seconds", ()),
    "if": (ConditionArgs, "condition", ("then", "else")),
    "repeat": (RepeatArgs, "times", ("until", "steps")),
    "set": (SetArgs, None, ()),
    "screenshot": (ScreenshotArgs, "name", ()),
    "call": (CallArgs, "flow", ("with",)),
    "fail": (FailArgs, "message", ()),
}
# Kinds whose bare value is itself a mapping (a selector or a condition), so a mapping
# is never mistaken for the argument model's own fields.
_WHOLE_VALUE = {"tap", "long_press", "scroll_to", "wait_for", "wait_gone", "assert", "if", "clear"}


@dataclass
class FlowStep:
    kind: str
    args: Any
    where: str
    timeout: float | None = None
    optional: bool = False
    label: str = ""
    then: list[FlowStep] = field(default_factory=list)
    otherwise: list[FlowStep] = field(default_factory=list)
    body: list[FlowStep] = field(default_factory=list)

    def describe(self) -> str:
        if self.label:
            return self.label
        a = self.args
        if self.kind in ("tap", "long_press", "wait_gone", "scroll_to"):
            return f"{self.kind} {a.target.describe()}"
        if self.kind in ("wait_for", "assert", "if"):
            return f"{self.kind} {a.condition.describe()}"
        if self.kind == "type":
            return f"type {a.text!r}" if not a.secret else "type ***"
        if self.kind in ("launch", "stop_app"):
            return f"{self.kind} {a.package or 'the app'}"
        if self.kind == "key":
            return f"key {a.name}"
        if self.kind == "call":
            return f"call {a.flow}"
        if self.kind == "set":
            return f"set {a.var}"
        if self.kind == "repeat":
            return f"repeat up to {a.times}x"
        if self.kind == "sleep":
            return f"sleep {a.seconds:g}s"
        return self.kind


@dataclass
class Flow:
    name: str
    description: str = ""
    package: str = ""
    variables: dict[str, str | None] = field(default_factory=dict)
    overlays: list[OverlayRule] = field(default_factory=list)
    never_tap: list[Selector] = field(default_factory=list)
    on_unknown: str = "stop"
    step_timeout: float | None = None
    run_timeout: float | None = None
    steps: list[FlowStep] = field(default_factory=list)
    builtin: bool = False

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description, "package": self.package,
                "variables": dict(self.variables), "steps": len(self.steps), "builtin": self.builtin,
                "overlays": [o.name for o in self.overlays], "never_tap": len(self.never_tap),
                "on_unknown": self.on_unknown, "outline": [s.describe() for s in self.steps]}

    def calls(self) -> set[str]:
        out: set[str] = set()

        def walk(steps: list[FlowStep]) -> None:
            for step in steps:
                if step.kind == "call":
                    out.add(step.args.flow)
                walk(step.then)
                walk(step.otherwise)
                walk(step.body)

        walk(self.steps)
        return out

    def selectors(self) -> list[Selector]:
        """Every selector the flow mentions, for checking roles before a run."""
        found: list[Selector] = []

        def dig(value: Any) -> None:
            if isinstance(value, Selector):
                found.append(value)
                if value.within is not None:
                    dig(value.within)
            elif isinstance(value, BaseModel):
                for name in type(value).model_fields:
                    dig(getattr(value, name))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    dig(item)

        def walk(steps: list[FlowStep]) -> None:
            for step in steps:
                dig(step.args)
                walk(step.then)
                walk(step.otherwise)
                walk(step.body)

        dig(self.overlays)
        dig(self.never_tap)
        walk(self.steps)
        return found


# ------------------------------------------------------------------- parsing
def _explain(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(p) for p in first["loc"] if p != "__root__")
    message = first["msg"].removeprefix("Value error, ")
    return f"{where}: {message}" if where else message


def _validate(model: type[BaseModel], value: Any, where: str) -> Any:
    try:
        return model.model_validate(value)
    except ValidationError as e:
        raise FlowError(f"{where}: {_explain(e)}") from None


def _parse_steps(raw: Any, where: str, depth: int, counter: list[int]) -> list[FlowStep]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FlowError(f"{where}: expected a list of steps")
    if depth > MAX_NESTING:
        raise FlowError(f"{where}: steps are nested more than {MAX_NESTING} levels deep")
    return [_parse_step(item, f"{where}[{index}]", depth, counter) for index, item in enumerate(raw)]


def _parse_step(raw: Any, where: str, depth: int, counter: list[int]) -> FlowStep:
    counter[0] += 1
    if counter[0] > MAX_STEPS:
        raise FlowError(f"the flow has more than {MAX_STEPS} steps")
    if isinstance(raw, str):
        raw = {raw: None}                           # `- back`, `- home`
    if not isinstance(raw, dict) or not raw:
        raise FlowError(f"{where}: a step is a mapping like `tap: {{id: ok}}`")
    kinds = [k for k in raw if k in _KINDS]
    if len(kinds) != 1:
        unknown = [str(k) for k in raw if k not in _KINDS and k not in _COMMON]
        if not kinds:
            raise FlowError(f"{where}: no action found (saw {', '.join(unknown) or 'nothing'}). "
                            f"Actions: {', '.join(_KINDS)}")
        raise FlowError(f"{where}: a step does one thing, but this one names {', '.join(kinds)}")
    kind = kinds[0]
    model, bare, options = _KINDS[kind]
    allowed = {kind, *_COMMON, *options}
    extra = [str(k) for k in raw if k not in allowed]
    if extra:
        raise FlowError(f"{where}.{kind}: unknown key(s) {', '.join(extra)}; allowed here: "
                        f"{', '.join(sorted(allowed - {kind}))}")
    value = raw[kind]
    nested = ("then", "else", "steps")
    if kind in _WHOLE_VALUE:
        args: dict[str, Any] = {} if value is None and kind == "clear" else {bare: value}
    elif isinstance(value, dict):
        args = dict(value)
    elif value is None:
        args = {}
    else:
        if bare is None:
            raise FlowError(f"{where}.{kind}: expected a mapping")
        args = {bare: value}
    if kind == "repeat" and isinstance(value, dict) and "steps" in args:
        raw = {**raw, "steps": args.pop("steps")}
    for key in options:
        if key in raw and key not in nested:
            args[key] = raw[key]
    step = FlowStep(kind=kind, args=_validate(model, args, f"{where}.{kind}"), where=where)
    if "timeout" in raw and raw["timeout"] is not None:
        try:
            step.timeout = float(raw["timeout"])
        except (TypeError, ValueError):
            raise FlowError(f"{where}: timeout must be a number of seconds") from None
        if not 0 < step.timeout <= 3600:
            raise FlowError(f"{where}: timeout must be between 0 and 3600 seconds")
    if "optional" in raw:
        if not isinstance(raw["optional"], bool):
            raise FlowError(f"{where}: optional must be true or false")
        step.optional = raw["optional"]
    if raw.get("label") is not None:
        step.label = str(raw["label"])[:120]
    if kind == "if":
        step.then = _parse_steps(raw.get("then"), f"{where}.then", depth + 1, counter)
        step.otherwise = _parse_steps(raw.get("else"), f"{where}.else", depth + 1, counter)
        if not step.then and not step.otherwise:
            raise FlowError(f"{where}.if: needs `then` and/or `else` with steps")
    if kind == "repeat":
        step.body = _parse_steps(raw.get("steps"), f"{where}.steps", depth + 1, counter)
        if not step.body:
            raise FlowError(f"{where}.repeat: needs `steps`")
    return step


_TOP_KEYS = {"name", "description", "package", "variables", "overlays", "never_tap", "on_unknown", "timeouts", "steps"}


def parse_flow(data: Any, *, name: str = "", forbidden_words: tuple[str, ...] | list[str] = (),
               builtin: bool = False) -> Flow:
    """Validate a loaded YAML document and turn it into a Flow."""
    if not isinstance(data, dict):
        raise FlowError("a flow is a YAML mapping with at least `steps`")
    unknown = [str(k) for k in data if k not in _TOP_KEYS]
    if unknown:
        raise FlowError(f"unknown key(s) {', '.join(unknown)}; a flow may have: {', '.join(sorted(_TOP_KEYS))}")
    flow = Flow(name=name or str(data.get("name") or "flow"), builtin=builtin,
                description=str(data.get("description") or "")[:2000])
    if data.get("package"):
        try:
            flow.package = check_package(str(data["package"]))
        except ValueError as e:
            raise FlowError(f"package: {e}") from None

    variables = data.get("variables") or {}
    if not isinstance(variables, dict):
        raise FlowError("variables: expected a mapping of name -> default (null = must be supplied)")
    for key, default in variables.items():
        try:
            check_name(str(key))
        except ValueError as e:
            raise FlowError(f"variables: {e}") from None
        if isinstance(default, (dict, list)):
            raise FlowError(f"variables.{key}: a default is a single value")
        if default not in (None, "") and looks_secret(str(key), forbidden_words):
            raise FlowError(f"variables.{key}: this looks like a secret, and a flow file is not the place for "
                            "one. Leave the default empty (null) and pass the value when the run is started.")
        flow.variables[str(key)] = None if default is None else str(default)

    overlays = data.get("overlays") or []
    if not isinstance(overlays, list):
        raise FlowError("overlays: expected a list")
    flow.overlays = [_validate(OverlayRule, item, f"overlays[{i}]") for i, item in enumerate(overlays)]
    if len({o.name for o in flow.overlays}) != len(flow.overlays):
        raise FlowError("overlays: two rules share a name")
    never = data.get("never_tap") or []
    if not isinstance(never, list):
        raise FlowError("never_tap: expected a list of selectors")
    flow.never_tap = [_validate(Selector, item, f"never_tap[{i}]") for i, item in enumerate(never)]

    flow.on_unknown = str(data.get("on_unknown") or "stop")
    if flow.on_unknown not in ("stop", "continue"):
        raise FlowError("on_unknown: use `stop` (ask a person) or `continue` (skip the step)")
    timeouts = data.get("timeouts") or {}
    if not isinstance(timeouts, dict) or set(timeouts) - {"step", "run"}:
        raise FlowError("timeouts: expected {step: seconds, run: seconds}")
    for key, limit in (("step", 3600), ("run", 86_400)):
        if timeouts.get(key) is not None:
            try:
                value = float(timeouts[key])
            except (TypeError, ValueError):
                raise FlowError(f"timeouts.{key}: expected a number of seconds") from None
            if not 0 < value <= limit:
                raise FlowError(f"timeouts.{key}: must be between 0 and {limit} seconds")
            setattr(flow, f"{key}_timeout", value)

    flow.steps = _parse_steps(data.get("steps"), "steps", 0, [0])
    if not flow.steps:
        raise FlowError("steps: a flow needs at least one step")
    return flow


def parse_flow_text(text: str, *, name: str = "", forbidden_words: tuple[str, ...] | list[str] = (),
                    builtin: bool = False) -> Flow:
    if len(text.encode("utf-8", errors="replace")) > MAX_FLOW_BYTES:
        raise FlowError(f"the flow is larger than {MAX_FLOW_BYTES // 1024} KB")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        at = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise FlowError(f"not valid YAML{at}: {getattr(e, 'problem', None) or e}") from None
    return parse_flow(data, name=name, forbidden_words=forbidden_words, builtin=builtin)


# ------------------------------------------------------------------- library
class FlowLibrary:
    """The flows a farm has: its own (automation/flows) and the bundled examples,
    which are addressed as `examples/<name>` and are read-only."""

    def __init__(self, store: AutomationStore, forbidden_words: tuple[str, ...] | list[str] = (),
                 examples_dir: Path = EXAMPLES_DIR):
        self.store = store
        self.forbidden_words = tuple(forbidden_words)
        self.examples_dir = Path(examples_dir)

    @staticmethod
    def check_name(name: str) -> str:
        if not isinstance(name, str) or not _FLOW_NAME.match(name):
            raise FlowError(f"bad flow name {name!r}: use lower-case letters, digits and dashes "
                            "(bundled examples are `examples/<name>`)")
        return name

    def _example_path(self, name: str) -> Path:
        return self.examples_dir / f"{check_slug(name.split('/', 1)[1])}.yaml"

    def text(self, name: str) -> str:
        self.check_name(name)
        if name.startswith("examples/"):
            try:
                return self._example_path(name).read_text(encoding="utf-8")
            except FileNotFoundError:
                raise KeyError(f"no bundled example flow {name!r}") from None
        return self.store.read_flow(name)

    def get(self, name: str) -> Flow:
        try:
            return parse_flow_text(self.text(name), name=name, forbidden_words=self.forbidden_words,
                                   builtin=name.startswith("examples/"))
        except FlowError as e:
            raise FlowError(f"flow {name!r}: {e}") from None

    def names(self) -> list[str]:
        examples = sorted(f"examples/{p.stem}" for p in self.examples_dir.glob("*.yaml")) \
            if self.examples_dir.is_dir() else []
        return [*self.store.flow_names(), *examples]

    def list(self) -> list[dict]:
        out = []
        for name in self.names():
            try:
                out.append(self.get(name).summary())
            except (FlowError, KeyError) as e:
                out.append({"name": name, "error": str(e), "builtin": name.startswith("examples/")})
        return out

    def save(self, name: str, text: str) -> Flow:
        """Validate, then store. A flow that does not parse is never written."""
        self.check_name(name)
        if name.startswith("examples/"):
            raise FlowError("bundled examples are read-only; save a copy under a name of your own")
        flow = parse_flow_text(text, name=name, forbidden_words=self.forbidden_words)
        self.store.write_flow(name, text)
        return flow

    def delete(self, name: str) -> bool:
        self.check_name(name)
        if name.startswith("examples/"):
            raise FlowError("bundled examples cannot be deleted")
        return self.store.delete_flow(name)

    def check(self, name: str, profiles: Profiles | None = None) -> Flow:
        """Everything that can be known before a run: the flow and every flow it
        calls parse, calls do not loop, and every @role has been taught."""
        seen: dict[str, Flow] = {}

        def visit(flow_name: str, trail: tuple[str, ...]) -> Flow:
            if flow_name in trail:
                raise FlowError(f"flows call each other in a loop: {' -> '.join((*trail, flow_name))}")
            if len(trail) >= MAX_CALL_DEPTH:
                raise FlowError(f"flows are nested more than {MAX_CALL_DEPTH} calls deep: {' -> '.join(trail)}")
            if flow_name not in seen:
                try:
                    seen[flow_name] = self.get(flow_name)
                except KeyError:
                    raise FlowError(f"flow {flow_name!r} does not exist"
                                    + (f" (called from {trail[-1]!r})" if trail else "")) from None
            flow = seen[flow_name]
            roles = _roles_for(flow, profiles)
            for selector in flow.selectors():
                try:
                    selector.with_roles(roles)
                except ValueError as e:
                    raise FlowError(f"flow {flow_name!r}: {e}") from None
            for called in sorted(flow.calls()):
                visit(called, (*trail, flow_name))
            return flow

        return visit(name, ())


def _roles_for(flow: Flow, profiles: Profiles | None) -> dict[str, Selector]:
    if profiles is None or not flow.package:
        return {}
    return profiles.load(flow.package).roles


# --------------------------------------------------------------- interpreter
@dataclass
class _Context:
    flow: Flow
    roles: dict[str, Selector]


class FlowRun(Session):
    """One device running one flow."""

    def __init__(self, flow: Flow, device: Any, config: AutomationConfig, *, library: FlowLibrary | None = None,
                 profiles: Profiles | None = None, artifacts: Path | None = None, **kwargs: Any):
        kwargs.setdefault("step_timeout", flow.step_timeout)
        kwargs.setdefault("run_timeout", flow.run_timeout)
        super().__init__(device, config, **kwargs)
        self.flow = flow
        self.library = library
        self.profiles = profiles
        self.artifacts = artifacts
        self.step_total = len(flow.steps)
        self._stack: list[_Context] = []
        self._size: tuple[int, int] | None = None
        self._shots = 0

    def run(self) -> dict:
        return self.execute(self._body)

    # -- contexts (a called flow brings its own roles, pop-ups and never-tap) --
    @property
    def ctx(self) -> _Context:
        return self._stack[-1]

    def _enter(self, flow: Flow) -> tuple[int, int]:
        profile: AppProfile | None = self.profiles.load(flow.package) if (self.profiles and flow.package) else None
        roles = profile.roles if profile else {}
        marks = (len(self.overlays), len(self.never_tap))
        if profile is not None:
            self.overlays.extend(profile.resolved_overlays())
            self.never_tap.extend(profile.resolved_never_tap())
        self.overlays.extend(o.model_copy(update={"guard": o.guard.with_roles(roles),
                                                  "dismiss": o.dismiss.with_roles(roles) if o.dismiss else None})
                             for o in flow.overlays)
        self.never_tap.extend(s.with_roles(roles) for s in flow.never_tap)
        self._stack.append(_Context(flow, roles))
        return marks

    def _leave(self, marks: tuple[int, int]) -> None:
        self._stack.pop()
        del self.overlays[marks[0]:]
        # The never-tap list only ever grows during a run: what a called flow
        # declared untouchable stays untouchable after it returns.

    def _body(self) -> None:
        self._enter(self.flow)
        missing = []
        for name, default in self.flow.variables.items():
            if name not in self.resolver.vars and default is not None:
                self.resolver.vars[name] = default
            if self.resolver.get(name) is None:
                missing.append(name)
        if missing:
            raise StepFailed("this flow needs a value for: " + ", ".join(missing) + ". Pass it when starting the "
                             "run, or add it to the shared values.")
        self._run_steps(self.flow.steps, top=True)

    def _run_steps(self, steps: list[FlowStep], top: bool = False) -> None:
        for index, step in enumerate(steps):
            if top:
                self.step_index = index
            self._run_step(step)

    def _run_step(self, step: FlowStep) -> None:
        self.check()
        label = step.describe()
        self.step_label = label
        container = step.kind in ("if", "repeat", "call")
        self.log(f"{step.where}: {label}")
        while True:
            try:
                if step.kind in _BLIND:
                    self.settle_overlays(step.where)
                self._dispatch(step)
                if not container:
                    self.done += 1
                return
            except StepTimeout as e:
                # An assertion is the author saying "this must hold": it is never
                # skipped just because the flow tolerates unknown screens elsewhere.
                policy = "stop" if step.kind == "assert" else self.ctx.flow.on_unknown
                decision = self.unknown(label, str(e), optional=step.optional, policy=policy)
            except ForbiddenTap as e:
                decision = self.unknown(label, str(e), optional=step.optional, policy="stop")
            except (MissingVariable, PoolEmpty) as e:
                decision = self.unknown(label, str(e), policy="stop")
            except UiError as e:
                decision = self.unknown(label, str(e), optional=step.optional, policy=self.ctx.flow.on_unknown)
            if decision == "skip":
                return

    # -- helpers -----------------------------------------------------------
    def _timeout(self, step: FlowStep) -> float:
        return step.timeout if step.timeout is not None else self.step_timeout

    def _sel(self, selector: Selector) -> Selector:
        return selector.with_roles(self.ctx.roles).rendered(self.lookup)

    def _text(self, template: str) -> str:
        return render(template, self.lookup)

    def _find(self, selector: Selector, timeout: float, scope: str) -> tuple[dict, list[dict]]:
        ready = self._sel(selector)

        def look(nodes: list[dict]) -> Any:
            node = find(nodes, ready)
            return (node, nodes) if node is not None else None

        return self.wait_until(look, timeout=timeout, what=ready.describe(), scope=scope)

    def _holds(self, condition: Condition, nodes: list[dict]) -> bool:
        if condition.visible is not None:
            return find(nodes, self._sel(condition.visible)) is not None
        if condition.gone is not None:
            return find(nodes, self._sel(condition.gone)) is None
        if condition.app is not None:
            package = self.device.current()[0] or foreground_package(nodes)
            return package == self._text(condition.app)
        if condition.equals is not None:
            return self._text(condition.equals[0]) == self._text(condition.equals[1])
        if condition.activity is not None:
            package, activity = self.device.current()
            pattern = render(condition.activity, self.lookup, escape=re.escape)
            try:
                return re.search(pattern, f"{package}/{activity}") is not None
            except re.error:
                return False
        if condition.any_of is not None:
            return any(self._holds(c, nodes) for c in condition.any_of)
        if condition.all_of is not None:
            return all(self._holds(c, nodes) for c in condition.all_of)
        return condition.negated is not None and not self._holds(condition.negated, nodes)

    def _now(self, condition: Condition, scope: str) -> bool:
        """Whether a condition holds on the screen as it is (after clearing pop-ups)."""
        try:
            return self.wait_until(lambda nodes: "yes" if self._holds(condition, nodes) else "no",
                                   timeout=min(self.step_timeout, float(self.config.read_timeout)),
                                   what=condition.describe(), scope=scope) == "yes"
        except StepTimeout:
            return False

    def _screen(self) -> tuple[int, int]:
        if self._size is None:
            self._size = tuple(self.device.size())  # type: ignore[assignment]
        return self._size  # type: ignore[return-value]

    def _focused_length(self) -> int:
        try:
            focused = next((n for n in self.read() if n["focused"] and n["editable"]), None)
        except UiError:
            return 0
        return len(focused["text"]) if focused else 0

    # -- steps -------------------------------------------------------------
    def _dispatch(self, step: FlowStep) -> None:
        getattr(self, f"_step_{step.kind}")(step)

    def _step_launch(self, step: FlowStep) -> None:
        a: LaunchArgs = step.args
        package = self._text(a.package) if a.package else self.ctx.flow.package
        if not package:
            raise StepFailed(f"{step.where}: launch needs a package (set `package` on the flow or the step)")
        try:
            check_package(package)
        except ValueError as e:
            raise StepFailed(f"{step.where}: {e}") from None
        if a.fresh:
            self.stop_app(package)
            self.pause(0.5)
        self.launch(package, self._text(a.activity) if a.activity else None)
        if a.wait is not None and not self.dry_run:
            self.wait_until(lambda nodes: self._holds(a.wait, nodes), timeout=self._timeout(step),
                            what=a.wait.describe(), scope=step.where)
        self.pause()

    def _step_stop_app(self, step: FlowStep) -> None:
        package = self._text(step.args.package) if step.args.package else self.ctx.flow.package
        if not package:
            raise StepFailed(f"{step.where}: stop_app needs a package")
        try:
            check_package(package)
        except ValueError as e:
            raise StepFailed(f"{step.where}: {e}") from None
        self.stop_app(package)
        self.pause()

    def _step_tap(self, step: FlowStep) -> None:
        node, nodes = self._find(step.args.target, self._timeout(step), step.where)
        self.tap_node(node, nodes)
        self.pause()

    def _step_long_press(self, step: FlowStep) -> None:
        node, nodes = self._find(step.args.target, self._timeout(step), step.where)
        self.tap_node(node, nodes, long_ms=step.args.ms)
        self.pause()

    def _step_type(self, step: FlowStep) -> None:
        a: TypeArgs = step.args
        text = self._text(a.text)
        names = variables_in(a.text)
        secret = a.secret or any(self.resolver.is_secret(n) for n in names)
        if a.into is not None:
            node, nodes = self._find(a.into, self._timeout(step), step.where)
            self.tap_node(node, nodes)
            self.pause(0.4)
        if a.clear and not self.dry_run:
            length = self._focused_length()
            if length:
                self.clear_field(length)
        # Show the template, not the value: a log line saying `{code}` is useful, one
        # carrying the code itself is a leak.
        self.type_text(text, shown=a.text if names else "", secret=secret)
        if a.enter:
            self.press_key("enter")
        self.pause()

    def _step_clear(self, step: FlowStep) -> None:
        if step.args.target is not None:
            node, nodes = self._find(step.args.target, self._timeout(step), step.where)
            self.tap_node(node, nodes)
            self.pause(0.4)
        if self.dry_run:
            self.clear_field(0)
            return
        length = self._focused_length()
        if length:
            self.clear_field(length)

    def _step_key(self, step: FlowStep) -> None:
        self.press_key(step.args.name)
        self.pause()

    def _step_back(self, step: FlowStep) -> None:
        a: BackArgs = step.args
        if a.until is None:
            self.back(presses=a.presses)
            return
        if not self.back(lambda nodes: self._holds(a.until, nodes), presses=a.presses) and not self.dry_run:
            raise StepTimeout(a.until.describe(), f"still not there after {a.presses} Back press(es)")

    def _step_home(self, step: FlowStep) -> None:
        self.press_key("home")
        self.pause()

    def _area(self, within: Selector | None, step: FlowStep) -> tuple[int, int, int, int]:
        if within is not None:
            node, _ = self._find(within, self._timeout(step), step.where)
            return tuple(node["bounds"])  # type: ignore[return-value]
        width, height = self._screen()
        return 0, 0, width, height

    @staticmethod
    def _stroke(area: tuple[int, int, int, int], direction: str, distance: float) -> tuple[int, int, int, int]:
        """Start and end of a finger stroke through the middle of an area."""
        left, top, right, bottom = area
        cx, cy = (left + right) // 2, (top + bottom) // 2
        dx, dy = int((right - left) * distance / 2), int((bottom - top) * distance / 2)
        return {"up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
                "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy)}[direction]

    def _step_swipe(self, step: FlowStep) -> None:
        a: SwipeArgs = step.args
        area = self._area(a.within, step)
        if a.direction is not None:
            stroke = self._stroke(area, a.direction, a.distance)
        else:
            assert a.start is not None and a.end is not None
            left, top, right, bottom = area
            width, height = right - left, bottom - top
            stroke = (left + int(a.start[0] * width), top + int(a.start[1] * height),
                      left + int(a.end[0] * width), top + int(a.end[1] * height))
        self.swipe(*stroke, ms=a.ms)
        self.pause()

    def _step_scroll_to(self, step: FlowStep) -> None:
        a: ScrollToArgs = step.args
        target = self._sel(a.target)
        # To see what is further down, the finger moves up.
        finger = {"down": "up", "up": "down", "left": "right", "right": "left"}[a.direction]
        before: tuple | None = None

        def look(nodes: list[dict]) -> Any:
            return ("found", nodes) if find(nodes, target) is not None else ("no", nodes)

        for swipes in range(a.max_swipes + 1):
            state, nodes = self.wait_until(look, timeout=self._timeout(step), what=target.describe(), scope=step.where)
            if state == "found":
                return
            if self.dry_run:
                raise StepTimeout(target.describe(), "not on the current screen; a real run would scroll")
            signature = tuple((n["id"], n["text"], tuple(n["bounds"])) for n in nodes)
            if signature == before:
                raise StepTimeout(target.describe(), f"reached the end of the list after {swipes} swipe(s)")
            before = signature
            if swipes == a.max_swipes:
                break
            self.swipe(*self._stroke(self._area(a.within, step), finger, 0.5), ms=350)
            self.pause()
        raise StepTimeout(target.describe(), f"not found after {a.max_swipes} swipe(s)")

    def _step_wait_for(self, step: FlowStep) -> None:
        condition: Condition = step.args.condition
        self.wait_until(lambda nodes: self._holds(condition, nodes), timeout=self._timeout(step),
                        what=condition.describe(), scope=step.where)

    def _step_wait_gone(self, step: FlowStep) -> None:
        target = self._sel(step.args.target)
        self.wait_until(lambda nodes: find(nodes, target) is None, timeout=self._timeout(step),
                        what=f"{target.describe()} to go away", scope=step.where)

    def _step_assert(self, step: FlowStep) -> None:
        condition: Condition = step.args.condition
        try:
            self.wait_until(lambda nodes: self._holds(condition, nodes),
                            timeout=step.timeout if step.timeout is not None else 2.0,
                            what=condition.describe(), scope=step.where)
        except StepTimeout as e:
            raise StepTimeout(step.args.message or f"{condition.describe()} (assertion)", e.detail) from None

    def _step_sleep(self, step: FlowStep) -> None:
        if not self.dry_run:
            self.sleep(step.args.seconds)

    def _step_if(self, step: FlowStep) -> None:
        branch = step.then if self._now(step.args.condition, step.where) else step.otherwise
        self._run_steps(branch)

    def _step_repeat(self, step: FlowStep) -> None:
        a: RepeatArgs = step.args
        rounds = 1 if self.dry_run else a.times        # a practice run changes nothing, so one look is enough
        for _ in range(rounds):
            if a.until is not None and self._now(a.until, step.where):
                return
            self._run_steps(step.body)
        if a.until is not None and not self.dry_run and not self._now(a.until, step.where):
            raise StepTimeout(a.until.describe(), f"still not true after {a.times} round(s)")

    def _step_set(self, step: FlowStep) -> None:
        a: SetArgs = step.args
        if a.source is not None:
            node, _ = self._find(a.source, self._timeout(step), step.where)
            value = str(node.get(a.attr) or "")
        elif a.pool is not None:
            value = self.resolver.take(a.pool)
        else:
            value = self._text(a.value or "")
        if a.regex is not None:
            found = re.search(a.regex, value)
            if found is None:
                raise StepTimeout(f"text matching /{a.regex}/ for {a.var}", f"saw {value[:60]!r}")
            value = found.group(1) if found.groups() else found.group(0)
        self.resolver.set(a.var, value)
        self.log(f"{a.var} = " + ("***" if self.resolver.is_secret(a.var) else repr(value[:60])))

    def _step_screenshot(self, step: FlowStep) -> None:
        if self.artifacts is None:
            self.log("no folder for screenshots in this run; skipped")
            return
        self._shots += 1
        name = step.args.name or f"shot{self._shots:02d}"
        path = Path(self.artifacts) / f"{name}-{time.strftime('%H%M%S')}.png"
        save_png(self.device.screenshot(), path)
        self.log(f"screenshot saved as {path.name}")

    def _step_call(self, step: FlowStep) -> None:
        a: CallArgs = step.args
        if self.library is None:
            raise StepFailed(f"{step.where}: this run has no flow library to call {a.flow!r} from")
        if len(self._stack) > MAX_CALL_DEPTH:
            raise StepFailed(f"{step.where}: flows are nested more than {MAX_CALL_DEPTH} calls deep")
        try:
            flow = self.library.get(a.flow)
        except (FlowError, KeyError) as e:
            raise StepFailed(f"{step.where}: {e}") from None
        # Arguments and the called flow's defaults are visible only inside the call.
        passed = {name: self._text(value) for name, value in a.variables.items()}
        saved = {name: self.resolver.vars.get(name) for name in {*passed, *flow.variables}}
        for name, default in flow.variables.items():
            if name not in self.resolver.vars and default is not None:
                self.resolver.vars[name] = default
        for name, value in passed.items():
            self.resolver.set(name, value)
        missing = [n for n in flow.variables if self.resolver.get(n) is None]
        if missing:
            raise StepFailed(f"{step.where}: {a.flow} needs a value for: {', '.join(missing)} (pass it under `with`)")
        marks = self._enter(flow)
        try:
            self._run_steps(flow.steps)
        finally:
            self._leave(marks)
            for name, value in saved.items():
                if value is None:
                    self.resolver.vars.pop(name, None)
                else:
                    self.resolver.vars[name] = value

    def _step_fail(self, step: FlowStep) -> None:
        raise StepFailed(self._text(step.args.message))

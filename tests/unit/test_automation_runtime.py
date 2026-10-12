"""Playing a trained program on one (scripted) device.

Covers the order a step is resolved in - element by score, alternatives, coordinates
only behind a picture check, remembered fix, model, person - and the rules no run can
get around: never-tap, guarded pop-ups, deadlines, dry runs.
"""
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(__file__))

from automation_fakes import FakeDevice, button, config, el, field, screen  # noqa: E402

from superfarm.automation import runtime  # noqa: E402
from superfarm.automation.memory import FixMemory, crop, save_png  # noqa: E402
from superfarm.automation.profiles import OverlayRule  # noqa: E402
from superfarm.automation.program import Program, ProgramStep  # noqa: E402
from superfarm.automation.runtime import ProgramRun, Session, bind_actions  # noqa: E402
from superfarm.automation.selectors import Selector  # noqa: E402
from superfarm.automation.values import Resolver, Values  # noqa: E402

HOME = screen(button("search", "Search", (20, 20, 220, 80)), el(id="title", text="Home", bounds=(0, 100, 480, 160)))
FORM = screen(field("query", "", (20, 20, 460, 80), focused=True), button("go", "Go", (20, 100, 220, 160)),
              button("wipe", "Erase everything", (20, 700, 460, 760)))
DONE = screen(el(id="result", text="Results", bounds=(0, 100, 480, 160)))
POPUP = screen(el(id="rate_title", text="Enjoying the app?", bounds=(40, 300, 440, 360)),
               button("rate_close", "Not now", (40, 400, 240, 460)),
               button("search", "Search", (20, 20, 220, 80)))
LABEL_ONLY = screen(el(id="rate_title", text="Enjoying the app?", bounds=(40, 300, 440, 360)),
                    button("search", "Search", (20, 20, 220, 80)))
OTHER = screen(el(id="surprise", text="Something else", bounds=(0, 100, 480, 160)),
               button("ok", "OK", (40, 400, 240, 460)))


def device(start: str = "home", **kw) -> FakeDevice:
    screens = {"home": HOME, "form": FORM, "done": DONE, "popup": POPUP, "label": LABEL_ONLY, "other": OTHER}
    taps = {("home", "search"): "form", ("form", "go"): "done", ("popup", "rate_close"): "home",
            ("popup", "search"): "popup", ("other", "ok"): "home", ("label", "search"): "form"}
    return FakeDevice(screens, start, taps=taps, keys={("other", "back"): "home"}, **kw)


def program(*steps: ProgramStep, **kw) -> Program:
    return Program(slug="demo", name="Demo", goal="search for something", package="com.example.app",
                   screen=[480, 854], steps=list(steps), **kw)


SEARCH = [ProgramStep(id="p000", kind="tap", selector={"id": "search"}, device=[120, 50]),
          ProgramStep(id="p001", kind="type", dynamic="term", selector={"id": "query"}),
          ProgramStep(id="p002", kind="tap", selector={"text": "Go"}, device=[120, 130])]


def start(run: Session) -> threading.Thread:
    thread = threading.Thread(target=run.run, daemon=True)
    thread.start()
    return thread


def answer(run: Session, action: str, seconds: float = 15.0) -> None:
    """Answer a waiting device and wait until it asks again or finishes - not merely
    until the state reads needs_attention, which it still does for a moment."""
    asked = run.waiting
    assert asked is not None and run.resolve_attention(action)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (run.waiting is not None and run.waiting is not asked) or run.finished:
            return
        time.sleep(0.01)
    raise AssertionError(f"nothing happened after {action}: {list(run.lines)[-5:]}")


def wait_for_state(run: Session, state: str, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while run.state != state and time.monotonic() < deadline:
        time.sleep(0.01)
    assert run.state == state, f"state is {run.state!r}: {list(run.lines)[-5:]}"


# ------------------------------------------------------------- the happy path
def test_a_program_plays_through_by_element():
    dev = device()
    run = ProgramRun(program(*SEARCH), dev, config(), resolver=Resolver(variables={"term": "earbuds"}))
    result = run.run()
    assert result["state"] == "done" and result["done"] == 3 and result["steps"] == 3
    assert [s[3] for s in dev.did("tap")] == ["search", "query", "go"]
    assert dev.did("text") == [("text", "earbuds")] and dev.screen == "done" and dev.closed
    assert any("found by 'search' (score" in line for line in result["log"])


def test_an_alternative_is_used_when_the_voted_element_is_gone():
    step = ProgramStep(id="p000", kind="tap", selector={"id": "renamed_in_this_version"},
                       alternatives=[{"text": "Search"}], device=[120, 50])
    dev = device()
    assert ProgramRun(program(step), dev, config()).run()["state"] == "done"
    assert dev.screen == "form"


def test_the_match_threshold_comes_from_the_config():
    step = ProgramStep(id="p000", kind="tap", selector={"text": "search"}, device=[400, 800])   # case differs: 3.5
    assert ProgramRun(program(step), device(), config()).run()["state"] == "done"
    strict = ProgramRun(program(step), device(), config(match_threshold=3.8, stop_on_unknown_screen=False))
    assert strict.run()["state"] == "done" and strict.device.did("tap")[0][3] != "search"    # guessed by position


def test_an_optional_step_is_skipped_when_its_element_is_not_there(monkeypatch):
    monkeypatch.setattr(runtime, "OPTIONAL_WAIT", 0.2)
    steps = [ProgramStep(id="p000", kind="tap", selector={"text": "Accept cookies"}, device=[5, 5], optional=True),
             *SEARCH]
    dev = device()
    result = ProgramRun(program(*steps), dev, config(), resolver=Resolver(variables={"term": "x"})).run()
    assert result["state"] == "done" and result["skipped"] == 1 and result["done"] == 3
    assert len(dev.did("tap")) == 3                                  # the recorded position was not used either


def test_other_step_kinds():
    steps = [ProgramStep(id="p000", kind="launch", package="com.example.app"),
             ProgramStep(id="p001", kind="swipe", device=[240, 700], to=[240, 200]),
             ProgramStep(id="p002", kind="key", key="back"),
             ProgramStep(id="p003", kind="wait", seconds=0.05),
             ProgramStep(id="p004", kind="long_press", selector={"id": "title"}, seconds=0.9),
             ProgramStep(id="p005", kind="assert", selector={"text": "Home"})]
    dev = device()
    assert ProgramRun(program(*steps), dev, config()).run()["state"] == "done"
    assert dev.did("launch") == [("launch", "com.example.app")]
    assert dev.did("swipe") == [("swipe", 240, 700, 240, 200)] and dev.did("key") == [("key", "back")]
    assert dev.did("long_press")[0][3] == 900


def test_recorded_coordinates_are_scaled_to_the_device():
    steps = [ProgramStep(id="p000", kind="swipe", device=[240, 700], to=[240, 200])]
    dev = device(size=(960, 1708))
    ProgramRun(program(*steps), dev, config()).run()
    assert dev.did("swipe") == [("swipe", 480, 1400, 480, 400)]


# ----------------------------------------------- coordinates and the picture
def _with_shot(tmp_path, dev: FakeDevice, *, selector: dict | None = None) -> Program:
    """A program whose only step has a picture of the area around (120, 50)."""
    draw = ImageDraw.Draw(dev.image)
    draw.rectangle([40, 20, 200, 80], fill=(240, 240, 240))
    draw.line([40, 20, 200, 80], fill=(200, 0, 0), width=4)
    box = [30, 0, 210, 140]
    step = ProgramStep(id="p000", kind="tap", selector=selector or {"id": "vanished"}, device=[120, 50],
                       shot="shots/p000.png", shot_box=box)
    prog = program(step)
    prog.dir = tmp_path
    save_png(crop(dev.image, box), tmp_path / "shots" / "p000.png")
    return prog


def test_coordinates_are_used_when_the_area_still_looks_the_same(tmp_path):
    dev = device()
    run = ProgramRun(_with_shot(tmp_path, dev), dev, config())
    result = run.run()
    assert result["state"] == "done" and dev.did("tap")[0][1:3] == (120, 50)
    assert any("still looks the same" in line for line in result["log"])


def test_coordinates_are_not_used_when_the_area_looks_different(tmp_path):
    dev = device()
    prog = _with_shot(tmp_path, dev)
    dev.image = Image.new("RGB", (480, 854), (255, 255, 255))         # a different screen now
    run = ProgramRun(prog, dev, config())
    thread = start(run)
    wait_for_state(run, "needs_attention")
    assert not dev.acted and "looks different" in run.waiting["reason"]
    run.resolve_attention("abort")
    thread.join(5)
    assert run.state == "failed" and "stopped by the operator" in run.error


def test_a_step_with_identity_but_no_picture_never_falls_back_to_coordinates():
    step = ProgramStep(id="p000", kind="tap", selector={"id": "vanished"}, device=[120, 50])
    dev = device()
    run = ProgramRun(program(step), dev, config())
    thread = start(run)
    wait_for_state(run, "needs_attention")
    assert not dev.acted
    run.resolve_attention("skip")
    thread.join(5)
    assert run.state == "done" and run.snapshot()["skipped"] == 1


def test_a_blind_step_uses_its_position_because_that_is_all_there_is():
    dev = device()
    assert ProgramRun(program(ProgramStep(id="p000", kind="tap", device=[120, 50])), dev, config()).run()["state"] == "done"
    assert dev.did("tap")[0][1:3] == (120, 50)


# ------------------------------------------------------------------ never-tap
NEVER = [Selector(text="Erase everything")]


def test_never_tap_stops_a_tap_found_by_element():
    dev = device("form")
    run = ProgramRun(program(ProgramStep(id="p000", kind="tap", selector={"id": "wipe"})), dev, config(), never_tap=NEVER)
    thread = start(run)
    wait_for_state(run, "needs_attention")
    assert "never-tap list" in run.waiting["reason"] and not dev.acted
    run.resolve_attention("abort")
    thread.join(5)


def test_never_tap_stops_a_tap_by_coordinates():
    dev = device("form")
    run = ProgramRun(program(ProgramStep(id="p000", kind="tap", device=[240, 730])), dev,
                     config(stop_on_unknown_screen=False), never_tap=NEVER)
    result = run.run()
    assert result["state"] == "failed" and "never-tap" in result["error"] and not dev.acted


def test_coordinates_are_refused_when_the_screen_cannot_be_checked():
    dev = device("form")
    dev.fail_reads = 10_000
    run = ProgramRun(program(ProgramStep(id="p000", kind="tap", device=[240, 100])), dev,
                     config(stop_on_unknown_screen=False), never_tap=NEVER)
    result = run.run()
    assert result["state"] == "failed" and "cannot be checked" in result["error"] and not dev.acted


# -------------------------------------------------------------------- pop-ups
RATE = OverlayRule(name="rate-us", guard={"id": "rate_title"}, dismiss={"id": "rate_close"})


def test_a_pop_up_is_closed_when_guard_and_dismiss_are_both_there():
    dev = device("popup")
    result = ProgramRun(program(SEARCH[0]), dev, config(), overlays=[RATE]).run()
    assert result["state"] == "done" and [s[3] for s in dev.did("tap")] == ["rate_close", "search"]
    assert any("closing the pop-up 'rate-us'" in line for line in result["log"])


def test_a_guard_alone_is_not_a_pop_up():
    """The lesson behind the rule: a guard that is also a permanent label on the page."""
    dev = device("label")
    result = ProgramRun(program(SEARCH[0]), dev, config(), overlays=[RATE]).run()
    assert result["state"] == "done" and [s[3] for s in dev.did("tap")] == ["search"]


def test_a_pop_up_that_will_not_close_is_left_alone_after_a_few_tries():
    dev = device("popup")
    dev.taps[("popup", "rate_close")] = "popup"                       # "closing" it changes nothing
    step = ProgramStep(id="p000", kind="tap", selector={"id": "title"})
    run = ProgramRun(program(step), dev, config(stop_on_unknown_screen=False), overlays=[RATE])
    result = run.run()
    assert len([s for s in dev.did("tap") if s[3] == "rate_close"]) == RATE.max_dismissals
    assert result["state"] == "failed" and any("leaving it alone" in line for line in result["log"])


def test_a_key_dismissed_pop_up_needs_an_id_guard():
    rule = OverlayRule(name="sheet", guard={"id": "surprise"}, key="back")
    dev = device("other")
    result = ProgramRun(program(SEARCH[0]), dev, config(), overlays=[rule]).run()
    assert result["state"] == "done" and dev.did("key") == [("key", "back")]
    with pytest.raises(ValueError, match="needs a guard with an `id`"):
        OverlayRule(name="bad", guard={"text": "Something else"}, key="back")
    with pytest.raises(ValueError, match="exactly one of"):
        OverlayRule(name="bad", guard={"id": "a"})
    with pytest.raises(ValueError, match="exactly one of"):
        OverlayRule(name="bad", guard={"id": "a"}, dismiss={"id": "b"}, key="back")
    with pytest.raises(ValueError, match="must name an element"):
        OverlayRule(name="bad", guard={"clickable": True}, dismiss={"id": "b"})


# ------------------------------------------------- needs attention, deadlines
def test_resume_retries_the_step_once_a_person_fixed_the_screen():
    dev = device("other")
    run = ProgramRun(program(SEARCH[0]), dev, config())
    thread = start(run)
    wait_for_state(run, "needs_attention")
    assert run.waiting["what"] == "tap search" and run.snapshot()["waiting"]["step"] == 0
    answer(run, "resume")
    assert run.state == "needs_attention"                              # still the wrong screen
    dev.screen = "home"                                                # the person navigated back
    answer(run, "resume")
    thread.join(5)
    assert run.state == "done" and dev.screen == "form"
    assert run.resolve_attention("resume") is False                    # nothing is waiting any more
    with pytest.raises(ValueError):
        run.resolve_attention("explode")


def test_waiting_for_a_person_does_not_use_up_the_run_time():
    dev = device("other")
    run = ProgramRun(program(SEARCH[0]), dev, config(), run_timeout=2.5)
    thread = start(run)
    wait_for_state(run, "needs_attention")
    time.sleep(2.0)                                                    # a slow human
    dev.screen = "home"
    run.resolve_attention("resume")
    thread.join(5)
    assert run.state == "done"


def test_a_device_that_is_never_answered_ends_as_needing_attention(monkeypatch):
    monkeypatch.setattr(runtime, "ATTENTION_TIMEOUT", 0.3)
    run = ProgramRun(program(SEARCH[0]), device("other"), config())
    result = run.run()
    assert result["state"] == "needs_attention" and "nobody answered" in result["error"]


def test_with_stopping_switched_off_nobody_is_asked():
    step = ProgramStep(id="p000", kind="tap", selector={"id": "vanished"})
    result = ProgramRun(program(step), device(), config(stop_on_unknown_screen=False)).run()
    assert result["state"] == "failed" and "stop_on_unknown_screen is off" in result["error"]


def test_the_run_deadline_ends_a_run_that_keeps_waiting():
    steps = [ProgramStep(id=f"p{i:03d}", kind="wait", seconds=0.4) for i in range(10)]
    started = time.monotonic()
    result = ProgramRun(program(*steps), device(), config(), run_timeout=1.0).run()
    assert result["state"] == "failed" and "automation.run_timeout" in result["error"]
    assert time.monotonic() - started < 3


def test_cancel_stops_a_run_in_the_middle_of_a_wait():
    run = ProgramRun(program(ProgramStep(id="p000", kind="tap", selector={"id": "vanished"})), device(),
                     config(step_timeout=30))
    thread = start(run)
    time.sleep(0.3)
    run.cancel.set()
    thread.join(3)
    assert not thread.is_alive() and run.state == "cancelled"


def test_cancel_while_waiting_for_a_person():
    run = ProgramRun(program(SEARCH[0]), device("other"), config())
    thread = start(run)
    wait_for_state(run, "needs_attention")
    run.cancel.set()
    thread.join(3)
    assert run.state == "cancelled" and run.waiting is None


def test_a_read_that_keeps_failing_is_a_timeout_not_a_crash():
    dev = device()
    dev.fail_reads = 2
    assert ProgramRun(program(SEARCH[0]), dev, config()).run()["state"] == "done"      # two bad reads, then fine
    dev = device()
    dev.fail_reads = 10_000
    no_position = ProgramStep(id="p000", kind="tap", selector={"id": "search"})
    result = ProgramRun(program(no_position), dev, config(stop_on_unknown_screen=False)).run()
    assert result["state"] == "failed" and not dev.acted


# -------------------------------------------------------------------- dry run
def test_a_dry_run_reads_and_matches_but_sends_nothing():
    dev = device()
    run = ProgramRun(program(*SEARCH), dev, config(), dry_run=True, resolver=Resolver(variables={"term": "x"}))
    result = run.run()
    assert result["state"] == "done" and not dev.acted and dev.reads >= 3
    assert "practice run: nothing was sent" in result["message"]
    assert result["done"] == 2                              # search and (type) were possible on this screen
    assert result["issues"] == ["tap Go: the element is not on screen"]
    assert any("would tap 'Search'" in line for line in result["log"])
    assert any("would type {term}" in line for line in result["log"])


def test_a_dry_run_does_not_dismiss_pop_ups_or_wait_for_people():
    dev = device("popup")
    step = ProgramStep(id="p000", kind="tap", selector={"id": "vanished"})
    result = ProgramRun(program(step), dev, config(), dry_run=True, overlays=[RATE]).run()
    assert result["state"] == "done" and not dev.acted and result["issues"]
    assert any("would close the pop-up" in line for line in result["log"])


# ------------------------------------------------------------- typed values
def test_a_missing_value_asks_for_a_person_and_resumes_when_it_exists(tmp_path):
    values = Values(tmp_path, ["password"])
    dev = device("form")
    run = ProgramRun(program(SEARCH[1]), dev, config(), resolver=Resolver(values, instance_id=2))
    thread = start(run)
    wait_for_state(run, "needs_attention")
    assert "no value for {term}" in run.waiting["reason"]
    values.set_shared("term", "added later")
    run.resolve_attention("resume")
    thread.join(5)
    assert run.state == "done" and dev.did("text") == [("text", "added later")]


def test_literal_text_and_clearing_what_was_in_the_field():
    filled = screen(field("query", "old text", (20, 20, 460, 80), focused=True))
    dev = FakeDevice({"form": filled}, "form")
    step = ProgramStep(id="p000", kind="type", text="new", selector={"id": "query"})
    assert ProgramRun(program(step), dev, config()).run()["state"] == "done"
    assert dev.did("clear") == [("clear", 8)] and dev.did("text") == [("text", "new")]


def test_secret_values_never_reach_the_log(tmp_path):
    values = Values(tmp_path, ["password"])
    dev = device("form")
    lines: list[str] = []
    step = ProgramStep(id="p000", kind="type", dynamic="password", selector={"id": "query"})
    run = ProgramRun(program(step), dev, config(), log=lines.append,
                     resolver=Resolver(values, variables={"password": "hunter2"}))
    result = run.run()
    assert dev.did("text") == [("text", "hunter2")]
    assert "hunter2" not in json.dumps(result) and "hunter2" not in "".join(lines)


# ------------------------------------------------- remembered fixes and models
class FakeModel:
    model = "test-model"

    def __init__(self, *replies):
        self.replies, self.asked = list(replies), []

    def ask(self, system, user, schema=None):
        self.asked.append(user)
        reply = self.replies.pop(0) if self.replies else {"analysis": "", "actions": []}
        if isinstance(reply, Exception):
            raise reply
        return reply


def _ok_number(dev: FakeDevice) -> int:
    from superfarm.automation.ui import interesting
    return next(i for i, n in enumerate(interesting(dev.screens["other"])) if n["id"] == "ok")


def test_a_model_fix_is_applied_remembered_and_replayed_without_the_model(tmp_path):
    memory = FixMemory(tmp_path)
    dev = device("other")
    model = FakeModel({"analysis": "a notice covers the screen", "actions": [
        {"type": "tap_element", "element": _ok_number(dev)}]})
    run = ProgramRun(program(SEARCH[0]), dev, config(), memory=memory, model=model)
    result = run.run()
    assert result["state"] == "done" and result["recovered"] == 1 and len(model.asked) == 1
    assert [s[3] for s in dev.did("tap")] == ["ok", "search"]
    sent = json.loads(model.asked[0].split("\n", 1)[1].rsplit("\n\n", 1)[0])
    assert sent["goal"] == "search for something" and sent["stuck_step"]["meant_to"] == "tap search"
    assert sent["screen"]["app"] == "com.example.app"
    fix = memory.entries()[0]
    assert fix.package == "com.example.app" and fix.actions[0]["selector"]["id"] == "ok"

    again = device("other")
    silent = FakeModel()
    result = ProgramRun(program(SEARCH[0]), again, config(), memory=memory, model=silent).run()
    assert result["state"] == "done" and silent.asked == []           # replayed from memory
    assert memory.entries()[0].hits == 2


def test_model_help_is_bounded(monkeypatch):
    dev = device("other")
    useless = {"analysis": "try waiting", "actions": [{"type": "wait", "seconds": 0.2}]}
    model = FakeModel(*[useless] * 20)
    run = ProgramRun(program(SEARCH[0]), dev, config(), model=model)
    thread = start(run)
    wait_for_state(run, "needs_attention", seconds=15)
    assert len(model.asked) == runtime.MAX_MODEL_ATTEMPTS
    answer(run, "resume")
    answer(run, "resume")
    # two recovery rounds per step at most, however often a person presses resume
    assert run.state == "needs_attention"
    assert len(model.asked) == runtime.MAX_MODEL_ATTEMPTS * runtime.MAX_RECOVERY_ROUNDS
    answer(run, "abort")
    thread.join(5)
    assert run.state == "failed"


def test_a_model_cannot_tap_what_is_on_the_never_tap_list():
    dev = device("form")
    from superfarm.automation.ui import interesting
    wipe = next(i for i, n in enumerate(interesting(FORM)) if n["id"] == "wipe")
    model = FakeModel({"analysis": "tap the big button", "actions": [{"type": "tap_element", "element": wipe}]})
    step = ProgramStep(id="p000", kind="tap", selector={"id": "vanished"})
    run = ProgramRun(program(step), dev, config(stop_on_unknown_screen=False), model=model, never_tap=NEVER)
    result = run.run()
    assert result["state"] == "failed" and not dev.acted
    assert any("recovery refused" in line and "never-tap" in line for line in result["log"])


def test_a_model_that_asks_for_a_person_or_fails_gets_one():
    from superfarm.automation.llm import LlmError
    for reply in ({"analysis": "captcha", "actions": [], "needs_human": True}, LlmError("server down")):
        dev = device("other")
        run = ProgramRun(program(SEARCH[0]), dev, config(), model=FakeModel(reply, reply))
        thread = start(run)
        wait_for_state(run, "needs_attention")
        assert not dev.acted
        run.resolve_attention("abort")
        thread.join(5)


def test_a_remembered_fix_that_no_longer_applies_counts_as_a_miss(tmp_path):
    memory = FixMemory(tmp_path)
    dev = device("other")
    from superfarm.automation.memory import dhash
    entry = memory.remember(package="com.example.app", dhash=dhash(dev.image),
                            actions=[{"type": "tap", "selector": {"id": "long_gone"}, "x": 1, "y": 1}])
    step = ProgramStep(id="p000", kind="tap", selector={"id": "search"})
    result = ProgramRun(program(step), dev, config(stop_on_unknown_screen=False), memory=memory).run()
    assert result["state"] == "failed" and not dev.acted                # never a blind tap at its coordinates
    assert memory.entries()[0].misses == 1 and memory.entries()[0].id == entry.id


def test_binding_turns_element_numbers_into_identity_and_position():
    elements = [{"id": "allow", "text": "Allow", "desc": "", "cls": "Button", "center": [50, 60]},
                {"id": "name", "text": "typed by someone", "desc": "", "cls": "EditText", "editable": True,
                 "center": [5, 6]}]
    bound = bind_actions([{"type": "tap_element", "element": 0}, {"type": "tap_element", "element": 1},
                          {"type": "tap_element", "element": 9}, {"type": "shell", "cmd": "rm -rf /"},
                          {"type": "key", "name": "back"}, {"type": "key", "name": "power"},
                          {"type": "swipe", "direction": "up"}, {"type": "wait", "seconds": 99}], elements)
    assert bound == [
        {"type": "tap", "selector": {"id": "allow", "text": "Allow", "cls": "Button"}, "x": 50, "y": 60},
        {"type": "tap", "selector": {"id": "name", "cls": "EditText"}, "x": 5, "y": 6},
        {"type": "key", "name": "back"}, {"type": "swipe", "direction": "up"}, {"type": "wait", "seconds": 10.0}]


# --------------------------------------------------------------------- jitter
def test_jitter_stays_well_inside_the_element():
    dev = device()
    session = Session(dev, config(human_jitter=True), rng=random.Random(7))
    node = next(n for n in HOME if n["id"] == "search")            # 20..220 x 20..80
    points = {session._point_in(node) for _ in range(200)}
    assert len(points) > 20                                        # it does vary
    assert all(80 <= x <= 160 and 38 <= y <= 62 for x, y in points)
    steady = Session(dev, config(human_jitter=False))
    assert steady._point_in(node) == (120, 50)


def test_back_waits_for_the_screen_to_close_before_pressing_again(monkeypatch):
    monkeypatch.setattr(runtime, "BACK_POLL", 0.3)
    dev = device("other")
    session = Session(dev, config())
    session._deadline = time.monotonic() + 30
    gone = lambda nodes: not any(n["id"] == "surprise" for n in nodes)  # noqa: E731
    assert session.back(gone, presses=3) is True
    assert dev.did("key") == [("key", "back")]                     # one press was enough, so only one was sent
    stuck = device("form")
    session = Session(stuck, config())
    session._deadline = time.monotonic() + 30
    assert session.back(lambda nodes: False, presses=2) is False
    assert len(stuck.did("key")) == 2

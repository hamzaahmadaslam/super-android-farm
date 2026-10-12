"""Hand-written flows: validation, the library, and the interpreter on a scripted device."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest
import yaml

sys.path.insert(0, os.path.dirname(__file__))

from automation_fakes import PACKAGE, FakeDevice, button, config, el, field, screen  # noqa: E402

from superfarm.automation import runtime  # noqa: E402
from superfarm.automation.flows import (  # noqa: E402
    EXAMPLES_DIR,
    FlowError,
    FlowLibrary,
    FlowRun,
    parse_flow,
    parse_flow_text,
)
from superfarm.automation.profiles import Profiles  # noqa: E402
from superfarm.automation.store import AutomationStore  # noqa: E402
from superfarm.automation.values import Resolver, Values  # noqa: E402

WORDS = ["password", "pin", "otp"]


def flow(text: str, **kw):
    return parse_flow_text(text, forbidden_words=WORDS, **kw)


# ---------------------------------------------------------------- validation
def test_every_step_kind_parses_in_its_short_and_long_form():
    parsed = flow("""
        name: everything
        package: com.example.app
        variables: {query: earbuds, code: null}
        on_unknown: continue
        timeouts: {step: 5, run: 60}
        overlays:
          - {name: rate, guard: {id: rate_title}, dismiss: {id: rate_close}}
          - {name: sheet, guard: {id: sheet_root}, key: back, max_dismissals: 1}
        never_tap: [{text: Delete account}, "Buy now"]
        steps:
          - launch: com.example.app
          - launch: {fresh: true, wait: {id: home}}
          - launch:
          - stop_app: com.example.app
          - tap: {id: search}
          - tap: Search
          - tap: "@search_button"
            timeout: 3
            optional: true
            label: open search
          - long_press: {id: item}
            ms: 900
          - type: hello
          - type: {text: "{query}", into: {id: box}, clear: true, enter: true, secret: false}
          - clear:
          - clear: {id: box}
          - key: enter
          - back
          - back: {until: {id: home}, presses: 3}
          - home
          - swipe: up
          - swipe: {direction: left, distance: 0.3, in: {id: pager}, ms: 200}
          - swipe: {from: [0.5, 0.8], to: [0.5, 0.2]}
          - scroll_to: {text: Privacy}
            direction: down
            max_swipes: 4
            in: {id: list}
          - wait_for: {id: results}
          - wait_for: {any: [{visible: Done}, {gone: {id: spinner}}, {activity: "\\\\.Main$"}, {app: com.example.app}]}
          - wait_gone: {id: spinner}
          - assert: {not: {visible: {text: Error}}}
            message: no error is shown
          - assert: {equals: ["{query}", earbuds]}
          - sleep: 0.5
          - if: {id: banner}
            then: [{tap: {id: banner_close}}]
            else:
              - key: tab
          - repeat: 3
            until: {id: end}
            steps: [{swipe: up}]
          - repeat: {times: 2, steps: [{key: tab}]}
          - set: {var: title, from: {id: title}, regex: "(\\\\d+)"}
          - set: {var: greeting, value: "hi {query}"}
          - set: {var: code, pool: codes}
          - screenshot: after
          - screenshot:
          - call: other-flow
            with: {query: phones}
          - fail: gave up
    """)
    kinds = [s.kind for s in parsed.steps]
    assert len(kinds) == 36 and set(kinds) == {
        "launch", "stop_app", "tap", "long_press", "type", "clear", "key", "back", "home", "swipe", "scroll_to",
        "wait_for", "wait_gone", "assert", "sleep", "if", "repeat", "set", "screenshot", "call", "fail"}
    assert parsed.package == "com.example.app" and parsed.on_unknown == "continue"
    assert parsed.variables == {"query": "earbuds", "code": None}
    assert parsed.step_timeout == 5 and parsed.run_timeout == 60
    assert [o.name for o in parsed.overlays] == ["rate", "sheet"] and parsed.never_tap[1].text == "Buy now"
    tap = parsed.steps[6]
    assert tap.args.target.role == "search_button" and tap.timeout == 3 and tap.optional and tap.describe() == "open search"
    assert parsed.steps[7].args.ms == 900 and parsed.steps[9].args.into.id == "box"
    scroll = next(s for s in parsed.steps if s.kind == "scroll_to")
    assert scroll.args.max_swipes == 4 and scroll.args.within.id == "list"
    branch = next(s for s in parsed.steps if s.kind == "if")
    assert branch.then[0].kind == "tap" and branch.otherwise[0].kind == "key"
    loops = [s for s in parsed.steps if s.kind == "repeat"]
    assert loops[0].args.times == 3 and loops[0].args.until.visible.id == "end" and loops[1].body[0].kind == "key"
    assert parsed.calls() == {"other-flow"}
    assert any(s.role == "search_button" for s in parsed.selectors())


@pytest.mark.parametrize("text, problem", [
    ("[]", "YAML mapping"),
    ("steps: []", "at least one step"),
    ("steps: {tap: x}", "expected a list of steps"),
    ("stpes: [{tap: x}]", "unknown key(s) stpes"),
    ("steps: [{tap: {id: a}, key: enter}]", "steps[0]: a step does one thing, but this one names tap, key"),
    ("steps: [{press: enter}]", "steps[0]: no action found (saw press)"),
    ("steps: [{tap: {id: a}, colour: red}]", "steps[0].tap: unknown key(s) colour"),
    ("steps: [{tap: {colour: red}}]", "steps[0].tap: target.colour"),
    ("steps: [{tap: {}}]", "steps[0].tap"),
    ("steps: [{tap: {id: a}, timeout: soon}]", "steps[0]: timeout must be a number"),
    ("steps: [{tap: {id: a}, timeout: 99999}]", "between 0 and 3600"),
    ("steps: [{tap: {id: a}, optional: maybe}]", "optional must be true or false"),
    ("steps: [{key: 'enter; reboot'}]", "steps[0].key: name"),
    ("steps: [{sleep: 9999}]", "steps[0].sleep: seconds"),
    ("steps: [{swipe: diagonal}]", "steps[0].swipe: direction"),
    ("steps: [{swipe: {from: [0.5, 0.5]}}]", "needs both `from` and `to`"),
    ("steps: [{swipe: {from: [5, 5], to: [0, 0]}}]", "fractions of the screen"),
    ("steps: [{if: {id: a}}]", "steps[0].if: needs `then` and/or `else`"),
    ("steps: [{if: {id: a}, then: [{tap: {id: b}, nope: 1}]}]", "steps[0].then[0].tap: unknown key(s) nope"),
    ("steps: [{repeat: 3}]", "steps[0].repeat: needs `steps`"),
    ("steps: [{repeat: 0, steps: [{key: tab}]}]", "steps[0].repeat: times"),
    ("steps: [{set: {var: x}}]", "exactly one of `from`"),
    ("steps: [{set: {var: 'bad name', value: x}}]", "bad value name"),
    ("steps: [{set: {var: x, value: y, regex: '('}}]", "is not valid"),
    ("steps: [{call: '../other'}]", "bad flow name"),
    ("steps: [{wait_for: {visible: {id: a}, gone: {id: b}}}]", "exactly one of"),
    ("steps: [{wait_for: {activity: '('}}]", "not a valid regular expression"),
    ("steps: [{back: sideways}]", "steps[0].back: expected a mapping"),
    ("package: nope\nsteps: [{home: }]", "package: bad package name"),
    ("on_unknown: panic\nsteps: [{home: }]", "on_unknown: use `stop`"),
    ("timeouts: {step: -1}\nsteps: [{home: }]", "timeouts.step"),
    ("timeouts: {walk: 1}\nsteps: [{home: }]", "timeouts: expected"),
    ("variables: [a]\nsteps: [{home: }]", "variables: expected a mapping"),
    ("variables: {'a b': 1}\nsteps: [{home: }]", "variables: bad value name"),
    ("variables: {password: hunter2}\nsteps: [{home: }]", "looks like a secret"),
    ("overlays: [{name: x, guard: {id: a}}]\nsteps: [{home: }]", "overlays[0]: overlay 'x' needs exactly one of"),
    ("overlays: [{name: x, guard: {text: Hi}, key: back}]\nsteps: [{home: }]", "needs a guard with an `id`"),
    ("overlays: [{name: x, guard: {id: a}, key: back}, {name: x, guard: {id: b}, key: back}]\nsteps: [{home: }]",
     "two rules share a name"),
    ("never_tap: [{colour: red}]\nsteps: [{home: }]", "never_tap[0]"),
    ("steps: [{tap: {id: a}", "not valid YAML at line 1"),
])
def test_invalid_flows_are_refused_with_the_place_and_the_reason(text, problem):
    with pytest.raises(FlowError) as error:
        flow(text)
    assert problem in str(error.value), str(error.value)


def test_a_secret_variable_may_be_declared_but_not_given_a_default():
    assert flow("variables: {password: null}\nsteps: [{type: '{password}'}]").variables == {"password": None}


def test_size_limits():
    nested = {"tap": {"id": "a"}}
    for _ in range(14):
        nested = {"if": {"id": "x"}, "then": [nested]}
    with pytest.raises(FlowError, match="nested more than"):
        parse_flow({"steps": [nested]})
    with pytest.raises(FlowError, match="more than 2000 steps"):
        parse_flow({"steps": [{"key": "tab"}] * 2001})
    with pytest.raises(FlowError, match="larger than"):
        flow("steps: [{home: }]\n#" + "x" * 300_000)


def test_yaml_is_loaded_safely():
    with pytest.raises(FlowError):
        flow("steps: [{tap: !!python/object/apply:os.system ['true']}]")


# ------------------------------------------------------------------- library
@pytest.fixture
def library(tmp_path) -> FlowLibrary:
    return FlowLibrary(AutomationStore(tmp_path), WORDS)


def test_the_bundled_examples_all_parse(library):
    names = [n for n in library.names() if n.startswith("examples/")]
    assert len(names) >= 5
    for name in names:
        parsed = library.check(name)
        assert parsed.builtin and parsed.description and parsed.steps
    assert {"examples/open-app", "examples/dismiss-permission-dialog", "examples/settings-search",
            "examples/type-into-focused-field", "examples/scroll-until-visible"} <= set(names)


def test_the_examples_name_no_app_but_the_system(library):
    """No app is special: bundled flows may only mention packages that are part of Android."""
    for path in EXAMPLES_DIR.glob("*.yaml"):
        document = yaml.safe_load(path.read_text())
        assert document.get("package") in (None, "com.android.settings"), path.name


def test_saving_validates_first_and_examples_are_read_only(library):
    with pytest.raises(FlowError, match="no action found"):
        library.save("mine", "steps: [{jump: high}]")
    assert library.store.flow_names() == []                       # nothing invalid was written
    saved = library.save("mine", "description: d\nsteps: [{home: }]")
    assert saved.name == "mine" and not saved.builtin and "mine" in library.names()
    assert library.get("mine").description == "d"
    assert {row["name"] for row in library.list()} >= {"mine", "examples/open-app"}
    for forbidden in (lambda: library.save("examples/open-app", "steps: [{home: }]"),
                      lambda: library.delete("examples/open-app"),
                      lambda: library.save("../escape", "steps: [{home: }]"),
                      lambda: library.get("Examples/Open")):
        with pytest.raises(FlowError):
            forbidden()
    with pytest.raises(KeyError):
        library.get("missing")
    with pytest.raises(KeyError):
        library.get("examples/missing")
    assert library.delete("mine") and not library.delete("mine")


def test_a_broken_flow_file_is_listed_with_its_error(library):
    library.store.write_flow("broken", "steps: [{jump: high}]")
    row = next(r for r in library.list() if r["name"] == "broken")
    assert "no action found" in row["error"]


def test_check_finds_missing_flows_loops_and_untaught_roles(library, tmp_path):
    library.save("a", "steps: [{call: b}]")
    with pytest.raises(FlowError, match="flow 'b' does not exist \\(called from 'a'\\)"):
        library.check("a")
    library.save("b", "steps: [{call: a}]")
    with pytest.raises(FlowError, match="call each other in a loop: a -> b -> a"):
        library.check("a")

    profiles = Profiles(tmp_path / "profiles")
    library.save("roles", "package: com.example.app\nsteps: [{tap: '@send'}]")
    with pytest.raises(FlowError, match="role @send has not been taught"):
        library.check("roles", profiles)
    profiles.set_role("com.example.app", "send", {"id": "btn_send"})
    assert library.check("roles", profiles).name == "roles"


# --------------------------------------------------------------- interpreter
HOME = screen(button("search", "Search", (20, 20, 220, 80)), el(id="title", text="Home", bounds=(0, 100, 480, 160)),
              el(id="count", text="Messages (12)", bounds=(0, 200, 480, 260)),
              button("wipe", "Delete account", (20, 700, 460, 760)))
FORM = screen(field("query", "old", (20, 20, 460, 80), focused=True), button("go", "Go", (20, 100, 220, 160)))
DONE = screen(el(id="result", text="Results for earbuds", bounds=(0, 100, 480, 160)))
POPUP = screen(el(id="rate_title", text="Enjoying the app?", bounds=(40, 300, 440, 360)),
               button("rate_close", "Not now", (40, 400, 240, 460)))
LIST_TOP = screen(el("androidx.recyclerview.widget.RecyclerView", id="list", bounds=(0, 100, 480, 800), scrollable=True,
                     children=[el(id="title", text=t, bounds=(0, 100 + i * 100, 480, 200 + i * 100))
                               for i, t in enumerate(["Network", "Apps", "Battery"])]))
LIST_END = screen(el("androidx.recyclerview.widget.RecyclerView", id="list", bounds=(0, 100, 480, 800), scrollable=True,
                     children=[el(id="title", text=t, bounds=(0, 100 + i * 100, 480, 200 + i * 100))
                               for i, t in enumerate(["Battery", "System", "About"])]))


def device(start: str = "home") -> FakeDevice:
    screens = {"home": HOME, "form": FORM, "done": DONE, "popup": POPUP, "top": LIST_TOP, "end": LIST_END,
               "launcher": screen(el(id="clock", text="12:00"), package="com.android.launcher3")}
    return FakeDevice(screens, start, taps={("home", "search"): "form", ("form", "go"): "done",
                                             ("popup", "rate_close"): "home"},
                      keys={("form", "back"): "home", ("done", "back"): "form", ("top", "swipe_up"): "end",
                            ("launcher", f"launch:{PACKAGE}"): "home"})


def run_flow(text: str, dev: FakeDevice, *, variables: dict | None = None, values: Values | None = None,
             cfg=None, **kw) -> tuple[dict, FlowRun]:
    kw.setdefault("resolver", Resolver(values, instance_id=1, variables=variables))
    run = FlowRun(flow(text), dev, cfg or config(), **kw)
    return run.run(), run


SEARCH = """
package: com.example.app
variables: {query: earbuds}
steps:
  - launch:
  - wait_for: {id: search}
  - tap: {id: search}
  - type: {text: "{query}", into: {id: query}, clear: true}
  - tap: Go
  - wait_for: {text_regex: "Results for {query}"}
"""


def test_a_flow_runs_from_launch_to_result():
    dev = device("launcher")
    result, _ = run_flow(SEARCH, dev)
    assert result["state"] == "done" and result["done"] == 6 and result["steps"] == 6
    assert [s[0] for s in dev.sent] == ["launch", "tap", "tap", "clear", "text", "tap"]
    assert dev.did("text") == [("text", "earbuds")] and dev.did("clear") == [("clear", 3)]
    assert dev.screen == "done" and dev.closed


def test_variables_come_from_the_run_then_the_device_then_shared_values(tmp_path):
    values = Values(tmp_path, WORDS)
    values.set_shared("query", "shared")
    text = SEARCH.replace("variables: {query: earbuds}", "variables: {query: null}").replace(
        '  - wait_for: {text_regex: "Results for {query}"}\n', "")
    dev = device("launcher")
    assert run_flow(text, dev, values=values)[0]["state"] == "done" and dev.did("text") == [("text", "shared")]
    dev = device("launcher")
    run_flow(text, dev, values=values, variables={"query": "passed"})
    assert dev.did("text") == [("text", "passed")]
    result, _ = run_flow(text, device("launcher"))
    assert result["state"] == "failed" and "needs a value for: query" in result["error"]


def test_if_else_on_presence_activity_app_and_equality():
    text = """
    variables: {mode: fast}
    steps:
      - if: {id: search}
        then: [{set: {var: a, value: "yes"}}]
        else: [{set: {var: a, value: "no"}}]
      - if: {gone: {id: search}}
        then: [{set: {var: b, value: "yes"}}]
        else: [{set: {var: b, value: "no"}}]
      - if: {activity: "\\\\.Main$"}
        then: [{set: {var: c, value: "yes"}}]
      - if: {app: com.other.app}
        then: [{set: {var: d, value: "yes"}}]
        else: [{set: {var: d, value: "no"}}]
      - if: {all: [{equals: ["{mode}", fast]}, {not: {visible: Nope}}, {any: [{id: nope}, {id: title}]}]}
        then: [{set: {var: e, value: "yes"}}]
    """
    _, run = run_flow(text, device())
    assert {k: run.resolver.vars[k] for k in "abcde"} == {"a": "yes", "b": "no", "c": "yes", "d": "no", "e": "yes"}


def test_repeat_until():
    text = """
    steps:
      - repeat: 5
        until: {text: System}
        steps: [{swipe: up}]
      - assert: {visible: System}
    """
    dev = device("top")
    assert run_flow(text, dev)[0]["state"] == "done" and len(dev.did("swipe")) == 1
    never = "on_unknown: continue\nsteps: [{repeat: 2, until: {text: Nowhere}, steps: [{key: tab}]}]"
    result, _ = run_flow(never, device("top"))
    assert result["state"] == "done" and "still not true after 2 round(s)" in result["issues"][0]


def test_set_captures_text_and_takes_from_pools(tmp_path):
    values = Values(tmp_path, WORDS)
    values.pool_add("codes", ["c-1", "c-2", "c-3"])
    text = """
    steps:
      - set: {var: unread, from: {id: count}, regex: "\\\\((\\\\d+)\\\\)"}
      - set: {var: label, from: {id: wipe}, attr: text}
      - set: {var: both, value: "{unread}/{label}"}
      - set: {var: first, pool: codes}
      - set: {var: second, pool: codes}
      - set: {var: same, value: "{pool.codes}{pool.codes}"}
    """
    _, run = run_flow(text, device(), values=values)
    got = run.resolver.vars
    assert got["unread"] == "12" and got["both"] == "12/Delete account"
    assert (got["first"], got["second"], got["same"]) == ("c-1", "c-2", "c-3c-3")
    assert values.pool_count("codes") == 0
    missing = "on_unknown: continue\nsteps: [{set: {var: x, from: {id: title}, regex: '(\\\\d+)'}}]"
    assert "text matching" in run_flow(missing, device())[0]["issues"][0]


def test_scroll_to_swipes_until_the_text_shows_and_knows_the_end_of_a_list():
    dev = device("top")
    result, _ = run_flow("steps: [{scroll_to: {text: System}, in: {id: list}}]", dev)
    assert result["state"] == "done" and dev.screen == "end"
    assert dev.did("swipe") == [("swipe", 240, 625, 240, 275)]        # inside the list, finger moving up
    dev = device("top")
    result, _ = run_flow("on_unknown: continue\nsteps: [{scroll_to: {text: Nowhere}}]", dev)
    assert "reached the end of the list after 2 swipe(s)" in result["issues"][0] and len(dev.did("swipe")) == 2
    already = device("end")
    assert run_flow("steps: [{scroll_to: {text: System}}]", already)[0]["state"] == "done" and not already.acted


def test_overlays_and_never_tap_of_a_flow():
    text = """
    overlays: [{name: rate, guard: {id: rate_title}, dismiss: {id: rate_close}}]
    never_tap: [{text: Delete account}]
    on_unknown: continue
    steps:
      - tap: {id: search}
      - back
      - tap: {id: wipe}
    """
    dev = device("popup")
    run = FlowRun(flow(text), dev, config())
    thread = threading.Thread(target=run.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while run.state != "needs_attention" and time.monotonic() < deadline:
        time.sleep(0.01)
    # the pop-up was closed, the search opened and closed again; the forbidden tap was refused and,
    # even though this flow continues past unknown screens, a person is asked
    assert [s[3] for s in dev.did("tap")] == ["rate_close", "search"] and dev.screen == "home"
    assert "never-tap list" in run.waiting["reason"]
    run.resolve_attention("skip")
    thread.join(5)
    assert run.state == "done" and not any(s[3] == "wipe" for s in dev.did("tap"))


def test_on_unknown_stop_asks_and_continue_skips(monkeypatch):
    text = "steps: [{tap: {id: nowhere}, timeout: 0.2}, {tap: {id: search}}]"
    dev = device()
    run = FlowRun(flow(text), dev, config())
    thread = threading.Thread(target=run.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while run.state != "needs_attention" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert run.waiting["what"] == "tap id='nowhere'" and "timed out waiting for id='nowhere'" in run.waiting["reason"]
    assert not dev.acted                                             # the next step did not run meanwhile
    run.resolve_attention("abort")
    thread.join(5)
    assert run.state == "failed"

    dev = device()
    result, _ = run_flow("on_unknown: continue\n" + text, dev)
    assert result["state"] == "done" and result["skipped"] == 1 and dev.screen == "form"
    assert result["message"] == "finished with 1 skipped step(s)"

    dev = device()
    result, _ = run_flow(text.replace("timeout: 0.2}", "timeout: 0.2, optional: true}"), dev)
    assert result["state"] == "done" and result["issues"] == [] and dev.screen == "form"


def test_a_failed_assert_is_never_skipped_silently():
    text = "on_unknown: continue\nsteps: [{assert: {visible: Nope}, message: the greeting is shown}, {tap: {id: search}}]"
    dev = device()
    run = FlowRun(flow(text), dev, config(stop_on_unknown_screen=False))
    result = run.run()
    assert result["state"] == "failed" and "the greeting is shown" in result["error"] and not dev.acted
    ok, _ = run_flow("steps: [{assert: {visible: Home}}, {assert: {not: {visible: Nope}}}]", device())
    assert ok["state"] == "done"


def test_fail_step_and_missing_package():
    result, _ = run_flow("variables: {why: no stock}\nsteps: [{fail: 'stopping: {why}'}]", device())
    assert result["state"] == "failed" and result["error"] == "stopping: no stock"
    result, _ = run_flow("steps: [{launch: }]", device())
    assert result["state"] == "failed" and "launch needs a package" in result["error"]
    result, _ = run_flow("variables: {p: 'bad package'}\nsteps: [{launch: '{p}'}]", device())
    assert result["state"] == "failed" and "bad package name" in result["error"]


def test_back_until_key_home_swipe_long_press_clear_and_sleep(monkeypatch):
    monkeypatch.setattr(runtime, "BACK_POLL", 0.3)
    text = """
    steps:
      - back: {until: {id: title}, presses: 3}
      - long_press: {id: title}
        ms: 800
      - swipe: {from: [0.25, 0.5], to: [0.75, 0.5]}
      - swipe: left
      - key: enter
      - home
      - sleep: 0.05
      - tap: {id: search}
      - clear:
      - stop_app: com.example.app
    """
    dev = device("done")
    result, _ = run_flow(text, dev)
    assert result["state"] == "done"
    assert dev.did("key")[:2] == [("key", "back"), ("key", "back")] and ("key", "home") in dev.sent
    assert dev.did("long_press")[0][3] == 800
    assert dev.did("swipe") == [("swipe", 120, 427, 360, 427), ("swipe", 360, 427, 120, 427)]
    assert dev.did("clear") == [("clear", 3)] and dev.did("stop") == [("stop", "com.example.app")]

    stuck = "on_unknown: continue\nsteps: [{back: {until: {id: nowhere}, presses: 2}}]"
    dev = device("form")
    result, _ = run_flow(stuck, dev)
    assert len(dev.did("key")) == 2 and "still not there after 2 Back press(es)" in result["issues"][0]


def test_wait_gone_and_step_timeouts():
    dev = device("popup")
    threading.Timer(0.3, lambda: setattr(dev, "screen", "home")).start()
    assert run_flow("steps: [{wait_gone: {id: rate_title}, timeout: 5}]", dev)[0]["state"] == "done"
    started = time.monotonic()
    result, _ = run_flow("on_unknown: continue\nsteps: [{wait_gone: {id: title}, timeout: 0.3}]", device())
    assert "to go away" in result["issues"][0] and time.monotonic() - started < 3


def test_screenshots_go_to_the_run_folder(tmp_path):
    result, _ = run_flow("steps: [{screenshot: home}, {screenshot: }]", device(), artifacts=tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert result["state"] == "done" and len(names) == 2
    assert names[0].startswith("home-") and names[1].startswith("shot02-") and all(n.endswith(".png") for n in names)
    assert run_flow("steps: [{screenshot: x}]", device())[0]["state"] == "done"      # no folder: skipped, not fatal


def test_call_scopes_variables_and_keeps_never_tap(tmp_path):
    store = AutomationStore(tmp_path)
    library = FlowLibrary(store, WORDS)
    library.save("type-it", """
        variables: {text: null, suffix: "!"}
        never_tap: [{text: Delete account}]
        overlays: [{name: inner, guard: {id: rate_title}, dismiss: {id: rate_close}}]
        steps:
          - type: "{text}{suffix}"
          - set: {var: inner_only, value: "x"}
    """)
    parent = """
    variables: {text: outer}
    steps:
      - call: type-it
        with: {text: "hello {text}"}
      - type: "{text}"
      - call: type-it
    """
    dev = device("form")
    run = FlowRun(flow(parent), dev, config(), library=library)
    result = run.run()
    assert result["state"] == "done"
    assert dev.did("text") == [("text", "hello outer!"), ("text", "outer"), ("text", "outer!")]
    assert run.resolver.vars["text"] == "outer" and "suffix" not in run.resolver.vars
    assert run.resolver.vars["inner_only"] == "x"                  # what a called flow captured is kept
    assert run.overlays == [] and [s.text for s in run.never_tap] == ["Delete account", "Delete account"]

    result = FlowRun(flow("steps: [{call: nope}]"), device(), config(), library=library).run()
    assert result["state"] == "failed" and "no flow 'nope'" in result["error"]
    library.save("needy", "variables: {must: null}\nsteps: [{home: }]")
    result = FlowRun(flow("steps: [{call: needy}]"), device(), config(), library=library).run()
    assert result["state"] == "failed" and "needs a value for: must" in result["error"]


def test_roles_and_profile_rules_apply_to_a_flow(tmp_path):
    profiles = Profiles(tmp_path)
    profiles.set_role(PACKAGE, "search_button", {"id": "search"})
    profiles.add_overlay(PACKAGE, {"name": "rate", "guard": {"id": "rate_title"}, "dismiss": {"id": "rate_close"}})
    profiles.add_never_tap(PACKAGE, {"text": "Delete account"})
    text = f"package: {PACKAGE}\non_unknown: continue\nsteps: [{{tap: '@search_button'}}, {{back: }}, {{tap: {{id: wipe}}}}]"
    dev = device("popup")
    run = FlowRun(flow(text), dev, config(stop_on_unknown_screen=False), profiles=profiles)
    result = run.run()
    assert [s[3] for s in dev.did("tap")] == ["rate_close", "search"]
    assert result["state"] == "failed" and "never-tap" in result["error"]


def test_a_dry_run_of_a_flow_sends_nothing(tmp_path):
    values = Values(tmp_path, WORDS)
    values.pool_add("codes", ["c-1"])
    text = SEARCH + "  - set: {var: code, pool: codes}\n  - repeat: 50\n    steps: [{key: tab}]\n  - sleep: 30\n"
    dev = device()
    started = time.monotonic()
    result, run = run_flow(text, dev, values=values, dry_run=True,
                           resolver=Resolver(values, instance_id=1, consume=False))
    assert result["state"] == "done" and not dev.acted and time.monotonic() - started < 5
    assert "practice run: nothing was sent" in result["message"] and values.pool_count("codes") == 1
    assert any("would tap 'Search'" in line for line in result["log"])
    assert len(result["issues"]) == 3                               # the steps that need a later screen


def test_typed_secrets_are_not_logged():
    lines: list[str] = []
    text = "variables: {password: null}\nsteps: [{type: '{password}'}, {type: {text: 'literal-secret', secret: true}}]"
    dev = device("form")
    values = Values("/nonexistent-never-written", WORDS)
    result, _ = run_flow(text, dev, log=lines.append, resolver=Resolver(values, variables={"password": "hunter2"}))
    assert dev.did("text") == [("text", "hunter2"), ("text", "literal-secret")]
    everything = "".join(lines) + str(result)
    assert "hunter2" not in everything and "literal-secret" not in everything
    assert any("typed {password}" in line for line in result["log"])


def test_flow_timeouts_override_the_farm_defaults():
    run = FlowRun(flow("timeouts: {step: 7, run: 70}\nsteps: [{home: }]"), device(), config())
    assert run.step_timeout == 7 and run.run_timeout == 70
    run = FlowRun(flow("steps: [{home: }]"), device(), config(step_timeout=3, run_timeout=33))
    assert run.step_timeout == 3 and run.run_timeout == 33


def test_the_example_flows_run_on_a_scripted_device(tmp_path):
    library = FlowLibrary(AutomationStore(tmp_path), WORDS)
    dev = device("launcher")
    run = FlowRun(library.get("examples/open-app"), dev, config(), library=library,
                  resolver=Resolver(variables={"package": PACKAGE}))
    assert run.run()["state"] == "done" and dev.sent[:2] == [("stop", PACKAGE), ("launch", PACKAGE)]

    dev = device("form")
    run = FlowRun(library.get("examples/type-into-focused-field"), dev, config(),
                  resolver=Resolver(variables={"text": "hello there"}))
    assert run.run()["state"] == "done" and dev.sent == [("clear", 3), ("text", "hello there")]

    dev = device("top")
    run = FlowRun(library.get("examples/scroll-until-visible"), dev, config(), resolver=Resolver(variables={"text": "About"}))
    assert run.run()["state"] == "done" and dev.screen == "end"

    permission = "com.android.permissioncontroller"
    dialog = screen(el(id=f"{permission}:id/permission_message", text="Allow the app to take pictures?", bounds=(40, 300, 440, 360)),
                    button(f"{permission}:id/permission_allow_foreground_only_button", "While using the app", (40, 400, 440, 460)),
                    button(f"{permission}:id/permission_deny_button", "Don't allow", (40, 500, 440, 560)), package=permission)
    for answer, expected in (("allow", "permission_allow_foreground_only_button"), ("deny", "permission_deny_button")):
        dev = FakeDevice({"dialog": dialog, "home": HOME}, "dialog",
                         taps={("dialog", "permission_allow_foreground_only_button"): "home",
                               ("dialog", "permission_deny_button"): "home"})
        run = FlowRun(library.get("examples/dismiss-permission-dialog"), dev, config(),
                      resolver=Resolver(variables={"answer": answer}))
        assert run.run()["state"] == "done" and [s[3] for s in dev.did("tap")] == [expected]
    quiet = device()
    assert FlowRun(library.get("examples/dismiss-permission-dialog"), quiet, config()).run()["state"] == "done"
    assert not quiet.acted


def test_pop_ups_are_cleared_before_steps_that_act_without_looking():
    text = """
    overlays: [{name: rate, guard: {id: rate_title}, dismiss: {id: rate_close}}]
    steps:
      - key: tab
      - swipe: up
    """
    dev = device("popup")
    result, _ = run_flow(text, dev)
    assert result["state"] == "done"
    assert [s[0] for s in dev.sent] == ["tap", "key", "swipe"] and dev.sent[0][3] == "rate_close"
    quiet = device()
    run_flow("steps: [{key: tab}]", quiet)
    assert quiet.reads == 0                              # no rules, no extra read

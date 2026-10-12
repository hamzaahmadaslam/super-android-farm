"""Alignment, voting and consensus training, and the trained program on disk.

Ported from the checks that pinned down the original bugs: a wrongly captured element
must not delete a step, a step the template recording missed must survive, and
training must say plainly what it left out.
"""
from __future__ import annotations

import pytest
from PIL import Image

from superfarm.automation.align import align, alignment_score, choose_reference, columns, similarity
from superfarm.automation.memory import save_png
from superfarm.automation.program import Program, ProgramStep
from superfarm.automation.store import AutomationStore, Recording, Step
from superfarm.automation.trainer import apply_annotation, build_consensus, describe_for_model, train

WORDS = ["password", "pin", "otp"]


def tap(rid: str = "", text: str = "", xy=(100, 200), activity="app/.Main") -> Step:
    element = {"resource_id": rid, "text": text, "desc": "", "cls": "Button"} if (rid or text) else None
    return Step(kind="tap", device=list(xy), element=element, activity=activity)


def typed(dynamic: str | None = None, text: str | None = None) -> Step:
    return Step(kind="type", dynamic=dynamic, text=text)


def recordings(plans: list[list[Step]], prefix: str = "r") -> list[Recording]:
    out = []
    for number, steps in enumerate(plans):
        for index, step in enumerate(steps):
            step.id = f"s{index:04d}"
        out.append(Recording(id=f"{prefix}{number}", device=f"127.0.0.1:1750{number}", screen=[480, 854], steps=steps))
    return out


# ---------------------------------------------------------------- similarity
def test_similarity_of_taps():
    assert similarity(tap("btn_next"), tap("btn_next")) == 1.0
    assert similarity(tap("", "Next"), tap("", "Next")) > 0.9
    # different ids at the same spot still look related...
    assert 0.3 < similarity(tap("btn_a", xy=(100, 200)), tap("btn_b", xy=(105, 205))) < 1.0
    # ...far apart they do not
    assert similarity(tap("btn_a", xy=(100, 200)), tap("btn_b", xy=(400, 800))) < 0.35


def test_similarity_of_other_kinds():
    swipe_up = Step(kind="swipe", device=[240, 700], to=[240, 200])
    swipe_down = Step(kind="swipe", device=[240, 200], to=[240, 700])
    assert similarity(tap("x"), swipe_up) == 0.0                       # a tap is never a swipe
    assert similarity(swipe_up, Step(kind="swipe", device=[200, 650], to=[210, 250])) == 0.9
    assert similarity(swipe_up, swipe_down) < 0.4
    assert similarity(typed("term"), typed("term")) == 1.0
    assert similarity(typed("term"), typed("other")) < 0.3
    assert similarity(typed(text="a"), typed(text="b")) == 0.85
    assert similarity(Step(kind="key", key="back"), Step(kind="key", key="BACK")) == 1.0
    assert similarity(Step(kind="key", key="back"), Step(kind="key", key="enter")) < 0.35
    assert similarity(Step(kind="launch", package="a.b"), Step(kind="launch", package="a.b")) == 1.0
    assert similarity(Step(kind="launch", package="a.b"), Step(kind="launch", package="c.d")) < 0.35
    assert similarity(Step(kind="wait"), Step(kind="wait")) >= 0.35


# ----------------------------------------------------------------- alignment
def test_alignment_steps_over_an_extra_action_and_reports_a_missing_one():
    reference = [tap("a"), tap("b"), tap("c")]
    assert align(reference, [tap("a"), tap("popup"), tap("b"), tap("c")]) == [0, 2, 3]
    assert align(reference, [tap("a"), tap("c")]) == [0, None, 1]
    assert align(reference, []) == [None, None, None]
    assert align([], reference) == []
    assert alignment_score(reference, list(reference)) == 1.0


def test_the_most_representative_recording_is_the_template():
    odd_one_out = [tap("x"), tap("y"), tap("z"), tap("w")]
    typical = [tap("a"), tap("b"), tap("c")]
    assert choose_reference([odd_one_out, typical, list(typical)]) in (1, 2)
    assert choose_reference([typical]) == 0


def test_a_wrongly_captured_element_does_not_delete_the_step():
    """Three recordings of one five-step task, where the element captured for the
    same tap differed every time, once trained down to a single step."""
    def swipe() -> Step:
        return Step(kind="swipe", device=[240, 700], to=[240, 200])
    runs = [
        [swipe(), tap("search_container", xy=(240, 120)), tap("view_drag_area", xy=(100, 300)),
         tap("view_drag_area", xy=(100, 300)), tap("et_phone_num", xy=(240, 400))],
        [swipe(), tap("", "Home", xy=(244, 124)), tap("", "Home", xy=(104, 304)),
         tap(xy=(102, 302)), tap("et_phone_num", xy=(242, 402))],
        [swipe(), tap("search_container", xy=(238, 118)), tap("", "Home", xy=(98, 298)),
         tap("", "Home", xy=(101, 301)), tap("icon", xy=(241, 401))],
    ]
    _, grouped = columns(runs)
    assert [len(column) for column in grouped] == [3, 3, 3, 3, 3]


def test_a_step_missing_from_the_template_is_recovered():
    _, grouped = columns([[tap("a"), tap("c")], [tap("a"), tap("b"), tap("c")], [tap("a"), tap("b"), tap("c")]])
    assert [(column[0].element or {}).get("resource_id") for column in grouped] == ["a", "b", "c"]
    assert [len(column) for column in grouped] == [3, 2, 3]


# ----------------------------------------------------------------- consensus
def three_searches() -> list[Recording]:
    """Three recordings of the same task; the middle one hit an extra pop-up."""
    plans = [
        [tap("search_bar"), typed("term"), tap("", "Go")],
        [tap("search_bar"), tap("", "Dismiss"), typed("term"), tap("", "Go")],
        [tap("search_bar"), typed("term"), tap("", "Go")],
    ]
    for number, steps in enumerate(plans):
        for step in steps:
            if step.kind == "tap":
                step.device = [100 + number, 200 + number]            # slight variation
    return recordings(plans)


def test_consensus_finds_the_common_path():
    result = build_consensus(three_searches())
    steps = result.steps
    assert [s.kind for s in steps] == ["tap", "type", "tap"]
    assert all(s.selector.get("text") != "Dismiss" for s in steps)     # the one-off pop-up is not in it
    assert steps[0].selector.get("id") == "search_bar"                 # selectors come from the elements
    assert steps[1].dynamic == "term"
    assert steps[0].device == [101, 201]                               # the median, not one sloppy click
    assert all(s.seen_in == 3 for s in steps) and not any(s.optional for s in steps)
    assert [s.id for s in steps] == ["p000", "p001", "p002"]


def test_training_says_what_it_left_out_and_why():
    result = build_consensus(three_searches())
    left_out = next(w for w in result.warnings if "left out" in w)
    assert "1 step(s) appeared in fewer than 2 of the 3 recordings" in left_out
    assert "tap 'Dismiss' (1 of 3)" in left_out
    assert any(result.reference in w and "agreed most" in w for w in result.warnings)


def test_a_step_only_one_recording_made_is_left_out_and_a_majority_step_is_optional():
    base = three_searches()
    extra = recordings([[tap("search_bar"), typed("term"), tap("", "Go"), tap("", "Done")]], prefix="x")
    steps = build_consensus(base + extra).steps
    assert [s.selector.get("text") for s in steps].count("Done") == 0
    # a step two of four made is kept, and marked as not always there
    two_of_four = recordings([[tap("a"), tap("b")], [tap("a"), tap("b")], [tap("a")], [tap("a")]])
    result = build_consensus(two_of_four)
    assert [(s.selector.get("id"), s.optional, s.seen_in) for s in result.steps] == [("a", False, 4), ("b", True, 2)]
    assert any("were not in every recording" in w and "p001" in w for w in result.warnings)


def test_one_bad_capture_is_outvoted():
    result = build_consensus(recordings([[tap("btn_ok")], [tap("btn_ok")], [tap("overlay")]]))
    step = result.steps[0]
    assert step.selector["id"] == "btn_ok" and {"id": "overlay", "cls": "Button"} in step.alternatives
    assert any("2 of 3 recordings agreed on 'btn_ok'" in w for w in result.warnings)


def test_no_majority_means_no_committed_selector():
    result = build_consensus(recordings([[tap("a", xy=(100, 200))], [tap("b", xy=(102, 201))],
                                         [tap("c", xy=(101, 199))], [tap("d", xy=(99, 202))]]))
    step = result.steps[0]
    assert step.selector == {} and len(step.alternatives) == 4 and step.device == [100, 200]
    assert step.intent == "unclear which element this is"
    assert any("no element had majority support" in w and "Re-record" in w for w in result.warnings)


def test_position_only_taps_are_called_out():
    result = build_consensus(recordings([[tap(xy=(300, 400))], [tap(xy=(306, 396))]]))
    assert len(result.steps) == 1 and result.steps[0].seen_in == 2      # same place: one step
    assert any("can only be found by position" in w and "p000" in w for w in result.warnings)

    # 300 px apart with nothing to identify them: far more likely two different
    # actions than one done sloppily, so they stay separate and optional
    scattered = build_consensus(recordings([[tap(xy=(100, 200))], [tap(xy=(400, 700))]]))
    assert len(scattered.steps) == 2
    assert all(s.optional and s.seen_in == 1 for s in scattered.steps)


def test_neighbouring_elements_become_alternatives():
    first, second = tap("row"), tap("row")
    first.candidates = [{"id": "title", "text": "Battery"}, {"id": "row"}]
    second.candidates = [{"id": "title", "text": "Battery"}]
    step = build_consensus(recordings([[first], [second]])).steps[0]
    assert step.selector["id"] == "row"
    assert {"id": "title", "text": "Battery"} in step.alternatives
    assert len(step.alternatives) <= 4


def test_a_single_recording_and_mixed_apps_are_warned_about():
    single = build_consensus(recordings([[tap("a")]]))
    assert any("single recording" in w for w in single.warnings)
    mixed = recordings([[tap("a")], [tap("a")]])
    mixed[0].package, mixed[1].package = "com.one.app", "com.two.app"
    assert any("different apps" in w for w in build_consensus(mixed).warnings)
    assert any("nothing could be derived" in w for w in build_consensus([Recording(id="e")]).warnings)


# ------------------------------------------------------ annotation by a model
class FakeModel:
    model = "test-model"

    def __init__(self, reply):
        self.reply, self.asked = reply, []

    def ask(self, system, user, schema=None):
        self.asked.append(user)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_a_model_can_describe_steps_but_never_change_them():
    steps = build_consensus(three_searches()).steps
    program = Program(slug="s", goal="search", steps=steps)
    before = [(s.id, s.kind, dict(s.selector), s.device) for s in steps]
    model = FakeModel({"steps": [
        {"id": "p000", "intent": "open the  search bar", "fragile": True, "wait_after": 3},
        {"id": "p001", "intent": "type the term", "wait_after": 99, "selector": {"id": "evil"}, "kind": "key"},
        {"id": "p999", "intent": "an invented step"},
        "not even a mapping"],
        "summary": "Searches for a term.", "notes": ["watch the keyboard", 7]})
    apply_annotation(program, model, 3, WORDS)
    assert [(s.id, s.kind, dict(s.selector), s.device) for s in program.steps] == before
    assert program.steps[0].intent == "open the search bar" and program.steps[0].wait_after == 3.0
    assert program.steps[1].intent == "type the term" and program.steps[1].wait_after == 0.0   # 99 s is refused
    assert len(program.steps) == 3
    assert program.notes[0] == "Searches for a term." and "watch the keyboard" in program.notes
    assert any("p000 looks fragile" in n for n in program.notes)
    assert program.model == "test-model"


def test_an_unreachable_model_leaves_a_valid_program():
    from superfarm.automation.llm import LlmError
    program = Program(slug="s", steps=build_consensus(three_searches()).steps)
    apply_annotation(program, FakeModel(LlmError("cannot reach the model server")), 3, WORDS)
    assert len(program.steps) == 3 and any("not described" in w for w in program.warnings)


def test_secrets_are_described_but_never_sent_to_a_model():
    field = {"id": "password_input", "cls": "EditText"}
    steps = [ProgramStep(id="p000", kind="type", text="hunter2", selector=field, seen_in=1),
             ProgramStep(id="p001", kind="type", dynamic="otp_code", seen_in=1),
             ProgramStep(id="p002", kind="type", text="hello", selector={"id": "comment"}, seen_in=1),
             ProgramStep(id="p003", kind="type", dynamic="greeting", seen_in=1)]
    described = describe_for_model(steps, 1, WORDS)
    assert "hunter2" not in str(described)
    assert described[0]["types"] == "text (withheld)" and described[1]["types"] == "a secret value"
    assert described[2]["types"] == "hello" and described[3]["types"] == "the value named greeting"
    model = FakeModel({"steps": []})
    apply_annotation(Program(slug="s", steps=steps), model, 1, WORDS)
    assert "hunter2" not in model.asked[0]


# ------------------------------------------------------------- train + disk
def test_train_writes_a_program_with_its_pictures(tmp_path):
    store = AutomationStore(tmp_path)
    dataset = store.create_dataset("Search", "search for something", "com.example.app")
    for recording in three_searches():
        recording.steps[0].shot, recording.steps[0].shot_box = "shots/s0000.png", [10, 110, 190, 290]
        save_png(Image.new("RGB", (180, 180), (200, 10, 10)), dataset.shot_path(recording.id, "shots/s0000.png"))
        dataset.save_recording(recording)
    lines: list[str] = []
    program = train(dataset, store.programs_dir, forbidden_words=WORDS, log=lines.append)
    assert program.slug == "search" and program.package == "com.example.app" and program.goal == "search for something"
    assert program.screen == [480, 854] and len(program.from_recordings) == 3
    assert any(line.startswith("! ") for line in lines)                # warnings are spoken, not hidden

    loaded = Program.load(store.programs_dir, "search")
    assert [s.kind for s in loaded.steps] == ["tap", "type", "tap"]
    assert loaded.steps[0].shot == "shots/p000.png" and loaded.steps[0].shot_box == [10, 110, 190, 290]
    assert loaded.shot_path(loaded.steps[0].shot).is_file()
    assert loaded.steps[2].shot is None
    assert dataset.summary(store.programs_dir)["trained"]

    # retraining replaces the folder: pictures of steps that are gone do not linger
    (loaded.dir / "shots" / "p777.png").write_bytes(b"old")
    train(dataset, store.programs_dir)
    assert not (loaded.dir / "shots" / "p777.png").exists()


def test_training_needs_recordings(tmp_path):
    store = AutomationStore(tmp_path)
    with pytest.raises(ValueError, match="no recordings yet"):
        train(store.create_dataset("empty"), store.programs_dir)


def test_program_round_trip_and_safe_delete(tmp_path):
    root = tmp_path / "programs"
    program = Program(slug="demo", name="Demo", goal="do the thing", created="now", package="com.example.app",
                      steps=[ProgramStep(id="p000", kind="tap", selector={"resource_id": "btn_go"}, device=[10, 20]),
                             ProgramStep(id="p001", kind="type", dynamic="term"),
                             ProgramStep(id="p002", kind="tap", device=[0, 0], optional=True, seen_in=2)])
    program.save(root)
    loaded = Program.load(root, "demo")
    assert len(loaded.steps) == 3 and loaded.steps[0].selector["resource_id"] == "btn_go"
    assert loaded.steps[0].has_selector() and not loaded.steps[2].has_selector()
    assert loaded.steps[2].device == [0, 0] and loaded.steps[2].optional and loaded.steps[2].seen_in == 2
    assert loaded.summary()["dynamic"] == ["term"] and loaded.summary()["with_selector"] == 1
    assert "term" in loaded.steps[1].label() and loaded.steps[0].label() == "tap btn_go"
    assert [p.slug for p in Program.list(root)] == ["demo"]
    assert Program.load(root, "nope") is None

    (tmp_path / "keep.txt").write_text("outside the programs folder")
    assert not Program.delete(root, "..") and (tmp_path / "keep.txt").is_file()
    assert not Program.delete(root, "demo/shots") and not Program.delete(root, "nope")
    assert loaded.shot_path("../../keep.txt") is None
    assert Program.delete(root, "demo") and Program.load(root, "demo") is None

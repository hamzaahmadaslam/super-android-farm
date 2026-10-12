"""Recording sessions and app profiles, on a scripted device."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from automation_fakes import PACKAGE, FakeDevice, button, config, el, field, screen  # noqa: E402

from superfarm.automation.memory import load_image  # noqa: E402
from superfarm.automation.profiles import AppProfile, Profiles, screen_elements  # noqa: E402
from superfarm.automation.recorder import Recorder, RecordingError, RecordingSession  # noqa: E402
from superfarm.automation.store import AutomationStore  # noqa: E402
from superfarm.automation.values import Values  # noqa: E402

WORDS = ["password", "pin", "otp"]
HOME = screen(
    el("android.widget.LinearLayout", id="row", bounds=(0, 100, 480, 200), clickable=True, children=[
        el(id="title", text="Battery", bounds=(20, 120, 200, 160))]),
    button("search", "Search", (20, 20, 220, 80)),
)
FORM = screen(field("query", "old", (20, 20, 460, 80), focused=True),
              field("secret_box", "", (20, 100, 460, 160), password=True))
LOGIN = screen(field("secret_box", "", (20, 100, 460, 160), password=True, focused=True))
PIN = screen(field("pin_entry", "", (20, 100, 460, 160), focused=True))


class Ordered(FakeDevice):
    """Remembers the order of reads, screenshots and inputs."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.order: list[str] = []

    def nodes(self, **kw):
        self.order.append("read")
        return super().nodes(**kw)

    def screenshot(self):
        self.order.append("shot")
        return super().screenshot()

    def tap(self, x, y):
        self.order.append("tap")
        super().tap(x, y)


def device(start: str = "home") -> Ordered:
    return Ordered({"home": HOME, "form": FORM, "login": LOGIN, "pin": PIN}, start,
                   taps={("home", "search"): "form"})


@pytest.fixture
def setup(tmp_path):
    store = AutomationStore(tmp_path)
    dataset = store.create_dataset("Search", "search for something", PACKAGE)
    values = Values(store.values_dir, WORDS)
    return store, dataset, values


def session(setup, dev: FakeDevice) -> RecordingSession:
    _, dataset, values = setup
    return RecordingSession(dev, dataset, config(forbidden_field_words=WORDS), values=values, instance_id=4)


# ------------------------------------------------------------------ recording
def test_a_tap_is_read_resolved_and_pictured_before_it_is_sent(setup):
    dev = device()
    rec = session(setup, dev)
    step = rec.tap(0.25, 0.058)                                   # (120, 49): the Search button
    assert dev.order == ["read", "shot", "tap"]                   # the tap comes last
    assert step["kind"] == "tap" and step["device"] == [120, 49]
    assert step["element"]["id"] == "search" and step["element"]["text"] == "Search"
    assert step["candidates"][0]["id"] == "search" and step["activity"].startswith(PACKAGE)
    assert step["shot"] == "shots/s0000.png" and step["shot_box"] == [30, 0, 210, 139]
    picture = load_image(setup[1].shot_path(rec.recording.id, step["shot"]))
    assert picture.size == (180, 139)
    # the element recorded is the one that was on screen *before* the tap changed it
    assert dev.screen == "form" and step["element"]["id"] == "search"


def test_a_row_is_recorded_with_its_label(setup):
    rec = session(setup, device())
    step = rec.tap(400, 150, normalized=False)                    # on the row, beside its label
    assert step["element"]["id"] == "row" and step["element"]["text"] == "Battery"
    assert {"id": "title", "text": "Battery", "cls": "TextView"}.items() <= step["candidates"][0].items() or \
        step["candidates"][0]["id"] == "row"


def test_a_tap_is_still_recorded_when_the_screen_cannot_be_read(setup):
    dev = device()
    dev.fail_reads = 1
    rec = session(setup, dev)
    step = rec.tap(120, 49, normalized=False)
    assert dev.did("tap") and "element" not in step and step["note"] == "the screen could not be read"
    assert "scripted failure" in step["read_problem"] and step["shot"]


def test_every_kind_of_step_and_incremental_saving(setup):
    store, dataset, values = setup
    values.set_shared("term", "earbuds")
    dev = device()
    rec = session(setup, dev)
    rec.launch(PACKAGE)
    rec.tap(120, 49, normalized=False)
    rec.text(name="term")
    rec.text("typed literally")
    rec.key("ENTER")
    rec.swipe(0.5, 0.8, 0.5, 0.2)
    rec.long_press(0.5, 0.05, ms=900)
    rec.wait(2)
    # saved after every action: a crash now would lose nothing
    on_disk = dataset.load_recording(rec.recording.id)
    assert [s.kind for s in on_disk.steps] == ["launch", "tap", "type", "type", "key", "swipe", "long_press", "wait"]
    assert on_disk.steps[2].dynamic == "term" and on_disk.steps[2].text is None
    assert on_disk.steps[2].element["id"] == "query" and on_disk.steps[2].element["text"] == ""
    assert on_disk.steps[3].text == "typed literally" and on_disk.steps[4].key == "enter"
    assert on_disk.steps[5].device == [240, 683] and on_disk.steps[5].to == [240, 170]
    assert on_disk.steps[6].seconds == 0.9 and on_disk.instance == 4 and on_disk.screen == [480, 854]
    assert dev.did("text") == [("text", "earbuds"), ("text", "typed literally")]
    assert dev.did("clear") == [("clear", 3), ("clear", 3)]       # what was in the field went first

    assert rec.undo() and len(dataset.load_recording(rec.recording.id).steps) == 7
    summary = rec.stop()
    assert summary["steps"] == 7 and summary["taps"] == 2 and summary["named_taps"] == 2
    assert "saved" in summary["summary"] and dev.closed
    with pytest.raises(RecordingError, match="has been stopped"):
        rec.tap(0.5, 0.5)


def test_assert_visible_records_an_expectation_without_touching_the_device(setup):
    dev = device()
    rec = session(setup, dev)
    step = rec.assert_visible(120, 140, normalized=False)
    assert step["kind"] == "assert" and step["element"]["text"] == "Battery" and not dev.acted
    with pytest.raises(RecordingError, match="nothing with an id, text or description"):
        rec.assert_visible(470, 840, normalized=False)


def test_a_literal_is_never_saved_from_a_password_field(setup):
    for start in ("login", "pin"):
        dev = device(start)
        rec = session(setup, dev)
        with pytest.raises(RecordingError, match="Give the value a name"):
            rec.text("hunter2")
        assert not dev.acted and rec.recording.steps == []
        # with a name it is typed now, and only the name is kept
        step = rec.text("hunter2", name="password")
        assert dev.did("text") == [("text", "hunter2")]
        assert step["dynamic"] == "password" and "text" not in step
        saved = (setup[1].recording_dir(rec.recording.id) / "recording.json").read_text()
        assert "hunter2" not in saved


def test_a_literal_is_refused_when_the_field_cannot_be_checked(setup):
    # The guard above looks at the focused field. When the screen cannot be read there is
    # nothing to look at, and saving the text anyway is how a password ends up on disk.
    dev = device("login")
    rec = session(setup, dev)
    dev.fail_reads = 1
    with pytest.raises(RecordingError, match="the screen could not be read.*try again, or give the value a name"):
        rec.text("hunter2")
    assert not dev.acted and rec.recording.steps == []
    dev.fail_reads = 1
    step = rec.text("hunter2", name="password")                   # a named value needs no check: it is not saved
    assert dev.did("text") == [("text", "hunter2")] and step["dynamic"] == "password"
    saved = (setup[1].recording_dir(rec.recording.id) / "recording.json").read_text()
    assert "hunter2" not in saved


def test_a_password_field_of_any_class_is_recognised(setup):
    # Toolkits that draw their own fields report password="true" on a class that is not an EditText.
    drawn = screen(el("android.view.View", id="box", bounds=(20, 100, 460, 160), password=True, focused=True))
    dev = Ordered({"drawn": drawn}, "drawn")
    rec = session(setup, dev)
    with pytest.raises(RecordingError, match="Give the value a name"):
        rec.text("hunter2")
    assert not dev.acted and rec.recording.steps == []


def test_typing_works_where_the_screen_is_readable_and_nothing_is_focused(setup):
    rec = session(setup, device("home"))
    assert rec.text("hello")["text"] == "hello"


def test_field_contents_never_enter_a_recording(setup):
    rec = session(setup, device("form"))
    step = rec.tap(240, 50, normalized=False)                     # the field that holds "old"
    assert step["element"]["id"] == "query" and step["element"]["text"] == ""
    # ... nor through the list of other ways to find the element, which is saved with the step
    assert step["candidates"] and all("text" not in c for c in step["candidates"] if c.get("id") == "query")
    saved = (setup[1].recording_dir(rec.recording.id) / "recording.json").read_text()
    assert '"old"' not in saved
    expected = rec.assert_visible(240, 50, normalized=False)
    assert all("text" not in c for c in expected["candidates"])
    # A label is identity, not content: its text stays.
    label = session(setup, device("home")).tap(100, 140, normalized=False)
    assert {"Battery"} <= {c.get("text") for c in label["candidates"]}


def test_named_values_must_exist_and_coordinates_must_be_on_screen(setup):
    rec = session(setup, device("form"))
    with pytest.raises(RecordingError, match="no value called 'missing'"):
        rec.text(name="missing")
    with pytest.raises(RecordingError, match="nothing to type"):
        rec.text("")
    with pytest.raises(ValueError):
        rec.text(name="bad name")
    for call in (lambda: rec.tap(1.5, 0.5), lambda: rec.tap(-1, 5, normalized=False),
                 lambda: rec.swipe(0.5, 0.5, 0.5, 9)):
        with pytest.raises(RecordingError):
            call()
    with pytest.raises(ValueError, match="bad package"):
        rec.launch("not a package")


def test_a_pool_value_can_be_recorded_by_name(setup):
    setup[2].pool_add("codes", ["c-1", "c-2"])
    dev = device("form")
    rec = session(setup, dev)
    step = rec.text(name="pool.codes")
    assert dev.did("text") == [("text", "c-1")] and step["dynamic"] == "pool.codes"
    assert setup[2].pool_count("codes") == 1


def test_cancel_throws_the_recording_away(setup):
    rec = session(setup, device())
    rec.tap(0.25, 0.058)
    rid = rec.recording.id
    rec.cancel()
    assert setup[1].load_recording(rid) is None and not rec.active


def test_the_recorder_allows_one_session_per_device(setup):
    store, dataset, values = setup
    made: list[FakeDevice] = []

    def factory(instance_id: int) -> FakeDevice:
        made.append(device())
        return made[-1]

    recorder = Recorder(store, config(forbidden_field_words=WORDS), factory, values)
    status = recorder.start("search", 4)
    assert status["active"] and status["instance"] == 4 and recorder.busy() == {4}
    with pytest.raises(RecordingError, match="already being recorded"):
        recorder.start("search", 4)
    recorder.start("search", 5)
    recorder.session(4).tap(0.25, 0.058)
    assert [s["steps"] for s in recorder.active()] == [1, 0]
    assert recorder.stop(4)["steps"] == 1 and recorder.busy() == {5}
    with pytest.raises(RecordingError, match="not being recorded"):
        recorder.session(4)
    with pytest.raises(RecordingError):
        recorder.stop(4)
    assert recorder.cancel(5) and not recorder.cancel(5)
    with pytest.raises(KeyError):
        recorder.start("no-such-dataset", 6)
    assert len(dataset.recording_ids()) == 1


# ------------------------------------------------------------------- profiles
def test_roles_are_taught_from_a_live_node(tmp_path):
    profiles = Profiles(tmp_path)
    assert profiles.load(PACKAGE) == AppProfile(package=PACKAGE) and profiles.list() == []
    search = next(n for n in HOME if n["id"] == "search")
    assert profiles.teach(PACKAGE, "search_button", search) == {"id": "search", "text": "Search"}
    assert profiles.teach(PACKAGE, "search_button", search, with_text=False) == {"id": "search"}
    field_node = next(n for n in FORM if n["id"] == "query")
    assert profiles.teach(PACKAGE, "box", field_node) == {"id": "query", "editable": True}
    profile = profiles.load(PACKAGE)
    assert profile.roles["search_button"].id == "search" and profile.roles["box"].editable is True
    assert profiles.list() == [PACKAGE]

    with pytest.raises(ValueError, match="belongs to"):
        profiles.teach("com.other.app", "x", search)
    with pytest.raises(ValueError, match="nothing stable"):
        profiles.teach(PACKAGE, "frame", HOME[0])
    with pytest.raises(ValueError, match="bad role name"):
        profiles.set_role(PACKAGE, "two words", {"id": "a"})
    with pytest.raises(ValueError):
        profiles.set_role(PACKAGE, "empty", {})
    assert "search_button" not in profiles.remove_role(PACKAGE, "search_button").roles


def test_overlays_and_never_tap_in_a_profile(tmp_path):
    profiles = Profiles(tmp_path)
    profiles.set_role(PACKAGE, "close", {"id": "rate_close"})
    profiles.add_overlay(PACKAGE, {"name": "rate", "guard": {"id": "rate_title"}, "dismiss": "@close"})
    profiles.add_overlay(PACKAGE, {"name": "rate", "guard": {"id": "rate_title2"}, "dismiss": "@close"})   # replaced
    profiles.add_never_tap(PACKAGE, "Delete account")
    profile = profiles.load(PACKAGE)
    assert [o.guard.id for o in profile.overlays] == ["rate_title2"]
    assert profile.resolved_overlays()[0].dismiss.id == "rate_close"
    assert profile.resolved_never_tap()[0].text == "Delete account"
    with pytest.raises(ValueError, match="needs a guard with an `id`"):
        profiles.add_overlay(PACKAGE, {"name": "bad", "guard": {"text": "Hello"}, "key": "back"})
    with pytest.raises(ValueError, match="no never-tap entry"):
        profiles.remove_never_tap(PACKAGE, 3)
    assert profiles.remove_never_tap(PACKAGE, 0).never_tap == []
    assert profiles.remove_overlay(PACKAGE, "rate").overlays == []
    assert profiles.delete(PACKAGE) and not profiles.delete(PACKAGE)


def test_profile_files_are_checked_on_load_and_named_by_package(tmp_path):
    profiles = Profiles(tmp_path)
    for bad in ("../x", "nope", "com.x/../../y", ""):
        with pytest.raises(ValueError):
            profiles.load(bad)
    (tmp_path / f"{PACKAGE}.yaml").write_text("roles: {send: {colour: red}}\n")
    with pytest.raises(ValueError, match="is not valid"):
        profiles.load(PACKAGE)
    assert profiles.get("") is None


def test_screen_elements_offer_a_selector_or_say_why_not():
    rows = screen_elements(HOME)
    by_text = {r["text"]: r for r in rows}
    assert by_text["Search"]["selector"] == {"id": "search", "text": "Search"}
    assert by_text["Battery"]["selector"] == {"id": "title", "text": "Battery"}
    anonymous = screen_elements(screen(el("android.view.View", bounds=(0, 0, 50, 50), clickable=True)))
    assert anonymous[0]["selector"] is None and "nothing stable" in anonymous[0]["why_not"]

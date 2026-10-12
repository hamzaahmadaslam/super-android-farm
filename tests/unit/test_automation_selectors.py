"""The two matchers: scoring for trained steps, exact selectors for flows and profiles."""
from __future__ import annotations

import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(__file__))

from automation_fakes import button, el, field, screen  # noqa: E402

from superfarm.automation.selectors import (  # noqa: E402
    MissingVariable,
    Selector,
    find,
    find_scored,
    forbidden_at,
    match,
    render,
    score,
    selector_from_node,
    variables_in,
)

NODES = screen(
    button("btn_go", "Go", (50, 150, 150, 250)),
    el(text="Go", bounds=(250, 550, 350, 650)),
    button("", "Cancel", (350, 650, 450, 750), enabled=False),
    el("android.widget.LinearLayout", id="row", bounds=(0, 300, 480, 400), clickable=True, children=[
        el(id="title", text="Erase all data", bounds=(20, 320, 300, 360)),
        el(id="summary", text="Factory reset", bounds=(20, 360, 300, 390))]),
    el("android.widget.LinearLayout", id="row", bounds=(0, 400, 480, 500), clickable=True, children=[
        el(id="title", text="Battery", bounds=(20, 420, 300, 460))]),
    field("query", "typed", (0, 0, 480, 80), focused=True, desc="Search box"),
)
GO_BUTTON, GO_LABEL = NODES[1], NODES[2]


# ------------------------------------------------------------------ scoring
def test_weights_match_the_documented_table():
    node = GO_BUTTON
    assert score(node, {"id": "btn_go"}) == 5.0
    assert score(node, {"text": "Go"}) == 4.0
    assert score(NODES[-1], {"desc": "Search box"}) == 3.0
    assert score(node, {"path": node["path"]}) == 3.0
    assert score(node, {"cls": "Button"}) == 0.5
    assert score(node, {"id": "btn_go", "text": "Go", "cls": "Button", "path": node["path"]}) == 12.5


def test_partial_evidence_scores_less_and_a_wrong_id_counts_against():
    assert score(GO_BUTTON, {"text": "go"}) == 3.5                       # case differs
    assert score(GO_BUTTON, {"resource_id": "com.example.app:id/btn_go"}) == 5.0
    assert score(NODES[5], {"text": "erase"}) == 1.5                     # only part of the label
    assert score(GO_BUTTON, {"id": "other", "text": "Go"}) == 3.0        # 4 for the text, -1 for the id
    assert score(GO_BUTTON, {"path": "/X[0]/FrameLayout[0]/Button[0]"}) == 1.5
    assert score(GO_BUTTON, {}) == 0.0


def test_disabled_and_invisible_elements_never_score():
    cancel = next(n for n in NODES if n["text"] == "Cancel")
    assert score(cancel, {"text": "Cancel"}) == 0.0
    hidden = {**GO_BUTTON, "bounds": [0, 0, 0, 0]}
    assert score(hidden, {"id": "btn_go"}) == 0.0


def test_found_by_id():
    node, how = find_scored(NODES, [{"resource_id": "btn_go"}])
    assert node is GO_BUTTON and "btn_go" in how


def test_two_matches_resolve_to_the_one_nearest_the_recorded_spot():
    node, _ = find_scored(NODES, [{"text": "Go"}], near=[290, 590])
    assert node is GO_LABEL
    node, _ = find_scored(NODES, [{"text": "Go"}], near=[100, 200])
    assert node is GO_BUTTON


def test_missing_and_disabled_elements_are_not_found():
    assert find_scored(NODES, [{"resource_id": "nope"}])[0] is None
    assert find_scored(NODES, [{"resource_id": "nope"}, {"text": "Cancel"}])[0] is None


def test_alternatives_are_tried_in_order():
    node, how = find_scored(NODES, [{"resource_id": "gone"}, {"resource_id": "btn_go"}])
    assert node is GO_BUTTON and "btn_go" in how
    # with no agreed element at all, every candidate any recording saw is tried
    node, _ = find_scored(NODES, [{}, {"resource_id": "gone"}, {"text": "Cancel"}, {"resource_id": "btn_go"}],
                          near=[100, 200])
    assert node is GO_BUTTON


def test_the_threshold_decides_what_is_good_enough():
    weak = [{"cls": "Button"}]                                       # 0.5, plus up to 1 for position
    assert find_scored(NODES, weak, near=[100, 200])[0] is None
    assert "below 3.0" in find_scored(NODES, weak, near=[100, 200])[1]
    assert find_scored(NODES, weak, near=[100, 200], threshold=1.0)[0] is GO_BUTTON
    assert find_scored(NODES, [{"text": "go"}], threshold=3.6)[0] is None


# ---------------------------------------------------------------- templating
def test_render_fills_variables_and_leaves_everything_else():
    lookup = {"name": "Ada", "n": "3"}.get
    assert render("Hello {name}", lookup) == "Hello Ada"
    assert render("{{literal}} {name}", lookup) == "{literal} Ada"
    assert render(r"\d{3} {n}", lookup) == r"\d{3} 3"              # a quantifier is not a variable
    assert render("no braces", None) == "no braces"
    assert variables_in("{a} and {pool.codes} and {{b}}") == ["a", "pool.codes"]


def test_a_missing_variable_says_where_to_define_it():
    with pytest.raises(MissingVariable) as error:
        render("Hi {who}", {}.get)
    assert error.value.name == "who" and "variables" in str(error.value)


def test_selectors_render_their_strings_and_escape_regex_values():
    lookup = {"target": "a.b", "id": "result"}.get
    selector = Selector(id="{id}", text_regex="ID:{target}\\d*", within={"text": "{target}"})
    ready = selector.rendered(lookup)
    assert ready.id == "result" and ready.text_regex == "ID:a\\.b\\d*" and ready.within.text == "a.b"
    assert selector.id == "{id}"                                    # the original is untouched
    plain = Selector(id="x")
    assert plain.rendered(lookup) is plain


# ----------------------------------------------------------- exact selectors
def test_exact_fields():
    assert [n["text"] for n in match(NODES, Selector(text="Go"))] == ["Go", "Go"]
    assert match(NODES, Selector(text="go")) == []                   # exact, never case-folded
    assert match(NODES, Selector(text="Erase")) == []                # never a substring
    assert find(NODES, Selector(id="btn_go")) is GO_BUTTON
    assert find(NODES, Selector(id="com.example.app:id/btn_go")) is GO_BUTTON
    assert find(NODES, Selector(id="com.other:id/btn_go")) is None
    assert find(NODES, Selector(desc="Search box"))["id"] == "query"
    assert find(NODES, Selector(cls="EditText"))["id"] == "query"
    assert find(NODES, Selector(cls="android.widget.EditText"))["id"] == "query"
    assert find(NODES, Selector(text_regex="(?i)BATT.*"))["text"] == "Battery"
    assert find(NODES, Selector(text_regex="Batt")) is None          # the whole text must match
    assert find(NODES, Selector(package="com.example.app", id="summary"))["text"] == "Factory reset"
    assert find(NODES, Selector(package="com.other", id="summary")) is None


def test_flags_and_enabled():
    assert find(NODES, Selector(editable=True, focused=True))["id"] == "query"
    assert find(NODES, Selector(editable=True, focused=False)) is None
    assert [n["id"] for n in match(NODES, Selector(text="Go", clickable=True))] == ["btn_go"]
    assert match(NODES, Selector(text="Cancel")) == []               # disabled: not matched by default
    assert match(NODES, Selector(text="Cancel", enabled=False))[0]["text"] == "Cancel"


def test_index_and_within():
    assert find(NODES, Selector(id="title", index=1))["text"] == "Battery"
    assert find(NODES, Selector(id="title", index=-1))["text"] == "Battery"
    assert match(NODES, Selector(id="title", index=5)) == []
    inner = Selector(id="title", within={"id": "row", "index": 1})
    assert [n["text"] for n in match(NODES, inner)] == ["Battery"]
    assert match(NODES, Selector(id="title", within={"id": "nope"})) == []
    # a row is addressed by the label inside it
    assert find(NODES, Selector(id="summary", within={"id": "row"}))["text"] == "Factory reset"


def test_string_shorthand():
    assert Selector.model_validate("Battery").text == "Battery"
    assert Selector.model_validate("id:btn_go").id == "btn_go"
    assert Selector.model_validate("desc:Search box").desc == "Search box"
    assert Selector.model_validate("regex:Go+").text_regex == "Go+"
    assert Selector.model_validate("@send").role == "send"


@pytest.mark.parametrize("bad", [
    {}, {"index": 0}, {"id": ""}, {"text": "a", "text_regex": "b"}, {"text_regex": "(unclosed"},
    {"colour": "red"}, {"role": "send", "text": "x"}, {"role": "bad name"}, {"enabled": False},
])
def test_invalid_selectors_are_refused(bad):
    with pytest.raises(ValidationError):
        Selector.model_validate(bad)


def test_roles_are_resolved_from_the_app_profile():
    roles = {"send": Selector(id="btn_go"), "row": Selector(id="row")}
    assert find(NODES, Selector.model_validate("@send").with_roles(roles)) is GO_BUTTON
    nested = Selector(id="title", within="@row").with_roles(roles)
    assert [n["text"] for n in match(NODES, nested)] == ["Erase all data", "Battery"]
    indexed = Selector(role="row", index=1).with_roles(roles)
    assert indexed.id == "row" and indexed.index == 1
    with pytest.raises(ValueError, match="has not been taught"):
        Selector.model_validate("@unknown").with_roles(roles)
    with pytest.raises(ValueError, match="not resolved"):
        match(NODES, Selector.model_validate("@send"))


def test_selector_from_node_prefers_what_is_stable():
    assert selector_from_node(GO_BUTTON) == {"id": "btn_go", "text": "Go"}
    assert selector_from_node(GO_LABEL) == {"text": "Go"}
    assert selector_from_node(NODES[-1]) == {"id": "query", "editable": True}     # never a field's content
    with pytest.raises(ValueError, match="nothing stable"):
        selector_from_node(NODES[0])


# ------------------------------------------------------------------ never-tap
def test_never_tap_covers_the_label_and_the_row_around_it():
    never = [Selector(text="Erase all data")]
    assert forbidden_at(NODES, 100, 340, never)["text"] == "Erase all data"      # on the label
    assert forbidden_at(NODES, 450, 395, never)["text"] == "Erase all data"      # elsewhere on its row
    assert forbidden_at(NODES, 100, 440, never) is None                           # the next row is fine
    assert forbidden_at(NODES, 100, 200, never) is None
    assert forbidden_at(NODES, 100, 340, []) is None


def test_never_tap_by_id_and_regex():
    assert forbidden_at(NODES, 100, 200, [Selector(id="btn_go")])["id"] == "btn_go"
    assert forbidden_at(NODES, 100, 375, [Selector(text_regex="(?i).*reset.*")])["text"] == "Factory reset"

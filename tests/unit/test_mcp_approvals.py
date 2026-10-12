"""The approval queue (superfarm.ai.approvals) and the policy decision (superfarm.ai.policy)."""
import dataclasses
import json
import stat
import threading
import time

import pytest

from superfarm.ai.approvals import APPROVED, DENIED, EXPIRED, PENDING, Approvals, Conflict
from superfarm.ai.policy import ALLOW, ASK, DENY, Policy, decide
from superfarm.ai.tools import all_tools
from superfarm.config import Config


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(tmp_path, clock=None, **kw) -> Approvals:
    return Approvals(tmp_path / "logs" / "approvals.jsonl", clock=clock or time.time, **kw)


def ask(store: Approvals, tool="start_androids", targets=(1, 2), session="s1", klass="change", **kw):
    return store.create(tool=tool, klass=klass, summary=f"{tool} on {list(targets)}", arguments={"selector": "1-2"},
                        targets=targets, caller="mcp:test", session=session, can_remember=klass == "change", **kw)


# ----------------------------------------------------------------------------- lifecycle

def test_a_request_starts_pending_and_carries_what_the_operator_needs(tmp_path):
    store = make(tmp_path, timeout_seconds=300)
    item = ask(store)
    assert item.state == PENDING and len(item.id) == 8
    assert item.expires == pytest.approx(item.created + 300)
    assert item.to_dict() == {"id": item.id, "created": item.created, "expires": item.expires, "caller": "mcp:test",
                              "session": "s1", "tool": "start_androids", "klass": "change",
                              "summary": "start_androids on [1, 2]", "arguments": {"selector": "1-2"},
                              "targets": [1, 2], "can_remember": True, "state": "pending", "decided_by": "",
                              "decided_at": 0.0, "reason": "", "remembered": False}
    assert [a.id for a in store.list("pending")] == [item.id] and store.get(item.id) is item


def test_approve_records_who_and_when(tmp_path):
    clock = Clock()
    store = make(tmp_path, clock)
    item = ask(store)
    clock.now += 7
    done = store.approve(item.id, by="panel (127.0.0.1)")
    assert (done.state, done.decided_by, done.decided_at) == (APPROVED, "panel (127.0.0.1)", clock.now)
    assert store.list("pending") == [] and [a.id for a in store.list("approved")] == [item.id]


def test_deny_keeps_the_operators_reason(tmp_path):
    store = make(tmp_path)
    done = store.deny(ask(store).id, by="terminal", reason="not during business hours")
    assert (done.state, done.reason, done.decided_by) == (DENIED, "not during business hours", "terminal")


def test_a_request_nobody_answers_expires(tmp_path):
    clock = Clock()
    store = make(tmp_path, clock, timeout_seconds=30)
    item = ask(store)
    clock.now += 29
    assert store.get(item.id).state == PENDING
    clock.now += 2
    expired = store.get(item.id)
    assert (expired.state, expired.decided_by) == (EXPIRED, "timeout")
    with pytest.raises(Conflict, match="expired"):
        store.approve(item.id, by="panel")          # too late: the assistant was already told nobody answered


def test_saying_the_same_twice_is_harmless_and_the_opposite_is_refused(tmp_path):
    store = make(tmp_path)
    yes, no = ask(store), ask(store)
    first = store.approve(yes.id, by="panel")
    again = store.approve(yes.id, by="terminal")
    assert again is first and again.decided_by == "panel"            # the first answer stands, unchanged
    with pytest.raises(Conflict, match="already approved by panel"):
        store.deny(yes.id, by="terminal")
    store.deny(no.id, by="panel", reason="no")
    assert store.deny(no.id, by="terminal", reason="other").reason == "no"
    with pytest.raises(Conflict, match="already denied"):
        store.approve(no.id, by="terminal")


def test_two_decisions_racing_leave_exactly_one_winner(tmp_path):
    store = make(tmp_path)
    for _ in range(25):
        item = ask(store)
        outcomes: list[str] = []
        start = threading.Barrier(2)

        def decide_(action, item=item, outcomes=outcomes, start=start):
            start.wait()
            try:
                getattr(store, action)(item.id, by=action)
                outcomes.append(action)
            except Conflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=decide_, args=(a,)) for a in ("approve", "deny")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(outcomes).count("conflict") == 1, outcomes
        winner = next(o for o in outcomes if o != "conflict")
        assert store.get(item.id).state == {"approve": APPROVED, "deny": DENIED}[winner]
        assert store.get(item.id).decided_by == winner


def test_unknown_and_malformed_ids(tmp_path):
    store = make(tmp_path)
    with pytest.raises(KeyError):
        store.get("0123abcd")
    for bad in ("", "../../etc", "ZZZZZZZZ", "1234567", None):
        with pytest.raises(ValueError):
            store.get(bad)
    with pytest.raises(ValueError):
        store.list("nonsense")
    with pytest.raises(ValueError):
        ask(store, session="has spaces")


def test_cancel_withdraws_a_pending_request_and_leaves_a_decided_one(tmp_path):
    store = make(tmp_path)
    waiting, done = ask(store), ask(store)
    store.approve(done.id, by="panel")
    gone = store.cancel(waiting.id)
    assert (gone.state, gone.decided_by) == (EXPIRED, "assistant") and "stopped waiting" in gone.reason
    assert store.cancel(done.id).state == APPROVED
    with pytest.raises(Conflict):
        store.approve(waiting.id, by="panel")


# ---------------------------------------------------------------------------------- wait

def test_wait_returns_as_soon_as_the_operator_answers(tmp_path):
    store = make(tmp_path)
    item = ask(store)
    threading.Timer(0.15, lambda: store.approve(item.id, by="panel")).start()
    began = time.monotonic()
    assert store.wait(item.id, 10).state == APPROVED
    assert time.monotonic() - began < 5


def test_wait_gives_up_after_its_timeout_with_the_request_still_pending(tmp_path):
    store = make(tmp_path)
    item = ask(store)
    began = time.monotonic()
    assert store.wait(item.id, 0.2).state == PENDING
    assert 0.15 < time.monotonic() - began < 3


def test_wait_ends_when_the_request_expires(tmp_path):
    store = make(tmp_path, timeout_seconds=0.2)
    assert store.wait(ask(store).id, 10).state == EXPIRED


# ------------------------------------------------------------------------------- restart

def test_history_survives_a_restart_and_nothing_pending_is_ever_approved_by_it(tmp_path):
    store = make(tmp_path)
    approved, denied, pending = ask(store), ask(store), ask(store)
    store.approve(approved.id, by="panel")
    store.deny(denied.id, by="panel", reason="no")

    again = make(tmp_path)
    assert again.get(approved.id).state == APPROVED and again.get(denied.id).reason == "no"
    lost = again.get(pending.id)
    assert (lost.state, lost.decided_by) == (EXPIRED, "daemon restart") and "restarted" in lost.reason
    with pytest.raises(Conflict):
        again.approve(pending.id, by="panel")
    assert [a.id for a in again.list()] == [pending.id, denied.id, approved.id]         # newest first, as created
    # ... and the expiry is itself on disk: a third start does not see it pending again.
    assert make(tmp_path).get(pending.id).state == EXPIRED


def test_the_file_is_owner_only_json_lines_and_a_torn_line_is_skipped(tmp_path):
    store = make(tmp_path)
    item = ask(store)
    store.approve(item.id, by="panel")
    path = tmp_path / "logs" / "approvals.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["state"] for line in lines] == ["pending", "approved"] and lines[0]["id"] == item.id
    with open(path, "a") as out:
        out.write('{"id": "deadbeef", "state": "appro')                 # the daemon died mid-write
    again = make(tmp_path)
    assert [a.id for a in again.list()] == [item.id]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_history_is_bounded_in_memory_and_on_disk(tmp_path):
    store = make(tmp_path, keep=10)
    ids = []
    for _ in range(80):
        item = ask(store)
        store.deny(item.id, by="panel")
        ids.append(item.id)
    kept = [a.id for a in store.list(limit=500)]
    assert len(kept) <= 60 and kept[0] == ids[-1]
    path = tmp_path / "logs" / "approvals.jsonl"
    assert len(path.read_text().splitlines()) <= 4 * 10 + 60
    again = make(tmp_path, keep=10)
    assert len(again.list(limit=500)) == 10 and again.list()[0].id == ids[-1]
    assert len(path.read_text().splitlines()) == 10


def test_a_pending_request_is_never_dropped_to_make_room(tmp_path):
    store = make(tmp_path, keep=10)
    waiting = ask(store)
    for _ in range(100):
        store.deny(ask(store).id, by="panel")
    assert store.get(waiting.id).state == PENDING


# ------------------------------------------------------------------------------ remember

def test_remember_covers_the_same_tool_on_the_same_androids_or_fewer_in_that_session(tmp_path):
    store = make(tmp_path)
    item = ask(store, tool="tap", targets=(1, 2, 3), session="s1")
    assert not store.remembered("s1", "tap", [1, 2, 3])
    done = store.approve(item.id, by="panel", remember=True)
    assert done.remembered
    assert store.remembered("s1", "tap", [1, 2, 3]) and store.remembered("s1", "tap", [2])
    assert not store.remembered("s1", "tap", [1, 4])                 # another Android: ask again
    assert not store.remembered("s1", "tap", [])                     # no Androids resolved: not covered
    assert not store.remembered("s1", "type_text", [1])              # another tool
    assert not store.remembered("s2", "tap", [1])                    # another session


def test_remember_is_refused_for_destructive_requests(tmp_path):
    store = make(tmp_path)
    item = ask(store, tool="delete_androids", klass="destructive", targets=(1,))
    assert item.can_remember is False
    done = store.approve(item.id, by="panel", remember=True)
    assert done.state == APPROVED and done.remembered is False
    assert not store.remembered("s1", "delete_androids", [1])


def test_remember_for_a_tool_without_androids_and_plain_approve_remembers_nothing(tmp_path):
    store = make(tmp_path)
    store.approve(ask(store, tool="cancel_task", targets=()).id, by="panel", remember=True)
    assert store.remembered("s1", "cancel_task", [])
    store.approve(ask(store, tool="tap", targets=(1,)).id, by="panel")
    assert not store.remembered("s1", "tap", [1])


def test_what_was_remembered_ends_with_the_session_a_restart_or_the_time_limit(tmp_path):
    clock = Clock()
    store = make(tmp_path, clock)
    store.approve(ask(store, tool="tap", targets=(1,), session="s1").id, by="panel", remember=True)
    store.approve(ask(store, tool="tap", targets=(1,), session="s2").id, by="panel", remember=True)
    still_open = ask(store, tool="swipe", targets=(1,), session="s1")

    assert store.end_session("s1") == 1
    assert not store.remembered("s1", "tap", [1]) and store.remembered("s2", "tap", [1])
    assert store.get(still_open.id).state == EXPIRED and "session ended" in store.get(still_open.id).reason

    clock.now += 9 * 3600
    assert not store.remembered("s2", "tap", [1])                    # hours later the grant has lapsed

    store.approve(ask(store, tool="tap", targets=(1,), session="s3").id, by="panel", remember=True)
    assert not make(tmp_path, clock).remembered("s3", "tap", [1])    # memory only: gone with the daemon


def test_reload_config_changes_the_timeout_of_new_requests_only(tmp_path):
    clock = Clock()
    store = make(tmp_path, clock, timeout_seconds=300)
    old = ask(store)
    store.reload_config(Config.model_validate({"ai": {"approval_timeout_seconds": 20}}))
    new = ask(store)
    assert old.expires - old.created == 300 and new.expires - new.created == 20


# -------------------------------------------------------------------------------- policy

READ_TOOL, CHANGE_TOOL, DESTRUCTIVE_TOOL = "list_androids", "start_androids", "delete_androids"
KLASS = {t.name: t.klass for t in all_tools()}


def policy(**ai) -> Policy:
    return Policy.from_config(Config.model_validate({"ai": ai}))


@pytest.mark.parametrize(("mode", "tool", "expected"), [
    ("read-only", READ_TOOL, ALLOW), ("read-only", CHANGE_TOOL, DENY), ("read-only", DESTRUCTIVE_TOOL, DENY),
    ("ask", READ_TOOL, ALLOW), ("ask", CHANGE_TOOL, ASK), ("ask", DESTRUCTIVE_TOOL, ASK),
    ("allow", READ_TOOL, ALLOW), ("allow", CHANGE_TOOL, ALLOW), ("allow", DESTRUCTIVE_TOOL, ALLOW),
])
def test_policy_matrix(mode, tool, expected):
    assert decide(tool, KLASS[tool], policy(policy=mode, deny=[])).action == expected


@pytest.mark.parametrize("mode", ["read-only", "ask", "allow"])
@pytest.mark.parametrize("tool", [READ_TOOL, CHANGE_TOOL, DESTRUCTIVE_TOOL])
def test_deny_wins_under_every_policy_even_over_auto_approve(mode, tool):
    decision = decide(tool, KLASS[tool], policy(policy=mode, deny=[tool], auto_approve=[tool]))
    assert decision.action == DENY and "ai.deny" in decision.reason


@pytest.mark.parametrize(("mode", "tool", "expected"), [
    ("ask", CHANGE_TOOL, ALLOW), ("ask", DESTRUCTIVE_TOOL, ALLOW),          # named by the operator: runs
    ("read-only", CHANGE_TOOL, DENY), ("read-only", DESTRUCTIVE_TOOL, DENY),   # read-only stays read-only
    ("allow", CHANGE_TOOL, ALLOW),
])
def test_auto_approve(mode, tool, expected):
    assert decide(tool, KLASS[tool], policy(policy=mode, deny=[], auto_approve=[tool])).action == expected


def test_ask_for_can_exempt_changing_actions_but_never_destructive_ones():
    relaxed = policy(policy="ask", ask_for=["destructive"], deny=[])
    assert decide(CHANGE_TOOL, "change", relaxed).action == ALLOW
    assert decide(DESTRUCTIVE_TOOL, "destructive", relaxed).action == ASK
    with pytest.raises(ValueError, match="must include `destructive`"):
        Config.model_validate({"ai": {"ask_for": ["change"]}})
    with pytest.raises(ValueError):
        Config.model_validate({"ai": {"ask_for": []}})


def test_the_defaults_are_ask_with_the_shell_switched_off():
    default = Policy.from_config(Config())
    assert (default.mode, default.ask_for, default.deny) == ("ask", {"change", "destructive"}, {"run_shell"})
    assert default.approval_timeout_seconds == 300 and default.auto_approve == frozenset()
    assert decide("run_shell", "destructive", default).action == DENY
    assert dataclasses.replace(default, forbidden_words=()) == Policy()       # the fallback is the same policy
    assert Policy.from_info({}).deny == {"run_shell"} and Policy.from_info({"deny": []}).deny == frozenset()


def test_a_misspelt_tool_name_in_the_config_is_an_error_not_a_silent_gap():
    for key in ("deny", "auto_approve"):
        with pytest.raises(ValueError, match="unknown tool name"):
            Config.model_validate({"ai": {key: ["delete_android"]}})          # the tool is delete_androids
    assert Config.model_validate({"ai": {"deny": ["tap", "run_shell", "tap"]}}).ai.deny == ["run_shell", "tap"]


def test_policy_round_trips_through_the_info_route_shape():
    original = policy(policy="ask", ask_for=["destructive"], auto_approve=["tap"], deny=["run_shell", "swipe"],
                      approval_timeout_seconds=42, max_screenshot_px=512)
    assert Policy.from_info(original.to_dict()) == original


def test_an_unknown_policy_word_denies_changes():
    assert decide(CHANGE_TOOL, "change", Policy(mode="whatever", deny=frozenset())).action == DENY
    assert decide(READ_TOOL, "read", Policy(mode="whatever", deny=frozenset())).action == ALLOW

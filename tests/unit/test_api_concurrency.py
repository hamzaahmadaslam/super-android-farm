"""Duplicate and concurrent requests are harmless: people double-click, and two tabs or a
script and the panel can ask for the same thing at the same moment.

The rule (superfarm/core/tasks.py): one operator request per Android at a time. A second
request that names an Android an active task has not finished with is refused with
409 `busy`, and the answer names that task. It is neither queued nor joined to the first.

Requests here are sent truly in parallel (one thread each, released together) against the
real app, the real InstanceManager and TaskManager, and core_fakes' Docker and ADB.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from functools import partial

import pytest
import test_control_support as S
from api_fakes import BASE, Api, api
from starlette.testclient import TestClient

from superfarm.config import load_config
from superfarm.core import backup as backup_mod
from superfarm.core.tasks import Task, TaskBusy, TaskManager

ROUNDS = 15            # a race that shows once in a few tries shows in these


@pytest.fixture
def a(tmp_path):
    with api(tmp_path) as made:
        made.core.mgr.lock_wait = 5.0          # the daemon's own is 30 s; core_fakes' 0.2 s would hide waiting
        yield made


def client(a: Api) -> TestClient:
    """A client of its own for each thread."""
    return TestClient(a.app, base_url=BASE, raise_server_exceptions=False, headers={"Authorization": f"Bearer {a.token}"})


def req(a: Api, verb: str, *args, **kw):
    """A request ready to be sent, on a client of its own."""
    return partial(getattr(client(a), verb), *args, **kw)


def together(*calls):
    """Run the calls at the same moment, one thread each. Returns their results in order."""
    barrier = threading.Barrier(len(calls))
    out: list = [None] * len(calls)

    def run(n: int, call) -> None:
        barrier.wait(10)
        out[n] = call()

    threads = [threading.Thread(target=run, args=(n, call)) for n, call in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), "a request did not come back"
    return out


def settle(a: Api, *responses) -> list[dict]:
    """Wait for the tasks of every 202 among the responses."""
    return [a.wait(r) for r in responses if r.status_code == 202]


def refused_by(response, task_id: str, ids: list[int]) -> None:
    """A 409 `busy` that names the task holding the Androids."""
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "busy" and error["detail"] == {"task": task_id, "ids": ids}
    assert f"task {task_id}" in error["message"]


def held_start(a: Api):
    """Make every start wait inside its boot sequence until the returned event is set."""
    gate, entered = threading.Event(), threading.Event()

    def net_wait(docker, r, timeout=90):
        entered.set()
        assert gate.wait(10), "the test never released the start"
        return {"ready": True, "state": "connected", "detail": ""}

    a.core.mgr.backends.net_wait_ready = net_wait
    return gate, entered


def held(a: Api) -> threading.Event:
    """Make every operation on an Android pause right after it has taken the Android's lock,
    while the returned event is clear: the fakes finish in microseconds, real work does not,
    and the second of two requests has to find the first one's work still going on."""
    gate = threading.Event()
    real = a.core.mgr.locked

    @contextmanager
    def locked(id, what, *, wait=None):
        with real(id, what, wait=wait):
            assert gate.wait(10), "the test never released the work"
            yield

    a.core.mgr.locked = locked
    gate.set()
    return gate


def up(a: Api, n: int) -> bool:
    return a.core.docker.running(f"sfx-android-{n}") and a.core.docker.running(f"sfx-net-{n}")


def creations(a: Api) -> list[str]:
    return a.core.docker.names_called("create")


# ------------------------------------------------------------------------------ starts

def test_two_starts_of_the_same_android_boot_it_once(a):
    gate = held(a)
    for n in range(1, ROUNDS + 1):
        a.client.post("/api/instances", json={"count": 1})
        gate.clear()
        first, second = together(*(req(a, "post", f"/api/instances/{n}/start") for _ in range(2)))
        gate.set()
        won, lost = (first, second) if first.status_code == 202 else (second, first)
        assert won.status_code == 202, (first.text, second.text)
        refused_by(lost, won.json()["task"]["id"], [n])
        (task,) = settle(a, first, second)
        assert task["results"] == {str(n): {"ok": True, "detail": task["results"][str(n)]["detail"]}}
        assert up(a, n) and a.client.get(f"/api/instances/{n}").json()["state"] == "booted"
    # One boot sequence each: one network container and one Android container, never a second try.
    assert sorted(creations(a)) == sorted(f"sfx-{kind}-{n}" for n in range(1, ROUNDS + 1) for kind in ("net", "android"))
    assert len(a.farm.tasks.list(limit=500)) == ROUNDS


def test_a_second_start_while_the_first_is_booting_is_refused_at_once(a):
    gate, entered = held_start(a)
    a.client.post("/api/instances", json={"count": 1})
    first = a.client.post("/api/instances/1/start")
    assert entered.wait(5)
    began = time.monotonic()
    for action in ("start", "restart", "recreate", "stop"):
        refused_by(a.client.post(f"/api/instances/1/{action}"), first.json()["task"]["id"], [1])
    refused_by(a.client.post("/api/instances/bulk", json={"action": "start", "selector": "all"}), first.json()["task"]["id"], [1])
    assert time.monotonic() - began < 2                 # an answer, not a wait for the Android's lock
    gate.set()
    assert a.wait(first)["results"]["1"]["ok"]
    assert creations(a) == ["sfx-net-1", "sfx-android-1"]
    # Once the task is through with it the Android takes requests again.
    assert a.wait(a.client.post("/api/instances/1/stop"))["results"]["1"]["ok"]


def test_start_and_stop_at_the_same_moment_one_wins_and_the_state_matches(a):
    gate = held(a)
    for n in range(1, 2 * ROUNDS + 1):
        a.client.post("/api/instances", json={"count": 1})
        if n % 2:                                           # half of them are running when the two requests arrive
            a.wait(a.client.post(f"/api/instances/{n}/start"))
        gate.clear()
        start, stop = together(req(a, "post", f"/api/instances/{n}/start"),
                               req(a, "post", f"/api/instances/{n}/stop"))
        gate.set()
        assert sorted((start.status_code, stop.status_code)) == [202, 409], (start.text, stop.text)
        won, lost = (start, stop) if start.status_code == 202 else (stop, start)
        refused_by(lost, won.json()["task"]["id"], [n])
        (task,) = settle(a, start, stop)
        assert task["results"][str(n)]["ok"]
        assert up(a, n) is (won is start)                   # the Android is where the request that won put it
    assert len(a.farm.tasks.list(limit=500)) == 3 * ROUNDS  # one task per pair (plus the starts above), never two


# ----------------------------------------------------------------------------- deletes

def gone(a: Api, n: int) -> None:
    """Nothing of Android n is left: no settings, no state, no containers, no volume."""
    assert not a.core.store.exists(n)
    assert not (a.farm.paths.instance(n) / "state.json").exists()
    assert not any(name.endswith(f"-{n}") for name in a.core.docker.containers)
    assert f"sfx-data-{n}" not in a.core.docker.volume_labels


def test_delete_during_a_start_is_refused_and_the_start_completes(a):
    gate, entered = held_start(a)
    a.client.post("/api/instances", json={"count": 1})
    start = a.client.post("/api/instances/1/start")
    assert entered.wait(5)
    began = time.monotonic()
    refused_by(a.client.delete("/api/instances/1"), start.json()["task"]["id"], [1])
    refused_by(a.client.post("/api/instances/bulk", json={"action": "delete", "selector": "1"}), start.json()["task"]["id"], [1])
    assert time.monotonic() - began < 2
    gate.set()
    assert a.wait(start)["results"]["1"]["ok"] and up(a, 1)
    assert a.client.delete("/api/instances/1").status_code == 200       # now it may go
    gone(a, 1)


def test_delete_and_start_at_the_same_moment_leave_a_whole_android_or_none(a):
    for _ in range(3 * ROUNDS):
        a.client.post("/api/instances", json={"count": 1})
        start, delete = together(req(a, "post", "/api/instances/1/start"),
                                 req(a, "delete", "/api/instances/1"))
        assert (start.status_code, delete.status_code) in ((202, 200), (202, 409), (404, 200)), (start.text, delete.text)
        for task in settle(a, start):
            result = task["results"].get("1")              # none at all: it was gone before the task looked for it
            if delete.status_code == 409:                  # the start was first: the Android is up and untouched
                refused_by(delete, task["id"], [1])
                assert result["ok"] and up(a, 1)
                assert a.client.delete("/api/instances/1").status_code == 200
            elif result and not result["ok"]:              # the delete was first: the start finds nothing to start
                assert "there is no Android 1" in result["detail"]
        gone(a, 1)                                         # in every order: no half an Android left behind
    assert a.client.get("/api/instances").json()["instances"] == []


def test_two_deletes_at_the_same_moment_delete_once(a):
    for _ in range(ROUNDS):
        a.client.post("/api/instances", json={"count": 1})
        a.wait(a.client.post("/api/instances/1/start"))
        before = a.core.docker.names_called("volume_rm").count("sfx-data-1")
        first, second = together(*(req(a, "delete", "/api/instances/1") for _ in range(2)))
        assert sorted((first.status_code, second.status_code)) == [200, 404], (first.text, second.text)
        lost = first if first.status_code == 404 else second
        assert lost.json()["error"] == {"code": "not_found", "message": "there is no Android 1"}
        assert a.core.docker.names_called("volume_rm").count("sfx-data-1") == before + 1
        gone(a, 1)


def test_a_delete_that_waited_for_another_delete_does_not_act_on_what_it_read_before(a):
    # The second delete has looked the Android up, then waits for the first to let go of it.
    a.client.post("/api/instances", json={"count": 1})
    a.wait(a.client.post("/api/instances/1/start"))
    inside, release = threading.Event(), threading.Event()
    real = a.core.docker.volume_rm

    def slow_rm(name):
        inside.set()
        assert release.wait(10)
        return real(name)

    a.core.docker.volume_rm = slow_rm
    first: list = []
    t = threading.Thread(target=lambda: first.append(a.client.delete("/api/instances/1")))
    t.start()
    assert inside.wait(5)
    second: list = []
    u = threading.Thread(target=lambda: second.append(client(a).delete("/api/instances/1")))
    u.start()
    time.sleep(0.3)                                        # the second is past its lookup and waiting for the lock
    release.set()
    t.join(10)
    u.join(10)
    assert first[0].status_code == 200 and first[0].json()["data"] == "removed"
    assert second[0].status_code == 404, second[0].text    # not a second "deleted" for an Android that is gone
    gone(a, 1)


def test_a_start_that_waited_for_a_delete_leaves_no_state_behind(a):
    # Core level: no task rule in the way, only the Android's lock.
    mgr = a.core.mgr
    mgr.create()
    mgr.start(1)
    inside, release = threading.Event(), threading.Event()
    real = a.core.docker.volume_rm

    def slow_rm(name):
        inside.set()
        assert release.wait(10)
        return real(name)

    a.core.docker.volume_rm = slow_rm
    deleting = threading.Thread(target=lambda: mgr.delete(1))
    deleting.start()
    assert inside.wait(5)
    failed: list = []

    def start() -> None:
        try:
            mgr.start(1)
        except KeyError as e:
            failed.append(e.args[0])

    starting = threading.Thread(target=start)
    starting.start()
    time.sleep(0.3)
    release.set()
    deleting.join(10)
    starting.join(10)
    assert failed == ["there is no Android 1"]
    gone(a, 1)
    # The id is free again, and whoever gets it starts clean: no error of an Android that is gone.
    assert a.client.post("/api/instances", json={"count": 1}).json()["instances"][0]["id"] == 1
    assert a.client.get("/api/instances/1").json()["last_error"] == ""


# -------------------------------------------------------------------------------- bulk

def test_two_bulk_actions_on_overlapping_selectors_the_second_is_refused_whole(a):
    gate, entered = held_start(a)
    a.client.post("/api/instances", json={"count": 4})
    first, second = together(
        req(a, "post", "/api/instances/bulk", json={"action": "start", "selector": "1-3"}),
        req(a, "post", "/api/instances/bulk", json={"action": "start", "selector": "2-4"}))
    assert sorted((first.status_code, second.status_code)) == [202, 409], (first.text, second.text)
    won, lost = (first, second) if first.status_code == 202 else (second, first)
    mine = won.json()["task"]["targets"]
    refused_by(lost, won.json()["task"]["id"], [2, 3])              # the Androids both name, and who has them
    assert entered.wait(5)
    # Refused as a whole: the Android only the loser named was not started on the side.
    other = ({1, 2, 3, 4} - set(mine)).pop()
    gate.set()
    assert all(r["ok"] for r in a.wait(won)["results"].values())
    assert {i for i in (1, 2, 3, 4) if up(a, i)} == set(mine) and not up(a, other)
    assert sorted(creations(a)) == sorted(f"sfx-{kind}-{i}" for i in mine for kind in ("net", "android"))
    # The same request again, now that nothing holds those Androids, is taken.
    assert all(r["ok"] for r in a.wait(client(a).post("/api/instances/bulk", json=json.loads(lost.request.content)))["results"].values())


def test_the_same_bulk_request_twice_runs_once(a):
    a.client.post("/api/instances", json={"count": 3})
    gate = held(a)
    for action in ("start", "restart", "stop", "delete"):
        body = {"action": action, "selector": "all", "force": True}
        gate.clear()
        first, second = together(*(req(a, "post", "/api/instances/bulk", json=body) for _ in range(2)))
        gate.set()
        assert sorted((first.status_code, second.status_code)) == [202, 409], (action, first.text, second.text)
        won, lost = (first, second) if first.status_code == 202 else (second, first)
        refused_by(lost, won.json()["task"]["id"], [1, 2, 3])
        assert all(r["ok"] for r in a.wait(won)["results"].values()), action
    assert a.client.get("/api/instances").json()["instances"] == []
    assert sorted(creations(a)) == sorted(f"sfx-{kind}-{i}" for i in (1, 2, 3) for kind in ("net", "android"))


def test_bulk_actions_on_different_androids_run_side_by_side(a):
    gate, entered = held_start(a)
    a.client.post("/api/instances", json={"count": 4})
    first, second = together(
        req(a, "post", "/api/instances/bulk", json={"action": "start", "selector": "1,2"}),
        req(a, "post", "/api/instances/bulk", json={"action": "start", "selector": "3,4"}))
    assert (first.status_code, second.status_code) == (202, 202)
    gate.set()
    assert all(r["ok"] for task in settle(a, first, second) for r in task["results"].values())


def test_an_android_a_running_task_has_finished_with_is_free_again(a):
    a.client.post("/api/instances", json={"count": 2})
    gate, entered = threading.Event(), threading.Event()

    def net_wait(docker, r, timeout=90):
        if r.names.id == 2:                                 # Android 2 hangs in its boot, Android 1 does not
            entered.set()
            assert gate.wait(10)
        return {"ready": True, "state": "connected", "detail": ""}

    a.core.mgr.backends.net_wait_ready = net_wait
    task = a.client.post("/api/instances/bulk", json={"action": "start", "selector": "all"})
    assert entered.wait(5)
    deadline = time.monotonic() + 5
    while "1" not in a.client.get(f"/api/tasks/{task.json()['task']['id']}").json()["task"]["results"]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert set(a.farm.tasks.busy_targets()) == {2}
    assert a.wait(a.client.post("/api/instances/1/stop"))["results"]["1"]["ok"]       # 1 is done: free
    refused_by(a.client.post("/api/instances/2/stop"), task.json()["task"]["id"], [2])    # 2 is not
    gate.set()
    a.wait(task)
    assert a.farm.tasks.busy_targets() == {}


def test_the_rule_is_one_atomic_step_in_the_task_manager():
    tasks = TaskManager(workers=8)
    gate = threading.Event()
    look = tasks._busy

    def slow_look():
        found = look()
        time.sleep(0.02)                # between "nobody has them" and queueing: where a second request would slip in
        return found

    tasks._busy = slow_look
    try:
        outcomes = together(*(lambda: _submit(tasks, gate) for _ in range(8)))
        won = [o for o in outcomes if not isinstance(o, TaskBusy)]
        assert len(won) == 1 and all(o.task is won[0] and o.ids == [1, 2] for o in outcomes if o is not won[0])
        assert tasks.busy_targets() == {1: won[0], 2: won[0]}
        # Work that is not exclusive neither holds Androids nor is held up (a scheduled backup, a shell command).
        aside = tasks.submit("shell", "uptime", lambda t: None, targets=[1, 2])
        tasks.wait(aside, 5)
        assert aside.state == "done"
    finally:
        gate.set()
        tasks.shutdown()


def test_a_cancel_that_was_accepted_is_never_followed_by_done(monkeypatch):
    # Somebody cancels at the very moment the work has returned and its outcome is being decided.
    tasks = TaskManager(workers=2)
    accepted: list[bool] = []
    real = Task.cancelled

    def cancelled(self) -> bool:
        seen = real.fget(self)
        if threading.current_thread().name.startswith("sf-task") and not accepted and not seen:
            late = threading.Thread(target=lambda: accepted.append(tasks.cancel(self.id)))
            late.start()
            late.join(0.3)              # it cannot get in before the outcome is set, and is told so afterwards
        return seen

    monkeypatch.setattr(Task, "cancelled", property(cancelled))
    try:
        task = tasks.wait(tasks.submit("test", "ends now", lambda t: None), 5)
        deadline = time.monotonic() + 5
        while not accepted and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (task.state, accepted) == ("done", [False])
    finally:
        tasks.shutdown()


def _submit(tasks: TaskManager, gate: threading.Event):
    try:
        return tasks.submit("instance.start", "start 2 Android(s)", lambda t: gate.wait(10), targets=[1, 2], exclusive=True)
    except TaskBusy as e:
        return e


# ----------------------------------------------------------------------------- backups

def make_backup(a: Api, name: str = "android-1/20250101-040000") -> str:
    folder = a.farm.backups.directory / name
    folder.mkdir(parents=True)
    data = b"tar-bytes"
    (folder / "data.tar.gz").write_bytes(data)
    manifest = {"kind": backup_mod.MANIFEST_KIND, "format": backup_mod.MANIFEST_FORMAT, "id": 1, "name": "Android 1",
                "created": time.time(), "reason": "manual", "data": "data.tar.gz", "android_version": "13",
                "files": {"data.tar.gz": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}
    (folder / backup_mod.MANIFEST).write_text(json.dumps(manifest))
    return name


def slow_restore(a: Api, monkeypatch, *, safety: str = ""):
    """Stand in for the copy itself (a helper container): it takes a while and may leave a safety backup."""
    gate, calls = threading.Event(), []

    def restore(archive, id=None, task=None, *, force=False, keep_network=False):
        calls.append(id)
        assert gate.wait(10)
        if id is not None:
            a.core.store.state_set(id, "safety_backup", safety or None)
        return id if id is not None else 99

    monkeypatch.setattr(a.farm.backups, "restore", restore)
    return gate, calls


def test_two_restores_over_the_same_android_restore_once(a, monkeypatch):
    a.client.post("/api/instances", json={"count": 1})
    name = make_backup(a)
    safety = str(a.farm.backups.directory / "android-1" / "20250102-030405")
    gate, calls = slow_restore(a, monkeypatch, safety=safety)
    body = {"archive": name, "id": 1, "force": True}
    first, second = together(*(req(a, "post", "/api/backups/restore", json=body) for _ in range(2)))
    assert sorted((first.status_code, second.status_code)) == [202, 409], (first.text, second.text)
    won, lost = (first, second) if first.status_code == 202 else (second, first)
    refused_by(lost, won.json()["task"]["id"], [1])
    # While its data is being replaced the Android takes no other request either.
    refused_by(a.client.post("/api/instances/1/start"), won.json()["task"]["id"], [1])
    refused_by(a.client.delete("/api/instances/1"), won.json()["task"]["id"], [1])
    gate.set()
    task = a.wait(won)
    assert task["state"] == "done" and calls == [1]
    # The result says where the data that was replaced went.
    assert task["output"] == {"id": 1, "archive": name, "new": False, "safety_backup": safety}


def test_the_restore_result_names_only_a_safety_backup_this_restore_made(a, monkeypatch):
    a.client.post("/api/instances", json={"count": 1})
    name = make_backup(a)
    a.core.store.state_set(1, "safety_backup", "/somewhere/from-an-earlier-restore")
    gate, _ = slow_restore(a, monkeypatch, safety="/somewhere/from-an-earlier-restore")     # as when none was needed
    gate.set()
    task = a.wait(a.client.post("/api/backups/restore", json={"archive": name, "id": 1, "force": True}))
    assert task["output"]["safety_backup"] == ""
    fresh_copy = a.wait(a.client.post("/api/backups/restore", json={"archive": name}))
    assert fresh_copy["output"] == {"id": 99, "archive": name, "new": True, "safety_backup": ""}


def test_two_manual_backups_of_the_same_androids_run_once(a):
    a.client.post("/api/instances", json={"count": 2})
    gate, entered = threading.Event(), threading.Event()
    real = a.core.docker.volume_exists

    def slow_exists(name):
        entered.set()
        assert gate.wait(10)
        return real(name)

    a.core.docker.volume_exists = slow_exists
    first, second = together(*(req(a, "post", "/api/backups", json={"selector": "all"}) for _ in range(2)))
    assert sorted((first.status_code, second.status_code)) == [202, 409], (first.text, second.text)
    won, lost = (first, second) if first.status_code == 202 else (second, first)
    refused_by(lost, won.json()["task"]["id"], [1, 2])
    gate.set()
    assert a.wait(won)["state"] == "done"
    # A scheduled backup is housekeeping, not an operator's request: it is never refused this way.
    assert a.farm.backups.create_many("all", reason="scheduled").exclusive is False


# ------------------------------------------------------------------------------ config

def test_two_config_saves_at_the_same_moment_both_land(a):
    for n in range(ROUNDS):
        first, second = together(
            req(a, "patch", "/api/config", json={"patch": {"android": {"fps": 21 + n}}}),
            req(a, "patch", "/api/config", json={"patch": {"stream": {"jpeg_quality": 50 + n}}}))
        assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
        for config in (a.farm.config, load_config(a.farm.paths)):           # in memory and in the file
            assert (config.android.fps, config.stream.jpeg_quality) == (21 + n, 50 + n)


def test_each_of_two_config_saves_reports_its_own_change(a, monkeypatch):
    # Both requests are past reading "the config before" when either saves.
    both = threading.Barrier(2)
    real = a.farm.set_config

    def set_config(patch):
        try:
            both.wait(0.5)
        except threading.BrokenBarrierError:
            pass
        return real(patch)

    monkeypatch.setattr(a.farm, "set_config", set_config)
    first, second = together(
        req(a, "patch", "/api/config", json={"patch": {"android": {"fps": 30}}}),
        req(a, "patch", "/api/config", json={"patch": {"server": {"port": 7801}}}))
    assert first.json()["changed"] == ["android.fps"] and first.json()["restart_daemon"] == []
    assert second.json()["changed"] == ["server.port"] and second.json()["restart_android"] == []
    assert [line["keys"] for line in a.audit_lines()] in ([["android.fps"], ["server.port"]], [["server.port"], ["android.fps"]])


def test_the_same_config_save_twice_is_harmless(a):
    body = {"patch": {"android": {"fps": 30}}}
    first, second = together(*(req(a, "patch", "/api/config", json=body) for _ in range(2)))
    assert (first.status_code, second.status_code) == (200, 200)
    assert sorted((first.json()["changed"], second.json()["changed"])) == [[], ["android.fps"]]     # the repeat changed nothing
    assert a.farm.config.android.fps == 30


# ---------------------------------------------------------------------------- instances

def test_two_edits_of_one_android_at_the_same_moment_both_land(a):
    a.client.post("/api/instances", json={"count": 1})
    # Both requests have read the saved settings before either writes them back.
    both = threading.Barrier(2)
    real = a.core.mgr._resolve

    def resolve(spec):
        try:
            both.wait(0.5)
        except threading.BrokenBarrierError:
            pass
        return real(spec)

    a.core.mgr._resolve = resolve
    first, second = together(req(a, "patch", "/api/instances/1", json={"patch": {"fps": 25}}),
                             req(a, "patch", "/api/instances/1", json={"patch": {"name": "Left hand"}}))
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    saved = a.core.store.get(1)
    assert (saved.fps, saved.name) == (25, "Left hand")


def test_two_creates_at_the_same_moment_make_two_whole_sets(a):
    for n in range(5):
        first, second = together(*(req(a, "post", "/api/instances", json={"count": 4}) for _ in range(2)))
        assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
        ids = [s["id"] for r in (first, second) for s in r.json()["instances"]]
        assert sorted(ids) == list(range(8 * n + 1, 8 * n + 9))               # no id twice, none half made and dropped
    assert a.core.store.ids() == list(range(1, 41))


# ------------------------------------------------------------------- Idempotency-Key

KEY = {"Idempotency-Key": "8f2c1e6a-create-dialog-1"}


def test_a_create_repeated_with_its_key_returns_the_first_result(a):
    body = {"count": 2, "name": "Phone"}
    first = a.client.post("/api/instances", json=body, headers=KEY)
    again = a.client.post("/api/instances", json=body, headers=KEY)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and [s["id"] for s in first.json()["instances"]] == [1, 2]
    assert "idempotency-replayed" not in first.headers and again.headers["idempotency-replayed"] == "true"
    assert a.core.store.ids() == [1, 2]
    # Without a key every request is a new one (and so is one with another key).
    assert [s["id"] for s in a.client.post("/api/instances", json=body).json()["instances"]] == [3, 4]
    other = {"Idempotency-Key": "8f2c1e6a-create-dialog-2"}
    assert [s["id"] for s in a.client.post("/api/instances", json=body, headers=other).json()["instances"]] == [5, 6]
    replay, new = a.audit_lines()[1], a.audit_lines()[2]
    assert (replay["status"], replay["ids"], new["ids"]) == (201, [1, 2], [3, 4])


def test_the_same_create_sent_many_times_at_once_creates_once(a):
    for n in range(5):
        key = {"Idempotency-Key": f"double-click-{n}"}
        answers = together(*(req(a, "post", "/api/instances", json={"count": 3}, headers=key) for _ in range(6)))
        assert [r.status_code for r in answers] == [201] * 6
        assert all(r.json() == answers[0].json() for r in answers)
        assert sum("idempotency-replayed" in r.headers for r in answers) == 5
        assert a.core.store.ids() == list(range(1, 3 * n + 4))


def test_a_key_used_for_a_different_request_is_refused(a):
    assert a.client.post("/api/instances", json={"count": 1}, headers=KEY).status_code == 201
    r = a.client.post("/api/instances", json={"count": 2}, headers=KEY)
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_reused"
    assert a.core.store.ids() == [1]


@pytest.mark.parametrize("key", ["", "has space", "x" * 129, "café", "tab\there"])
def test_a_malformed_key_is_a_400(a, key):
    r = a.client.post("/api/instances", json={"count": 1}, headers={"Idempotency-Key": key.encode("latin-1")})
    assert r.status_code == 400 and "Idempotency-Key" in r.json()["error"]["message"]
    assert a.core.store.ids() == []


def test_a_create_that_failed_is_not_remembered(a):
    bad = a.client.post("/api/instances", json={"count": 1, "template": {"android_version": "99"}}, headers=KEY)
    assert bad.status_code == 400
    good = a.client.post("/api/instances", json={"count": 1}, headers=KEY)       # the corrected form, same dialog
    assert good.status_code == 201 and a.core.store.ids() == [1]


def test_keys_are_forgotten_after_a_while_and_their_number_is_bounded(a):
    remembered = a.app.state.idempotency
    assert (remembered.ttl, remembered.limit) == (600.0, 256)
    now = [1000.0]
    remembered.clock = lambda: now[0]
    a.client.post("/api/instances", json={"count": 1}, headers=KEY)
    now[0] += 599
    assert a.client.post("/api/instances", json={"count": 1}, headers=KEY).headers.get("idempotency-replayed") == "true"
    now[0] += 2                                             # ten minutes after the first: a new request
    assert a.client.post("/api/instances", json={"count": 1}, headers=KEY).json()["instances"][0]["id"] == 2
    remembered.limit = 3
    for n in range(5):
        a.client.post("/api/instances", json={"count": 1}, headers={"Idempotency-Key": f"k{n}"})
    assert list(remembered._entries) == ["k2", "k3", "k4"]


# ------------------------------------------------------------------------------- apps

def test_two_uploads_of_the_same_app_file_leave_one_app(a):
    apk = S.make_apk()
    for _ in range(5):
        answers = together(*(req(a, "post", "/api/apps/library", params={"filename": "app.apk"}, content=apk,
                                                        headers={"Content-Type": "application/vnd.android.package-archive"})
                             for _ in range(3)))
        assert [r.status_code for r in answers] == [201] * 3, [r.text for r in answers]
        assert len({r.json()["app"]["id"] for r in answers}) == 1
        (app,) = a.client.get("/api/apps/library").json()["apps"]
        assert app["sha256"] == hashlib.sha256(apk).hexdigest()
        folder = a.farm.library.dir(app["id"])
        assert (folder / "base.apk").read_bytes() == apk
        # Nothing staged, replaced or temporary is left next to it.
        assert [p.name for p in a.farm.library.root.iterdir() if p.is_dir()] == [app["id"]]
        assert list((a.farm.paths.run / "uploads").iterdir()) == []


# ------------------------------------------------------------------------------ cancel

def test_cancelling_twice_or_after_the_end_is_harmless(a):
    started, release = threading.Event(), threading.Event()

    def work(task):
        started.set()
        while not release.is_set():
            task.sleep(0.02)

    running = a.farm.tasks.submit("test", "a long job", work)
    assert started.wait(5)
    first, second = together(*(req(a, "post", f"/api/tasks/{running.id}/cancel") for _ in range(2)))
    assert (first.status_code, second.status_code) == (200, 200)
    a.farm.tasks.wait(running, 5)
    # Asked again when it has ended as cancelled: that is what was asked for, so it is not an error.
    late = a.client.post(f"/api/tasks/{running.id}/cancel")
    assert late.status_code == 200 and late.json()["task"]["state"] == "cancelled"
    # A task that ran to its end was not cancelled, and the caller is told so.
    done = a.farm.tasks.submit("test", "a short job", lambda t: 42)
    a.farm.tasks.wait(done, 5)
    refused = a.client.post(f"/api/tasks/{done.id}/cancel")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "conflict"
    assert "already finished (done)" in refused.json()["error"]["message"]
    assert a.client.get(f"/api/tasks/{done.id}").json()["task"]["output"] == 42


def test_cancel_racing_the_end_of_a_task_never_errors(a):
    for _ in range(ROUNDS):
        task = a.farm.tasks.submit("test", "about to end", lambda t: t.sleep(0.01))
        answers = together(*(req(a, "post", f"/api/tasks/{task.id}/cancel") for _ in range(3)))
        a.farm.tasks.wait(task, 5)
        final = a.client.get(f"/api/tasks/{task.id}").json()["task"]["state"]
        assert final in ("done", "cancelled")
        assert {r.status_code for r in answers} <= {200, 409}, [r.text for r in answers]
        # 200 means it was (or will be) cancelled, 409 that it ran to its end: never one for the other.
        assert {r.status_code for r in answers} == ({200} if final == "cancelled" else {409}), (final, [r.text for r in answers])

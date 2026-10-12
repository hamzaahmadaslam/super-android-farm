"""Shared values, per-instance values, pools, and the secret-name guard."""
from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from superfarm.automation import values as values_mod
from superfarm.automation.values import PoolEmpty, Resolver, Values, check_name, looks_secret

WORDS = ["password", "passwd", "pin", "otp", "2fa", "seed", "card", "cvv", "ssn"]


@pytest.fixture
def values(tmp_path) -> Values:
    return Values(tmp_path / "values", WORDS)


# -------------------------------------------------------------- secret names
@pytest.mark.parametrize("name", ["password", "user_password", "userpassword", "PASSWORD", "pin", "user_pin",
                                  "pinCode", "userPIN", "otp_code", "login2fa", "my_2fa", "card_number", "cvv",
                                  "seed_phrase", "passwd1"])
def test_names_that_look_like_secrets(name):
    assert looks_secret(name, WORDS)


@pytest.mark.parametrize("name", ["shipping", "spinner", "opinion", "discard", "search_term", "greeting", "city",
                                  "seedling", "cardinal", "message", ""])
def test_names_that_only_contain_a_short_word_by_accident(name):
    assert not looks_secret(name, WORDS)


def test_value_names_are_identifiers():
    assert check_name("search_term") == "search_term"
    for bad in ("", "1st", "a b", "a-b", "a.b", "../x", "x" * 65, None):
        with pytest.raises(ValueError):
            check_name(bad)


# -------------------------------------------------------------------- shared
def test_reading_a_shared_value_never_consumes_it(values, tmp_path):
    values.set_shared("search_term", "wireless earbuds")
    for _ in range(5):
        assert values.get_shared("search_term") == "wireless earbuds"
    assert Values(tmp_path / "values", WORDS).get_shared("search_term") == "wireless earbuds"
    values.set_shared("search_term", "phone case")
    assert values.shared() == {"search_term": "phone case"}
    assert values.remove_shared("search_term") and not values.remove_shared("search_term")
    assert values.get_shared("search_term") is None


def test_secret_looking_names_are_never_stored(values, tmp_path):
    for write in (lambda: values.set_shared("password", "hunter2"),
                  lambda: values.set_instance(3, "user_pin", "1234"),
                  lambda: values.pool_add("otp", ["111111"])):
        with pytest.raises(ValueError) as error:
            write()
        assert "never stored" in str(error.value) and "start the run" in str(error.value)
    assert not (tmp_path / "values").exists() or "hunter2" not in "".join(
        p.read_text() for p in (tmp_path / "values").rglob("*") if p.is_file())


def test_values_are_bounded_and_files_are_private(values, tmp_path):
    with pytest.raises(ValueError, match="too long"):
        values.set_shared("blob", "x" * 10_001)
    values.set_shared("city", "Lisbon")
    assert stat.S_IMODE(os.stat(tmp_path / "values" / "shared.json").st_mode) == 0o600


# -------------------------------------------------------------- per instance
def test_per_instance_values_are_separate(values):
    values.set_instance(1, "nickname", "one")
    values.set_instance(2, "nickname", "two")
    assert values.get_instance(1, "nickname") == "one" and values.get_instance(2, "nickname") == "two"
    assert values.get_instance(3, "nickname") is None and values.instance(3) == {}
    assert values.remove_instance(1, "nickname") and values.get_instance(1, "nickname") is None
    with pytest.raises(ValueError):
        values.set_instance("../7", "nickname", "x")


# --------------------------------------------------------------------- pools
def test_a_pool_hands_out_each_line_exactly_once(values):
    assert values.pool_add("codes", ["a1", " b2 ", "", "c3"]) == 3
    assert values.pool_count("codes") == 3
    assert [values.pool_take("codes", instance_id=i) for i in (1, 2, 3)] == ["a1", "b2", "c3"]
    with pytest.raises(PoolEmpty, match="add lines"):
        values.pool_take("codes")
    used = values.pool_used("codes")
    assert [u["value"] for u in used] == ["a1", "b2", "c3"] and used[0]["instance"] == 1
    assert values.pools() == [{"name": "codes", "remaining": 0, "used": 3}]
    assert values.pool_add("codes", ["d4"]) == 1 and values.pool_take("codes") == "d4"


def test_pool_names_and_lines_are_checked(values):
    for bad in ("../x", "a/b", "", "a b"):
        with pytest.raises(ValueError):
            values.pool_add(bad, ["x"])
        with pytest.raises(ValueError):
            values.pool_take(bad)
    with pytest.raises(ValueError, match="single line"):
        values.pool_add("codes", ["two\nlines"])


def test_the_journal_is_written_before_the_line_is_removed(values, monkeypatch):
    """Crash between "journalled" and "removed": the value may be lost, never reused."""
    values.pool_add("codes", ["a1", "b2", "c3"])

    def crash(*_args, **_kwargs):
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(values, "_write_pool", crash)
    with pytest.raises(KeyboardInterrupt):
        values.pool_take("codes", instance_id=1)
    monkeypatch.undo()

    # the crash left a1 in the pool file, and in the journal as used
    assert values._pool_lines("codes") == ["a1", "b2", "c3"]
    assert values.pool_used("codes")[-1]["value"] == "a1"
    # the next take repairs first: a1 is gone for good, b2 is handed out
    assert values.pool_take("codes", instance_id=2) == "b2"
    assert values._pool_lines("codes") == ["c3"]
    assert [u["value"] for u in values.pool_used("codes")] == ["a1", "b2"]
    assert values.pool_take("codes") == "c3"


def test_a_crash_after_the_removal_loses_nothing_more(values, monkeypatch):
    values.pool_add("codes", ["a1", "b2"])
    real = values._journal
    calls = []

    def journal(name, entry):
        calls.append(entry)
        if entry.get("done"):
            raise KeyboardInterrupt("power cut")              # removal done, marker not written
        real(name, entry)

    monkeypatch.setattr(values, "_journal", journal)
    with pytest.raises(KeyboardInterrupt):
        values.pool_take("codes")
    monkeypatch.undo()
    assert values._pool_lines("codes") == ["b2"]
    assert values.pool_take("codes") == "b2"                  # b2 was not dropped by the repair


def test_adding_lines_also_repairs_an_interrupted_take(values, monkeypatch):
    values.pool_add("codes", ["a1", "b2"])
    monkeypatch.setattr(values, "_write_pool", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        values.pool_take("codes")
    monkeypatch.undo()
    values.pool_add("codes", ["c3"])
    assert values._pool_lines("codes") == ["b2", "c3"]


def test_concurrent_takes_never_share_a_line(values):
    lines = [f"v{i:03d}" for i in range(60)]
    values.pool_add("codes", lines)
    taken: list[str] = []
    lock = threading.Lock()

    def worker(instance: int) -> None:
        for _ in range(10):
            value = values.pool_take("codes", instance_id=instance)
            with lock:
                taken.append(value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(taken) == lines and values.pool_count("codes") == 0


def test_pool_files_are_private_and_journal_lines_are_whole(values, tmp_path):
    values.pool_add("codes", ["a1"])
    values.pool_take("codes")
    pools = tmp_path / "values" / "pools"
    for name in ("codes.txt", "codes.used.jsonl"):
        assert stat.S_IMODE(os.stat(pools / name).st_mode) == 0o600
    for line in (pools / "codes.used.jsonl").read_text().splitlines():
        json.loads(line)
    assert values.pool_delete("codes") and not values.pool_delete("codes")
    assert values.pools() == []


def test_a_long_journal_is_read_from_its_tail(values, monkeypatch):
    monkeypatch.setattr(values_mod, "JOURNAL_TAIL", 300)
    values.pool_add("codes", [f"value-number-{i}" for i in range(30)])
    for _ in range(30):
        values.pool_take("codes")
    assert values.pool_used("codes")[-1]["value"] == "value-number-29"


# ------------------------------------------------------------------ resolver
def test_resolver_order(values):
    values.set_shared("city", "shared-city")
    values.set_shared("term", "shared-term")
    values.set_instance(4, "city", "instance-city")
    resolver = Resolver(values, instance_id=4, serial="127.0.0.1:17504", variables={"term": "run-term"})
    assert resolver.get("term") == "run-term"                 # passed to the run
    assert resolver.get("city") == "instance-city"            # this device's own value
    assert Resolver(values, instance_id=5).get("city") == "shared-city"
    assert resolver.get("instance_id") == "4" and resolver.get("serial") == "127.0.0.1:17504"
    assert resolver.get("nothing") is None
    resolver.set("city", "captured")
    assert resolver.get("city") == "captured" and values.get_instance(4, "city") == "instance-city"


def test_pool_placeholder_takes_one_line_per_run(values):
    values.pool_add("codes", ["a1", "b2", "c3"])
    resolver = Resolver(values, instance_id=1)
    assert resolver.get("pool.codes") == "a1" and resolver.get("pool.codes") == "a1"
    assert resolver.take("codes") == "b2"                     # an explicit take is always a fresh line
    assert Resolver(values, instance_id=2).get("pool.codes") == "c3"
    with pytest.raises(PoolEmpty):
        Resolver(values, instance_id=3).get("pool.codes")


def test_a_practice_run_leaves_pools_alone(values):
    values.pool_add("codes", ["a1"])
    resolver = Resolver(values, instance_id=1, consume=False)
    assert "pool codes" in resolver.get("pool.codes") and "pool codes" in resolver.take("codes")
    assert values.pool_count("codes") == 1 and values.pool_used("codes") == []
    with pytest.raises(PoolEmpty):
        Resolver(values, consume=False).take("empty_pool")


def test_secrets_are_redacted_from_anything_logged(values):
    resolver = Resolver(values, variables={"password": "hunter2", "city": "Lisbon"})
    assert resolver.is_secret("password") and not resolver.is_secret("city")
    assert resolver.redact("typed hunter2 in Lisbon") == "typed *** in Lisbon"
    resolver.set("otp_code", "424242")
    resolver.mark_secret("s3cr3t")
    assert resolver.redact("424242 and s3cr3t") == "*** and ***"
    assert resolver.vars["password"] == "hunter2"             # still usable by the run itself

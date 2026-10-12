"""Reading the screen: parsing, element resolution, and the two bounded readers.

The readers are tested against a fake `adb` executable and a fake helper process, so
the real code paths run (argument building, time limits, clean-up, reaping) without
a device.
"""
from __future__ import annotations

import os
import stat
import sys
import textwrap
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from automation_fakes import button, el, field, screen, to_xml  # noqa: E402

from superfarm.automation import ui  # noqa: E402
from superfarm.automation.ui import (  # noqa: E402
    Cancelled,
    Device,
    DumpReader,
    FallbackReader,
    U2Reader,
    UiError,
    candidates_at,
    element_at,
    foreground_package,
    interesting,
    make_reader,
    parse_hierarchy,
)
from superfarm.core.adb import Adb  # noqa: E402
from superfarm.util.proc import alive  # noqa: E402

GOOD_XML = to_xml(el("android.widget.FrameLayout", bounds=(0, 0, 480, 854), children=[
    button("ok", "OK", (10, 10, 110, 60))]))

# A stand-in for the adb binary. Its behaviour is read from files next to it, and
# every device command it receives is appended to calls.log.
FAKE_ADB = r'''#!/usr/bin/env python3
import os, shlex, sys, time
here = os.path.dirname(os.path.abspath(__file__))
def read(name, default=""):
    try:
        return open(os.path.join(here, name)).read().strip()
    except OSError:
        return default
args = sys.argv[1:]
if args[:1] == ["-s"]:
    args = args[2:]
verb, line = args[0], (args[1] if len(args) > 1 else "")
with open(os.path.join(here, "calls.log"), "a") as log:
    log.write(verb + " " + line + "\n")
words = shlex.split(line)
files = os.path.join(here, "files")
os.makedirs(files, exist_ok=True)
mode = read("mode", "ok")
if words[:1] == ["timeout"]:
    if read("no_timeout") == "1":
        sys.stderr.write("/system/bin/sh: timeout: inaccessible or not found\n"); sys.exit(127)
    words = words[2:]
if words[:2] == ["uiautomator", "dump"]:
    count = int(read("count", "0")) + 1
    open(os.path.join(here, "count"), "w").write(str(count))
    if mode == "hang":
        time.sleep(60)
    if mode == "fail" or (mode == "fail-once" and count == 1):
        print("ERROR: could not get idle state."); sys.exit(0)
    body = open(os.path.join(here, "screen.xml")).read()
    if mode == "truncated":
        body = body[: len(body) // 2]
    open(os.path.join(files, os.path.basename(words[2])), "w").write(body)
    print("UI hierchary dumped to: " + words[2]); sys.exit(0)
if words[:1] == ["cat"]:
    path = os.path.join(files, os.path.basename(words[1]))
    if not os.path.exists(path):
        sys.stderr.write("cat: No such file or directory\n"); sys.exit(1)
    sys.stdout.write(open(path).read()); sys.exit(0)
if words[:2] == ["rm", "-f"]:
    try:
        os.remove(os.path.join(files, os.path.basename(words[2])))
    except OSError:
        pass
    sys.exit(0)
if words[:3] == ["cmd", "package", "resolve-activity"]:
    print("priority=0 preferredOrder=0\n" + words[-1] + "/.Main"); sys.exit(0)
if words[:2] == ["am", "start"]:
    print("Starting: Intent { cmp=" + words[-1] + " }"); sys.exit(0)
sys.exit(0)
'''


@pytest.fixture
def fake_adb(tmp_path):
    binary = tmp_path / "adb"
    binary.write_text(FAKE_ADB)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "screen.xml").write_text(GOOD_XML)
    # An old dump left behind by some earlier, failed read. Nothing may ever return it.
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "window_dump.xml").write_text(GOOD_XML.replace("OK", "STALE"))

    def calls() -> list[str]:
        log = tmp_path / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def mode(value: str) -> None:
        (tmp_path / "mode").write_text(value)

    return Adb("127.0.0.1:17599", binary=str(binary)), calls, mode, tmp_path


# ------------------------------------------------------------------ parsing
def test_parse_gives_every_identifying_field():
    nodes = screen(el("android.widget.LinearLayout", id="row", bounds=(0, 100, 480, 200), clickable=True, children=[
        el(id="android:id/title", text=" Battery ", bounds=(20, 120, 200, 160)),
        field("query", "hello", (220, 120, 460, 160), focused=True)]))
    row, title, edit = nodes[1], nodes[2], nodes[3]
    assert row["id"] == "row" and row["resource_id"] == "com.example.app:id/row"
    assert row["clickable"] and row["cls"] == "LinearLayout" and row["full_class"] == "android.widget.LinearLayout"
    assert title["id"] == "title" and title["resource_id"] == "android:id/title"
    assert title["text"] == "Battery"                      # surrounding whitespace is not identity
    assert title["bounds"] == [20, 120, 200, 160] and title["center"] == [110, 140]
    assert title["path"] == "/FrameLayout[0]/LinearLayout[0]/TextView[0]"
    assert title["path"].startswith(row["path"] + "/") and title["depth"] == 2
    assert edit["editable"] and edit["focused"] and not title["editable"]
    assert edit["package"] == "com.example.app" and edit["enabled"]


def test_parse_refuses_anything_that_is_not_a_complete_hierarchy():
    for bad in ("", "ERROR: could not get idle state.", GOOD_XML[: len(GOOD_XML) // 2],
                "<?xml version='1.0'?><other/>", "<?xml version='1.0'?><hierarchy rotation='0'></hierarchy>"):
        with pytest.raises(UiError):
            parse_hierarchy(bad)


def test_parse_refuses_a_document_type():
    hostile = GOOD_XML.replace("<hierarchy", "<!DOCTYPE x [<!ENTITY a 'aaaa'>]><hierarchy", 1)
    with pytest.raises(UiError, match="document type"):
        parse_hierarchy(hostile)


def test_parse_skips_leading_noise_before_the_xml():
    assert parse_hierarchy("WARNING: linker\n" + GOOD_XML)[1]["text"] == "OK"


# ------------------------------------------------------- element resolution
ROWS = screen(
    el("android.widget.LinearLayout", id="row", bounds=(0, 100, 480, 200), clickable=True, children=[
        el(id="title", text="Network", bounds=(20, 120, 200, 160))]),
    el("android.widget.LinearLayout", id="row", bounds=(0, 200, 480, 300), clickable=True, children=[
        el(id="title", text="Battery", bounds=(20, 220, 200, 260))]),
    el(id="footer", text="v1.0", bounds=(0, 800, 480, 854)),
    button("off", "Disabled", (300, 400, 400, 450), enabled=False),
    el("android.view.View", bounds=(0, 500, 480, 600), clickable=True),
)


def test_element_at_takes_the_tapped_row_and_the_label_inside_it():
    element = element_at(ROWS, 300, 250)          # on the row, away from its label
    assert element["id"] == "row" and element["text"] == "Battery" and element["clickable"]
    assert element["bounds"] == [0, 200, 480, 300]
    assert element_at(ROWS, 50, 140)["text"] == "Network"


def test_element_at_without_anything_clickable_uses_the_smallest_named_node():
    assert element_at(ROWS, 100, 820)["id"] == "footer"


def test_element_at_ignores_disabled_elements_and_empty_space():
    assert element_at(ROWS, 350, 425)["cls"] == "FrameLayout"        # only the frame is left
    assert element_at(ROWS, 9000, 9000) is None


def test_element_at_reports_an_anonymous_clickable_as_such():
    element = element_at(ROWS, 240, 550)
    assert element["cls"] == "View" and not element["id"] and not element["text"]


def test_candidates_are_the_named_neighbours_smallest_first():
    found = candidates_at(ROWS, 50, 240)
    assert [c.get("id") for c in found] == ["title", "row"]
    assert found[0]["text"] == "Battery" and all("path" in c for c in found)
    assert candidates_at(ROWS, 240, 550) == []


def test_interesting_keeps_labelled_or_actionable_enabled_nodes():
    picked = interesting(ROWS)
    assert all(n["enabled"] for n in picked)
    assert {"Network", "Battery", "v1.0"} <= {n["text"] for n in picked}
    assert not any(n["cls"] == "FrameLayout" for n in picked)
    assert len(interesting(ROWS, limit=2)) == 2


def test_foreground_package_is_the_one_owning_the_screen():
    assert foreground_package(ROWS) == "com.example.app"
    assert foreground_package([]) == ""


# ---------------------------------------------------------- built-in reader
def test_dump_reader_reads_through_a_unique_path_and_removes_it(fake_adb):
    adb, calls, _, tmp = fake_adb
    reader = DumpReader(adb)
    first = reader.read(timeout=10)
    second = reader.read(timeout=10)
    assert first[1]["text"] == "OK" and second[1]["text"] == "OK"
    dumps = [c.split()[-1] for c in calls() if "uiautomator dump" in c]
    assert len(dumps) == 2 and dumps[0] != dumps[1]
    assert all(d.startswith(ui.REMOTE_DIR + "/sf-ui-") for d in dumps)
    removed = [c.split()[-1] for c in calls() if c.startswith("shell rm -f")]
    assert removed == dumps
    assert not any("pkill" in c for c in calls())                # nothing to stop after a normal read
    assert os.listdir(tmp / "files") == ["window_dump.xml"]      # nothing of ours is left behind


def test_a_failed_dump_never_returns_an_older_screen(fake_adb):
    adb, calls, mode, _ = fake_adb
    mode("fail")
    with pytest.raises(UiError) as error:
        DumpReader(adb).read(timeout=10)
    assert "could not get idle state" in str(error.value)
    assert "superfarm[u2]" in str(error.value)                   # says how to fix busy screens
    assert not any("window_dump" in c for c in calls())          # the stale file was never read
    assert len([c for c in calls() if "uiautomator dump" in c]) == 3       # it did retry
    assert len({c.split()[-1] for c in calls() if "uiautomator dump" in c}) == 3


def test_dump_reader_retries_after_one_failure(fake_adb):
    adb, calls, mode, _ = fake_adb
    mode("fail-once")
    assert DumpReader(adb).read(timeout=10)[1]["text"] == "OK"
    assert len([c for c in calls() if "uiautomator dump" in c]) == 2


def test_a_truncated_dump_is_an_error_not_a_screen(fake_adb):
    adb, _, mode, _ = fake_adb
    mode("truncated")
    with pytest.raises(UiError, match="not complete XML"):
        DumpReader(adb).read(timeout=10)


def test_a_hanging_dump_is_cut_off_at_the_time_limit(fake_adb):
    adb, calls, mode, _ = fake_adb
    mode("hang")
    started = time.monotonic()
    with pytest.raises(UiError):
        DumpReader(adb).read(timeout=1.5)
    assert time.monotonic() - started < 5
    assert any(c.startswith("shell rm -f") for c in calls())     # clean-up still ran
    # the dump left running on the device is stopped, by exact name, before its file is removed
    assert calls().index("shell pkill -x uiautomator") < next(i for i, c in enumerate(calls()) if c.startswith("shell rm -f"))


def test_cancel_ends_a_read_at_once(fake_adb):
    adb, _, mode, _ = fake_adb
    mode("hang")
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        DumpReader(adb).read(timeout=30, cancel=cancel)
    assert time.monotonic() - started < 3


def test_dump_reader_copes_with_a_device_that_has_no_timeout_command(fake_adb):
    adb, calls, _, tmp = fake_adb
    (tmp / "no_timeout").write_text("1")
    reader = DumpReader(adb)
    assert reader.read(timeout=10)[1]["text"] == "OK"
    assert reader.read(timeout=10)[1]["text"] == "OK"
    with_timeout = [c for c in calls() if c.startswith("shell timeout")]
    assert len(with_timeout) == 1                                # learned after the first try


def test_a_missing_adb_binary_says_how_to_fix_it(tmp_path):
    with pytest.raises(UiError, match="platform tools"):
        DumpReader(Adb("127.0.0.1:17599", binary=str(tmp_path / "nope"))).read(timeout=3)


# -------------------------------------------------------- persistent reader
WORKER = textwrap.dedent('''
    import json, os, sys, time
    mode = sys.argv[1]
    open(sys.argv[2], "w").write(str(os.getpid()))
    for line in sys.stdin:
        command = json.loads(line)["cmd"]
        if command == "connect" and mode == "no-connect":
            print(json.dumps({"ok": False, "error": "RuntimeError: device offline"}), flush=True); continue
        if command == "dump" and mode == "slow":
            time.sleep(60)
        if command == "dump" and mode == "noise":
            print("not json at all", flush=True); continue
        answer = {"ok": True}
        if command == "dump":
            answer["xml"] = open(sys.argv[3]).read()
        print(json.dumps(answer), flush=True)
        if command == "close":
            break
''')


def _u2(tmp_path, mode: str) -> tuple[U2Reader, callable]:
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    xml = tmp_path / "u2.xml"
    xml.write_text(GOOD_XML)
    pidfile = tmp_path / f"pid-{mode}"
    reader = U2Reader(Adb(f"127.0.0.1:{17500 + len(mode)}"), worker_argv=[sys.executable, str(script), mode, str(pidfile), str(xml)])

    def pid() -> int:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return int(pidfile.read_text())

    return reader, pid


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
    return not alive(pid)


def test_u2_reader_keeps_one_helper_for_many_reads(tmp_path):
    reader, pid = _u2(tmp_path, "ok")
    try:
        assert reader.read(timeout=10)[1]["text"] == "OK"
        first = pid()
        assert reader.read(timeout=10)[1]["text"] == "OK"
        assert pid() == first and alive(first)
    finally:
        reader.close()
    assert _gone(first)


def test_u2_reader_kills_a_helper_that_passes_the_deadline(tmp_path):
    reader, pid = _u2(tmp_path, "slow")
    started = time.monotonic()
    with pytest.raises(UiError, match="no answer within"):
        reader.read(timeout=1.0)
    assert time.monotonic() - started < 6
    assert _gone(pid())
    reader.close()


def test_u2_reader_reaps_its_helper_on_cancel(tmp_path):
    reader, pid = _u2(tmp_path, "slow")
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    with pytest.raises(Cancelled):
        reader.read(timeout=30, cancel=cancel)
    assert _gone(pid())


def test_u2_reader_reports_what_the_helper_said(tmp_path):
    reader, _ = _u2(tmp_path, "no-connect")
    with pytest.raises(UiError, match="device offline"):
        reader.read(timeout=5)
    noisy, _ = _u2(tmp_path, "noise")
    with pytest.raises(UiError, match="not JSON"):
        noisy.read(timeout=5)


def test_only_one_persistent_reader_per_device(tmp_path):
    reader, _ = _u2(tmp_path, "ok")
    twin = U2Reader(reader.adb, worker_argv=reader._argv)
    try:
        reader.read(timeout=10)
        with pytest.raises(UiError, match="already reading"):
            twin.read(timeout=5)
    finally:
        reader.close()
        twin.close()


def test_auto_mode_falls_back_to_the_built_in_reader(tmp_path, fake_adb):
    adb, _, _, _ = fake_adb
    broken, _ = _u2(tmp_path, "no-connect")
    reader = FallbackReader(broken, DumpReader(adb))
    assert reader.read(timeout=10)[1]["text"] == "OK"            # first failure: answered by the fallback
    assert reader.read(timeout=10)[1]["text"] == "OK"
    assert reader.failures == 2 and reader.name == "uiautomator"   # and now it stays there
    reader.close()


def test_make_reader_modes(monkeypatch, fake_adb):
    adb = fake_adb[0]
    assert isinstance(make_reader(adb, "uiautomator"), DumpReader)
    monkeypatch.setattr(ui, "u2_available", lambda: False)
    assert isinstance(make_reader(adb, "auto"), DumpReader)
    with pytest.raises(UiError, match=r"pip install 'superfarm\[u2\]'"):
        make_reader(adb, "u2")
    monkeypatch.setattr(ui, "u2_available", lambda: True)
    assert isinstance(make_reader(adb, "auto"), FallbackReader)
    assert isinstance(make_reader(adb, "u2"), U2Reader)
    with pytest.raises(ValueError):
        make_reader(adb, "telepathy")


# ------------------------------------------------------------------- device
def test_device_inputs_are_argument_lists_quoted_for_the_device(fake_adb):
    adb, calls, _, _ = fake_adb
    device = Device(adb)
    device.tap(10, 20)
    device.long_press(5, 6, 900)
    device.key("backspace")
    device.text("it's 5 o'clock; rm -rf /")
    device.clear_focused(3)
    assert "shell input tap 10 20" in calls()
    assert "shell input swipe 5 6 5 6 900" in calls()
    assert "shell input keyevent KEYCODE_DEL" in calls()
    typed = next(c for c in calls() if c.startswith("shell input text"))
    assert typed == """shell input text 'it'"'"'s%s5%so'"'"'clock;%srm%s-rf%s/'"""      # one quoted word
    end = calls().index("shell input keyevent KEYCODE_MOVE_END")
    assert calls()[end + 1] == "shell input keyevent" + " KEYCODE_DEL" * 5


def test_typing_survives_percent_signs_and_long_lines(fake_adb):
    """`input text` turns "%s" into a space and has no escape for it."""
    adb, calls, _, _ = fake_adb
    device = Device(adb)
    device.text("100% sure")
    device.text("a%sb")
    device.text("x" * 130)
    device.text("two\nlines")
    typed = [c[len("shell input text "):] for c in calls() if c.startswith("shell input text")]
    assert typed[0] == "100%%ssure"                       # "% " -> "%" + "%s": still a percent and a space
    assert typed[1:3] == ["a%", "sb"]                     # a literal "%s" is split between two commands
    assert typed[3:6] == ["x" * 60, "x" * 60, "x" * 10]
    assert typed[6:] == ["two", "lines"] and "shell input keyevent KEYCODE_ENTER" in calls()
    device.clear_focused(120)
    batches = [c.count("KEYCODE_DEL") for c in calls() if "KEYCODE_DEL" in c]
    assert batches == [50, 50, 22]


def test_device_refuses_what_it_cannot_do_safely(fake_adb):
    device = Device(fake_adb[0])
    with pytest.raises(UiError, match="printable ASCII"):
        device.text("héllo")
    with pytest.raises(UiError, match="unknown key"):
        device.key("back; reboot")
    for bad in ("", "settings", "com.example.app; reboot", "../etc"):
        with pytest.raises(UiError, match="bad package"):
            device.launch(bad)
        with pytest.raises(UiError, match="bad package"):
            device.stop_app(bad)


def test_device_launch_resolves_the_launcher_activity(fake_adb):
    adb, calls, _, _ = fake_adb
    assert Device(adb).launch("com.example.app") == "com.example.app/.Main"
    assert "shell am start -n com.example.app/.Main" in calls()
    assert Device(adb).launch("com.example.app", ".Other") == "com.example.app/.Other"


def test_device_nodes_use_its_reader_and_its_time_limit(fake_adb):
    adb = fake_adb[0]
    seen = {}

    class Reader:
        name = "stub"

        def read(self, *, timeout, cancel=None):
            seen["timeout"] = timeout
            return parse_hierarchy(GOOD_XML)

        def close(self):
            seen["closed"] = True

    device = Device(adb, reader=Reader(), read_timeout=12)
    assert device.nodes()[1]["text"] == "OK" and seen["timeout"] == 12
    device.nodes(timeout=3)
    assert seen["timeout"] == 3
    device.nodes(timeout=500)
    assert seen["timeout"] == 12                                  # never beyond the configured limit
    device.close()
    assert seen["closed"]

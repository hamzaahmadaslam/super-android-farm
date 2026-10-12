"""Tables, colour rules and the small formatters of the CLI."""
import io
import os

import pytest

from superfarm.cli import render
from superfarm.cli.render import Output, cell_width, clip, fit_widths, table


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def widths_of(text: str) -> set[int]:
    return {cell_width(line) for line in text.splitlines()}


def test_columns_are_aligned_and_lines_have_no_trailing_space():
    text = table(("ID", "NAME", "STATE"), [(1, "alpha", "booted"), (12, "b", "stopped")], right=(0,))
    lines = text.splitlines()
    assert lines == ["ID  NAME   STATE",
                     " 1  alpha  booted",
                     "12  b      stopped"]
    assert all(line == line.rstrip() for line in lines)


def test_table_without_width_never_truncates():
    long = "x" * 500
    assert long in table(("A", "B"), [("1", long)], width=None)


@pytest.mark.parametrize("limit", [40, 60, 80, 120])
def test_table_fits_the_terminal_width(limit):
    rows = [(i, f"android-{i}", "booted", "detail " * 30) for i in range(1, 6)]
    text = table(("ID", "NAME", "STATE", "DETAIL"), rows, width=limit)
    assert max(widths_of(text)) <= limit
    # The narrow, scannable columns survive whole; only the long one gives way.
    assert "android-5" in text and "booted" in text
    assert render.ELLIPSIS in text


def test_fit_widths_takes_from_the_widest_column_first():
    assert fit_widths([2, 10, 50], [2, 4, 6], 40) == [2, 10, 24]        # 40 - two gaps of 2 = 36 for cells
    assert fit_widths([2, 10, 50], [2, 4, 6], None) == [2, 10, 50]
    assert fit_widths([3, 3], [3, 3], 200) == [3, 3]


def test_fit_widths_never_goes_below_the_floor():
    # Too narrow for everything: columns stop at max(MIN_COLUMN, header), the line may overflow.
    assert fit_widths([30, 30, 30], [4, 4, 4], 10) == [render.MIN_COLUMN] * 3
    assert fit_widths([3, 40], [3, 12], 5) == [3, 12]


def test_clip_marks_the_cut_and_respects_wide_characters():
    assert clip("abcdefgh", 5) == "abcd" + render.ELLIPSIS
    assert clip("abc", 5) == "abc"
    assert clip("abc", 0) == ""
    wide = "測試應用程式"            # six double-width characters
    assert cell_width(wide) == 12
    assert cell_width(clip(wide, 7)) <= 7
    assert cell_width("é") == 1                        # a combining accent takes no column


def test_wide_characters_keep_columns_aligned():
    text = table(("NAME", "STATE"), [("測試", "ok"), ("ab", "ok")])
    lines = text.splitlines()
    assert cell_width(lines[1]) == cell_width(lines[2])


def test_cells_cannot_inject_control_sequences_or_newlines():
    text = table(("A",), [("evil\x1b[31mred\nsecond line\ttab",)])
    assert "\x1b" not in text and text.count("\n") == 1
    assert "evil[31mred second line tab" in text


def test_cell_formatting_of_common_values():
    assert render.text(None) == "-"
    assert render.text(True) == "yes" and render.text(False) == "no"
    assert render.text(["a", "b"]) == "a,b" and render.text([]) == "-"
    assert render.text(2.0) == "2" and render.text(2.5) == "2.5"
    assert render.text("") == "-"


def test_row_length_mismatch_is_a_programming_error():
    with pytest.raises(ValueError):
        table(("A", "B"), [("only one",)])


def test_colour_only_on_a_terminal_without_no_color():
    assert render.colour_wanted(Tty(), {}) is True
    assert render.colour_wanted(Tty(), {"NO_COLOR": "1"}) is False
    assert render.colour_wanted(Tty(), {"NO_COLOR": ""}) is True        # empty means unset (no-color.org)
    assert render.colour_wanted(Tty(), {"TERM": "dumb"}) is False
    assert render.colour_wanted(io.StringIO(), {}) is False


def test_colour_does_not_change_alignment():
    rows = [(1, "booted"), (2, "error")]
    plain = table(("ID", "STATE"), rows, state_columns=(1,))
    coloured = table(("ID", "STATE"), rows, state_columns=(1,), colour=True)
    assert "\033[32m" in coloured and "\033[31m" in coloured
    import re
    assert re.sub(r"\033\[[0-9;]*m", "", coloured) == plain


def test_output_table_is_plain_when_piped_and_cut_on_a_terminal(monkeypatch):
    piped = io.StringIO()
    Output(stdout=piped, stderr=io.StringIO(), env={}).table(("STATE", "D"), [("booted", "y" * 300)], state_columns=(0,))
    assert "\033[" not in piped.getvalue() and "y" * 300 in piped.getvalue()

    monkeypatch.setattr(render.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((50, 24)))
    term = Tty()
    Output(stdout=term, stderr=io.StringIO(), env={}).table(("STATE", "D"), [("booted", "y" * 300)], state_columns=(0,))
    assert "\033[32m" in term.getvalue()
    no_colour = Tty()
    Output(stdout=no_colour, stderr=io.StringIO(), env={"NO_COLOR": "1"}).table(("STATE", "D"), [("booted", "y" * 300)])
    assert "\033[" not in no_colour.getvalue()
    assert max(len(line) for line in no_colour.getvalue().splitlines()) <= 50


def test_json_output_is_exactly_the_data():
    out = io.StringIO()
    Output(json_mode=True, stdout=out, stderr=io.StringIO(), env={}).json({"a": [1, 2], "b": "ü"})
    import json
    assert json.loads(out.getvalue()) == {"a": [1, 2], "b": "ü"}


def test_status_line_only_on_a_terminal_and_is_cleared_before_output():
    err = Tty()
    out = Output(stdout=io.StringIO(), stderr=err, env={})
    out.status("working 1/3")
    assert "working 1/3" in err.getvalue() and "\n" not in err.getvalue()
    out.print("result")
    assert err.getvalue().endswith("\r\033[K")

    silent = io.StringIO()
    Output(stdout=io.StringIO(), stderr=silent, env={}).status("working")
    assert silent.getvalue() == ""
    quiet = Tty()
    Output(quiet=True, stdout=io.StringIO(), stderr=quiet, env={}).status("working")
    assert quiet.getvalue() == ""


def test_quiet_drops_notes_but_never_errors():
    err = io.StringIO()
    out = Output(quiet=True, stdout=io.StringIO(), stderr=err, env={})
    out.note("a hint")
    out.error("it broke")
    assert err.getvalue() == "sf: it broke\n"


def test_formatters():
    assert render.duration(None) == "-" and render.duration(5) == "5s"
    assert render.duration(125) == "2m05s" and render.duration(3725) == "1h02m" and render.duration(90000) == "1d01h"
    assert render.size(None) == "-" and render.size(512) == "512 B" and render.size(1536) == "1.5 KiB"
    assert render.size(5 * 1024 ** 3) == "5.0 GiB"
    assert render.when(0) == "-" and render.when(None) == "-"
    assert render.pairs([("id", 3), ("state", "booted")]).splitlines() == ["id     3", "state  booted"]

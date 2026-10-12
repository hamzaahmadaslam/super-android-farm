"""Terminal output: aligned tables, colour only when it is welcome, small formatters.

Everything a command prints goes through an `Output`, which knows whether the reader is a
person at a terminal or a script. Rules that hold everywhere:

* `--json` prints the data exactly as it was received and nothing else on stdout.
* Tables are cut to the terminal width only when stdout *is* a terminal. A pipe gets every
  cell in full, because `grep` and `awk` must never see a value shortened by a window size.
* Colour needs a terminal and the absence of NO_COLOR (https://no-color.org).
* Progress and hints go to stderr so stdout stays clean for the result.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, TextIO

ELLIPSIS = "…"
MIN_COLUMN = 6          # a column is never squeezed below this (or its own width, if smaller)
GAP = "  "

_CODES = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "cyan": "36", "dim": "2", "bold": "1"}
# One place decides what a word looks like, so `ls`, `tasks` and result tables agree.
STATE_COLOURS = {
    "booted": "green", "running": "green", "ok": "green", "done": "green", "connected": "green", "up": "green",
    "starting": "yellow", "created": "yellow", "queued": "yellow", "warn": "yellow", "connecting": "yellow",
    "reconnecting": "yellow", "cancelled": "yellow", "needs_attention": "yellow",
    "error": "red", "failed": "red", "fail": "red", "auth_failed": "red", "down": "red",
    "stopped": "dim", "missing": "dim", "skip": "dim",
}


def colour_wanted(stream: TextIO, env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    if env.get("NO_COLOR") or env.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def cell_width(text: str) -> int:
    """Columns a string takes on screen. East-Asian wide characters take two, combining
    marks none; app labels and device names are often not ASCII."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def clip(text: str, width: int) -> str:
    """Shorten to at most `width` columns, marking the cut with an ellipsis."""
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = cell_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + ELLIPSIS


def text(value: Any) -> str:
    """One cell: no line breaks, no control characters (a device name or a log line from
    an Android must not be able to move the cursor or recolour the terminal)."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple, set)):
        return ",".join(text(v) for v in value) or "-"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    s = " ".join(str(value).split())
    return "".join(ch for ch in s if ch.isprintable()) or "-"


def fit_widths(natural: Sequence[int], headers: Sequence[int], limit: int | None) -> list[int]:
    """Column widths that fit `limit`, taking space from the widest columns first.

    The narrow columns (ids, states) are what a reader scans by, so they stay whole; the
    long free-text column gives way. Nothing shrinks below MIN_COLUMN or its header."""
    widths = list(natural)
    if limit is None or not widths:
        return widths
    floor = [min(n, max(MIN_COLUMN, h)) for n, h in zip(natural, headers, strict=True)]
    budget = limit - len(GAP) * (len(widths) - 1)
    while sum(widths) > budget:
        candidates = [i for i, w in enumerate(widths) if w > floor[i]]
        if not candidates:
            break
        widest = max(candidates, key=lambda i: (widths[i], i))
        widths[widest] -= 1
    return widths


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]], *, width: int | None = None,
          right: Iterable[int] = (), colour: bool = False, state_columns: Iterable[int] = ()) -> str:
    """An aligned plain-text table. `width` cuts it to a terminal; None never cuts.

    right           indexes of right-aligned (numeric) columns
    state_columns   indexes whose words are coloured by STATE_COLOURS when colour is on
    """
    cells = [[text(c) for c in row] for row in rows]
    count = len(headers)
    for row in cells:
        if len(row) != count:
            raise ValueError(f"row has {len(row)} cells, table has {count} columns")
    natural = [max([cell_width(h)] + [cell_width(r[i]) for r in cells]) for i, h in enumerate(headers)]
    widths = fit_widths(natural, [cell_width(h) for h in headers], width)
    right_set, state_set = set(right), set(state_columns)

    def line(row: Sequence[str], *, header: bool = False) -> str:
        parts = []
        for i, raw in enumerate(row):
            shown = clip(raw, widths[i])
            pad = " " * (widths[i] - cell_width(shown))
            if i in right_set:
                padded = pad + shown
            else:
                # The last column is not padded: no trailing spaces, with or without colour.
                padded = shown if i == count - 1 else shown + pad
            if colour:
                # Colour goes on after padding, so escape codes never count as width.
                if header:
                    padded = paint(padded, "bold")
                elif i in state_set and raw in STATE_COLOURS:
                    padded = paint(padded, STATE_COLOURS[raw])
            parts.append(padded)
        return GAP.join(parts)

    return "\n".join([line(list(headers), header=True), *(line(r) for r in cells)])


def paint(value: str, name: str) -> str:
    return f"\033[{_CODES[name]}m{value}\033[0m"


def pairs(items: Iterable[tuple[str, Any]]) -> str:
    """`key  value` lines with the keys aligned (detail views)."""
    rows = [(k, text(v)) for k, v in items]
    if not rows:
        return ""
    pad = max(cell_width(k) for k, _ in rows)
    return "\n".join(f"{k}{' ' * (pad - cell_width(k))}{GAP}{v}".rstrip() for k, v in rows)


def duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600:02d}h"


def size(count: float | None) -> str:
    """Bytes as a short binary-unit figure (1536 -> 1.5 KiB)."""
    if count is None:
        return "-"
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "-"


def when(timestamp: float | None) -> str:
    """A Unix time as local date and time; `-` for never."""
    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


class Output:
    """Where a command writes, and how."""

    def __init__(self, *, json_mode: bool = False, quiet: bool = False, stdout: TextIO | None = None,
                 stderr: TextIO | None = None, env: Mapping[str, str] | None = None):
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.json_mode = json_mode
        self.quiet = quiet
        self.env = os.environ if env is None else env
        self.tty = self._isatty(self.stdout)
        self.err_tty = self._isatty(self.stderr)
        self.colour = colour_wanted(self.stdout, self.env)
        self._status_shown = False

    @staticmethod
    def _isatty(stream: TextIO) -> bool:
        try:
            return bool(stream.isatty())
        except (AttributeError, ValueError):
            return False

    @property
    def width(self) -> int | None:
        """Terminal width when stdout is a terminal, else None (never cut piped output)."""
        if not self.tty:
            return None
        return shutil.get_terminal_size((100, 24)).columns

    # ---------------------------------------------------------------- stdout
    def print(self, value: str = "") -> None:
        self.clear_status()
        self.stdout.write(value + "\n")

    def json(self, data: Any) -> None:
        self.clear_status()
        self.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[Any]], *, right: Iterable[int] = (),
              state_columns: Iterable[int] = (), empty: str = "") -> None:
        rows = list(rows)
        if not rows and empty:
            self.note(empty)
            return
        self.print(table(headers, rows, width=self.width, right=right, colour=self.colour,
                         state_columns=state_columns))

    def pairs(self, items: Iterable[tuple[str, Any]]) -> None:
        self.print(pairs(items))

    # ---------------------------------------------------------------- stderr
    def note(self, message: str) -> None:
        """Something a person wants to read and a script does not (suppressed by -q)."""
        if not self.quiet:
            self._to_stderr(message + "\n")

    def error(self, message: str) -> None:
        self._to_stderr(f"sf: {message}\n")

    def _to_stderr(self, value: str) -> None:
        # stdout first: when both go to one pipe or file, a hint must not overtake the
        # result it comments on.
        self.clear_status()
        try:
            self.stdout.flush()
        except (OSError, ValueError):
            pass
        self.stderr.write(value)

    def status(self, message: str) -> None:
        """One line that is overwritten in place. Only on a terminal: in a log file a
        progress line repeated twice a second is noise."""
        if self.quiet or not self.err_tty:
            return
        width = shutil.get_terminal_size((100, 24)).columns
        self.stderr.write("\r\033[K" + clip(text(message), max(10, width - 1)))
        self.stderr.flush()
        self._status_shown = True

    def clear_status(self) -> None:
        if self._status_shown:
            self.stderr.write("\r\033[K")
            self.stderr.flush()
            self._status_shown = False

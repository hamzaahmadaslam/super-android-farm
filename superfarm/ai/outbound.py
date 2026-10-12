"""The last check on anything about to be sent to a model.

Two rules, applied together by `for_model()`:

  * by name   - an entry whose *name* looks like a credential is replaced (safety.redact,
                which also honours automation.forbidden_field_words);
  * by value  - the *contents* of every file under <home>/secrets, and any value a caller
                marks as secret for this run, are replaced wherever they appear in text.

The first rule needs structure (a mapping with names). The second needs none, which is why
the model clients apply it to every outgoing message: a tool result, a log line or a pasted
command that happens to carry the API token is cleaned whatever shape it arrived in.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .safety import redact

MASK = "***"
MIN_SECRET = 6             # shorter strings ("pin", a 4-digit code) would blank ordinary words
MAX_SECRET_FILE = 64 * 1024


def secret_values(secrets_dir: Path | None) -> list[str]:
    """Every credential the farm keeps on disk, as the strings to look for.

    A secret file is either one value (a token) or a few lines (user name, then password);
    each line counts on its own, so a password is caught without its user name next to it.
    """
    found: set[str] = set()
    if secrets_dir is None:
        return []
    try:
        files = [p for p in Path(secrets_dir).iterdir() if p.is_file()]
    except OSError:
        return []
    for path in files:
        try:
            if path.stat().st_size > MAX_SECRET_FILE:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in [text, *text.splitlines()]:
            line = line.strip()
            if len(line) >= MIN_SECRET:
                found.add(line)
    return sorted(found, key=len, reverse=True)


def scrub_text(text: str, values: Iterable[str]) -> str:
    """`text` with every known secret value replaced. Longest first, so a value that
    contains another is removed whole."""
    for value in values:
        if value and value in text:
            text = text.replace(value, MASK)
    return text


def for_model(value: Any, *, words: Iterable[str] = (), values: Iterable[str] = ()) -> Any:
    """A copy of a JSON-like value that is safe to put in a prompt."""
    ordered = sorted({v for v in values if isinstance(v, str) and len(v) >= MIN_SECRET}, key=len, reverse=True)

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return scrub_text(item, ordered)
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(v) for v in item]
        return item

    return walk(redact(value, words))

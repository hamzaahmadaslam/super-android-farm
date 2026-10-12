"""YAML as the farm reads it from people: plain data, no aliases.

`yaml.safe_load` refuses code but still honours aliases (`&name` ... `*name`), and a text of
a few hundred bytes that nests them unfolds into gigabytes as soon as something walks or
prints the result. Nothing the farm reads has a use for them, so every text that comes
from outside (a flow, an app profile, the settings inside a farm backup) is loaded here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class PlainLoader(yaml.SafeLoader):
    """SafeLoader that refuses an alias the moment it meets one, before anything is built."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.composer.ComposerError(None, None, "aliases (&name ... *name) are not supported here: "
                                              "write the value out", self.peek_event().start_mark)
        return super().compose_node(parent, index)


def load(text: str | bytes) -> Any:
    """Like yaml.safe_load, raising yaml.YAMLError for a text that uses an alias."""
    return yaml.load(text, Loader=PlainLoader)  # noqa: S506 - PlainLoader is a SafeLoader


def read(path: Path, default: Any = None) -> Any:
    """The file's content, or `default` when there is no file or it is empty."""
    try:
        with open(path, encoding="utf-8") as f:
            data = load(f.read())
    except FileNotFoundError:
        return default
    return default if data is None else data

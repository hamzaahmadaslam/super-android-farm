#!/usr/bin/env python3
"""Generate the step reference inside docs/AUTOMATION.md from the flow models.

    python scripts/gen_flow_docs.py                          rewrite the block in docs/AUTOMATION.md
    python scripts/gen_flow_docs.py --check docs/AUTOMATION.md   exit 1 when the block is not current

The block sits between two marker comments in the guide. Everything outside them is written
by hand and left alone. The block is built from the pydantic models and the step table in
superfarm/automation/flows.py, the selector model in selectors.py and the overlay model in
profiles.py, so a step, key or default cannot change without the reference changing too.
"""
from __future__ import annotations

import argparse
import sys
import types
import typing
from pathlib import Path

from pydantic_core import PydanticUndefined

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BEGIN = "<!-- BEGIN GENERATED: flow reference (scripts/gen_flow_docs.py). Do not edit by hand. -->"
END = "<!-- END GENERATED: flow reference -->"

# Keys that hold lists of steps are not fields of an argument model.
STEP_LISTS = {"then": "list of steps run when the condition holds",
              "else": "list of steps run when it does not",
              "steps": "list of steps that make up one round"}
COMMON = (("timeout", "number", "Seconds this step may wait for what it needs, more than 0 and at most 3600. Default: `timeouts.step` of the flow, then `automation.step_timeout`."),
          ("optional", "true / false", "Default `false`. When true, a step that cannot be done on the screen is skipped and counted, instead of stopping this Android to ask a person."),
          ("label", "text", "Name shown in the run's progress instead of the generated description (up to 120 characters)."))


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _type(annotation: object) -> str:
    from superfarm.automation.flows import Condition
    from superfarm.automation.selectors import Selector

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        inner = [a for a in args if a is not type(None)]
        return " or ".join(_type(a) for a in inner)
    if origin is typing.Literal:
        return " or ".join(f"`{a}`" for a in args)
    if origin is tuple:
        if args and all(a is float for a in args):
            return "[x, y] as fractions of the screen"
        return f"{len(args)} texts" if args and all(a is str for a in args) else "list"
    if origin is list:
        return f"list of {_type(args[0])}" if args else "list"
    if origin is dict:
        return f"map of {_type(args[0])} to {_type(args[1])}"
    if isinstance(annotation, type):
        if issubclass(annotation, Selector):
            return "selector"
        if issubclass(annotation, Condition):
            return "condition"
        return {str: "text", int: "whole number", float: "number", bool: "true / false"}.get(annotation, annotation.__name__)
    return str(annotation)


def _limits(info: object) -> str:
    bits = []
    for meta in getattr(info, "metadata", []):
        for attr, label in (("ge", "at least"), ("gt", "more than"), ("le", "at most"), ("lt", "less than"),
                            ("min_length", "length at least"), ("max_length", "length at most")):
            value = getattr(meta, attr, None)
            if value is not None:
                bits.append(f"{label} {value:g}" if isinstance(value, float) else f"{label} {value}")
        if getattr(meta, "pattern", None):
            bits.append(f"matches `{meta.pattern}`")
    return ", ".join(bits)


def _notes(info: object) -> str:
    text, limits = " ".join((info.description or "").split()), _limits(info)
    return f"{text} ({limits})" if text and limits else text or (limits[0].upper() + limits[1:] if limits else "")


def _default(info: object) -> str:
    if info.is_required():
        return "required"
    default = info.default
    if default is None or default is PydanticUndefined:
        return ""
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    if isinstance(default, str):
        return f"`{default}`" if default else ""
    return f"`{default}`"


def _rows(model: type) -> list[tuple[str, str, str, str]]:
    rows = []
    for name, info in model.model_fields.items():
        key = info.alias or name
        rows.append((key, _type(info.annotation), _default(info), _notes(info)))
    return rows


def _table(rows: list[tuple[str, str, str, str]], out: list[str]) -> None:
    out += ["| Key | Type | Default | Notes |", "| --- | --- | --- | --- |"]
    out += [f"| `{k}` | {_cell(t)} | {_cell(d)} | {_cell(n)} |" for k, t, d, n in rows]
    out.append("")


def render_block() -> str:
    from superfarm.automation.flows import _KINDS, _WHOLE_VALUE, Condition
    from superfarm.automation.profiles import AppProfile, OverlayRule
    from superfarm.automation.selectors import Selector

    out: list[str] = [BEGIN, ""]

    out += ["### Selectors", "",
            "A selector describes one element on the live screen. It is a mapping with any of these keys, a bare string "
            "(exact visible text, or `id:`, `text:`, `desc:` and `regex:` prefixes as a shorthand), or `@name` to use a "
            "role taught in the app's profile. Text values may contain `{variables}`. A selector needs at least one of "
            "the identifying or flag keys, and `text` and `text_regex` cannot be combined.", ""]
    _table(_rows(Selector), out)

    out += ["### Conditions", "",
            "A condition says what must be true on screen. A bare selector means `visible`. A condition names exactly one of these keys.", ""]
    _table(_rows(Condition), out)

    out += ["### Steps", "",
            f"A step names exactly one action: {', '.join(f'`{k}`' for k in _KINDS)}. A step that names two, or a key "
            "the action does not accept, is refused when the flow is loaded, with its position. `back` and `home` can be "
            "written as a bare word (`- home`). These keys work beside every action:", "",
            "| Key | Type | Notes |", "| --- | --- | --- |"]
    out += [f"| `{k}` | {t} | {_cell(n)} |" for k, t, n in COMMON]
    out.append("")

    for kind, (model, bare, options) in _KINDS.items():
        out += [f"#### {kind}", ""]
        whole = kind in _WHOLE_VALUE
        fields = model.model_fields
        if whole:
            info = fields[bare]
            out.append(f"The value is a {_type(info.annotation)}: `{kind}: <{_type(info.annotation)}>`.")
        elif not fields:
            out.append(f"Takes no arguments: `- {kind}`.")
        elif bare is None:
            out.append(f"The value is a mapping: `{kind}: {{key: value}}`.")
        else:
            out.append(f"The bare value is `{bare}`: `{kind}: <{bare}>`. A mapping with the keys below also works.")
        out.append("")
        if not whole and fields:
            _table(_rows(model), out)
        beside = []
        inside = {i.alias or n for n, i in fields.items()}
        for key in options:
            if not whole and key in inside:
                continue                      # already in the table above
            if key in STEP_LISTS:
                beside.append((key, "list of steps", "", STEP_LISTS[key]))
                continue
            match = next(((n, i) for n, i in fields.items() if (i.alias or n) == key), None)
            if match:
                name, info = match
                beside.append((key, _type(info.annotation), _default(info), _notes(info)))
        also = [k for k in options if not whole and k in inside]
        if also:
            out += ["These keys may also be written beside the action, as in the shipped examples: "
                    + ", ".join(f"`{k}`" for k in also) + ".", ""]
        if beside:
            out += ["Keys beside the action:", ""]
            _table(beside, out)

    out += ["### Pop-up rules", "",
            "`overlays` in a flow, and in an app profile, are pop-up rules. A rule fires only when its `guard` element and its "
            "`dismiss` control are both on screen, or when `key: back` is used with a guard that has an `id`.", ""]
    _table(_rows(OverlayRule), out)

    out += ["### Top-level keys of a flow", "",
            "| Key | Meaning |", "| --- | --- |",
            "| `name` | Flow name. Lower-case letters, digits and `-`, up to 64 characters. |",
            "| `description` | Free text, up to 2000 characters. |",
            "| `package` | App the flow is for. Its profile (roles, pop-up rules, never-tap list) applies. |",
            "| `variables` | Names with a default; `null` means the run must supply it. A default for a name that looks like a secret is refused. |",
            "| `overlays` | Pop-up rules, see above. They are added to the profile's. |",
            "| `never_tap` | Selectors no step may tap. Added to the profile's. |",
            "| `on_unknown` | `stop` (default): a screen nothing covers stops this Android and asks a person. `continue`: skip the step. |",
            "| `timeouts` | `step` (seconds, up to 3600) and `run` (seconds, up to 86400). |",
            "| `steps` | The steps. At least one, at most 2000, nested at most 12 levels. |", ""]

    out += ["### App profile keys", "",
            "A profile belongs to one package and is stored as `automation/profiles/<package>.yaml`.", ""]
    _table(_rows(AppProfile), out)
    out += [END]
    return "\n".join(out)


def apply(text: str) -> str:
    if BEGIN not in text or END not in text:
        raise SystemExit(f"the guide is missing the marker lines:\n{BEGIN}\n{END}")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return head + render_block() + tail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the flow step reference inside docs/AUTOMATION.md.")
    ap.add_argument("--check", metavar="FILE", help="exit 1 when the block in FILE is not current")
    ap.add_argument("file", nargs="?", default=str(ROOT / "docs" / "AUTOMATION.md"))
    args = ap.parse_args(argv)
    path = Path(args.check or args.file)
    text = path.read_text(encoding="utf-8")
    new = apply(text)
    if args.check:
        if new != text:
            print(f"{path} is out of date: run  python scripts/gen_flow_docs.py", file=sys.stderr)
            return 1
        return 0
    path.write_text(new, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

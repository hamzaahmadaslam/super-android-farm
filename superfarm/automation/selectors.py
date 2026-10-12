"""Finding an element on a live screen: two matchers for two jobs.

**Scoring** is for trained programs. A recorded element is evidence, not a contract:
an app can change a label, reuse one id for every row of a list, or draw the same
view one level deeper. Demanding that every recorded field match makes a step fail
on a screen a person would have no trouble with, so each kind of evidence adds to a
score and the best element above a threshold wins.

    same resource id 5 | same text 4 | same description 3 | same path 3 |
    same class 0.5     | near the recorded spot, up to 1  | act at 3 or more

**Exact selectors** are for hand-written flows and app profiles, where the author
says precisely what to look for and a near miss must not be acted on:

    {id, text, text_regex, desc, cls, package, editable, clickable, focused,
     checked, selected, enabled, index, within: <selector>}

Selector strings may contain {variables}; a selector written as "@name" refers to a
role taught in the app's profile.
"""
from __future__ import annotations

import re
from typing import Any
from collections.abc import Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ui import visible

W_ID, W_TEXT, W_DESC, W_PATH, W_CLASS, W_NEAR = 5.0, 4.0, 3.0, 3.0, 0.5, 1.0
DEFAULT_THRESHOLD = 3.0
NEAR_PIXELS = 400.0          # beyond this, position adds nothing

Lookup = Callable[[str], "str | None"]
_VARIABLE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_.]*)\}")
_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


class MissingVariable(KeyError):
    """A {variable} in a flow or selector has no value."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return (f"no value for {{{self.name}}}: define it under the flow's `variables`, pass it when "
                "starting the run, or add it to the shared values")


# ------------------------------------------------------------------ scoring
def _tail(path: str, depth: int) -> str:
    return "/".join((path or "").split("/")[-depth:])


def wanted_id(selector: dict) -> str:
    # Elements recorded before the short id existed only carry the package-qualified
    # resource id; its last segment is the same thing.
    return str(selector.get("id") or str(selector.get("resource_id") or "").rsplit("/", 1)[-1])


def score(node: dict, selector: dict) -> float:
    """How well a live element answers to a recorded one."""
    if not selector or not node.get("enabled", True) or not visible(node):
        return 0.0
    total = 0.0
    want = wanted_id(selector)
    if want:
        if want == node.get("id"):
            total += W_ID
        elif want.casefold() == (node.get("id") or "").casefold():
            # Not "contains": `title` inside `rate_title` is a different element, and
            # at 3 points a substring would be enough on its own to act on.
            total += 3.0
        else:
            total -= 1.0           # both sides have an id and they differ: evidence against
    text = selector.get("text")
    if text:
        node_text = node.get("text") or ""
        if text == node_text:
            total += W_TEXT
        elif text.casefold() == node_text.casefold():
            total += 3.5
        elif node_text and text.casefold() in node_text.casefold():
            total += 1.5
    desc = selector.get("desc")
    if desc:
        node_desc = node.get("desc") or ""
        if desc == node_desc:
            total += W_DESC
        elif desc.casefold() == node_desc.casefold():
            total += 2.5
    path = selector.get("path")
    if path:
        if path == node.get("path"):
            total += W_PATH
        elif _tail(path, 2) == _tail(node.get("path", ""), 2):
            total += 1.5
    if selector.get("cls") and selector["cls"] == node.get("cls"):
        total += W_CLASS
    return total


def label(selector: dict) -> str:
    return str(wanted_id(selector) or selector.get("text") or selector.get("desc")
               or selector.get("path") or selector.get("cls") or "?")


def find_scored(nodes: list[dict], candidates: Iterable[dict], *, near: list | tuple | None = None,
                threshold: float = DEFAULT_THRESHOLD) -> tuple[dict | None, str]:
    """The element a step means, out of everything on screen.

    `candidates` is the step's own selector followed by the alternatives the
    recordings saw. Among equals, the one nearest to where it was recorded wins:
    two "Go" labels on one screen are told apart by where the person tapped.
    """
    best, best_score, best_from = None, 0.0, {}
    for selector in candidates:
        if not selector:
            continue
        for node in nodes:
            value = score(node, selector)
            if value <= 0:
                continue
            if near and node.get("center"):
                distance = max(abs(node["center"][0] - near[0]), abs(node["center"][1] - near[1]))
                value += max(0.0, W_NEAR * (1.0 - distance / NEAR_PIXELS))
            if value > best_score:
                best, best_score, best_from = node, value, selector
    if best is None:
        return None, "not found"
    if best_score < threshold:
        return None, f"best match {label(best_from)!r} scored {best_score:.1f}, below {threshold:.1f}"
    return best, f"{label(best_from)!r} (score {best_score:.1f})"


# ---------------------------------------------------------------- templating
def render(template: str, lookup: Lookup | None, *, escape: Callable[[str], str] | None = None) -> str:
    """Fill {name} placeholders. `{{` and `}}` are literal braces.

    Anything in braces that is not a plain name (a regex quantifier like `\\d{3}`)
    is left exactly as written.
    """
    if "{" not in template and "}" not in template:
        return template

    def fill(match: re.Match) -> str:
        if match.group(0) == "{{":
            return "{"
        if match.group(0) == "}}":
            return "}"
        name = match.group(1)
        value = lookup(name) if lookup else None
        if value is None:
            raise MissingVariable(name)
        return escape(str(value)) if escape else str(value)

    return _VARIABLE.sub(fill, template)


def variables_in(template: str) -> list[str]:
    return [m.group(1) for m in _VARIABLE.finditer(template or "") if m.group(1)]


# ----------------------------------------------------------- exact selectors
_FLAGS = ("editable", "clickable", "focused", "checked", "selected")
_STRINGS = ("id", "text", "text_regex", "desc", "cls", "package")


class Selector(BaseModel):
    """An exact description of one element (or a few) on screen."""
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(None, max_length=300, description="Resource id: the short name (`title`) or the full `package:id/title`.")
    text: str | None = Field(None, max_length=500, description="Exact visible text.")
    text_regex: str | None = Field(None, max_length=500, description="Regular expression the whole visible text must match.")
    desc: str | None = Field(None, max_length=500, description="Exact content description (accessibility label).")
    cls: str | None = Field(None, max_length=200, description="Class: short (`Button`) or full (`android.widget.Button`).")
    package: str | None = Field(None, max_length=200, description="Package the element belongs to.")
    editable: bool | None = None
    clickable: bool | None = None
    focused: bool | None = None
    checked: bool | None = None
    selected: bool | None = None
    enabled: bool | None = Field(None, description="Default: only enabled elements match. `false` matches only disabled ones.")
    index: int | None = Field(None, ge=-50, le=200, description="Which match to use when several fit (0 = first, -1 = last).")
    within: Selector | None = Field(None, description="Only elements inside one that matches this selector.")
    role: str | None = Field(None, description="Name of a selector taught in the app profile (written `@name`).")

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("@"):
                return {"role": text[1:]}
            for prefix, key in (("id:", "id"), ("text:", "text"), ("desc:", "desc"), ("regex:", "text_regex")):
                if text.startswith(prefix):
                    return {key: text[len(prefix):].strip()}
            return {"text": text}
        return value

    @model_validator(mode="after")
    def _sane(self) -> Selector:
        if self.role is not None:
            if not _ROLE_NAME.match(self.role):
                raise ValueError(f"bad role name {self.role!r}")
            if any(getattr(self, k) is not None for k in (*_STRINGS, *_FLAGS, "enabled")):
                raise ValueError("a role reference (@name) can only be combined with `index` and `within`")
            return self
        for key in _STRINGS:
            if getattr(self, key) == "":
                raise ValueError(f"selector `{key}` is empty; leave it out instead")
        if not any(getattr(self, k) is not None for k in (*_STRINGS, *_FLAGS)):
            raise ValueError("a selector needs at least one of: " + ", ".join((*_STRINGS, *_FLAGS)))
        if self.text is not None and self.text_regex is not None:
            raise ValueError("use either `text` or `text_regex`, not both")
        if self.text_regex is not None and not variables_in(self.text_regex):
            try:
                re.compile(self.text_regex)
            except re.error as e:
                raise ValueError(f"text_regex {self.text_regex!r} is not a valid regular expression: {e}") from None
        return self

    # -- helpers -----------------------------------------------------------
    def describe(self) -> str:
        if self.role:
            return f"@{self.role}"
        parts = [f"{k}={getattr(self, k)!r}" for k in _STRINGS if getattr(self, k) is not None]
        parts += [f"{k}={getattr(self, k)}" for k in (*_FLAGS, "index") if getattr(self, k) is not None]
        if self.within is not None:
            parts.append(f"within({self.within.describe()})")
        return ", ".join(parts)

    def identifying(self) -> bool:
        """Whether it names something specific (an id, text or description) rather
        than only a kind of element."""
        return any(getattr(self, k) for k in ("id", "text", "text_regex", "desc"))

    def with_roles(self, roles: dict[str, Selector] | None) -> Selector:
        """Replace `@name` references by the selectors the app profile holds."""
        within = self.within.with_roles(roles) if self.within is not None else None
        if self.role is None:
            return self if within is self.within else self.model_copy(update={"within": within})
        taught = (roles or {}).get(self.role)
        if taught is None:
            raise ValueError(f"role @{self.role} has not been taught for this app; open the app's profile and "
                             "assign it from a live screen")
        taught = taught.with_roles(roles) if taught.role or taught.within else taught
        update: dict[str, Any] = {}
        if self.index is not None:
            update["index"] = self.index
        if within is not None:
            update["within"] = within
        return taught.model_copy(update=update) if update else taught

    def rendered(self, lookup: Lookup | None) -> Selector:
        """Fill {variables} in every string field."""
        update: dict[str, Any] = {}
        for key in _STRINGS:
            value = getattr(self, key)
            if value is not None and "{" in value:
                update[key] = render(value, lookup, escape=re.escape if key == "text_regex" else None)
        if self.within is not None:
            inner = self.within.rendered(lookup)
            if inner is not self.within:
                update["within"] = inner
        return self.model_copy(update=update) if update else self

    def to_plain(self) -> dict:
        return self.model_dump(exclude_none=True)


def _matches(node: dict, selector: Selector) -> bool:
    if not visible(node):
        return False
    if node.get("enabled", True) != (True if selector.enabled is None else selector.enabled):
        return False
    if selector.id is not None:
        # A full `package:id/name` is compared whole; a short name against the short id.
        field = "resource_id" if ("/" in selector.id or ":" in selector.id) else "id"
        if node.get(field) != selector.id:
            return False
    if selector.text is not None and node.get("text") != selector.text:
        return False
    if selector.text_regex is not None:
        try:
            if re.fullmatch(selector.text_regex, (node.get("text") or "")[:2000], re.DOTALL) is None:
                return False
        except re.error:
            return False
    if selector.desc is not None and node.get("desc") != selector.desc:
        return False
    if selector.cls is not None and selector.cls not in (node.get("cls"), node.get("full_class")):
        return False
    if selector.package is not None and node.get("package") != selector.package:
        return False
    for flag in _FLAGS:
        wanted = getattr(selector, flag)
        if wanted is not None and bool(node.get(flag)) != wanted:
            return False
    return True


def match(nodes: list[dict], selector: Selector) -> list[dict]:
    """Every element the selector describes, in screen-tree order. Exact: never a
    substring, never a nearby position."""
    if selector.role is not None:
        raise ValueError(f"role @{selector.role} was not resolved against an app profile")
    pool = nodes
    if selector.within is not None:
        prefixes = tuple(a["path"] + "/" for a in match(nodes, selector.within))
        pool = [n for n in nodes if n["path"].startswith(prefixes)] if prefixes else []
    found = [n for n in pool if _matches(n, selector)]
    if selector.index is None:
        return found
    try:
        return [found[selector.index]]
    except IndexError:
        return []


def find(nodes: list[dict], selector: Selector) -> dict | None:
    found = match(nodes, selector)
    return found[0] if found else None


def selector_from_node(node: dict) -> dict:
    """The exact selector that best names a node picked on a live screen.

    The id is the most stable thing an element has; text is added only when there is
    no id, or the id is shared (every row of a list) and the label is what tells
    this element apart - the caller decides that by keeping or dropping `text`.
    """
    selector: dict[str, Any] = {}
    if node.get("id"):
        selector["id"] = node["id"]
    if node.get("editable"):
        selector["editable"] = True       # a field's text is its content, never its identity
    elif node.get("text"):
        selector["text"] = node["text"]
    if node.get("desc") and "id" not in selector and "text" not in selector:
        selector["desc"] = node["desc"]         # the last resort: descriptions get translated
    if not any(k in selector for k in ("id", "text", "desc")):
        raise ValueError("this element has no id, text or description, so nothing stable can name it")
    return selector


def forbidden_at(nodes: list[dict], x: int, y: int, never_tap: Iterable[Selector]) -> dict | None:
    """The never-tap element a tap at (x, y) would hit, or None.

    A tap is delivered to the innermost clickable view under the point, so a
    forbidden label is also hit by tapping anywhere else on the row or button that
    contains it. Both cases are refused.
    """
    never = list(never_tap)
    if not never:
        return None
    under = [n for n in nodes if visible(n) and n["bounds"][0] <= x <= n["bounds"][2]
             and n["bounds"][1] <= y <= n["bounds"][3]]
    clickable = sorted((n for n in under if n.get("clickable")),
                       key=lambda n: (n["bounds"][2] - n["bounds"][0]) * (n["bounds"][3] - n["bounds"][1]))
    target = clickable[0] if clickable else None
    for selector in never:
        for node in match(nodes, selector):
            if any(node is u for u in under):
                return node
            if target is not None and node["path"].startswith(target["path"] + "/"):
                return node
    return None

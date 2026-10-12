"""App profiles: what the farm has been taught about one app.

A profile belongs to a package and holds three things that every flow and trained
program for that app shares:

  * **roles** - named exact selectors ("search_button", "message_field"). Flows refer
    to them as `@search_button`, so when an app update renames an id, one role is
    re-taught and every flow keeps working.
  * **overlays** - pop-ups to clear whenever they appear. A rule needs an exact
    *guard* element and the *dismiss* control; it fires only when both are on
    screen. (A text-only guard once matched a permanent label on the page behind and
    dismissed "it" forever.)
  * **never_tap** - elements no run may ever tap, whatever a recording, a flow or a
    model says: "Delete account", "Buy now".

Roles are taught from a live screen: read the device's elements, pick one, give it a
name. Nothing here is specific to any app; a profile is data.

    automation/profiles/<package>.yaml
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..util.files import inside, locked, write_yaml
from . import plainyaml
from .selectors import _ROLE_NAME, Selector, selector_from_node
from .store import check_package
from .ui import interesting


class OverlayRule(BaseModel):
    """A pop-up and how to close it."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=60, description="What this pop-up is, for logs.")
    guard: Selector = Field(..., description="Element that proves the pop-up is showing.")
    dismiss: Selector | None = Field(None, description="Control that closes it. Tapped only while the guard is also visible.")
    key: Literal["back"] | None = Field(None, description="Close with the Back key instead, for a pop-up with no close control.")
    max_dismissals: int = Field(2, ge=1, le=10, description="Attempts per step before the run stops instead of looping.")

    @model_validator(mode="after")
    def _one_way_out(self) -> OverlayRule:
        if (self.dismiss is None) == (self.key is None):
            raise ValueError(f"overlay {self.name!r} needs exactly one of `dismiss` (a selector) or `key: back`")
        if self.guard.role is None and not self.guard.identifying():
            raise ValueError(f"overlay {self.name!r}: the guard must name an element by id, text or description")
        # Back has no second control to confirm the pop-up is really there, so its
        # guard has to be something a normal page would not also show: an id.
        if self.key is not None and self.guard.role is None and not self.guard.id:
            raise ValueError(f"overlay {self.name!r}: a pop-up closed with Back needs a guard with an `id` "
                             "(text alone can also be a permanent label on the page behind it)")
        return self


class AppProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: str
    roles: dict[str, Selector] = Field(default_factory=dict)
    overlays: list[OverlayRule] = Field(default_factory=list)
    never_tap: list[Selector] = Field(default_factory=list)
    notes: str = Field("", max_length=4000)

    @model_validator(mode="after")
    def _check(self) -> AppProfile:
        check_package(self.package)
        for name, selector in self.roles.items():
            if not _ROLE_NAME.match(name):
                raise ValueError(f"bad role name {name!r}: use letters, digits, _ and -")
            if selector.role is not None:
                raise ValueError(f"role {name!r} cannot be defined as another role")
        names = [o.name for o in self.overlays]
        if len(names) != len(set(names)):
            raise ValueError("two overlays share a name")
        return self

    def resolved_overlays(self) -> list[OverlayRule]:
        return [o.model_copy(update={"guard": o.guard.with_roles(self.roles),
                                     "dismiss": o.dismiss.with_roles(self.roles) if o.dismiss else None})
                for o in self.overlays]

    def resolved_never_tap(self) -> list[Selector]:
        return [s.with_roles(self.roles) for s in self.never_tap]


class Profiles:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def _file(self, package: str) -> Path:
        return inside(self.root, self.root / f"{check_package(package)}.yaml")

    # -- reading -----------------------------------------------------------
    def list(self) -> list[str]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.glob("*.yaml")):
            try:
                out.append(check_package(path.stem))
            except ValueError:
                continue
        return out

    def load(self, package: str) -> AppProfile:
        """The profile of a package; an empty one when nothing was taught yet."""
        if not package:
            raise ValueError("no package given")
        try:
            data = plainyaml.read(self._file(package), None)
        except yaml.YAMLError as e:
            raise ValueError(f"the profile of {package} is not valid ({getattr(e, 'problem', None) or e}); fix or "
                             f"delete {self._file(package).name} in the automation/profiles folder") from None
        if not isinstance(data, dict):
            return AppProfile(package=package)
        try:
            return AppProfile.model_validate({**data, "package": package})
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise ValueError(f"the profile of {package} is not valid ({where}: {first['msg']}); fix or delete "
                             f"{self._file(package).name} in the automation/profiles folder") from None

    def get(self, package: str) -> AppProfile | None:
        """Like load(), but None for no package at all (a flow that names none)."""
        return self.load(package) if package else None

    # -- writing -----------------------------------------------------------
    def save(self, profile: AppProfile) -> AppProfile:
        profile = AppProfile.model_validate(profile.model_dump())       # re-check after edits
        path = self._file(profile.package)
        with self._lock, locked(path):
            write_yaml(path, profile.model_dump(exclude_none=True, exclude_defaults=False))
        return profile

    def delete(self, package: str) -> bool:
        path = self._file(package)
        with self._lock:
            if not path.is_file():
                return False
            path.unlink()
            return True

    def _edit(self, package: str, change) -> AppProfile:
        with self._lock:
            profile = self.load(package)
            data = profile.model_dump(exclude_none=True)
            change(data)
            try:
                return self.save(AppProfile.model_validate(data))
            except ValidationError as e:
                raise ValueError(e.errors()[0]["msg"]) from None

    def set_role(self, package: str, role: str, selector: dict | str) -> AppProfile:
        if not _ROLE_NAME.match(role or ""):
            raise ValueError(f"bad role name {role!r}: use letters, digits, _ and -")
        return self._edit(package, lambda d: d.setdefault("roles", {}).__setitem__(role, selector))

    def remove_role(self, package: str, role: str) -> AppProfile:
        return self._edit(package, lambda d: d.get("roles", {}).pop(role, None))

    def teach(self, package: str, role: str, node: dict, *, with_text: bool = True) -> dict:
        """Give a role to an element picked on a live screen. Returns the selector.

        `with_text=False` drops the label from the selector, for an element whose id
        is stable but whose text changes (a counter, a localised caption).
        """
        if node.get("package") and node["package"] != package:
            raise ValueError(f"that element belongs to {node['package']}, not {package}; a role must be taught "
                             "on the app's own screen")
        selector: dict[str, Any] = selector_from_node(node)
        if not with_text and "id" in selector:
            selector.pop("text", None)
        self.set_role(package, role, selector)
        return selector

    def add_overlay(self, package: str, rule: dict) -> AppProfile:
        def change(data: dict) -> None:
            overlays = [o for o in data.get("overlays", []) if o.get("name") != rule.get("name")]
            data["overlays"] = [*overlays, rule]
        return self._edit(package, change)

    def remove_overlay(self, package: str, name: str) -> AppProfile:
        return self._edit(package, lambda d: d.__setitem__(
            "overlays", [o for o in d.get("overlays", []) if o.get("name") != name]))

    def add_never_tap(self, package: str, selector: dict | str) -> AppProfile:
        return self._edit(package, lambda d: d.setdefault("never_tap", []).append(selector))

    def remove_never_tap(self, package: str, index: int) -> AppProfile:
        def change(data: dict) -> None:
            items = data.get("never_tap", [])
            if not 0 <= index < len(items):
                raise ValueError(f"no never-tap entry {index}")
            del items[index]
        return self._edit(package, change)


def screen_elements(nodes: list[dict], limit: int = 120) -> list[dict]:
    """The elements of a live screen worth assigning a role to, for the panel.

    Each comes with the selector teaching would store, or the reason it cannot be
    taught (nothing stable to name it by).
    """
    out = []
    for number, node in enumerate(interesting(nodes, limit)):
        entry = {"n": number, "id": node["id"], "text": node["text"][:120], "desc": node["desc"][:120],
                 "cls": node["cls"], "package": node["package"], "bounds": node["bounds"],
                 "clickable": node["clickable"], "editable": node["editable"], "focused": node["focused"]}
        try:
            entry["selector"] = selector_from_node(node)
        except ValueError as e:
            entry["selector"] = None
            entry["why_not"] = str(e)
        out.append(entry)
    return out

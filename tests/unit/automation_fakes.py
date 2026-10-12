"""Stand-ins shared by the automation unit tests: a scripted device and screen builders.

Screens are written as nested elements and turned into real uiautomator XML, then
parsed by the production parser - so every test also exercises the node model the
real device produces. FakeDevice plays those screens back and moves between them
when the code under test taps, types or presses keys.
"""
from __future__ import annotations

import copy
import threading
from html import escape
from types import SimpleNamespace

from PIL import Image

from superfarm.automation.ui import UiError, parse_hierarchy
from superfarm.config import AutomationConfig

PACKAGE = "com.example.app"


def el(cls: str = "android.widget.TextView", *, id: str = "", text: str = "", desc: str = "",
       bounds: tuple[int, int, int, int] = (0, 0, 10, 10), clickable: bool = False, focused: bool = False,
       enabled: bool = True, password: bool = False, scrollable: bool = False, package: str = PACKAGE,
       children: tuple | list = ()) -> dict:
    return {"cls": cls, "id": id, "text": text, "desc": desc, "bounds": bounds, "clickable": clickable,
            "focused": focused, "enabled": enabled, "password": password, "scrollable": scrollable,
            "package": package, "children": list(children)}


def button(id: str = "", text: str = "", bounds: tuple[int, int, int, int] = (0, 0, 10, 10), **kw) -> dict:
    return el("android.widget.Button", id=id, text=text, bounds=bounds, clickable=True, **kw)


def field(id: str = "", text: str = "", bounds: tuple[int, int, int, int] = (0, 0, 10, 10), **kw) -> dict:
    return el("android.widget.EditText", id=id, text=text, bounds=bounds, clickable=True, **kw)


def to_xml(*roots: dict) -> str:
    def one(node: dict, index: int) -> str:
        resource = node["id"]
        if resource and ":" not in resource:
            resource = f"{node['package']}:id/{resource}"
        left, top, right, bottom = node["bounds"]
        attrs = {"index": index, "text": node["text"], "resource-id": resource, "class": node["cls"],
                 "package": node["package"], "content-desc": node["desc"],
                 "clickable": str(node["clickable"]).lower(), "enabled": str(node["enabled"]).lower(),
                 "focused": str(node["focused"]).lower(), "password": str(node["password"]).lower(),
                 "scrollable": str(node["scrollable"]).lower(), "bounds": f"[{left},{top}][{right},{bottom}]"}
        inner = "".join(one(child, i) for i, child in enumerate(node["children"]))
        return "<node " + " ".join(f'{k}="{escape(str(v), quote=True)}"' for k, v in attrs.items()) + f">{inner}</node>"

    body = "".join(one(root, i) for i, root in enumerate(roots))
    return f"<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">{body}</hierarchy>"


def screen(*items: dict, package: str = PACKAGE) -> list[dict]:
    """Nodes of a screen whose elements sit inside one full-screen frame."""
    root = el("android.widget.FrameLayout", bounds=(0, 0, 480, 854), package=package, children=items)
    return parse_hierarchy(to_xml(root))


def config(**changes) -> AutomationConfig:
    """Automation settings that keep tests fast and deterministic."""
    base = {"settle_seconds": 0.0, "human_jitter": False, "step_timeout": 1, "run_timeout": 30, "read_timeout": 5}
    return AutomationConfig(**{**base, **changes})


class FakeDevice:
    """A device made of named screens.

    `taps` maps (screen name, id-or-text of the tapped element) to the next screen;
    `keys` maps (screen name, key) the same way. Everything sent is kept in `sent`.
    """

    def __init__(self, screens: dict[str, list[dict]], start: str, *, taps: dict | None = None,
                 keys: dict | None = None, size: tuple[int, int] = (480, 854), package: str = PACKAGE,
                 activity: str = ".Main"):
        self.serial = "fake:5555"
        self.screens = screens
        self.screen = start
        self.taps = taps or {}
        self.keys = keys or {}
        self._size = size
        self.package, self.activity = package, activity
        self.sent: list[tuple] = []
        self.reads = 0
        self.fail_reads = 0              # the next N reads raise UiError
        self.read_delay = 0.0
        self.closed = False
        self.image = Image.new("RGB", size, (30, 30, 30))
        self.on_text = None              # callable(device, value)
        self.launched: list[str] = []
        self.lock = threading.Lock()

    # -- reading -----------------------------------------------------------
    def nodes(self, *, timeout: float | None = None, cancel: threading.Event | None = None) -> list[dict]:
        self.reads += 1
        if self.read_delay:
            if cancel is not None and cancel.wait(self.read_delay):
                from superfarm.automation.ui import Cancelled
                raise Cancelled()
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise UiError("could not read the screen (scripted failure)")
        return copy.deepcopy(self.screens[self.screen])

    def size(self) -> tuple[int, int]:
        return self._size

    def current(self) -> tuple[str, str]:
        return self.package, self.package + self.activity

    def screenshot(self) -> Image.Image:
        return self.image.copy()

    # -- input -------------------------------------------------------------
    def _hit(self, x: int, y: int) -> dict | None:
        under = [n for n in self.screens[self.screen] if n["bounds"][0] <= x <= n["bounds"][2]
                 and n["bounds"][1] <= y <= n["bounds"][3] and (n["id"] or n["text"])]
        return min(under, key=lambda n: (n["bounds"][2] - n["bounds"][0]) * (n["bounds"][3] - n["bounds"][1]),
                   default=None)

    def tap(self, x: int, y: int) -> None:
        hit = self._hit(x, y)
        self.sent.append(("tap", x, y, (hit or {}).get("id") or (hit or {}).get("text") or ""))
        for name in ((hit or {}).get("id"), (hit or {}).get("text")):
            if name and (self.screen, name) in self.taps:
                self.screen = self.taps[(self.screen, name)]
                return

    def long_press(self, x: int, y: int, ms: int = 700) -> None:
        self.sent.append(("long_press", x, y, ms))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.sent.append(("swipe", x1, y1, x2, y2))
        direction = "up" if y2 < y1 else "down" if y2 > y1 else "left" if x2 < x1 else "right"
        if (self.screen, f"swipe_{direction}") in self.keys:
            self.screen = self.keys[(self.screen, f"swipe_{direction}")]

    def key(self, name: str) -> None:
        self.sent.append(("key", str(name).lower()))
        if (self.screen, str(name).lower()) in self.keys:
            self.screen = self.keys[(self.screen, str(name).lower())]

    def text(self, value: str) -> None:
        self.sent.append(("text", value))
        if self.on_text is not None:
            self.on_text(self, value)

    def clear_focused(self, length_hint: int = 48) -> None:
        self.sent.append(("clear", length_hint))

    def launch(self, package: str, activity: str | None = None) -> str:
        self.sent.append(("launch", package))
        self.launched.append(package)
        if (self.screen, f"launch:{package}") in self.keys:
            self.screen = self.keys[(self.screen, f"launch:{package}")]
        return f"{package}/.Main"

    def stop_app(self, package: str) -> None:
        self.sent.append(("stop", package))

    def close(self) -> None:
        self.closed = True

    # -- for assertions ----------------------------------------------------
    def did(self, kind: str) -> list[tuple]:
        return [s for s in self.sent if s[0] == kind]

    @property
    def acted(self) -> bool:
        """Whether anything at all was sent to the device."""
        return bool(self.sent)


class FakeManager:
    """The part of the instance manager automation uses."""

    def __init__(self, ids: list[int]):
        self.ids = list(ids)

    def select(self, selector: str) -> list[int]:
        if selector == "all":
            return list(self.ids)
        if selector == "none":
            return []
        out = []
        for part in str(selector).split(","):
            if "-" in part:
                a, b = (int(v) for v in part.split("-"))
                out += [i for i in self.ids if a <= i <= b]
            elif int(part) in self.ids:
                out.append(int(part))
        return out

    def adb(self, instance_id: int):
        return SimpleNamespace(serial=f"fake:{instance_id}")

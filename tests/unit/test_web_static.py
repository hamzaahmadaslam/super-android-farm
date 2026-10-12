"""The web panel as shipped: static, self-contained, and speaking only the documented API.

The panel has no build step and no JavaScript test runner, so these checks read the files:
nothing may load from another host (an air-gapped farm must work), nothing may need a
looser content policy than `default-src 'self'`, every import must exist, and every API
path the panel calls must be one of the routes of the HTTP contract. Where `node` is
installed the files are also parsed and the pure helpers are exercised.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "superfarm" / "web"
JS = sorted((WEB / "js").rglob("*.js"))
CSS = sorted((WEB / "css").glob("*.css"))
TEXT_FILES = [WEB / "index.html", *JS, *CSS, *sorted((WEB / "img").glob("*.svg"))]
SVG_NAMESPACE = "http://www.w3.org/2000/svg"      # an identifier, never fetched

# The HTTP contract (spec, Addendum 5). `{}` is a path parameter, `{a|b}` a fixed set.
CONTRACT = """
/api/health /api/auth/login /api/auth/logout /api/auth/me
/api/system /api/system/capacity /api/system/doctor /api/system/metrics /api/system/gpus
/api/config /api/config/schema
/api/images /api/images/{}/{ensure|build}
/api/instances /api/instances/{} /api/instances/{}/{start|stop|restart|recreate} /api/instances/{}/clone
/api/instances/bulk /api/instances/{}/logs
/api/device-profiles
/api/tasks /api/tasks/{} /api/tasks/{}/cancel
/api/net/profiles /api/net/profiles/{} /api/net/credentials /api/net/credentials/{}
/api/net/gateways /api/net/gateways/{} /api/net/gateways/{}/{up|down} /api/instances/{}/net
/api/apps/library /api/apps/library/{}
/api/apps/{install|ensure|uninstall|launch|stop|clear|enable|disable|grant|revoke}
/api/apps/matrix /api/instances/{}/apps
/api/files/{push|gallery} /api/instances/{}/files /api/instances/{}/files/pull
/api/device/{locale|timezone|geo|proxy|clipboard|animations|dark-mode|font-scale|open-url|wake|reboot}
/api/instances/{}/info /api/instances/{}/screenshot /api/instances/{}/logcat
/api/shell /api/shell/{}/output
/api/instances/{}/frame.jpg /api/instances/{}/stream.mjpg /api/instances/{}/h264
/api/input/{tap|swipe|long-press|text|key}
/api/windows /api/windows/{open|close|grid|close-all}
/api/camera /api/camera/{play|stop|mic|attach|install-hal} /api/camera/sources /api/media
/api/listen /api/listen/{}
/api/backups /api/backups/farm /api/backups/verify /api/backups/restore /api/backups/download
/api/events
/api/approvals /api/approvals/{} /api/approvals/{}/{approve|deny} /api/mcp/info
/api/ai/models /api/ai/models/{}/test
/api/ai/conversations /api/ai/conversations/{} /api/ai/conversations/{}/{messages|cancel}
/api/ai/pilot /api/ai/pilot/sessions /api/ai/pilot/sessions/{} /api/ai/pilot/sessions/{}/{cancel|confirm}
/api/ai/pilot/sessions/{}/devices/{}/resolve /api/ai/pilot/{}/{train|summary}
""".split()
CONTRACT_PREFIXES = ("/api/automation/",)          # "expose every public method of the facade 1:1"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_panel_exists_and_is_small_files_only():
    assert (WEB / "index.html").is_file()
    assert JS and CSS
    other = [p.name for p in WEB.rglob("*") if p.is_file() and p.suffix not in (".html", ".js", ".css", ".svg")]
    assert other == [], f"unexpected file types in the panel: {other}"


# ------------------------------------------------------------------ self-contained

@pytest.mark.parametrize("path", TEXT_FILES, ids=lambda p: str(p.relative_to(WEB)))
def test_no_external_urls(path: Path):
    text = read(path).replace(SVG_NAMESPACE, "")
    found = re.findall(r"""(?:https?:|wss?:)?//[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+[^\s'"`)]*""", text)
    assert found == [], f"{path.name} points at another host: {found}"
    assert "@import" not in text, f"{path.name} uses @import"


@pytest.mark.parametrize("path", JS, ids=lambda p: str(p.relative_to(WEB)))
def test_js_needs_no_looser_content_policy(path: Path):
    text = read(path)
    for pattern, why in (
        (r"\beval\s*\(", "eval"),
        (r"\bnew\s+Function\b", "new Function"),
        (r"\bset(?:Timeout|Interval)\s*\(\s*['\"`]", "a string passed to a timer"),
        (r"\b(?:inner|outer)HTML\b", "HTML built from strings"),
        (r"\binsertAdjacentHTML\b", "HTML built from strings"),
        (r"document\.write", "document.write"),
        (r"setAttribute\(\s*['\"]style['\"]", "an inline style attribute"),
        (r"setAttribute\(\s*['\"]on", "an inline event handler"),
        (r"javascript:", "a javascript: URL"),
    ):
        assert not re.search(pattern, text), f"{path.name} uses {why}"


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_script = False
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag):
        self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline_script = True


def page() -> _Page:
    parser = _Page()
    parser.feed(read(WEB / "index.html"))
    return parser


def test_index_has_no_inline_script_style_or_handlers():
    parsed = page()
    assert not parsed.inline_script, "index.html contains an inline script"
    assert not any(tag == "style" for tag, _ in parsed.tags), "index.html contains a <style> block"
    for tag, attrs in parsed.tags:
        bad = [name for name in attrs if name == "style" or name.startswith("on")]
        assert bad == [], f"<{tag}> has inline {bad}"
        if tag == "script":
            assert attrs.get("type") == "module" and attrs.get("src"), "scripts must be external ES modules"


def test_index_references_only_existing_local_assets():
    seen = 0
    for tag, attrs in page().tags:
        for name in ("src", "href"):
            target = attrs.get(name)
            if not target or target.startswith("#"):
                continue
            seen += 1
            assert not re.match(r"^([a-z]+:|//|/)", target), f"<{tag} {name}={target!r}> is not a relative local path"
            assert (WEB / target).is_file(), f"<{tag} {name}={target!r}> does not exist"
    assert seen >= 5


def test_css_urls_are_local():
    for path in CSS:
        for target in re.findall(r"url\(\s*['\"]?([^'\")]+)", read(path)):
            assert not re.match(r"^([a-z]+:|//)", target), f"{path.name}: url({target})"
            assert (path.parent / target).is_file(), f"{path.name}: url({target}) does not exist"


# ------------------------------------------------------------------------ imports

IMPORT = re.compile(r"""(?:^|\n)\s*(?:import|export)\b[^'"`;]*?\bfrom\s*['"]([^'"]+)['"]|\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)|(?:^|\n)\s*import\s*['"]([^'"]+)['"]""")


def imports_of(path: Path) -> list[str]:
    return [a or b or c for a, b, c in IMPORT.findall(read(path))]


@pytest.mark.parametrize("path", JS, ids=lambda p: str(p.relative_to(WEB)))
def test_every_import_resolves_to_a_file(path: Path):
    for target in imports_of(path):
        assert target.startswith(("./", "../")), f"{path.name} imports {target!r}: only relative modules exist here"
        resolved = (path.parent / target).resolve()
        assert resolved.is_file(), f"{path.name} imports {target!r}, which does not exist"
        assert WEB.resolve() in resolved.parents, f"{path.name} imports {target!r} from outside the panel"


def test_every_module_is_reachable_from_main():
    todo, seen = [WEB / "js" / "main.js"], set()
    while todo:
        current = todo.pop().resolve()
        if current in seen:
            continue
        seen.add(current)
        todo.extend((current.parent / target) for target in imports_of(current))
    unused = [str(p.relative_to(WEB)) for p in JS if p.resolve() not in seen]
    assert unused == [], f"modules nothing imports: {unused}"


def test_every_navigation_link_has_a_page():
    main = read(WEB / "js" / "main.js")
    routes = set(re.findall(r"(\w+): \w+\.render", main))
    links = {attrs["href"][2:] for tag, attrs in page().tags if tag == "a" and (attrs.get("href") or "").startswith("#/")}
    assert links and links <= routes, f"links without a page: {links - routes}"
    for name in routes:
        module = WEB / "js" / "pages" / f"{name}.js"
        assert re.search(r"export (async )?function render\(", read(module)), f"{module.name} has no render()"


# ---------------------------------------------------------------------- API paths

def api_paths(text: str) -> list[str]:
    """Every '/api/...' the source builds, with `${...}` turned into `{}` and any query
    string dropped. `${API}` (a module's own prefix constant) is expanded first."""
    constants = dict(re.findall(r"const (\w+) = '(/api/[^']*)';", text))
    for name, value in constants.items():
        text = text.replace("`${" + name + "}", "`" + value)
    out = []
    for match in re.finditer(r"""(['"`])(/api/)""", text):
        quote, i, path = match.group(1), match.end(), "/api/"
        while i < len(text) and text[i] != quote and text[i] not in "?\n":
            if quote == "`" and text.startswith("${", i):
                depth, j = 1, i + 2
                while depth and j < len(text):
                    depth += {"{": 1, "}": -1}.get(text[j], 0)
                    j += 1
                expression = text[i + 2:j - 1]
                # query(...) appends "?a=b": not part of the path.
                path += "" if expression.startswith("query(") else "{}"
                i = j
            else:
                path += text[i]
                i += 1
        out.append(path)
    return out


def in_contract(path: str) -> bool:
    if path.startswith(CONTRACT_PREFIXES) or path + "/" in CONTRACT_PREFIXES:
        return True
    parts = path.strip("/").split("/")
    for route in CONTRACT:
        wanted = route.strip("/").split("/")
        if len(wanted) != len(parts):
            continue
        for have, want in zip(parts, wanted, strict=True):
            if want == "{}":
                continue
            if want.startswith("{"):
                if have != "{}" and have not in want[1:-1].split("|"):
                    break
            elif have != want:
                break
        else:
            return True
    return False


def test_path_extraction_understands_templates():
    sample = "const API = '/api/automation';\n" \
             "get(`/api/instances/${id}/files/pull${query({ path: e.path })}`); post(`${API}/runs`);\n" \
             "post(`/api/apps/${action}`, {}); get('/api/tasks', { active: true });"
    assert api_paths(sample) == ["/api/automation", "/api/instances/{}/files/pull", "/api/automation/runs",
                                 "/api/apps/{}", "/api/tasks"]
    assert in_contract("/api/apps/{}") and in_contract("/api/apps/install") and not in_contract("/api/apps/nope")
    assert in_contract("/api/instances/{}/stream.mjpg") and not in_contract("/api/instances/{}/nope")
    assert in_contract("/api/automation/runs") and not in_contract("/api/stream/capabilities")


def test_the_panel_calls_only_documented_routes():
    called: dict[str, str] = {}
    for path in JS:
        for found in api_paths(read(path)):
            called.setdefault(found, path.name)
    assert len(called) > 40, "the extraction found suspiciously few API calls"
    unknown = {p: f for p, f in called.items() if not in_contract(p)}
    assert unknown == {}, f"calls to routes the API contract does not have: {unknown}"


def test_mutations_carry_the_csrf_header_and_stay_same_origin():
    api = read(WEB / "js" / "api.js")
    assert "X-SF-CSRF" in api and "credentials: 'same-origin'" in api
    # One wrapper talks to the daemon; no page calls fetch on its own.
    assert "transportRequest(path, init)" in api, "api.js sends through transport.js"
    others = [p.name for p in JS if p.name != "transport.js" and re.search(r"\bfetch\s*\(|XMLHttpRequest", read(p))]
    assert others == [], f"modules bypassing api.js: {others}"
    # The token is typed into the login form and sent once; it is never kept.
    for path in JS:
        assert not re.search(r"(local|session)Storage\.setItem\([^)]*token", read(path), re.I), path.name


def test_the_network_is_named_in_one_module():
    """transport.js is the only module that opens a request, a socket or a media address.
    In the panel it passes straight through to the browser; the public demo replaces this
    one file with a backend that runs in the page (demo/mock/), so its four exports are a
    contract, and nothing else in the panel may reach the network around it."""
    transport = read(WEB / "js" / "transport.js")
    assert set(re.findall(r"^export (?:const|function) (\w+)", transport, re.M)) == {
        "request", "openSocket", "mediaUrl", "streamInto"}
    assert not IMPORT.search(transport), "transport.js stands alone"
    assert "export const request = (path, init) => fetch(path, init);" in transport
    assert "export const mediaUrl = (path) => path;" in transport
    assert not re.search(r"(local|session)Storage|document\.cookie|indexedDB", transport), "a pass-through keeps nothing"
    for path in JS:
        if path.name == "transport.js":
            continue
        text = read(path)
        assert not re.search(r"\bnew\s+WebSocket\b|\bEventSource\b|\bsendBeacon\b", text), f"{path.name} opens a connection past transport.js"
        # A picture or a download the browser loads by itself goes through mediaUrl() / streamInto().
        direct = re.findall(r"(?:\.src\s*=|\bsrc:|\bhref:)\s*[`'\"]/api/[^`'\"]*", text)
        assert direct == [], f"{path.name} hands the browser an API address past transport.js: {direct}"
    pages = "".join(read(p) for p in JS if p.name not in ("api.js", "transport.js"))
    assert "transport.js" not in pages, "pages import api.js, which re-exports the transport"


def _token_blocks() -> list[tuple[str, set[str]]]:
    tokens = read(WEB / "css" / "tokens.css")
    return [(selector.strip(), set(re.findall(r"(--[\w-]+)\s*:", body)))
            for selector, body in re.findall(r"(:root[^{]*)\{([^}]*)\}", tokens)]


def test_themes_define_the_same_tokens():
    """Dark is the base, light is a complete second theme: the light blocks (system
    setting, explicit choice) must redefine every colour token and nothing else."""
    blocks = _token_blocks()
    base = [tokens for selector, tokens in blocks if selector == ":root"]
    assert len(base) == 2, "expected a scale block and a dark colour block"
    scale, dark = base
    assert not scale & dark, f"defined twice: {sorted(scale & dark)}"
    light = [tokens for selector, tokens in blocks if "data-theme" in selector]
    assert len(light) == 2, "expected a system-light block and an explicit light block"
    for tokens in light:
        assert tokens == dark, f"light theme differs: {sorted(tokens ^ dark)}"
    used = set()
    for path in CSS:
        used |= set(re.findall(r"var\((--[\w-]+)", read(path)))
    assert used <= scale | dark, f"undefined custom properties: {sorted(used - (scale | dark))}"


def test_colours_live_in_the_tokens_only():
    """One place to change a theme: no stylesheet but tokens.css holds a raw colour."""
    for path in CSS:
        if path.name == "tokens.css":
            continue
        raw = re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", read(path))
        assert raw == [], f"{path.name} has raw colours: {raw[:5]}"
    names = [p.name for p in CSS]
    linked = [attrs["href"] for tag, attrs in page().tags if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert linked[0] == "css/tokens.css", "tokens.css must load first"
    assert sorted(linked) == sorted(f"css/{name}" for name in names), "every stylesheet is linked, and only those"


# ------------------------------------------------------------------ accessibility

def _theme_colours() -> dict[str, dict[str, str]]:
    tokens = read(WEB / "css" / "tokens.css")
    bodies = [(sel.strip(), body) for sel, body in re.findall(r"(:root[^{]*)\{([^}]*)\}", tokens)]
    dark = next(body for sel, body in bodies[1:] if sel == ":root")
    light = next(body for sel, body in bodies if 'data-theme="light"' in sel)
    return {name: dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)) for name, body in (("dark", dark), ("light", light))}


def _rgba(value: str) -> tuple[tuple[float, float, float], float]:
    value = value.strip()
    if value.startswith("#"):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)), 1.0
    m = re.match(r"rgb\((\d+) (\d+) (\d+)(?: / ([\d.]+)%)?\)", value)
    assert m, f"unexpected colour syntax: {value}"
    return (float(m[1]), float(m[2]), float(m[3])), (float(m[4]) / 100 if m[4] else 1.0)


def _over(top: str, under: tuple[float, float, float]) -> tuple[float, float, float]:
    (r, g, b), a = _rgba(top)
    return tuple(c * a + u * (1 - a) for c, u in zip((r, g, b), under, strict=True))


def _contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    def luminance(colour):
        parts = [c / 255 for c in colour]
        parts = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
        return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_colours_meet_wcag_aa_on_every_surface(theme: str):
    """4.5:1 for every text colour on every surface it is used on, in both themes; state
    colours also on their own tinted badge; 3:1 for the focus ring."""
    t = _theme_colours()[theme]
    page = _rgba(t["--bg"])[0]
    surfaces = {"bg": page, "surface": _over(t["--surface"], page), "solid": _rgba(t["--surface-solid"])[0],
                "surface-2": _rgba(t["--surface-2"])[0], "surface-3": _rgba(t["--surface-3"])[0]}
    low = []
    for text in ("--text", "--muted", "--faint", "--accent", "--ok", "--warn", "--fail", "--info", "--idle"):
        for name, surface in surfaces.items():
            if name == "surface-3" and text not in ("--text", "--muted", "--faint"):
                continue                      # hover fill: only plain text sits on it
            got = _contrast(_rgba(t[text])[0], surface)
            if got < 4.5:
                low.append((text, name, round(got, 2)))
    for kind in ("ok", "warn", "fail", "info", "idle"):
        for name in ("surface", "surface-2"):
            got = _contrast(_rgba(t[f"--{kind}"])[0], _over(t[f"--{kind}-soft"], surfaces[name]))
            if got < 4.5:
                low.append((f"--{kind}", f"its badge on {name}", round(got, 2)))
    for stop in ("--accent-a", "--accent-b"):
        got = _contrast(_rgba(t["--accent-text"])[0], _rgba(t[stop])[0])
        if got < 4.5:
            low.append(("--accent-text", stop, round(got, 2)))
    tinted = _over(t["--accent-soft"], surfaces["surface"])
    for text in ("--text", "--muted"):
        if _contrast(_rgba(t[text])[0], tinted) < 4.5:
            low.append((text, "accent-soft", round(_contrast(_rgba(t[text])[0], tinted), 2)))
    assert low == [], f"{theme}: below 4.5:1: {low}"
    assert _contrast(_rgba(t["--focus"])[0], page) >= 3 and _contrast(_rgba(t["--focus"])[0], surfaces["solid"]) >= 3



def test_motion_and_focus_rules_are_present():
    base = read(WEB / "css" / "base.css")
    reduced = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", base, re.S)
    assert reduced, "animations must stop under prefers-reduced-motion"
    assert "animation: none !important" in reduced.group(1) and "transition: none !important" in reduced.group(1)
    assert ":focus-visible" in base and "outline: 2px solid var(--focus)" in base
    for path in CSS:
        assert not re.search(r"outline:\s*(none|0)\b(?![^}]*:focus-visible)", read(path).replace(
            ":focus:not(:focus-visible) { outline: none; }", "").replace(".main:focus { outline: none; }", "").replace(
            ".palette-input input:focus-visible { outline: none; }", "")), f"{path.name} removes a focus ring"


def test_icon_only_controls_in_the_page_are_labelled():
    for tag, attrs in page().tags:
        if tag == "button" and "icon-only" in (attrs.get("class") or ""):
            assert attrs.get("aria-label"), f"icon-only button {attrs.get('id')} has no aria-label"
        if tag == "img":
            assert "alt" in attrs, "every image states its alt text (empty when decorative)"


# -------------------------------------------------------------------------- icons

def icon_names() -> set[str]:
    source = read(WEB / "js" / "icons.js")
    start = source.index("const PATHS = {")
    table = source[start:source.index("\n};", start)]
    return {quoted or bare for quoted, bare in re.findall(r"^  (?:'([\w-]+)'|(\w+)):", table, re.M)}


def test_every_icon_used_is_drawn():
    known = icon_names()
    assert len(known) > 40
    used: dict[str, str] = {}
    for path in JS:
        text = read(path)
        for name in re.findall(r"\bicon\(\s*'([\w-]+)'", text):
            used.setdefault(name, path.name)
        # button(label, run, kind, 'icon') / linkButton(...) / emptyState('icon', ...) / panel(.., { icon: '..' })
        for name in re.findall(r"\bemptyState\(\s*'([\w-]+)'", text) + re.findall(r"\bicon: '([\w-]+)'", text):
            used.setdefault(name, path.name)
    for _tag, attrs in page().tags:
        if attrs.get("data-icon"):
            used.setdefault(attrs["data-icon"], "index.html")
    missing = {name: where for name, where in used.items() if name not in known}
    assert missing == {}, f"icons used but not drawn in icons.js: {missing}"
    assert "createElementNS" in read(WEB / "js" / "icons.js"), "icons are built as DOM nodes, never parsed from markup"


def test_help_topics_exist_and_quote_the_schema():
    source = read(WEB / "js" / "components" / "help.js")
    known = {quoted or bare for quoted, bare in re.findall(r"^  (?:'([\w-]+)'|(\w+)): \{", source, re.M)}
    assert {"rendering", "network", "killswitch", "capacity", "selector"} <= known
    assert "/api/config/schema" in source, "help text comes from the config schema"
    for path in JS:
        for name in re.findall(r"\bhelp\('([\w-]+)'\)", read(path)) + re.findall(r"\btopic: '([\w-]+)'", read(path)):
            assert name in known, f"{path.name} asks for help topic {name!r}, which help.js does not have"


# ---------------------------------------------------------------------- shortcuts

def test_page_shortcuts_are_unique_and_match_the_navigation():
    keys = [attrs["data-key"] for tag, attrs in page().tags if tag == "a" and attrs.get("data-key")]
    links = [attrs["href"] for tag, attrs in page().tags if tag == "a" and attrs.get("data-key")]
    assert len(keys) == len(set(keys)) == len(links) >= 10, "one distinct letter per page"
    assert all(len(key) == 1 and key.islower() for key in keys)
    shortcuts = read(WEB / "js" / "components" / "shortcuts.js")
    assert ".live-surface" in shortcuts and "dialog[open]" in shortcuts, "single keys must not fire while typing or in a dialog"


# ------------------------------------------------------------------- double press

def test_one_press_one_action_is_built_in_once():
    """A double click must not act twice. The guard lives in the element builder (every
    button and form goes through it), the dialog, and the one function that sends
    requests; pages do not each reinvent it."""
    dom = read(WEB / "js" / "dom.js")
    assert "GUARDED = { button: 'onclick', form: 'onsubmit' }" in dom and "aria-busy" in dom and "aria-disabled" in dom
    assert "export function busy(" in dom and "export function single(" in dom
    api = read(WEB / "js" / "api.js")
    assert "pending.has(key)" in api and "REPEATABLE" in api, "identical changes in flight are sent once; live input may repeat"
    assert re.search(r"REPEATABLE = /\^\\/api\\/\(input", api), "taps and keys are never de-duplicated"
    dialog = read(WEB / "js" / "components" / "dialog.js")
    assert "ARM_MS" in dialog and "if (running ||" in dialog and "? cancel : field).focus()" in dialog
    # Buttons are built with h(), never with createElement, or they would miss the guard.
    for path in JS:
        if path.name in ("dom.js", "icons.js"):
            continue
        assert "createElement(" not in read(path).replace("document.createElement('span')", ""), f"{path.name} builds elements past h()"
    # A handler that fires a request without handing its promise back cannot be guarded.
    for path in JS:
        for line in read(path).splitlines():
            if re.search(r"event\.preventDefault\(\);\s*attempt\(", line):
                raise AssertionError(f"{path.name}: a submit handler must return its promise: {line.strip()[:80]}")


def test_the_approvals_inbox_cannot_be_answered_by_accident():
    page_js = read(WEB / "js" / "pages" / "approvals.js")
    card = read(WEB / "js" / "components" / "approval-card.js")
    # The headline is the daemon's summary; neither the page nor the card words a request itself.
    assert "item.summary" in card and "approval-summary' }, item.summary)" in card
    # Append only, armed after a delay, a second click inside the card for destructive ones.
    assert "list.append(slot.el)" in page_js and "prepend" not in page_js and "insertBefore" not in page_js
    assert "ARM_MS" in card and "approval-confirm" in card and "dataset: action" in card
    for text in (page_js, card):
        assert ".focus(" not in text, "nothing about an approval takes the keyboard focus by itself"
        assert "confirmDialog" not in text and "openDialog" not in text, "the destructive confirmation is inside the card"
    # One card, used by the inbox and by the Assistant's transcript: the same rules in both.
    assert "approvalCard(" in page_js and "approvalCard(" in read(WEB / "js" / "pages" / "assistant.js")
    # No shortcut and no toast answers a request: only the store's two functions call these,
    # and only the card calls the store's functions.
    for path in JS:
        text = read(path)
        if path.name != "approvals.js":
            assert "/approve`" not in text and "/deny`" not in text, f"{path.name} answers approvals"
        if path.name not in ("approvals.js", "approval-card.js"):
            assert not re.search(r"\b(approve|deny)\(", text), f"{path.name} answers approvals"
    shortcuts = read(WEB / "js" / "components" / "shortcuts.js")
    assert "approve" not in shortcuts.lower()
    store = read(WEB / "js" / "approvals.js")
    assert "data.now - Date.now() / 1000" in store, "time left is counted against the server's clock"


def test_model_and_device_text_is_never_markup_and_the_farm_record_is_set_apart():
    assistant = read(WEB / "js" / "pages" / "assistant.js")
    # Rendered as text nodes through h(); the farm's record has its own labelled block.
    assert "farm-record" in assistant and "Written by the farm, not by the model" in assistant
    assert "event.record" in assistant and "untrusted" in assistant and "From the device: untrusted" in assistant
    for name in ("assistant.js", "pilot.js", "ai-models.js"):
        text = read(WEB / "js" / "pages" / name)
        assert not re.search(r"\bfetch\s*\(|new WebSocket|new Image|XMLHttpRequest", text), f"{name} must use api.js and the live component"
    models = read(WEB / "js" / "pages" / "ai-models.js")
    assert "sf ai models key" in models and "type: 'password'" not in models, "keys are stored from the terminal, never typed into the panel"


# ------------------------------------------------------------------------ wording

def test_no_removed_options_or_private_words():
    for path in TEXT_FILES:
        text = read(path)
        lowered = text.lower()
        for word in ("bi" + "go", "clau" + "de", "anthro" + "pic", "ham" + "za"):
            assert word not in lowered, f"{path.name} contains {word!r}"
    form = read(WEB / "js" / "instance-form.js")
    for line in form.splitlines():
        if "device.timezone" in line or "device.geo" in line:
            assert not re.search(r"\bauto\b", line), f"time zone / mock location no longer take 'auto': {line.strip()[:90]}"


def test_capacity_shows_the_assumed_rendering_mode_verbatim():
    common = read(WEB / "js" / "common.js")
    assert "estimate.mode)" in common and "estimate.mode_reason" in common
    assert "estimate.mode ||" not in common, "the mode is shown as the API returns it, with no substitute wording"


# --------------------------------------------------------------------------- node

def _node() -> str | None:
    """A node that loads `.js` files with import/export as modules without a package.json
    (syntax detection, on by default from 22.7). Older ones would misread the panel's files."""
    found = shutil.which("node")
    if not found:
        return None
    try:
        version = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        major, minor = (int(x) for x in version.lstrip("v").split(".")[:2])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return found if (major, minor) >= (22, 7) else None


node = _node()
needs_node = pytest.mark.skipif(node is None, reason="needs node 22.7 or newer")


@needs_node
@pytest.mark.parametrize("path", JS, ids=lambda p: str(p.relative_to(WEB)))
def test_js_parses(path: Path):
    result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


_HELPERS = """
import { parseSelector, selectorOf, bytes, duration, mb, single } from 'BASE/dom.js';
import { diff } from 'BASE/components/form.js';
import { codecFromAnnexB } from 'BASE/components/live.js';
import { parseHash } from 'BASE/router.js';
import { query } from 'BASE/api.js';
const fleet = [
  { id: 1, state: 'booted', tags: ['eu'] }, { id: 2, state: 'stopped', tags: [] },
  { id: 3, state: 'running', tags: ['eu', 'spare'] }, { id: 7, state: 'error', tags: [] },
];
const pick = (s) => [...parseSelector(s, fleet)].sort((a, b) => a - b);
let bad = '';
try { parseSelector('nonsense', fleet); } catch (e) { bad = e.message; }
let runs = 0;
const twice = await Promise.all([single('k', async () => { runs += 1; return 7; }), single('k', async () => { runs += 1; return 8; })]);
const again = await single('k', async () => { runs += 1; return 9; });
console.log(JSON.stringify({
  once: [twice, again, runs],
  all: pick('all'), running: pick('running'), stopped: pick('stopped'), tag: pick('tag:EU'),
  range: pick('1-3'), reversed: pick('3-1'), single: pick('7'), unknown: pick('9'),
  union: pick('1, 7'), minus: pick('all,!tag:spare,!2'), empty: pick(''), bad,
  compact: selectorOf([3, 1, 2, 7, 9, 10]), one: selectorOf([5]), none: selectorOf([]),
  round: pick(selectorOf(pick('all'))),
  bytes: [bytes(0), bytes(1536), bytes(5 * 1024 ** 3)], mb: [mb(512), mb(2048)],
  duration: [duration(5), duration(125), duration(3700), duration(90000)],
  same: diff({ a: 1, b: { c: [1, 2] } }, { a: 1, b: { c: [1, 2] } }) === undefined,
  changed: diff({ a: 1, b: { c: 2, d: 3 }, e: [1] }, { a: 1, b: { c: 5, d: 3 }, e: [1, 2] }),
  removed: diff({ map: { x: { p: 1 }, y: { p: 2 } } }, { map: { x: { p: 1 } } }),
  codec: codecFromAnnexB(new Uint8Array([0, 0, 0, 1, 0x67, 0x42, 0xc0, 0x1f, 0xaa, 0, 0, 1, 0x68, 1])),
  nocodec: codecFromAnnexB(new Uint8Array([0, 0, 1, 0x65, 1, 2, 3, 4, 5])),
  hash: [parseHash('#/android/5/apps'), parseHash(''), parseHash('#/control/1-3%2C7')],
  query: [query({ a: 1, b: '', c: null, d: 'x y' }), query({}), query(null)],
}));
"""


@needs_node
def test_pure_helpers_behave(tmp_path: Path):
    script = tmp_path / "helpers.mjs"
    script.write_text(_HELPERS.replace("BASE", (WEB / "js").as_uri()), encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["once"] == [[7, 7], 9, 2], "the same thing asked twice at once runs once; afterwards it runs again"
    assert got["all"] == [1, 2, 3, 7]
    assert got["running"] == [1, 3] and got["stopped"] == [2, 7]
    assert got["tag"] == [1, 3]
    assert got["range"] == [1, 2, 3] == got["reversed"]
    assert got["single"] == [7] and got["unknown"] == [] and got["empty"] == []
    assert got["union"] == [1, 7]
    assert got["minus"] == [1, 7]
    assert "nonsense" in got["bad"]
    assert got["compact"] == "1-3,7,9-10" and got["one"] == "5" and got["none"] == ""
    assert got["round"] == [1, 2, 3, 7]
    assert got["bytes"] == ["0 B", "1.5 KB", "5.0 GB"] and got["mb"] == ["512 MB", "2.0 GB"]
    assert got["duration"] == ["5s", "2m 5s", "1h 1m", "1d 1h"]
    assert got["same"] is True
    assert got["changed"] == {"b": {"c": 5}, "e": [1, 2]}
    assert got["removed"] == {"map": {"y": None}}
    assert got["codec"] == "avc1.42c01f" and got["nocodec"] is None
    assert got["hash"] == [{"name": "android", "params": ["5", "apps"]}, {"name": "", "params": []},
                           {"name": "control", "params": ["1-3,7"]}]
    assert got["query"] == ["?a=1&d=x+y", "", ""]


def test_the_assistant_page_never_draws_an_event_twice():
    """Sending a message and the poll timer can both fetch "events after N" at the same
    moment. The page must skip events it has already drawn, by their number."""
    source = (WEB / "js" / "pages" / "assistant.js").read_text(encoding="utf-8")
    assert "if (event.n <= last) continue;" in source

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
    others = [p.name for p in JS if p.name != "api.js" and re.search(r"\bfetch\s*\(|XMLHttpRequest", read(p))]
    assert others == [], f"modules bypassing api.js: {others}"
    # The token is typed into the login form and sent once; it is never kept.
    for path in JS:
        assert not re.search(r"(local|session)Storage\.setItem\([^)]*token", read(path), re.I), path.name


def test_themes_define_the_same_tokens():
    base = read(WEB / "css" / "base.css")
    blocks = re.findall(r"(:root[^{]*)\{([^}]*)\}", base)
    names = [(selector.strip(), set(re.findall(r"(--[\w-]+)\s*:", body))) for selector, body in blocks]
    light = next(tokens for selector, tokens in names if selector == ":root")
    dark = [tokens for selector, tokens in names if "dark" in selector or "not(" in selector]
    assert len(dark) == 2, "expected a system-dark block and an explicit dark block"
    colours = {t for t in light if t not in ("--radius", "--gap", "--font", "--mono")}
    for tokens in dark:
        assert tokens == colours, f"dark theme differs: {sorted(tokens ^ colours)}"
    used = set()
    for path in CSS:
        used |= set(re.findall(r"var\((--[\w-]+)", read(path)))
    assert used <= light, f"undefined custom properties: {sorted(used - light)}"


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
import { parseSelector, selectorOf, bytes, duration, mb } from 'BASE/dom.js';
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
console.log(JSON.stringify({
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

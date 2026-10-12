"""The demo site as committed: what it depends on, what it may contact, what it says.

The demo (demo/) is a static website: a landing page, the documentation, and the real panel
over a fake backend that runs in the visitor's browser. These checks read its files. They
hold the promises the demo makes in words: it is built from four packages, it is deployed
with the headers in vercel.json and nothing configured by hand, the fake backend cannot
reach a network or read a visitor's file, and everything it shows is invented.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import test_web_static as web

from superfarm.api.app import SECURITY_HEADERS

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo"
MOCK = DEMO / "mock"
REPOSITORY = "https://github.com/" + "ham" + "zaahmadaslam/super-android-farm"
COPYRIGHT = "Copyright 2026 " + "Ham" + "za Ahmad Aslam"
LIVE_DEMO = "androidfarm." + "ham" + "zaahmadaslam.com"
SKIP_DIRS = {"node_modules", ".next", "out", "generated"}


def sources() -> list[Path]:
    """Every committed text file of the demo, plus the two files it has outside demo/."""
    found = [p for p in DEMO.rglob("*") if p.is_file() and p.suffix in (".js", ".mjs", ".jsx", ".css", ".json", ".py", ".txt", "")
             and not (set(p.relative_to(DEMO).parts[:-1]) & SKIP_DIRS) and "public/panel" not in p.as_posix() and "public/docs" not in p.as_posix()]
    found += list((MOCK / "generated").glob("*.js"))
    return sorted({*found, ROOT / "vercel.json", ROOT / "docs" / "DEMO.md"})


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def mock_sources() -> list[Path]:
    return sorted(p for p in MOCK.rglob("*.js"))


# ----------------------------------------------------------------------- dependencies

def test_the_site_depends_on_four_packages_and_pins_them():
    package = json.loads(read(DEMO / "package.json"))
    assert set(package["dependencies"]) == {"next", "react", "react-dom", "marked"}
    assert "devDependencies" not in package and "optionalDependencies" not in package
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", version) for version in package["dependencies"].values()), "exact versions, no ranges"
    lock = json.loads(read(DEMO / "package-lock.json"))
    assert lock["packages"][""]["dependencies"] == package["dependencies"], "package-lock.json is out of step with package.json"
    assert all("resolved" not in entry or entry["resolved"].startswith("https://registry.npmjs.org/")
               for entry in lock["packages"].values()), "every package comes from the npm registry"


def test_nothing_built_or_installed_is_committed():
    ignore = read(DEMO / ".gitignore")
    for line in ("/node_modules/", "/.next/", "/out/", "/public/panel/", "/public/docs/", "/generated/"):
        assert f"\n{line}\n" in ignore, f"demo/.gitignore must list {line}, anchored with its leading slash"
    if shutil.which("git"):
        tracked = subprocess.run(["git", "ls-files", "demo"], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.split()
        bad = [p for p in tracked if re.search(r"^demo/(node_modules|\.next|out|public/panel|public/docs|generated)/", p)]
        assert bad == [], f"built or copied files are tracked: {bad[:5]}"


def test_every_file_the_build_needs_is_committed():
    """The site is built from a fresh clone. A file that exists here but is ignored by git (an
    ignore rule that matches too much) would build without an error and fail in the browser."""
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    needed = [p.relative_to(ROOT).as_posix() for p in sources() if DEMO in p.parents]
    assert len(needed) > 40 and any("mock/generated/" in p for p in needed)
    ignored = subprocess.run(["git", "check-ignore", "--no-index", *needed], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.split()
    assert ignored == [], f"needed by the build but ignored by git: {ignored}"
    for path in mock_sources():
        for target in web.imports_of(path):
            assert (path.parent / target).is_file(), f"{path.name} imports {target}, which does not exist"


def test_there_is_no_hosted_ci():
    """Gates run locally (scripts/check.sh). The demo deploys from a push with no workflow."""
    assert not (ROOT / ".github" / "workflows").exists()
    assert not any((ROOT / name).exists() for name in (".gitlab-ci.yml", ".circleci", "azure-pipelines.yml", ".travis.yml"))


# ------------------------------------------------------------------------ deployment

def vercel() -> dict:
    return json.loads(read(ROOT / "vercel.json"))


def headers_for(path: str) -> dict[str, str]:
    """The headers vercel.json gives one path, read the way demo/scripts/serve.mjs reads them."""
    out = {}
    for rule in vercel()["headers"]:
        if re.fullmatch(rule["source"], path):
            out.update({h["key"].lower(): h["value"] for h in rule["headers"]})
    return out


def test_vercel_needs_no_dashboard_setting():
    config = vercel()
    assert config["framework"] is None, "a static output directory, whatever the dashboard would guess"
    assert config["installCommand"].startswith("cd demo && npm ci") and config["buildCommand"] == "cd demo && npm run build"
    assert config["outputDirectory"] == "demo/out" and config["trailingSlash"] is True
    assert config["github"] == {"silent": True}
    scripts = json.loads(read(DEMO / "package.json"))["scripts"]
    assert scripts["build"] == "node scripts/sync-panel.mjs && NEXT_TELEMETRY_DISABLED=1 next build && node scripts/postbuild.mjs"
    assert "output: 'export'" in read(DEMO / "next.config.mjs") and "trailingSlash: true" in read(DEMO / "next.config.mjs")
    assert not (DEMO / "app" / "api").exists() and not list(DEMO.glob("middleware.*")), "a static export has no server code"


def test_a_push_that_does_not_touch_the_demo_is_not_built(tmp_path):
    """The ignore rule, run for real in a scratch repository: exit 0 skips the build, 1 builds."""
    command = vercel()["ignoreCommand"]
    assert command.startswith("git diff --quiet") and command.endswith("|| exit 1")
    git = shutil.which("git")
    if not git:
        pytest.skip("needs git")

    def run(*argv: str) -> None:
        subprocess.run([git, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *argv], cwd=tmp_path, check=True,
                       capture_output=True, timeout=30)

    def commit(path: str) -> int:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(target.read_text() + "x" if target.exists() else "x")
        run("add", "-A")
        run("commit", "-q", "-m", path)
        return subprocess.run(["sh", "-c", command], cwd=tmp_path, capture_output=True, timeout=30).returncode

    run("init", "-q")
    assert commit("superfarm/core/x.py") == 1, "the first commit has nothing to compare with: build"
    assert commit("superfarm/core/x.py") == 0, "a change to the daemon does not rebuild the demo"
    assert commit("tests/unit/test_x.py") == 0
    for path in ("demo/mock/seed.js", "superfarm/web/js/main.js", "docs/GPU.md", "README.md", "vercel.json"):
        assert commit(path) == 1, f"a change to {path} must rebuild the demo"


def test_security_headers():
    for path in ("/", "/docs/gpu/", "/panel/", "/panel/js/main.js", "/_next/static/chunks/a.js"):
        got = headers_for(path)
        assert got["x-content-type-options"] == "nosniff" and got["x-frame-options"] == "DENY" and got["referrer-policy"] == "no-referrer", path
        csp = got["content-security-policy"]
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp, f"{path}: {csp}"
        assert "http:" not in csp and "https:" not in csp, f"{path}: the policy names no other host"
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "default-src 'self'" in csp
    site = headers_for("/docs/gpu/")["content-security-policy"]
    assert "script-src 'self'" in site and "style-src 'self'" in site and "connect-src 'self'" in site and "blob:" not in site


def test_the_demo_panel_runs_under_the_real_panels_policy():
    """The panel in the demo gets exactly the policy the daemon sends with the real one, so a
    panel change that needs a looser policy fails in both places."""
    daemon = dict(SECURITY_HEADERS)[b"content-security-policy"].decode()
    assert headers_for("/panel/")["content-security-policy"] == daemon
    assert headers_for("/panel/js/demo/transport.js")["content-security-policy"] == daemon


def test_the_panel_is_not_indexed_and_the_rest_is():
    assert headers_for("/panel/")["x-robots-tag"] == "noindex, nofollow" and "x-robots-tag" not in headers_for("/docs/")
    robots = read(DEMO / "public" / "robots.txt")
    assert "Disallow: /panel/" in robots and "Allow: /" in robots
    assert 'name="robots" content="noindex, nofollow"' in read(DEMO / "scripts" / "sync-panel.mjs")
    assert "max-age=31536000, immutable" in headers_for("/_next/static/chunks/a.js")["cache-control"]
    assert headers_for("/panel/js/main.js")["cache-control"] == "public, max-age=0, must-revalidate", "a new build of the panel is seen at once"


def test_pages_need_no_inline_script():
    """The static export writes inline scripts; postbuild.mjs moves them into files and fails
    the build if one is left. The pages themselves set no inline style or handler."""
    post = read(DEMO / "scripts" / "postbuild.mjs")
    assert "process.exit(1)" in post and "_next/static/inline" in post
    for path in sorted((DEMO / "app").rglob("*.jsx")) + sorted((DEMO / "lib").glob("*.jsx")):
        text = read(path)
        assert "dangerouslySetInnerHTML" not in text, f"{path.name}: HTML strings never reach the page"
        assert not re.search(r"\bstyle=\{", text) and not re.search(r"\bon[A-Z]\w+=\{", text), f"{path.name}: no inline style or handler"
        assert "next/script" not in text and "next/font" not in text and "next/image" not in text, path.name


# ------------------------------------------------------------------- the fake backend

def test_the_seam_is_the_same_on_both_sides():
    exports = lambda text: set(re.findall(r"^export (?:async )?(?:const|function) (\w+)", text, re.M))      # noqa: E731
    assert exports(read(MOCK / "transport.js")) == exports(read(web.WEB / "js" / "transport.js")) == {"request", "openSocket", "mediaUrl", "streamInto"}
    sync = read(DEMO / "scripts" / "sync-panel.mjs")
    assert "export * from './demo/transport.js';" in sync and "process.exit(1)" in sync


@pytest.mark.parametrize("path", mock_sources(), ids=lambda p: p.relative_to(MOCK).as_posix())
def test_the_fake_backend_cannot_reach_a_network_or_read_a_file(path: Path):
    text = read(path)
    for pattern, why in (
        (r"\bfetch\s*\(", "fetch"), (r"XMLHttpRequest", "XMLHttpRequest"), (r"\bnew\s+WebSocket\b", "a WebSocket"), (r"\bEventSource\b", "EventSource"),
        (r"\bsendBeacon\b", "sendBeacon"), (r"\bimportScripts\b|serviceWorker", "a worker"), (r"\bimport\s*\(\s*['\"`]\s*(https?:)?//", "a remote import"),
        (r"\blocalStorage\b|document\.cookie|\bindexedDB\b", "storage that outlives the tab"),
        (r"\bFileReader\b|\.arrayBuffer\s*\(|\.text\s*\(\)|\.stream\s*\(|\.bytes\s*\(|readAs[A-Z]", "reading a file's content"),
        (r"\beval\s*\(|\bnew\s+Function\b|\b(?:inner|outer)HTML\b|insertAdjacentHTML|document\.write", "something the content policy forbids"),
        (r"setAttribute\(\s*['\"]style['\"]", "an inline style"),
    ):
        assert not re.search(pattern, text), f"{path.name} uses {why}"
    urls = set(re.findall(r"https?://[^\s'\"`)<>]+", text))
    allowed = {REPOSITORY, f"{REPOSITORY}/blob/HEAD/DISCLAIMER.md"}
    if path.parent.name == "generated":
        return                                # real settings and tool texts mention real addresses; they are shown, never contacted
    # The address an MCP client would use on the farm's own machine is shown as text, as the real panel shows it.
    urls = {url for url in urls if not url.startswith("http://127.0.0.1:")}
    assert urls <= allowed, f"{path.name} names an address: {sorted(urls - allowed)}"


def test_state_lives_in_session_storage_only():
    """One tab's storage: it survives a reload and is gone with the tab or in a new private window."""
    assert "globalThis.sessionStorage" in read(MOCK / "store.js")
    code = lambda text: "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))      # noqa: E731
    elsewhere = [p.name for p in mock_sources() if p.name != "store.js" and "sessionStorage" in code(read(p))]
    assert elsewhere == [], f"only store.js touches the storage: {elsewhere}"
    transport = read(MOCK / "transport.js")
    assert "value.name, size: value.size" in transport, "a chosen file is reduced to its name and size"
    assert "if (path === '/api/auth/login')" in transport and "body = {};" in transport, "the typed token stops at the transport"


DOCUMENTATION = [ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


def test_every_address_is_from_a_documentation_range():
    """Invented servers, tunnels and public addresses use only the ranges reserved for
    documentation (RFC 5737). 127.0.0.1 is where the panel shows ADB and the daemon."""
    wrong = []
    for path in mock_sources():
        if path.parent.name == "generated":
            continue
        for found in re.findall(r"(?<![\w.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\w.]*\d)", read(path)):
            address = ipaddress.ip_address(found)
            if not (address == ipaddress.ip_address("127.0.0.1") or any(address in net for net in DOCUMENTATION)):
                wrong.append(f"{path.name}: {found}")
    assert wrong == [], f"addresses outside the documentation ranges: {wrong}"
    assert "198.51.100." in read(MOCK / "routes" / "net.js") and "203.0.113." in read(MOCK / "farm.js")


def test_apps_and_devices_are_invented():
    seed = read(MOCK / "seed.js")
    packages = re.findall(r"package: '([^']+)'", seed)
    assert len(packages) >= 3 and all(p.startswith("example.") for p in packages), packages
    assert "example.${word}" in read(MOCK / "routes" / "control.js"), "an uploaded app gets a package under example."
    presets = re.findall(r"\['([a-z-]+)', '([^']+)', '([A-Z0-9-]+)', \d+", seed)
    assert len(presets) >= 4 and all(key.startswith(("example-", "sample-")) for key, _, _ in presets), "device presets in the demo are made up"


def test_the_simulated_assistant_is_labelled_and_bounded():
    approvals = read(MOCK / "routes" / "approvals.js")
    assert "(simulated)" in approvals and "const MAX_PENDING = 2;" in approvals and "pending().length < MAX_PENDING" in approvals
    ai = read(MOCK / "routes" / "ai.js")
    assert "not connected to any language model" in ai


def test_the_banner_says_what_the_demo_is():
    chrome = read(MOCK / "chrome.js")
    assert "Demo: nothing here is real." in chrome
    assert "No Androids, VPNs or servers are connected, and your changes stay in this browser tab." in chrome
    for label in ("Repository", "Docs", "Disclaimer", "Reset demo"):
        assert f"'{label}'" in chrome, f"the banner offers {label}"
    assert "not opened, uploaded or kept" in chrome and "thrown away" in chrome, "file and password fields say what happens to them"
    assert "Any text works as the token" in chrome


# --------------------------------------------------------------------------- wording

@pytest.mark.parametrize("path", sources(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_private_words(path: Path):
    text = read(path)
    cleaned = text.replace(REPOSITORY, "").replace(COPYRIGHT, "").replace(LIVE_DEMO, "")
    for word in ("bi" + "go", "clau" + "de", "anthro" + "pic", "ham" + "za"):
        assert word not in cleaned.lower(), f"{path.name} contains {word!r}"


def test_the_document_links_where_it_should():
    doc = read(ROOT / "docs" / "DEMO.md")
    for target in ("](../README.md)", "](README.md)", "](../DISCLAIMER.md)", "](VERIFICATION.md)"):
        assert target in doc, f"docs/DEMO.md links {target}"
    for command in ("npm ci", "npm run build", "npm run preview", "demo/scripts/generate.py", "scripts/check.sh"):
        assert command in doc, f"docs/DEMO.md mentions {command}"
    scripts = json.loads(read(DEMO / "package.json"))["scripts"]
    assert {"build", "preview", "dev", "sync"} <= set(scripts), "every npm command the document names exists"
    body = re.sub(r"```.*?```", "", doc, flags=re.S)
    assert not re.search(r"^\s*([-*]|\d+\.)\s", body, re.M) and "|" not in body, "docs/DEMO.md is written in sentences: no lists, no tables"


# ------------------------------------------------------------------------------ node

@web.needs_node
@pytest.mark.parametrize("path", [p for p in sources() if p.suffix in (".js", ".mjs")], ids=lambda p: p.relative_to(ROOT).as_posix())
def test_scripts_parse(path: Path):
    result = subprocess.run([web.node, "--check", str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


@web.needs_node
def test_the_state_survives_being_saved_and_loaded(tmp_path):
    """What sessionStorage does on a reload: the state goes through JSON and work in
    progress carries on from the clock, not from timers that were lost."""
    script = tmp_path / "reload.mjs"
    script.write_text(f"""
const m = await import({json.dumps((MOCK / 'index.js').as_uri())});
let t = 1_800_000_000; m.setClock(() => t);
m.handle('POST', '/api/auth/login', {{}});
const task = m.handle('POST', '/api/instances/6/start', {{}}).body.task;
const copy = JSON.parse(JSON.stringify(m.db));
for (const key of Object.keys(m.db)) delete m.db[key];
Object.assign(m.db, copy);                                  // "the page was reloaded"
const before = m.handle('GET', '/api/instances/6').body.state;
t += 60; m.advance();
const after = m.handle('GET', '/api/instances/6').body;
const done = m.handle('GET', '/api/tasks/' + task.id).body.task;
m.reset();
console.log(JSON.stringify({{ before, after: after.state, mode: after.gpu.mode, task: done.state, result: done.results['6'], afterReset: m.handle('GET', '/api/instances/6').status }}));
""", encoding="utf-8")
    result = subprocess.run([web.node, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["before"] == "starting" and got["after"] == "booted" and got["mode"] == "software"
    assert got["task"] == "done" and got["result"]["ok"] and "booted in" in got["result"]["detail"]
    assert got["afterReset"] == 401, "a reset starts from the seed: signed out, as a new tab is"

#!/usr/bin/env bash
# The project's quality gate. Everything runs on this machine; nothing runs on a
# hosted CI service.
#
#   scripts/check.sh            fast gate: lint, unit tests, static checks      (about 3 minutes)
#   scripts/check.sh --live     also the live tests against real containers     (needs Docker + an Android image)
#   scripts/check.sh --quick    lint and static checks only                     (seconds)
#
# Exit code 0 means every gate passed. Install it as a pre-push hook with
#   scripts/check.sh --install-hook
# so a push is refused while a gate is failing.
set -u
cd "$(dirname "$0")/.." || exit 2
ROOT=$PWD
PY=${SF_PYTHON:-$ROOT/.venv/bin/python}
[ -x "$PY" ] || PY=python3
mode=fast
case "${1:-}" in
  --live) mode=live ;;
  --quick) mode=quick ;;
  --install-hook)
    hook=$ROOT/.git/hooks/pre-push
    printf '#!/bin/sh\nexec "%s/scripts/check.sh"\n' "$ROOT" >"$hook" && chmod +x "$hook"
    echo "pre-push hook installed: $hook"; exit 0 ;;
  "" ) ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac

failed=()
step() {   # step <name> <command...>
  local name=$1; shift
  printf '\n== %s\n' "$name"
  if "$@"; then echo "ok: $name"; else echo "FAILED: $name"; failed+=("$name"); fi
}

lint() { "$PY" -m ruff check superfarm tests scripts; }

shell_syntax() {
  local rc=0 f
  while IFS= read -r f; do bash -n "$f" || rc=1; done < <(git ls-files '*.sh' 'install.sh')
  if command -v shellcheck >/dev/null 2>&1; then
    git ls-files '*.sh' 'install.sh' | xargs -r shellcheck -S error || rc=1
  fi
  return $rc
}

js_syntax() {
  command -v node >/dev/null 2>&1 || { echo "node not installed: skipped"; return 0; }
  local rc=0 f
  while IFS= read -r f; do node --check "$f" || rc=1; done < <(git ls-files 'superfarm/web/**/*.js' 'demo/**/*.js' 'demo/**/*.mjs' 2>/dev/null)
  return $rc
}

# Nothing private may be committed. Generic secret shapes are checked here; words that
# are private to one checkout (names, hosts) are listed one per line in the untracked
# file .git/info/private-words so they never appear in the repository themselves.
no_secrets() {
  local rc=0 words=$ROOT/.git/info/private-words
  if git grep -nIE -e '-----BEGIN [A-Z ]*PRIVATE KEY-----' -e 'gh[pousr]_[A-Za-z0-9]{30,}' -e 'sk-[A-Za-z0-9]{32,}' -- . ':!tests' ; then
    echo "possible secret material in tracked files"; rc=1
  fi
  if git ls-files | grep -E '(^|/)(secrets/|auth\.txt$)|\.(ovpn|pem|key|apk|xapk|apks|apkm|so|ko)$'; then
    echo "files of a kind that must not be committed"; rc=1
  fi
  if [ -s "$words" ]; then
    # .git/info/private-allow lists the exact strings in which such a word is expected
    # (the repository address, the copyright line); they are cut out before the search.
    local allow=$ROOT/.git/info/private-allow hits
    hits=$(git grep -nIi -f "$words" -- . | "$PY" -c '
import pathlib, re, sys
allow = [l for l in pathlib.Path(sys.argv[1]).read_text().splitlines() if l] if pathlib.Path(sys.argv[1]).is_file() else []
words = [l for l in pathlib.Path(sys.argv[2]).read_text().splitlines() if l]
for line in sys.stdin:
    rest = line
    for a in allow:
        rest = re.sub(re.escape(a), "", rest, flags=re.I)
    if any(w.lower() in rest.split(":", 2)[-1].lower() for w in words):
        sys.stdout.write(line)
' "$allow" "$words")
    if [ -n "$hits" ]; then printf '%s\n' "$hits"; echo "private words found in tracked files"; rc=1; fi
  fi
  return $rc
}

config_docs() {   # the settings reference is generated; it must match the code
  [ -f docs/CONFIGURATION.md ] || { echo "docs/CONFIGURATION.md not written yet: skipped"; return 0; }
  "$PY" -m superfarm.cli.main config docs --check docs/CONFIGURATION.md
}

doc_links() {     # every relative link in the Markdown files must resolve
  "$PY" - <<'PY'
import pathlib, re, subprocess, sys
files = [pathlib.Path(p) for p in subprocess.run(["git", "ls-files", "*.md"], capture_output=True, text=True).stdout.split()]
link = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
bad = []
for f in files:
    text = re.sub(r"```.*?```", "", f.read_text(encoding="utf-8"), flags=re.S)
    anchors = lambda p: {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-")
                         for h in re.findall(r"^#+\s+(.*)$", p.read_text(encoding="utf-8"), flags=re.M)}
    for target in link.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):      # http:, mailto: and friends are not checked offline
            continue
        path, _, frag = target.partition("#")
        dest = (f.parent / path).resolve() if path else f.resolve()
        if not dest.exists():
            bad.append(f"{f}: missing {target}")
        elif frag and dest.suffix == ".md" and frag.lower() not in anchors(dest):
            bad.append(f"{f}: no heading #{frag} in {dest.name}")
print(f"{len(files)} Markdown files checked")
if bad:
    print("\n".join(bad)); sys.exit(1)
PY
}

doc_generated() {   # the CLI, API and flow-step references are generated; they must match the code
  "$PY" scripts/gen_cli_docs.py --check docs/CLI.md &&
  "$PY" scripts/gen_api_docs.py --check docs/API.md &&
  "$PY" scripts/gen_flow_docs.py --check docs/AUTOMATION.md
}

unit() { "$PY" -m pytest -o addopts="" -q tests/unit; }
live() { SF_LIVE=1 "$PY" -m pytest -o addopts="" -q tests/live; }

step "lint (ruff)" lint
step "shell scripts" shell_syntax
step "panel JavaScript syntax" js_syntax
step "no secrets or private data" no_secrets
step "documentation links" doc_links
step "settings reference up to date" config_docs
step "generated references up to date" doc_generated
if [ "$mode" != quick ]; then step "unit tests" unit; fi
if [ "$mode" = live ]; then step "live tests" live; fi

echo
if [ ${#failed[@]} -eq 0 ]; then echo "ALL GATES PASSED ($mode)"; exit 0; fi
printf 'FAILED GATES (%d): ' "${#failed[@]}"; printf '%s; ' "${failed[@]}"; echo; exit 1

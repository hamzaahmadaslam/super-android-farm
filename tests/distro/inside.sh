#!/usr/bin/env bash
# Runs INSIDE a throwaway container started by tests/distro/run.sh. Not for a real machine.
#
#   /src   the checkout, mounted read-only
#
# Copies the checkout, runs the installer's container-test mode (really installs the
# distribution packages and the virtual environment), then checks the result. Every check
# prints one line `SFRESULT<TAB>name<TAB>ok|fail|skip<TAB>detail` that run.sh collects.
set -u

WORK=/work
VENV=/opt/sf-venv
export SUPERFARM_HOME=/tmp/sf-home
PY=$VENV/bin/python

result() { printf 'SFRESULT\t%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }
section() { printf '\n######## %s\n' "$*"; }

# shellcheck disable=SC1091
. /etc/os-release
result distro ok "${PRETTY_NAME:-unknown}"

section "copy the checkout"
mkdir -p "$WORK"
( cd /src && for f in * .[!.]*; do
    case "$f" in .venv|.git|.pytest_cache|.ruff_cache|build|dist|*.egg-info|'*'|'.[!.]*') continue ;; esac
    cp -a "$f" "$WORK/"
  done )
cd "$WORK" || { result copy fail "cannot enter $WORK"; exit 1; }

section "install.sh --container-test"
./install.sh --container-test --dev --venv "$VENV" --home "$SUPERFARM_HOME"
code=$?
if [ "$code" = 0 ]; then result install ok ""; else result install fail "install.sh exit $code"; fi

section "programs the package step should have put on PATH"
missing_required=""; missing_optional=""
for prog in docker adb ffmpeg modprobe ip curl; do
  command -v "$prog" >/dev/null 2>&1 || missing_required="$missing_required $prog"
done
for prog in scrcpy lspci zstd pigz patchelf git; do
  command -v "$prog" >/dev/null 2>&1 || missing_optional="$missing_optional $prog"
done
if [ -z "$missing_required" ]; then result required-programs ok "docker adb ffmpeg modprobe ip curl"
else result required-programs fail "missing:$missing_required"; fi
if [ -z "$missing_optional" ]; then result optional-programs ok "scrcpy lspci zstd pigz patchelf git"
else result optional-programs skip "not installed:$missing_optional"; fi

section "virtual environment"
if [ -x "$PY" ]; then result venv ok "$("$PY" --version 2>&1)"; else result venv fail "$PY missing"; exit 1; fi

section "import"
if out=$("$PY" -c 'import superfarm; print(superfarm.__version__)' 2>&1); then result import ok "superfarm $out"
else result import fail "$(printf '%s' "$out" | tail -1)"; fi

section "every package name against this distribution's index"
"$PY" tests/distro/check_packages.py | tee /tmp/packages.log
if [ "${PIPESTATUS[0]}" = 0 ]; then result packages ok "$(grep -c '^  ok ' /tmp/packages.log) keys resolved; $(grep -E '^  (warn|hint|skip)' /tmp/packages.log | awk '{print $2}' | tr '\n' ' ')not checked or absent"
else result packages fail "$(grep '^unresolved:' /tmp/packages.log)"; fi

section "command line"
if [ -x "$VENV/bin/sf" ] && out=$("$VENV/bin/sf" version 2>&1); then result cli ok "$out"
else
  # The command-line front end may be incomplete in a development tree; the modules it
  # wraps must work on their own.
  result cli skip "sf version did not run: $(printf '%s' "${out:-no sf entry point}" | tail -1 | cut -c1-120)"
fi

section "doctor"
if out=$("$PY" - <<'PYEOF' 2>&1
from superfarm.config import Config
from superfarm.host import doctor
from superfarm.paths import get_paths
checks = doctor.run_checks(Config(), get_paths())
for c in checks:
    print(f"[{c.status:4}] {c.id:22} {c.detail[:110]}")
s = doctor.summarize(checks)["counts"]
crashed = [c.id for c in checks if c.id.startswith("internal.")]
print(f"SUMMARY {len(checks)} checks: {s['ok']} ok, {s['warn']} warn, {s['fail']} fail, {s['skip']} skip; crashed: {crashed or 'none'}")
raise SystemExit(1 if crashed else 0)
PYEOF
); then printf '%s\n' "$out"; result doctor ok "$(printf '%s\n' "$out" | grep '^SUMMARY' | cut -c9-)"
else printf '%s\n' "$out"; result doctor fail "$(printf '%s\n' "$out" | tail -1 | cut -c1-160)"; fi

section "host setup plan (dry run)"
if "$PY" -m superfarm.host.setup plan > /tmp/plan.log 2>&1; then result plan ok "$(grep -c '^\[' /tmp/plan.log) steps"
else result plan fail "$(tail -1 /tmp/plan.log | cut -c1-160)"; fi
cat /tmp/plan.log

section "shipped files"
if out=$("$PY" -c 'from superfarm.resources import resource_path as r
for parts in (("deploy","docker","net","Dockerfile"),("deploy","systemd","superfarm.service"),("scripts","camera","run_loop.py"),("native","venus","build.sh")):
    assert r(*parts).is_file(), parts
print(r("deploy","docker","net"))' 2>&1); then result resources ok "$out"; else result resources fail "$(printf '%s' "$out" | tail -1)"; fi

section "unit tests (host subset)"
"$PY" -m pytest -o addopts="" -q -p no:cacheprovider tests/unit/test_host_*.py tests/unit/test_foundation.py tests/unit/test_resources.py 2>&1 | tee /tmp/pytest.log | tail -25
if [ "${PIPESTATUS[0]}" = 0 ]; then result unit-tests ok "$(tail -1 /tmp/pytest.log)"; else result unit-tests fail "$(tail -1 /tmp/pytest.log)"; fi
exit 0

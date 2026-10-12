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
SF=$VENV/bin/sf
if out=$("$SF" version 2>&1); then result cli ok "$out"
else result cli fail "sf version: $(printf '%s' "$out" | tail -1 | cut -c1-160)"; fi

section "doctor"
# A container has no kernel modules of its own, no Docker daemon, no desktop: the doctor is
# expected to report failures (exit code 1). What must not happen is a crash: output that
# is not the JSON document, or a check that broke (reported with an `internal.` id).
"$SF" doctor --json > /tmp/doctor.json 2> /tmp/doctor.err
code=$?
if out=$("$PY" - "$code" <<'PYEOF' 2>&1
import json
import sys
code = int(sys.argv[1])
data = json.load(open("/tmp/doctor.json"))
checks, counts = data["checks"], data["summary"]["counts"]
for c in checks:
    print(f"[{c['status']:4}] {c['id']:22} {c['detail'][:110]}")
crashed = [c["id"] for c in checks if c["id"].startswith("internal.")]
unfixable = [c["id"] for c in checks if c["status"] in ("warn", "fail") and not c["fix"]]
print(f"SUMMARY {len(checks)} checks: {counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail, "
      f"{counts['skip']} skip; exit {code}; crashed: {crashed or 'none'}; without a fix line: {unfixable or 'none'}")
expected = 1 if counts["fail"] else 0
raise SystemExit(0 if not crashed and not unfixable and code == expected else 1)
PYEOF
); then printf '%s\n' "$out"; result doctor ok "$(printf '%s\n' "$out" | grep '^SUMMARY' | cut -c9-)"
else printf '%s\n' "$out"; cat /tmp/doctor.err; result doctor fail "$(printf '%s\n' "$out" | tail -1 | cut -c1-200)"; fi
# The same run as a person sees it: the table and the fix lines must render too.
if "$SF" doctor > /tmp/doctor.txt 2>&1; [ $? -le 1 ] && grep -q ' ok, .* warnings, .* failed, .* skipped' /tmp/doctor.txt; then
  result doctor-text ok "$(tail -1 /tmp/doctor.txt | cut -c1-120)"
else result doctor-text fail "$(tail -1 /tmp/doctor.txt | cut -c1-160)"; fi
sed -n '/^\[fail\]/,$p' /tmp/doctor.txt

section "host setup plan (dry run, through the command line)"
if "$SF" host plan > /tmp/plan.log 2>&1; then result plan ok "$(grep -c '^\[' /tmp/plan.log) steps"
else result plan fail "$(tail -1 /tmp/plan.log | cut -c1-160)"; fi
cat /tmp/plan.log

section "host capacity and GPUs (read-only)"
if "$SF" host capacity > /tmp/capacity.log 2>&1 && "$SF" host gpus >> /tmp/capacity.log 2>&1; then
  result capacity ok "$(head -1 /tmp/capacity.log | cut -c1-120)"
else result capacity fail "$(tail -1 /tmp/capacity.log | cut -c1-160)"; fi
cat /tmp/capacity.log

section "shipped files"
if out=$("$PY" -c 'from superfarm.resources import resource_path as r
for parts in (("deploy","docker","net","Dockerfile"),("deploy","systemd","superfarm.service"),("scripts","camera","run_loop.py"),("native","venus","build.sh")):
    assert r(*parts).is_file(), parts
print(r("deploy","docker","net"))' 2>&1); then result resources ok "$out"; else result resources fail "$(printf '%s' "$out" | tail -1)"; fi

section "wheel (a plain, non-editable install into a second environment)"
# The installer above installs the checkout in place. A wheel puts the Dockerfiles, unit
# templates, scripts and native sources under <prefix>/share/superfarm-resources instead;
# superfarm.resources must find them there on this distribution's Python layout too.
WHEEL_VENV=/opt/sf-wheel-venv
wheel_check() {
  "$PY" -m pip wheel --quiet --no-deps -w /tmp/wheel "$WORK" || return 1
  python3 -m venv "$WHEEL_VENV" || return 1
  "$WHEEL_VENV/bin/python" -m pip install --quiet /tmp/wheel/superfarm-*.whl || return 1
  # From another folder, so nothing can be picked up from the checkout by accident.
  ( cd / && "$WHEEL_VENV/bin/python" - <<'PYEOF'
import os
import superfarm
from superfarm.resources import resource_path as r, resource_root
assert "/work" not in superfarm.__file__, superfarm.__file__
for parts in (("deploy", "docker", "net", "Dockerfile"), ("deploy", "docker", "android", "Dockerfile"),
              ("deploy", "systemd", "superfarm.service"), ("deploy", "systemd", "superfarm-host.service"),
              ("scripts", "camera", "run_loop.py"), ("scripts", "host", "sign-module.sh"),
              ("native", "venus", "build.sh"), ("native", "v4l2loopback", "build.sh")):
    assert r(*parts).is_file(), f"missing from the wheel install: {'/'.join(parts)}"
assert os.access(r("deploy", "docker", "net", "entrypoint.sh"), os.X_OK), "scripts lost their executable bit"
print(f"WHEELROOT {resource_root()}")
PYEOF
  ) || return 1
  ( cd / && "$WHEEL_VENV/bin/sf" doctor --json > /tmp/wheel-doctor.json; [ $? -le 1 ] ) || return 1
  "$WHEEL_VENV/bin/python" -c 'import json, sys
checks = json.load(open("/tmp/wheel-doctor.json"))["checks"]
crashed = [c["id"] for c in checks if c["id"].startswith("internal.")]
print(f"doctor from the wheel: {len(checks)} checks, crashed: {crashed or None}")
sys.exit(1 if crashed or not checks else 0)'
}
if wheel_check > /tmp/wheel.log 2>&1; then result wheel ok "$(grep '^WHEELROOT' /tmp/wheel.log | cut -c11-); $(tail -1 /tmp/wheel.log)"
else result wheel fail "$(tail -1 /tmp/wheel.log | cut -c1-200)"; fi
cat /tmp/wheel.log

section "unit tests (fast subset: host, foundation, resources, local command line)"
"$PY" -m pytest -o addopts="" -q -p no:cacheprovider -rs tests/unit/test_host_*.py tests/unit/test_foundation.py \
  tests/unit/test_resources.py tests/unit/test_cli_parser.py tests/unit/test_cli_local.py tests/unit/test_cli_values.py \
  2>&1 | tee /tmp/pytest.log | tail -25
if [ "${PIPESTATUS[0]}" = 0 ]; then result unit-tests ok "$(tail -1 /tmp/pytest.log)"; else result unit-tests fail "$(tail -1 /tmp/pytest.log)"; fi
exit 0

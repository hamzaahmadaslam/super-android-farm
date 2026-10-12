#!/usr/bin/env bash
# Check install.sh on every supported distribution family, without root on this machine.
#
#   tests/distro/run.sh [ubuntu] [debian] [fedora] [arch] [suse]      (default: all five)
#
# For each distribution a throwaway container is started from its base image with this
# checkout mounted read-only. Inside it (tests/distro/inside.sh), as the container's root:
#   - the checkout is copied and `./install.sh --container-test --dev` is run, which really
#     installs the distribution packages, creates the virtual environment and pip-installs
#     the project, and skips kernel modules, kernel limits, groups and systemd;
#   - every package name the farm knows for that family is resolved against the
#     distribution's package index;
#   - the package is imported and `sf version`, `sf doctor` (as JSON and as text; it must
#     report the container's limits, not crash), `sf host plan`, `sf host capacity` and
#     `sf host gpus` are run;
#   - a wheel is built and installed, not editable, into a second environment, and the
#     shipped files and the doctor are checked from that copy;
#   - a fast part of the unit tests is run: host, foundation, resources, local commands.
#
# What this does NOT prove: anything that needs a real kernel or init system (module
# loading, sysctl, groups, the systemd units), and kernel header package names.
#
# Containers are named sftest-distro-<name>, capped at 2 GB RAM, run one at a time and are
# removed afterwards. The base images stay cached. Needs Docker and network access.
# Logs: one file per distribution in $SF_DISTRO_OUT (default: a new temporary folder).
set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
declare -A IMAGE=(
  [ubuntu]=ubuntu:24.04
  [debian]=debian:12
  [fedora]=fedora:44
  [arch]=archlinux:latest
  [suse]=opensuse/tumbleweed:latest
)
ORDER=(ubuntu debian fedora arch suse)
TIMEOUT=${SF_DISTRO_TIMEOUT:-2400}

targets=("$@")
[ ${#targets[@]} -gt 0 ] || targets=("${ORDER[@]}")
for t in "${targets[@]}"; do
  [ -n "${IMAGE[$t]:-}" ] || { echo "unknown distribution '$t' (known: ${ORDER[*]})" >&2; exit 2; }
done
command -v docker >/dev/null 2>&1 || { echo "docker is needed" >&2; exit 2; }

OUT=${SF_DISTRO_OUT:-$(mktemp -d -t sf-distro.XXXXXX)}
mkdir -p "$OUT"

current=""
cleanup() { [ -z "$current" ] || docker rm -f "$current" >/dev/null 2>&1 || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM

field() {   # field <log> <name> -> "status<TAB>detail" of the last SFRESULT line with that name
  awk -F'\t' -v n="$2" '$1 == "SFRESULT" && $2 == n { s = $3; d = $4 } END { if (s != "") printf "%s\t%s", s, d }' "$1"
}

overall=0
for key in "${targets[@]}"; do
  name="sftest-distro-$key"
  log="$OUT/$key.log"
  echo "== $key (${IMAGE[$key]}) -> $log"
  current=$name
  # A container of this exact name can only be the leftover of an interrupted earlier run.
  docker rm -f "$name" >/dev/null 2>&1 || true
  # In the background and waited for: bash runs a trap only between commands, so with the
  # container in the foreground an interrupt would be ignored until the container ended.
  timeout "$TIMEOUT" docker run --name "$name" --memory 2g --memory-swap 2g --cpus 4 \
    -v "$REPO:/src:ro" -e SF_INSTALL_NO_SYSTEM=1 "${IMAGE[$key]}" \
    bash /src/tests/distro/inside.sh > "$log" 2>&1 &
  wait $!
  code=$?
  docker rm -f "$name" >/dev/null 2>&1 || true
  current=""
  [ "$code" = 0 ] || { echo "   container exited with code $code (124 = timed out after ${TIMEOUT}s)"; overall=1; }
  grep -q $'^SFRESULT\t[^\t]*\tfail' "$log" && overall=1
done

echo
printf '%-8s | %-28s | %-8s | %-8s | %-6s | %-6s | %-6s | %-6s | %-6s | %-10s\n' \
  distro release install packages venv import cli doctor wheel unit-tests
for key in "${targets[@]}"; do
  log="$OUT/$key.log"
  col() { local v; v=$(field "$log" "$1" | cut -f1); printf '%s' "${v:--}"; }
  printf '%-8s | %-28s | %-8s | %-8s | %-6s | %-6s | %-6s | %-6s | %-6s | %-10s\n' \
    "$key" "$(field "$log" distro | cut -f2 | cut -c1-28)" "$(col install)" "$(col packages)" "$(col venv)" \
    "$(col import)" "$(col cli)" "$(col doctor)" "$(col wheel)" "$(col unit-tests)"
done
echo
for key in "${targets[@]}"; do
  grep $'^SFRESULT\t' "$OUT/$key.log" | awk -F'\t' -v k="$key" '$3 != "ok" || $2 == "packages" || $2 == "doctor" || $2 == "unit-tests" || $2 == "cli" || $2 == "wheel" { printf "%-8s %-18s %-5s %s\n", k, $2, $3, $4 }'
done
echo
echo "logs: $OUT"
exit "$overall"

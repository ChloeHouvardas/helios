#!/usr/bin/env bash
# test-all.sh — the ONE test command for the whole repository.
#
# Usage:
#   scripts/test-all.sh              # every suite, offline
#   scripts/test-all.sh -x           # extra args are passed to each pytest run
#
# Every suite under platform/*/tests, apps/*/tests,
# entry-point/examples/*/tests and entry-point/checks/tests is run separately,
# in its own pytest process. Separate processes matter: the suites have their
# own conftest.py files, and `platform/financial-tools/conftest.py` puts its
# package root on sys.path, which must not leak into another suite's run.
#
# Prints one `== <suite>: N passed` line per suite and a total at the end.
# Exits non-zero if ANY suite fails, errors, or collects nothing — a suite that
# silently stops being discovered must not look like a pass.
#
# Everything here is offline. No suite needs a network call, a Docker daemon, a
# model token, or provider credentials; tests that would need them skip with a
# stated reason.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "test-all.sh: no repo venv at .venv — run scripts/bootstrap-venv.sh first." >&2
  exit 2
fi

# --- discover suites -------------------------------------------------------
SUITES=()
add_suite() {
  local dir="$1"
  [[ -d "$dir" ]] || return 0
  # A directory of conftest-only or empty tests is not a suite.
  if compgen -G "$dir/test_*.py" >/dev/null; then
    SUITES+=("$dir")
  fi
}

for pattern in "platform" "apps" "entry-point/examples"; do
  for parent in $pattern/*/; do
    [[ -d "$parent" ]] || continue
    add_suite "${parent%/}/tests"
  done
done
# entry-point/checks is not nested one level deeper than the others.
add_suite "entry-point/checks/tests"
# The teammate packages under financial-tools ship their own tests/ directories
# (email_tool, payments_collect, helios_forecast); the package-level tests/ does
# not include them, so they are discovered one level deeper.
for parent in platform/financial-tools/*/; do
  [[ -d "$parent" ]] || continue
  add_suite "${parent%/}/tests"
done

if [[ ${#SUITES[@]} -eq 0 ]]; then
  echo "test-all.sh: discovered no test suites — discovery is broken." >&2
  exit 2
fi

# --- run -------------------------------------------------------------------
failures=()
total_passed=0
ran=0

for suite in "${SUITES[@]}"; do
  output="$("$PY" -m pytest "$suite" -p no:cacheprovider --tb=short "$@" 2>&1)"
  status=$?
  ran=$((ran + 1))

  # pytest's own summary line, e.g. "79 passed in 0.75s" or "1 failed, 3 passed".
  summary="$(printf '%s\n' "$output" | grep -oE '[0-9]+ (passed|failed|error|errors|skipped|xfailed)(, [0-9]+ (passed|failed|error|errors|skipped|xfailed))*' | tail -1)"
  passed="$(printf '%s\n' "$output" | grep -oE '[0-9]+ passed' | tail -1 | grep -oE '[0-9]+' || true)"
  [[ -n "$passed" ]] || passed=0

  if [[ $status -eq 0 ]]; then
    total_passed=$((total_passed + passed))
    echo "== $suite: ${passed} passed"
  else
    failures+=("$suite")
    echo "== $suite: FAILED (${summary:-exit $status})"
    printf '%s\n' "$output" | tail -40
    echo
  fi
done

# --- report ----------------------------------------------------------------
echo
if [[ ${#failures[@]} -gt 0 ]]; then
  echo "FAILED suites (${#failures[@]} of $ran):"
  for f in "${failures[@]}"; do echo "  - $f"; done
  echo "$total_passed passed in the suites that succeeded."
  exit 1
fi

echo "all $ran suites green: $total_passed passed"
exit 0

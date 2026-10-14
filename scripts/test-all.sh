#!/usr/bin/env bash
# Run every test suite in the repository and print one "N passed" line per suite.
#
# Exit status is 0 only if every suite that exists passes. A missing suite is
# skipped, not failed, so the script works on branches where a package has not
# landed yet.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -x ".venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="${PYTHON:-python3}"
  echo "test-all: WARNING no .venv, using $PY (run scripts/bootstrap-venv.sh)" >&2
fi

status=0

run_suite() {
  local label="$1" dir="$2"
  if [[ ! -d "$REPO_ROOT/$dir" ]]; then
    echo "SKIP  $label (no $dir)"
    echo
    return 0
  fi
  echo "--- $label ---"
  # Capture $? before any other command runs: `local rc=$?` would report the
  # status of the `local` builtin instead of pytest's.
  ( cd "$REPO_ROOT/$dir" && "$PY" -m pytest -q )
  rc=$?
  case $rc in
    0) echo "OK    $label" ;;
    # pytest exit 5 = "no tests collected". Some packages declare a testpaths
    # they do not have yet (platform/action-agent-builder ships no tests/).
    # That is an empty suite, not a broken one, so it is reported as skipped.
    5) echo "SKIP  $label (no tests collected)" ;;
    *) echo "FAIL  $label"; status=1 ;;
  esac
  echo
}

run_suite "fin_tools"         "platform/financial-tools"
run_suite "agent_toolkit"     "platform/action-agent-builder"
run_suite "audit_agent"       "platform/audit-agent-builder"
run_suite "agent_integration" "platform/agent-integration"
run_suite "invoice-payer"     "entry-point/examples/invoice-payer"

# The seam check proves the two agent builders and the financial tools still
# agree on the tool contract. It prints "seam check: OK" or exits non-zero.
if [[ -d "$REPO_ROOT/platform/agent-integration" ]]; then
  echo "--- selfcheck (agent_integration.cli) ---"
  out="$( cd "$REPO_ROOT/platform/agent-integration" && "$PY" -m agent_integration.cli selfcheck 2>&1 )"
  rc=$?
  echo "$out" | tail -15
  if [[ $rc -ne 0 ]]; then
    echo "FAIL  selfcheck"
    status=1
  else
    echo "OK    selfcheck"
  fi
  echo
fi

if [[ $status -eq 0 ]]; then
  echo "test-all: all suites green"
else
  echo "test-all: FAILURES above"
fi
exit $status

#!/usr/bin/env bash
# Run every offline test suite in the repository and report per-suite counts.
#
# Exit status is 0 only when every suite passes. Each suite prints its own
# "N passed" summary line so a regression in one package is attributable
# without re-running the others.
#
# Two suites set `addopts = "-q"` in their own pyproject.toml
# (audit-agent-builder, financial-tools). Passing -q again makes pytest
# doubly quiet and SUPPRESSES the summary line entirely, which looks exactly
# like a hang. Do not add -q here.
#
# All suites are offline. Nothing here needs Docker; the deployment engine
# tests mock the docker client.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Prefer the repo venv; fall back to a system interpreter that has pytest.
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

# agent-integration ships a committed venv whose site-packages it depends on.
AI_PY="$REPO_ROOT/platform/agent-integration/.venv/bin/python"
[ -x "$AI_PY" ] || AI_PY="$PY"

failures=()
totals_passed=0
totals_skipped=0

# run_suite <label> <workdir> <interpreter> [pytest args...]
run_suite() {
  local label="$1" workdir="$2" interp="$3"; shift 3
  echo "=============================================================="
  echo "SUITE: $label"
  echo "  cwd:  $workdir"
  echo "  py:   $interp ($("$interp" -V 2>&1))"
  echo "--------------------------------------------------------------"

  if [ ! -d "$REPO_ROOT/$workdir" ]; then
    echo "  SKIP: directory not present"
    failures+=("$label (missing directory)")
    return
  fi

  local out rc summary
  out="$(cd "$REPO_ROOT/$workdir" && "$interp" -m pytest "$@" 2>&1)"
  rc=$?
  # pytest writes progress with carriage returns; normalise so the summary is
  # reliably matchable.
  summary="$(printf '%s' "$out" | tr '\r' '\n' | grep -E '^[0-9]+ passed|passed|failed|error' | tail -1)"
  printf '%s\n' "$out" | tr '\r' '\n' | tail -15
  echo "  -> exit $rc"

  local p s
  p="$(printf '%s' "$summary" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | head -1)"
  s="$(printf '%s' "$summary" | grep -oE '[0-9]+ skipped' | grep -oE '[0-9]+' | head -1)"
  totals_passed=$((totals_passed + ${p:-0}))
  totals_skipped=$((totals_skipped + ${s:-0}))

  if [ $rc -ne 0 ]; then
    failures+=("$label (exit $rc)")
  fi
  echo
}

run_suite "audit-agent-builder" "platform/audit-agent-builder" "$PY" tests
run_suite "financial-tools"     "platform/financial-tools"     "$PY" tests
run_suite "agent-integration"   "platform/agent-integration"   "$AI_PY" tests
run_suite "helios-collect"      "apps/helios-collect"          "$PY" tests -q
run_suite "invoice-payer"       "entry-point/examples/invoice-payer" "$PY" tests -q

# Optional suites: present only once their task has landed.
[ -d "$REPO_ROOT/platform/deployment/tests" ] && \
  run_suite "deployment-engine" "platform/deployment" "$PY" tests

echo "=============================================================="
echo "TOTAL: $totals_passed passed, $totals_skipped skipped"
if [ ${#failures[@]} -gt 0 ]; then
  echo "FAILED SUITES:"
  for f in "${failures[@]}"; do echo "  - $f"; done
  exit 1
fi
echo "ALL SUITES GREEN"

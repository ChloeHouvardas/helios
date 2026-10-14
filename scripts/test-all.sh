#!/usr/bin/env bash
# test-all.sh — run every test suite in the repo and report per-suite counts.
#
#   scripts/test-all.sh            # all suites
#   scripts/test-all.sh app-builder   # one suite, by name
#
# Exit 0 only when every discovered suite passes. Each suite prints its own
# "N passed" line, because a single aggregate number hides which package broke —
# and this repo has several agents working in parallel, so "something failed" is
# not an actionable message.
#
# Suites are discovered by convention: any directory matching the patterns below
# that contains a `tests/` directory with test files. Discovery rather than a
# hand-maintained list, so a new package is picked up by the next run instead of
# being silently untested.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
FILTER="${1:-}"

if [ ! -x "$PY" ]; then
    echo "test-all: no venv at $REPO/.venv — run scripts/bootstrap-venv.sh first" >&2
    exit 2
fi

# Suite roots, relative to the repo. A suite is <root>/tests.
SUITE_ROOTS=(
    "platform/action-agent-builder"
    "platform/audit-agent-builder"
    "platform/agent-integration"
    "platform/financial-tools"
    "platform/app-builder"
    "entry-point"
    "apps/helios-collect"
)

total_suites=0
failed_suites=()
skipped_suites=()

for root in "${SUITE_ROOTS[@]}"; do
    name="$(basename "$root")"
    if [ -n "$FILTER" ] && [ "$FILTER" != "$name" ]; then
        continue
    fi

    testdir="$REPO/$root/tests"
    if [ ! -d "$testdir" ]; then
        skipped_suites+=("$name (no tests/)")
        continue
    fi
    if ! ls "$testdir"/test_*.py >/dev/null 2>&1; then
        skipped_suites+=("$name (no test files)")
        continue
    fi

    total_suites=$((total_suites + 1))
    echo ""
    echo "=== $name ($root) ==="

    # Run from the suite root so that app-local imports (`import config`,
    # `from book import Book`) resolve the way they do in production, where the
    # app directory is the working directory.
    #
    # --override-ini="addopts=" clears whatever each package's pyproject declares,
    # then we apply exactly one -q ourselves. This matters: four packages here set
    # addopts = "-q", and passing -q on the command line as well makes it -qq,
    # which suppresses the "N passed" summary line. Per-suite counts are the point
    # of this script — an aggregate exit code alone hides which package broke, and
    # this repo has several agents working in parallel.
    if (cd "$REPO/$root" && "$PY" -m pytest tests -q --override-ini="addopts=" 2>&1); then
        :
    else
        failed_suites+=("$name")
    fi
done

echo ""
echo "==============================================="
echo "test-all: $total_suites suite(s) run"
if [ ${#skipped_suites[@]} -gt 0 ]; then
    echo "test-all: skipped — ${skipped_suites[*]}"
fi
if [ ${#failed_suites[@]} -gt 0 ]; then
    echo "test-all: FAILED — ${failed_suites[*]}"
    exit 1
fi
if [ "$total_suites" -eq 0 ]; then
    echo "test-all: nothing matched ${FILTER:-<no filter>}" >&2
    exit 2
fi
echo "test-all: all suites green"
exit 0

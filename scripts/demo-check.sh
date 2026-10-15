#!/usr/bin/env bash
# demo-check.sh — run the DEMOFLOW.md test ladder, one line per check.
#
#   scripts/demo-check.sh all
#   scripts/demo-check.sh T0 T1 T2          # offline rungs only
#   scripts/demo-check.sh T3 --tenant demo  # Docker rungs on this machine
#
# Prints `<id> PASS|FAIL <observed>` per check and exits non-zero on any FAIL.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$REPO/.venv/bin/python" "$REPO/scripts/demo_check.py" "$@"

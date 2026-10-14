#!/usr/bin/env bash
# bootstrap-venv.sh — build the ONE Python environment every Helios session uses.
#
# Usage:
#   scripts/bootstrap-venv.sh          # from the repo root, or anywhere
#
# Why one venv: the repo has several packages that require Python >=3.13, and
# the system `python3` on some machines is a different minor version with none
# of the dependencies installed. Anything run outside this venv is not testing
# what CI tests. `scripts/test-all.sh` uses this venv's interpreter.
#
# Idempotent: re-running reuses the existing .venv and re-resolves the pins.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
PY="$VENV/bin/python"

# --- interpreter ------------------------------------------------------------
# 3.13 explicitly: audit_agent and agent_integration declare requires-python
# ">=3.13". Never fall back to `python3`, which is not guaranteed to be 3.13.
PY313="${PYTHON313:-}"
if [[ -z "$PY313" ]]; then
  for candidate in python3.13 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)' 2>/dev/null; then
        PY313="$candidate"
        break
      fi
    fi
  done
fi
if [[ -z "$PY313" ]]; then
  echo "bootstrap-venv.sh: no Python >= 3.13 found on PATH." >&2
  echo "Install Python 3.13, or point PYTHON313 at an interpreter." >&2
  exit 1
fi
echo "== interpreter: $($PY313 -V) ($(command -v "$PY313"))"

# --- venv -------------------------------------------------------------------
if [[ ! -x "$PY" ]]; then
  echo "== creating .venv"
  "$PY313" -m venv "$VENV"
fi

echo "== upgrading pip / setuptools / wheel"
"$PY" -m pip install --upgrade pip setuptools wheel >/dev/null

# --- first-party packages, editable ----------------------------------------
# Order matters only for readability; pip resolves the graph. These five are the
# whole tracked platform: the action agent toolkit, the standalone audit agent,
# the integration layer that imports both, the financial tools, and the
# deployment engine.
echo "== installing first-party packages (editable)"
"$PY" -m pip install \
  -e platform/action-agent-builder \
  -e platform/audit-agent-builder \
  -e platform/agent-integration \
  -e platform/financial-tools \
  -e platform/deployment

# --- third-party, pinned ---------------------------------------------------
echo "== installing pinned dependencies from requirements-dev.txt"
"$PY" -m pip install -r requirements-dev.txt

# --- verify -----------------------------------------------------------------
echo "== verifying imports"
"$PY" - <<'EOF'
import importlib

# `engine` is the deployment engine. Importing it creates
# platform/deployment/.helios-run/tenants/ at module scope (see
# engine/orchestrator.py), which is a gitignored runtime directory.
for module in (
    "agent_toolkit",
    "audit_agent",
    "agent_integration",
    "fin_tools",
    "engine",
):
    importlib.import_module(module)
    print(f"   ok {module}")
EOF

echo
echo "bootstrap complete. Run tests with: scripts/test-all.sh"

#!/usr/bin/env bash
# bootstrap-venv.sh — create the repo venv and install every local package.
#
#   scripts/bootstrap-venv.sh
#
# Why this exists
# ---------------
# The system `python3` on this machine is 3.14 and has none of the repo's
# dependencies installed. Every package here declares `requires-python = ">=3.13"`,
# and the agent runtime pins behaviour against 3.13. Using the system interpreter
# produces import errors that look like code bugs and are not.
#
# This script is the single way to get a working interpreter. It is idempotent:
# re-running it reuses the existing `.venv` and just re-installs the packages, so
# a new local package is picked up without deleting anything.
#
# All packages are installed EDITABLE (-e). That matters: the whole point of this
# repo is that a generated app imports the shared runtime, and a non-editable
# install would test a frozen copy rather than the source you are editing.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"

# Prefer an explicit 3.13. `python3` may be 3.14 (see above); fall back to it only
# when no 3.13 is on PATH, and let pip's requires-python check report the mismatch
# rather than silently guessing.
PYTHON_BIN=""
for candidate in python3.13 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo "bootstrap-venv: no python3 on PATH" >&2
    exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
    echo "bootstrap-venv: creating $VENV from $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
    "$PYTHON_BIN" -m venv "$VENV"
fi

PY="$VENV/bin/python"
"$PY" -m pip install --quiet --upgrade pip

# Local packages, in dependency order. Contracts before dependents: agent_toolkit
# and audit_agent are independent of each other, agent_integration imports both,
# and fin_tools bridges agent_toolkit. app_builder consumes all of them at
# validation time, so it goes last.
#
# All installed EDITABLE (-e). That matters: the whole point of this repo is that
# a generated app imports the shared runtime, and a non-editable install would
# test a frozen copy rather than the source you are editing.
PACKAGES=(
    "platform/action-agent-builder"
    "platform/audit-agent-builder"
    "platform/agent-integration"
    "platform/app-builder"
)

for pkg in "${PACKAGES[@]}"; do
    if [ -f "$REPO/$pkg/pyproject.toml" ]; then
        echo "bootstrap-venv: installing $pkg"
        "$PY" -m pip install --quiet -e "$REPO/$pkg"
    else
        # Not an error: a session may be mid-flight and its package may not be
        # committed yet. Say so, because a silently skipped package later shows up
        # as an ImportError that looks unrelated.
        echo "bootstrap-venv: skipping $pkg (no pyproject.toml yet)"
    fi
done

# platform/financial-tools is a FLAT layout with no [build-system] section, so
# `pip install -e` fails on package discovery (setuptools cannot tell `fin_tools`
# apart from `tests` and `fixtures`). It is on the path via its own conftest.py
# when pytest runs inside it; for everyone else we add a .pth file, which is
# exactly what an editable install would have produced.
#
# Written into the venv, not the repo, so it touches nothing another session owns.
FIN_TOOLS="$REPO/platform/financial-tools"
if [ -d "$FIN_TOOLS/fin_tools" ]; then
    echo "bootstrap-venv: putting financial-tools on the path"
    SITE_PACKAGES="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    printf '%s\n' "$FIN_TOOLS" > "$SITE_PACKAGES/fin_tools.pth"
fi

# Test and web tooling. Kept here rather than in each package's dependencies so a
# deployed app does not drag pytest along.
echo "bootstrap-venv: installing dev tooling"
"$PY" -m pip install --quiet pytest

echo "bootstrap-venv: done — use $VENV/bin/python"

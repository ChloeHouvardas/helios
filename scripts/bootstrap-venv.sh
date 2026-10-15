#!/usr/bin/env bash
# Create the repository venv used by every Helios task.
#
# Python 3.13+ is required: the platform packages use features that are not
# available on 3.12, and the golden base image is python:3.13-slim. Never fall
# back to a bare `python3` without checking its version first -- on this machine
# `python3` happens to be Anaconda 3.13.9, but that is a coincidence, not a
# contract.
#
# Idempotent: re-running on an existing healthy venv is a no-op.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"

# --- locate a 3.13+ interpreter -------------------------------------------
find_python() {
  local candidate
  for candidate in python3.13 python3.14 python3 "${PYTHON:-}"; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

PY="$(find_python)" || {
  echo "bootstrap-venv: no Python 3.13+ interpreter found." >&2
  echo "Install one (for example: brew install --formula python@3.13) and re-run." >&2
  exit 1
}

if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)' 2>/dev/null; then
  echo "bootstrap-venv: reusing existing venv at $VENV ($("$VENV/bin/python" -V))"
else
  echo "bootstrap-venv: creating venv with $PY ($("$PY" -V))"
  "$PY" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet pytest httpx

# The deployment engine's own pinned requirements: docker-py, fastapi, uvicorn,
# redis, python-multipart. Without these `import docker` fails at collection and
# the engine cannot be tested or run.
REQ="$REPO_ROOT/platform/deployment/requirements.txt"
if [ -f "$REQ" ]; then
  echo "bootstrap-venv: installing deployment engine requirements"
  "$VENV/bin/python" -m pip install --quiet -r "$REQ"
fi
# App containers serve Jinja2/HTMX pages; the golden base image installs the same
# set, so local runs match what a deployed app gets.
"$VENV/bin/python" -m pip install --quiet "jinja2>=3.1" "numpy>=1.26"

# The platform packages are installed editable so that source edits are picked
# up without a reinstall. `agent-integration` has its own committed venv, which
# test-all.sh prefers; installing it here too keeps a single entry point working.
#
# Install order is the dependency order, not alphabetical: agent-integration
# declares dependencies on the distributions `agent-toolkit` and
# `audit-agent-builder`, so both must exist first. `--no-deps` is used because
# those are local paths, not PyPI packages, and the third-party deps are already
# installed above.
#
# financial-tools is deliberately NOT in this list. Its pyproject.toml declares
# no [build-system], so setuptools' auto-discovery finds fin_tools, fixtures,
# research and tests side by side, refuses to guess, and the editable install
# fails. That package is owned by another agent (spec section C forbids editing
# platform/financial-tools), so instead of fixing its packaging we expose it the
# way HANDOFF.md already documents for it: a path entry, below.
for pkg in action-agent-builder audit-agent-builder agent-integration; do
  if [ -f "$REPO_ROOT/platform/$pkg/pyproject.toml" ]; then
    "$VENV/bin/python" -m pip install --quiet --no-deps -e "$REPO_ROOT/platform/$pkg" || \
      echo "bootstrap-venv: WARNING editable install of $pkg failed; test-all.sh falls back to PYTHONPATH" >&2
  fi
done

# Put fin_tools on the path with a .pth file, which is equivalent to an editable
# install for import purposes and requires no change to the package itself.
SITE_PACKAGES="$("$VENV/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
echo "$REPO_ROOT/platform/financial-tools" > "$SITE_PACKAGES/helios-fin-tools.pth"
echo "bootstrap-venv: fin_tools exposed via $SITE_PACKAGES/helios-fin-tools.pth"

echo "bootstrap-venv: ready -> $VENV/bin/python ($("$VENV/bin/python" -V))"

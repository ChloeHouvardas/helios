#!/usr/bin/env bash
# Create the repository virtualenv at .venv (Python 3.13).
#
# Why --system-site-packages: the machine's Python 3.13 interpreter already
# carries every third-party package this repo needs (numpy, pydantic, jinja2,
# jsonschema, PyYAML, pytest). Inheriting them keeps bootstrap offline and
# reproducible; the venv still isolates the repo's own packages, which are
# installed in editable mode below.
#
# Never use the system `python3` directly for repo work: it is not guaranteed to
# be 3.13 on every machine and carries no repo packages.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"

# Prefer an explicit 3.13 interpreter; fall back to whatever `python3` is.
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  for candidate in python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
      if [[ "$version" == "3.13" ]]; then PY="$candidate"; break; fi
      PY="${PY:-$candidate}"
    fi
  done
fi
if [[ -z "$PY" ]]; then
  echo "bootstrap-venv: no python3 interpreter found" >&2
  exit 1
fi

echo "bootstrap-venv: using $PY ($("$PY" -V 2>&1))"

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PY" -m venv --system-site-packages "$VENV"
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true

# Repo packages, editable so tests pick up edits without reinstalling.
# Each is optional: a package may be absent on a branch that has not merged it.
for pkg in \
  "platform/financial-tools" \
  "platform/action-agent-builder" \
  "platform/audit-agent-builder" \
  "platform/agent-integration"
do
  if [[ -f "$REPO_ROOT/$pkg/pyproject.toml" ]]; then
    "$VENV/bin/python" -m pip install --quiet -e "$REPO_ROOT/$pkg" || \
      echo "bootstrap-venv: skipped $pkg (install failed)" >&2
  fi
done

echo "bootstrap-venv: ready at $VENV"

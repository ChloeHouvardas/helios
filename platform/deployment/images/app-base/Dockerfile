# Helios golden base image: Python 3.13 + the platform packages pre-installed.
#
# An app image is built by COPYing the bundle over this base and running
# uvicorn, with NO pip install step (see the Dockerfile the orchestrator
# injects). That only works if everything an app can import is already here, so
# the four platform packages are installed editable into /opt/helios/platform.
#
# Build context is the REPOSITORY ROOT (the engine passes path=REPO), which is
# why every COPY source is a platform/... path. See .dockerignore at the repo
# root -- without it the context would include a 100 MB committed-adjacent venv.
#
# Layer order is deliberate and is the reason a platform edit rebuilds quickly:
#   1. pinned third-party deps        <- cached until requirements change
#   2. platform source + editable     <- only this layer is invalidated
# Editable installs with --no-deps mean layer 2 writes a path finder rather than
# resolving a dependency graph, so it takes seconds.
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- 1. pinned third-party runtime deps ---------------------------------
# Pinned to a floor, not an exact version, because these are the same floors the
# platform packages declare in their own pyproject.toml files; pinning tighter
# here than there would let the image and the packages disagree about what is
# supported. Anything added here must be recorded in the commit body (spec C.4).
#
# jinja2 and python-multipart are not incidental: app containers serve
# server-rendered Jinja2/HTMX pages and accept multipart form posts, so a
# generated app needs both at import time with no install step available.
RUN pip install \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.27" \
        "httpx>=0.27" \
        "redis>=5.0" \
        "pydantic>=2.7" \
        "pyyaml>=6.0" \
        "jsonschema>=4.21" \
        "jinja2>=3.1" \
        "python-multipart>=0.0.9" \
        "numpy>=1.26"

# --- 2. platform packages ------------------------------------------------
# Install order is the dependency order, not alphabetical: agent-integration
# declares dependencies on the distributions `agent-toolkit` (built from
# action-agent-builder) and `audit-agent-builder`, so both must be present
# before it. `fin-tools` has no dependencies.
COPY platform/action-agent-builder /opt/helios/platform/action-agent-builder
COPY platform/audit-agent-builder  /opt/helios/platform/audit-agent-builder
COPY platform/financial-tools      /opt/helios/platform/financial-tools
COPY platform/agent-integration    /opt/helios/platform/agent-integration

RUN pip install --no-deps -e /opt/helios/platform/action-agent-builder \
 && pip install --no-deps -e /opt/helios/platform/audit-agent-builder \
 && pip install --no-deps -e /opt/helios/platform/agent-integration

# financial-tools is exposed by path rather than installed. Its pyproject.toml
# declares no [build-system], so setuptools' auto-discovery finds fin_tools,
# fixtures, research and tests side by side, refuses to guess, and an editable
# install fails. That package is owned by another agent, so its packaging is not
# ours to fix; a .pth file gives the identical import behaviour.
ENV PYTHONPATH=/opt/helios/platform/financial-tools

# The model driver talks to an OpenAI-compatible endpoint (DashScope) using
# stdlib urllib, so qwen-agent is only needed by the advisory reviewer, which is
# NON-BINDING and optional. Install it last and tolerate failure: a base image
# that cannot build because an optional advisor is unavailable would block every
# deploy for a feature that never changes a verdict.
RUN pip install "qwen-agent>=0.0.17" \
 || echo "app-base: qwen-agent unavailable; model advisory disabled"

# --- 3. app SDK ----------------------------------------------------------
# Copied rather than installed: the SDK is a single module with no dependencies,
# and apps import it as `helios_sdk`.
COPY platform/deployment/sdk/helios_sdk /usr/local/lib/python3.13/site-packages/helios_sdk

# Verify the image is actually usable before it is tagged. A base image missing
# a platform package fails much later, as a confusing ImportError inside a
# deployed app's smoke test; failing here names the problem at its source.
RUN python -c "import agent_toolkit, audit_agent, agent_integration, fin_tools, helios_sdk, fastapi, jinja2, multipart; print('app-base: platform packages importable')"

WORKDIR /app

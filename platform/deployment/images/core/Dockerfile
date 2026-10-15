# Helios tenant core: the platform's financial tools service, one per tenant.
#
# This is `python -m fin_tools` -- the same registry the apps compose in-process,
# served over HTTP so the platform owns the tools, the ledger, the knowledge
# graph and the human-approval record (specs/financial-tools-service.md). The
# audit sidecar is the only container holding the execution secret and so the
# only caller; apps propose, the sidecar forwards, this service executes.
#
# Build context is the REPOSITORY ROOT (the engine passes path=REPO), so the
# COPY source is a platform/... path. python:3.13 to match the rest of the
# platform.
#
# Layer order: pinned third-party deps first (cached until they change), then
# the package source, so a tools edit rebuilds in seconds.
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# The floors fin-tools' own pyproject pins exactly; the same set app-base
# installs, so a tool that imports fine in an app imports fine here.
RUN pip install \
        "numpy>=1.26" \
        "jinja2>=3.1" \
        "jsonschema>=4.21" \
        "pydantic>=2.7" \
        "openpyxl>=3.1" \
        "pyyaml>=6.0" \
        "tzdata>=2024.1"

COPY platform/financial-tools /opt/helios/platform/financial-tools
RUN pip install --no-deps -e /opt/helios/platform/financial-tools

# Durable state (ledger, balances, outbox, verified graph nodes, idempotency,
# the market-data cache) lives on the tenant volume the engine mounts here.
# Market data defaults to the committed fixtures so a tenant with no Tushare
# token still answers; the engine overrides the mode when a token is present.
ENV HELIOS_DATA_DIR=/data \
    HELIOS_MARKET_MODE=fixture
RUN mkdir -p /data

RUN python -c "import fin_tools, fin_tools.service, fin_tools.persistence; print('core: fin_tools importable')"

EXPOSE 9000
CMD ["python", "-m", "fin_tools", "--host", "0.0.0.0", "--port", "9000"]

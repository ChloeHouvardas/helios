# Trusted Helios audit sidecar (official -- never built by Qwen or user code).
#
# Python 3.13 to match the platform packages, and audit-agent-builder is
# installed for real: the sidecar loads the app's manifest with
# audit_agent.manifest.AuditManifest and writes its decisions with
# audit_agent.audit_log.AuditLog. Importing the same code the in-app gate uses
# is the whole mechanism by which the two gates cannot drift.
#
# Build context is the REPOSITORY ROOT (the engine passes path=REPO), so the
# COPY sources are platform/... paths, not image-relative ones.
FROM python:3.13-slim-bookworm

RUN pip install --no-cache-dir \
        "fastapi>=0.110" "uvicorn>=0.27" "httpx>=0.27" \
        "pydantic>=2.6" "pyyaml>=6.0" "jsonschema>=4.21"

# The manifest loader and the hash-chained log writer.
COPY platform/audit-agent-builder /opt/helios/platform/audit-agent-builder
RUN pip install --no-cache-dir -e /opt/helios/platform/audit-agent-builder

# /policy is where the engine injects the app's audit manifest before this
# container starts. Owned by root and mounted read-only from the app's point of
# view: the app has no volume and no exec access here, so it cannot rewrite its
# own policy after deploy.
RUN mkdir -p /policy /data/audit && chmod 0555 /policy

COPY platform/deployment/images/auditor/server.py /app/server.py
ENV PYTHONUNBUFFERED=1 \
    HELIOS_AUDIT_MANIFEST_PATH=/policy/audit.yaml \
    HELIOS_DATA_DIR=/data
WORKDIR /app
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]

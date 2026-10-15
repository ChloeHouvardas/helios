# Golden base image: runtime + Helios App Builder SDK pre-installed.
# App builds become a single COPY layer, finishing in seconds.
FROM python:3.12-slim-bookworm

RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    "redis>=5.0" \
    "httpx>=0.27" \
    "PyYAML>=6.0"

COPY sdk/helios_sdk/__init__.py /usr/local/lib/python3.12/site-packages/helios_sdk/__init__.py

ENV PYTHONUNBUFFERED=1
WORKDIR /app
EXPOSE 3000

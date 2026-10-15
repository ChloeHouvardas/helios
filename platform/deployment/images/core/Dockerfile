# Helios tenant ecosystem core: mock Financial Tools + shared Knowledge Graph.
#
# Build context is the REPOSITORY ROOT (the engine passes path=REPO), so the COPY
# source is a platform/... path. python:3.13 to match the rest of the platform.
FROM python:3.13-slim-bookworm

RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.27"

COPY platform/deployment/images/core/server.py /app/server.py
ENV PYTHONUNBUFFERED=1
WORKDIR /app
EXPOSE 9000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9000"]

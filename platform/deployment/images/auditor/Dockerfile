# Trusted Helios Audit Agent image (official — never built by Qwen/user code).
FROM python:3.12-slim-bookworm

RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.27" "httpx>=0.27"

COPY images/auditor/server.py /app/server.py
ENV PYTHONUNBUFFERED=1
WORKDIR /app
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]

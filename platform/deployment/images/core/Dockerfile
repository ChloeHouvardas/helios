# Helios tenant ecosystem core: mock Financial Tools + shared Knowledge Graph.
FROM python:3.12-slim-bookworm

RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.27"

COPY images/core/server.py /app/server.py
ENV PYTHONUNBUFFERED=1
WORKDIR /app
EXPOSE 9000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9000"]

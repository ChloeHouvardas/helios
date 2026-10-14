#!/usr/bin/env bash
# End-to-end proof: deploy the sample app, route through nginx, run the
# zero-trust action pipeline (app -> audit sidecar -> helios core), and show
# the audit trail.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$PATH"
export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.orbstack/run/docker.sock}"

echo "== 1. deploy via helios CLI (zip POST, no Dockerfile, no git) =="
.venv/bin/python cli/helios.py deploy examples/sample-app --tenant demo

BASE=http://localhost/apps/sample-collect-demo

echo; echo "== 2. health check through nginx =="
# wait for nginx to pick up the new config and the app to be ready
for i in $(seq 1 10); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -sf "$BASE/health"; echo

echo; echo "== 3. action proposal executed via audit sidecar -> helios core =="
curl -sf -X POST "$BASE/demo/collect-payment"; echo

echo; echo "== 4. identical second payment -> held as possible duplicate =="
curl -sf -X POST "$BASE/demo/collect-payment"; echo

echo; echo "== 5. audit trail (decisions live only in the trusted sidecar) =="
docker exec demo-sample-collect-audit python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/audit-log').read().decode())"

echo; echo "== 6. event bus (Redis pub/sub between apps on tenant net) =="
curl -sf -X POST "$BASE/demo/announce-event"; sleep 1; curl -sf "$BASE/events"; echo

echo; echo "ALL CHECKS DONE"

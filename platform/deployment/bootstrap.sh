#!/usr/bin/env bash
# Helios local bootstrap: deployment engine + nginx ingress + python env.
# Prereq: a Docker provider (OrbStack, Colima, or Docker Desktop) and python3.13.
#
# Imported from the Mac-mini build. Two things changed on import: the Docker
# socket is now detected rather than assumed to be OrbStack, and the venv is
# built on python3.13 — audit_agent and agent_integration both require >=3.13,
# and one interpreter for the whole repo is the point of scripts/bootstrap-venv.sh.
set -euo pipefail
cd "$(dirname "$0")"

# --- Docker socket detection ------------------------------------------------
# Order: an explicit $DOCKER_HOST always wins, then the socket of whichever
# provider is actually present. The upstream version hard-coded OrbStack, which
# silently produced "daemon unreachable" for anyone on Colima or Docker Desktop.
detect_docker_host() {
  if [ -n "${DOCKER_HOST:-}" ]; then
    printf '%s' "$DOCKER_HOST"
    return 0
  fi
  local candidate
  for candidate in \
    "$HOME/.orbstack/run/docker.sock" \
    "$HOME/.colima/default/docker.sock" \
    "/var/run/docker.sock"
  do
    if [ -S "$candidate" ]; then
      printf 'unix://%s' "$candidate"
      return 0
    fi
  done
  return 1
}

if DETECTED="$(detect_docker_host)"; then
  export DOCKER_HOST="$DETECTED"
  echo "==> docker socket: $DOCKER_HOST"
else
  echo "==> no Docker socket found; start OrbStack, Colima or Docker Desktop,"
  echo "    or export DOCKER_HOST explicitly."
fi

echo "==> docker daemon"
if ! docker info >/dev/null 2>&1; then
  if [ -x /usr/bin/open ] && [ -d "/Applications/OrbStack.app" ]; then
    echo "    daemon not up — starting OrbStack"
    open -a OrbStack
    sleep 5
  else
    echo "    daemon not reachable at ${DOCKER_HOST:-<unset>}. Start your Docker"
    echo "    provider and re-run."
    exit 1
  fi
fi

echo "==> shared ingress network + nginx (reverse proxy)"
docker network inspect helios_ingress_net >/dev/null 2>&1 || \
  docker network create helios_ingress_net
if ! docker ps --format '{{.Names}}' | grep -qx helios-nginx; then
  docker rm -f helios-nginx >/dev/null 2>&1 || true
  mkdir -p .helios-run/nginx
  cat > .helios-run/nginx/default.conf <<'EOF'
server {
    listen 80 default_server;
    server_name _;

    location / {
        return 200 'Helios Platform - No app deployed at this path';
        add_header Content-Type text/plain;
    }
}
EOF
  docker run -d --name helios-nginx --restart unless-stopped \
    --network helios_ingress_net \
    -v "$(pwd)/.helios-run/nginx:/etc/nginx/conf.d:ro" \
    -p 80:80 \
    nginx:alpine
fi

# --- python env -------------------------------------------------------------
# python3.13, matching the repo floor. Prefer the shared repo venv when the
# caller built it with scripts/bootstrap-venv.sh, since it already has these
# deps pinned; fall back to a local venv so the engine still boots standalone.
PY313="${PYTHON313:-python3.13}"
if [ -x ../../.venv/bin/python ]; then
  echo "==> using the shared repo venv at ../../.venv"
  PYBIN=../../.venv/bin/python
  UVICORN=../../.venv/bin/uvicorn
else
  echo "==> python virtualenv ($($PY313 -V 2>&1)) + engine deps"
  [ -d .venv ] || "$PY313" -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  PYBIN=.venv/bin/python
  UVICORN=.venv/bin/uvicorn
fi

echo "==> deployment engine on 127.0.0.1:8100 (stops any previous engine process)"
pkill -f 'uvicorn engine.app:app' 2>/dev/null || true
sleep 1
nohup "$UVICORN" engine.app:app --host 127.0.0.1 --port 8100 \
  > engine.log 2>&1 &
for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8100/health >/dev/null 2>&1 && break
  sleep 1
done

echo
echo "engine health:   $(curl -sf http://127.0.0.1:8100/health || echo FAILED)"
echo "ingress entry:   http://localhost/  (routes via /apps/<app>-<tenant> prefix)"
echo "app hostname:    http://<app>-<tenant>.helios.test  (needs a local DNS entry"
echo "                 or the path-based route above)"
echo
echo "next: ./test-deploy.sh   # deploys the sample app end-to-end"

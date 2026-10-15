#!/usr/bin/env bash
# Helios local bootstrap: Traefik + deployment engine + python env.
# Prereq: brew install colima docker python@3.12  (and `colima start` done once)
set -euo pipefail
cd "$(dirname "$0")"

export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.orbstack/run/docker.sock}"

echo "==> docker daemon (OrbStack)"
if ! docker info >/dev/null 2>&1; then
  echo "    daemon not up — starting OrbStack"
  open -a OrbStack
  sleep 5
fi

echo "==> shared ingress network + nginx (reverse proxy)"
docker network inspect helios_ingress_net >/dev/null 2>&1 || \
  docker network create helios_ingress_net
if ! docker ps --format '{{.Names}}' | grep -qx helios-nginx; then
  docker rm -f helios-nginx >/dev/null 2>&1 || true
  mkdir -p .helios-run/nginx
  cat > .helios-run/nginx/default.conf <<'EOF'
server {
    listen 80;
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

echo "==> python virtualenv (python3.12) + engine deps"
[ -d .venv ] || python3.12 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "==> deployment engine on 127.0.0.1:8100 (stops any previous engine process)"
pkill -f 'uvicorn engine.app:app' 2>/dev/null || true
sleep 1
nohup .venv/bin/uvicorn engine.app:app --host 127.0.0.1 --port 8100 \
  > engine.log 2>&1 &
for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8100/health >/dev/null 2>&1 && break
  sleep 1
done

echo
echo "engine health:   $(curl -sf http://127.0.0.1:8100/health || echo FAILED)"
echo "traefik entry:   http://localhost/  (routes via /apps/<app>-<tenant> prefix)"
echo "traefik dash:    http://localhost:8080/dashboard/"
echo
echo "next: ./test-deploy.sh   # deploys the sample app end-to-end"

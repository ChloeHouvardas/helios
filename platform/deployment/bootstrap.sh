#!/usr/bin/env bash
# Helios local bootstrap: Docker daemon + nginx ingress + deployment engine.
#
# Idempotent: re-running is safe and will not duplicate containers or networks.
#
# Socket detection order matches engine/orchestrator.py and the session spec:
#   1. $DOCKER_HOST if set
#   2. OrbStack socket
#   3. Colima socket
#   4. /var/run/docker.sock
# Whichever answers `docker info` first wins. On a machine with Docker Desktop
# installed, /var/run/docker.sock is a symlink into the Desktop socket, so the
# right action is to START DESKTOP -- not to install Colima alongside it, which
# would create two competing daemons and a second docker binary.
#
# Requires python3.13+: the platform packages declare that floor. A bare
# `python3` is never assumed to satisfy it.
set -euo pipefail
cd "$(dirname "$0")"

ENGINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ENGINE_ROOT/../.." && pwd)"
VENV="$REPO_ROOT/.venv"
# The engine resolves its run directory at the REPO root (engine/orchestrator.py:
# RUN = REPO / ".helios-run"), so nginx must mount that one or it serves its
# default page while the engine believes it routed an app.
RUN_DIR="$REPO_ROOT/.helios-run"
PORT="${HELIOS_ENGINE_PORT:-8100}"

# --- 1. Docker daemon -----------------------------------------------------
echo "==> docker daemon"
detect_docker() {
  if [ -n "${DOCKER_HOST:-}" ]; then
    echo "    DOCKER_HOST=$DOCKER_HOST"
    docker info >/dev/null 2>&1 && return 0
  fi
  for sock in "$HOME/.orbstack/run/docker.sock" \
              "$HOME/.colima/default/docker.sock" \
              /var/run/docker.sock; do
    [ -S "$sock" ] || continue
    if DOCKER_HOST="unix://$sock" docker info >/dev/null 2>&1; then
      export DOCKER_HOST="unix://$sock"
      echo "    using $sock"
      return 0
    fi
  done
  return 1
}

if ! detect_docker; then
  echo "    no daemon answering; trying to start one"
  if command -v colima >/dev/null 2>&1; then
    colima start --cpu 3 --memory 4 --disk 50 || true
    detect_docker || true
  elif [ -d /Applications/Docker.app ]; then
    open -a Docker
    for _ in $(seq 1 60); do
      detect_docker && break
      sleep 2
    done
  else
    echo "ERROR: no Docker daemon and neither Colima nor Docker Desktop is installed." >&2
    echo "Install one of:" >&2
    echo "  brew install --formula colima docker && colima start --cpu 3 --memory 4 --disk 50" >&2
    echo "  (or install Docker Desktop)" >&2
    exit 1
  fi
fi
docker info >/dev/null 2>&1 || { echo "ERROR: docker daemon still unreachable" >&2; exit 1; }
echo "    daemon OK ($(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?'))"

# --- 2. shared ingress network + nginx ------------------------------------
echo "==> ingress network + nginx"
docker network inspect helios_ingress_net >/dev/null 2>&1 || \
  docker network create helios_ingress_net >/dev/null

# nginx must mount the SAME directory the engine writes routes into. The engine
# resolves its run dir from the repository root, so mounting a different path
# here leaves nginx serving its default page while the engine believes it has
# routed an app -- a 200 that is not the app.
mkdir -p "$RUN_DIR/nginx"
if [ ! -f "$RUN_DIR/nginx/default.conf" ]; then
  cat > "$RUN_DIR/nginx/default.conf" <<'EOF'
server {
    listen 80 default_server;
    server_name _;

    location / {
        return 200 'Helios Platform - No app deployed at this path';
        add_header Content-Type text/plain;
    }
}
EOF
fi

if docker ps --format '{{.Names}}' | grep -qx helios-nginx; then
  echo "    helios-nginx already running"
else
  docker rm -f helios-nginx >/dev/null 2>&1 || true
  docker run -d --name helios-nginx --restart unless-stopped \
    --network helios_ingress_net \
    -v "$RUN_DIR/nginx:/etc/nginx/conf.d:ro" \
    -p 80:80 nginx:alpine >/dev/null
  echo "    helios-nginx started"
fi

# --- 3. python venv --------------------------------------------------------
echo "==> python venv"
if [ ! -x "$VENV/bin/python" ]; then
  "$REPO_ROOT/scripts/bootstrap-venv.sh"
fi
"$VENV/bin/python" -c 'import sys; assert sys.version_info >= (3,13), sys.version' 2>/dev/null || {
  echo "ERROR: $VENV is not Python 3.13+. Recreate it with scripts/bootstrap-venv.sh" >&2
  exit 1
}

# --- 4. deployment engine --------------------------------------------------
echo "==> deployment engine on 127.0.0.1:$PORT"
if curl -sf -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  # Something is already serving this port. Do not kill it blindly: another
  # session may own it, and it may be running older code.
  echo "    port $PORT already in use; leaving that process alone"
  echo "    NOTE: verify it is running THIS engine -- check that"
  echo "          curl -s http://127.0.0.1:$PORT/api/v1/doctor returns 200."
  echo "          A 404 there means it is an older engine; use another port via"
  echo "          HELIOS_ENGINE_PORT, or stop that process yourself."
else
  pkill -f "uvicorn engine.app:app --host 127.0.0.1 --port $PORT" 2>/dev/null || true
  sleep 1
  ( cd "$ENGINE_ROOT" && \
    HELIOS_REPO_ROOT="$REPO_ROOT" nohup "$VENV/bin/python" -m uvicorn \
      engine.app:app --host 127.0.0.1 --port "$PORT" \
      > "$ENGINE_ROOT/engine.log" 2>&1 & )
  for _ in $(seq 1 40); do
    curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 1
  done
fi

echo
echo "engine health: $(curl -sf -m 5 "http://127.0.0.1:$PORT/health" || echo FAILED)"
echo "app entry:     http://localhost/apps/<app>-<tenant>/   (prefix stripped by nginx)"
echo "virtual host:  http://<app>-<tenant>.helios.test       (needs a hosts entry)"
echo
echo "next: $VENV/bin/python cli/helios.py doctor --engine http://127.0.0.1:$PORT"
echo "      $VENV/bin/python cli/helios.py deploy ../../apps/helios-collect --tenant demo"

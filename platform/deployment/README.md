# Helios — local deployment infrastructure (Mac mini)

Implements the stack recommended in the design doc: **docker-py control plane +
nginx reverse proxy + per-tenant Docker networks + audit-sidecar
(zero-trust) pattern + golden base images + Redis event bus.** Kubernetes and
Portainer-as-deployer are deliberately skipped.

**Note:** The design doc mentions Traefik, but we use nginx instead. Traefik's
Docker API client (v1.24) is incompatible with OrbStack's newer API (min v1.40).
Nginx provides the same zero-reload routing via config file generation during
deployment. Redis is used for the **event bus** (pub/sub between apps on the
tenant network), not for routing.

Everything runs in Docker (Colima VM on this Mac), so containers talk over real
Docker networks exactly like the production server would.

## Layout

```
helios/
├── engine/               # Automatic Deployment Engine (FastAPI + docker-py)
│   ├── app.py            #   POST /api/v1/deploy  (zip ingestion + static audit)
│   ├── orchestrator.py   #   OrchestratorDriver: build → smoke → attach → route
│   └── contracts.py      #   helios.yaml schema + hardcoded-secret linter
├── images/
│   ├── app-base/         # helios-app-base:latest  (golden image, SDK pre-baked)
│   ├── auditor/          # helios-auditor:latest   (trusted, never user-built)
│   └── core/             # helios-core:latest      (mock Financial Tools + KG)
├── sdk/helios_sdk/       # App Builder SDK: propose_action / publish_event
├── cli/helios.py         # `helios deploy <app-dir>` — zip + POST, no git
├── examples/sample-app/  # template of a Qwen-generated app (main.py + helios.yaml)
├── bootstrap.sh          # nginx + ingress net + venv + engine
└── test-deploy.sh        # end-to-end proof
```

## What each image does (and who builds it)

| Image | Purpose | Who builds it |
|---|---|---|
| **helios-app-base** | Golden base image with FastAPI/uvicorn/redis/httpx + `helios_sdk` pre-installed. Qwen-generated apps only ship `main.py` + `helios.yaml`; the engine injects a Dockerfile that does `FROM helios-app-base` + `COPY . /app`, so builds are a single COPY layer (seconds). | **Helios team** builds once per SDK version; engine rebuilds automatically when `sdk/` changes. |
| **helios-auditor** | Trusted audit sidecar. Holds `EXECUTION_SECRET`, enforces tool allow-lists, amount caps, duplicate detection. The only container allowed to call `helios-core` `/tools/*`. Qwen-generated apps only have `ACTION_PROPOSAL_TOKEN` and can merely *propose* actions to this sidecar. | **Helios team** builds and maintains. Engine starts one instance per app automatically during deploy — Qwen never builds or modifies it. |
| **helios-core** | Mock Financial Tools (payments/email/KG) + Knowledge Graph. Returns simulated refs locally; swap to real providers for production. Runs once per tenant. | **Helios team** builds. |

**Key point:** Qwen (the coding agent) only writes `main.py` + `helios.yaml`. All infrastructure images (auditor, core, base) are built and maintained by the Helios team. The engine orchestrates everything — Qwen doesn't write Dockerfiles or manage containers.

## Request flow

```
Qwen/coding agent ──(helios deploy: zip)──▶ engine :8100
engine:  lint secrets → build on golden base (seconds) → sandbox /health smoke
         │  fail → crash logs returned as JSON for the agent to fix
         ▼
  tenant_demo_net (internal bridge)          helios_ingress_net
  ┌────────────────────────────────┐          ┌─────────────┐
  │ demo-sample-collect-app ───────┼──propose─▶│  nginx :80  │──▶ http://localhost/apps/sample-collect-demo
  │        │ (ACTION token, no     │          └─────────────┘
  │        │  execution secret)    │
  │        ▼                       │
  │ demo-sample-collect-audit      │  holds EXECUTION secret, dedupe/caps/
  │        │ (executes)            │  allow-list policy
  │        ▼                       │
  │ helios-core   helios-redis ◀───┼── events pub/sub between apps
  └────────────────────────────────┘
```

## Bring-up (done for you by the agent, rerunnable any time)

```bash
brew install --formula colima docker python@3.12   # runtime + CLI
colima start --cpu 3 --memory 4 --disk 50          # Docker VM (Apple Silicon)
cd helios && ./bootstrap.sh                        # nginx + engine + venv
./test-deploy.sh                                   # end-to-end verification
```

Handy commands:

```bash
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
curl -s localhost:8100/api/v1/apps                 # deployment registry view
curl -s localhost:8100/api/v1/apps/demo-sample-collect-app/logs  # app logs (for Qwen feedback loop)
docker logs demo-sample-collect-app                # live app stdout (→ Qwen on failures)
.venv/bin/python cli/helios.py deploy <dir> --tenant acme   # new tenant = new network
```

## Demo URL note

Wildcard DNS (`collect.tenant1.helios.local`) needs an `/etc/hosts` entry per
app, so the platform also routes by **path prefix**: `http://localhost/apps/<app>-<tenant>`
always works with zero setup. Add hosts lines later only if you want the
host-based rule for the pitch.

## What's mocked vs. production

| Piece | Local | Production swap |
|---|---|---|
| Financial Tools (payments/email/KG) | `images/core` returns simulated refs | real bank/email providers |
| Knowledge Graph store | SQLite in core | tenant Postgres per design doc |
| Action tokens | rotated per deploy, engine-held | KMS-issued short-lived |
| Auth/tenant identity | `--tenant` flag | login + tenant provisioning |

See `DEV-NOTES.md` for design decisions and follow-ups.

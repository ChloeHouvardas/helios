# Helios Infrastructure Spec Sheet

This document describes what the current infrastructure code does, how to use it, and what's mocked vs implemented.

---

## Overview

The infrastructure provides:
- **Automatic deployment** of Qwen-generated apps via zip upload
- **Container orchestration** with Docker networks, audit sidecars, and shared services
- **Zero-trust security** via audit sidecars that hold execution secrets
- **Inter-app communication** via Docker DNS and Redis pub/sub
- **Platform tools** (payments, email, knowledge graph) via helios-core

**Status**: Core deployment pipeline works end-to-end. Platform tools are mocked (return simulated responses). Intent analysis layer is not yet built.

---

## Components

### 1. Deployment Engine

**Location**: `engine/`

**What it does**:
- Accepts zip uploads of apps via `POST /api/v1/deploy`
- Validates `helios.yaml` manifest
- Lints for hardcoded secrets (regex-based: sk-, AKIA, ghp_, private keys)
- Injects a Dockerfile (`FROM helios-app-base:latest`)
- Builds the Docker image
- Runs a smoke test (checks `/health` endpoint)
- Creates the audit sidecar container
- Configures nginx routing
- Returns deployment metadata (URLs, container names, etc.)

**API Endpoints**:

```
GET /health
  → {"status": "ok", "service": "helios-deployment-engine"}

POST /api/v1/deploy
  Body: multipart/form-data with "bundle" (zip file) and optional "tenant" field
  → 200: {"status": "deployed", "tenant": "...", "app": "...", "image": "...", ...}
  → 422: {"stage": "manifest|static_audit|build|smoke_test", "error": "..."}

GET /api/v1/apps
  → {"apps": [{"container": "...", "status": "...", "url": "..."}]}

GET /api/v1/apps/{container_name}/logs?tail=200
  → {"container": "...", "logs": ["line1", "line2", ...]}
```

**How to use**:
```bash
# Start the engine
cd helios
.venv/bin/uvicorn engine.app:app --host 0.0.0.0 --port 8100

# Or use bootstrap.sh (also starts Traefik/nginx)
./bootstrap.sh
```

**Current limitations**:
- Runs on localhost only (no auth/tenant isolation at the engine level)
- Smoke test timeout is hardcoded to 45 seconds
- No rollback on failed deployment (old containers are removed before new ones start)

---

### 2. Orchestrator

**Location**: `engine/orchestrator.py`

**What it does**:
- Wraps docker-py to manage container lifecycle
- Creates per-tenant Docker networks (`tenant_<id>_net`, internal=True)
- Creates shared ingress network (`helios_ingress_net`)
- Builds golden base images if not present
- Deploys apps with audit sidecars
- Generates nginx configs for routing

**Key methods**:

```python
class Orchestrator:
    def __init__(self):
        # Connects to Docker daemon via DOCKER_HOST env var
    
    def deploy(self, tenant: str, manifest: Manifest, app_dir: Path) -> dict:
        # Full deployment pipeline:
        # 1. Ensure base images exist
        # 2. Build app image
        # 3. Smoke test
        # 4. Create audit sidecar
        # 5. Configure nginx routing
    
    def list_apps(self) -> list[dict]:
        # Returns all deployed apps with their URLs
    
    def logs(self, container_name: str, tail: int = 200) -> list[str]:
        # Returns container logs as list of strings
```

**Network topology**:

```
helios_ingress_net (external, for nginx routing)
  ├── helios-nginx (port 80)
  ├── demo-sample-collect-app
  └── demo-sample-collect-audit

tenant_demo_net (internal, no outbound internet)
  ├── demo-sample-collect-app (alias: sample-collect)
  ├── demo-sample-collect-audit (alias: sample-collect-audit)
  ├── helios-core-demo (alias: helios-core)
  └── helios-redis-demo (alias: helios-redis)
```

**Current limitations**:
- No automatic rebuild of golden images when SDK changes
- No health checks after deployment (only smoke test during deploy)
- No resource limits on containers (CPU/memory)

---

### 3. Contracts

**Location**: `engine/contracts.py`

**What it does**:
- Validates `helios.yaml` manifest schema
- Lints for hardcoded secrets (regex patterns)
- Sanitizes app/tenant names (lowercase, alphanumeric + hyphens)

**Manifest schema**:

```yaml
name: my-app          # required, sanitized to lowercase
tenant: demo          # optional, defaults to "demo"
entry: main:app       # required, Python module:app object
port: 3000            # optional, defaults to 3000
permissions:          # optional, list of allowed tools
  - payments
  - email
  - knowledge-graph
  - events
```

**Secret patterns detected**:
- OpenAI-style API keys (`sk-...`)
- AWS access key IDs (`AKIA...`)
- GitHub personal access tokens (`ghp_...`)
- Slack tokens (`xox...`)
- Private keys (`-----BEGIN ... PRIVATE KEY-----`)

**Current limitations**:
- Regex-based detection only (no semantic analysis)
- No detection of environment variable references that might leak secrets

---

### 4. SDK

**Location**: `sdk/helios_sdk/`

**What it does**:
- Pre-installed in the golden base image
- Provides `propose_action()` for action agents to propose financial actions
- Provides `publish_event()` and `subscribe_events()` for inter-app communication

**API**:

```python
from helios_sdk import propose_action, publish_event, subscribe_events

# Propose a financial action (goes through audit sidecar)
result = propose_action(
    "payments",
    to="Northwind Supplies",
    amount=250.0,
    invoice="INV-0042"
)
# Returns: {"http_status": 200, "response": {"status": "executed", ...}}

# Publish an event to the tenant event bus
publish_event("receivable.rescheduled", invoice="INV-0042", new_due="2026-10-28")
# Returns: {"status": "published", "channel": "demo:events"}

# Subscribe to events (runs in a daemon thread)
def handle_event(event):
    print(f"Received: {event}")

subscribe_events(handle_event)
# Returns: Thread object
```

**Environment variables used** (injected automatically):
- `HELIOS_APP_NAME` — app name
- `HELIOS_TENANT` — tenant name
- `HELIOS_ACTION_TOKEN` — token for proposing actions
- `HELIOS_AUDIT_URL` — audit sidecar URL
- `HELIOS_REDIS_URL` — Redis connection string

**Current limitations**:
- No retry logic for failed proposals
- No batching for events
- No authentication on event bus (any app in the tenant can publish/subscribe)

---

### 5. Images

#### 5.1 Golden Base Image

**Location**: `images/app-base/Dockerfile`

**What it does**:
- Base image for all Qwen-generated apps
- Pre-installs: FastAPI, uvicorn, redis, httpx, PyYAML
- Pre-installs the `helios_sdk` module

**How it's used**:
- The deployment engine injects a Dockerfile that does `FROM helios-app-base:latest`
- Apps only need to provide their code + `helios.yaml`
- Builds are fast (single COPY layer)

**Current limitations**:
- Not automatically rebuilt when SDK changes
- No versioning (always uses `:latest`)

---

#### 5.2 Audit Sidecar Image

**Location**: `images/auditor/`

**What it does**:
- Holds the `EXECUTION_SECRET` (only credential that can call helios-core)
- Validates action proposals from the app container
- Enforces policies:
  - Tool allow-list (from `helios.yaml`)
  - Amount caps (currently hardcoded to $10,000)
  - Duplicate detection (120-second window)
- Logs all decisions to an in-memory audit trail
- Can query app logs via `GET http://host.docker.internal:8100/api/v1/apps/{container}/logs`

**API**:

```
POST /execute
  Headers: Authorization: Bearer <ACTION_TOKEN>
  Body: {"action": "payments", "payload": {...}}
  → 200: {"status": "executed|held_for_review", "audit_id": "...", ...}

GET /health
  → {"status": "ok", "role": "audit-agent", "app": "...", "allowed_tools": [...]}

GET /audit-log
  → {"decisions": [{"audit_id": "...", "decision": "...", ...}]}
```

**Environment variables** (injected by orchestrator):
- `HELIOS_APP_NAME` — app name
- `HELIOS_TENANT` — tenant name
- `EXPECTED_ACTION_TOKEN` — token to validate app proposals
- `HELIOS_EXECUTION_SECRET` — secret to call helios-core
- `HELIOS_CORE_URL` — helios-core URL
- `HELIOS_ENGINE_URL` — deployment engine URL (for log queries)
- `ALLOWED_TOOLS` — comma-separated list of allowed tools

**Current limitations**:
- Amount cap is hardcoded (not configurable per tenant/app)
- Duplicate detection is in-memory only (lost on restart)
- No intent analysis layer yet (this is what your teammate is building)
- Audit log is in-memory (max 500 entries, lost on restart)

---

#### 5.3 Core Image (Platform Tools)

**Location**: `images/core/`

**What it does**:
- Mock implementation of financial tools
- Stores knowledge graph nodes in SQLite
- Logs all tool calls in SQLite
- Returns simulated responses (no real payments/emails)

**API**:

```
POST /tools/{tool}
  Headers: Authorization: Bearer <EXECUTION_SECRET>
  Body: {"to": "...", "amount": 100.0, ...}
  → 200: {"tool": "payments", "status": "simulated_sent", "ref": "..."}

GET /kg/nodes
  Headers: Authorization: Bearer <EXECUTION_SECRET>
  → {"nodes": [{"entity": "invoice:INV-0042", "props": "{...}", "updated": 1234567890}]}
```

**Available tools**:
- `payments` — returns `{"status": "simulated_sent", "ref": "...", "to": "...", "amount": ...}`
- `email` — returns `{"status": "simulated_sent", "ref": "...", "to": "...", "subject": "..."}`
- `knowledge-graph` — upserts entity into `kg_nodes` table, returns `{"status": "upserted", "entity": "..."}`

**Database**:
- SQLite at `/tmp/helios_graph.db`
- Tables:
  - `tool_calls(ref, ts, tool, payload)` — audit log of all tool invocations
  - `kg_nodes(entity, props, updated)` — knowledge graph state

**Current limitations**:
- All tools are mocked (no real payments/emails)
- SQLite is not production-ready (no concurrent writes, no replication)
- No authentication beyond the execution secret
- No rate limiting

---

### 6. CLI

**Location**: `cli/helios.py`

**What it does**:
- Zips the app folder
- POSTs it to the deployment engine
- Returns the deployment response

**Usage**:

```bash
cd helios
.venv/bin/python cli/helios.py deploy /path/to/app --tenant demo
```

**Current limitations**:
- No validation before upload (errors come from the engine)
- No progress indication
- No support for git repos (only local folders)

---

### 7. Bootstrap Script

**Location**: `bootstrap.sh`

**What it does**:
- Checks if Docker daemon is running (starts OrbStack if not)
- Creates the ingress network
- Starts nginx with default config
- Creates Python virtualenv
- Installs dependencies
- Starts the deployment engine

**Usage**:

```bash
cd helios
./bootstrap.sh
```

**Current limitations**:
- Assumes OrbStack is installed (no fallback to Colima/Docker Desktop)
- nginx config is static (doesn't auto-reload on app deploy — the orchestrator handles this)

---

### 8. Test Script

**Location**: `test-deploy.sh`

**What it does**:
- Deploys the sample app
- Tests health endpoint
- Tests action proposal (payment)
- Tests duplicate detection
- Shows audit trail
- Tests event bus

**Usage**:

```bash
cd helios
./test-deploy.sh
```

**Current limitations**:
- Hardcoded to deploy `examples/sample-app` with tenant `demo`
- No cleanup of previous deployments

---

## Network & Routing

### How routing works

1. App is deployed to `tenant_demo_net` (internal) and `helios_ingress_net` (external)
2. Orchestrator gets the app's IP on `helios_ingress_net`
3. Orchestrator generates nginx config with two server blocks:
   - Hostname-based: `sample-collect-demo.helios.test` → app
   - Path-based: `localhost/apps/sample-collect-demo` → app
4. Orchestrator writes config to `.helios-run/nginx/sample-collect-demo.conf`
5. Orchestrator reloads nginx

### Accessing deployed apps

```bash
# Via path prefix (works without DNS setup)
curl http://localhost/apps/sample-collect-demo/health

# Via hostname (requires /etc/hosts entry)
echo "127.0.0.1 sample-collect-demo.helios.test" >> /etc/hosts
curl http://sample-collect-demo.helios.test/health
```

---

## Inter-App Communication

### Via HTTP (Docker DNS)

```python
import httpx

# Call another app in the same tenant
response = httpx.get("http://sample-collect:3000/health")
```

Container names are resolvable as hostnames within the tenant network.

### Via Events (Redis pub/sub)

```python
from helios_sdk import publish_event, subscribe_events

# Publish
publish_event("invoice.paid", invoice="INV-0042", amount=250.0)

# Subscribe
def handle(event):
    print(event)
subscribe_events(handle)
```

Events are published to `<tenant>:events` channel. All apps in the tenant receive all events.

---

## Security Model

### Zero-trust architecture

```
App Container (untrusted)
  ↓ proposes action (ACTION_TOKEN)
Audit Sidecar (trusted)
  ↓ validates + executes (EXECUTION_SECRET)
helios-core (platform tools)
```

- App containers only have `ACTION_PROPOSAL_TOKEN`
- Only audit sidecars have `EXECUTION_SECRET`
- App containers cannot reach helios-core directly
- Audit sidecars enforce allow-lists, amount caps, duplicate detection

### Current limitations

- No rate limiting on action proposals
- No encryption at rest for secrets
- No audit log persistence (in-memory only)
- No intent analysis layer yet

---

## What's Not Yet Built

1. **Intent analysis layer** — Your teammate is building this. Will sit between audit sidecar and helios-core.
2. **Real platform tools** — Current tools are mocked. Your other teammates are building real payment/email/KG APIs.
3. **Persistent audit logs** — Currently in-memory. Need to persist to database.
4. **Multi-tenant isolation at the engine level** — Currently all tenants share the same engine.
5. **Resource limits** — No CPU/memory limits on containers.
6. **Health checks post-deploy** — Only smoke test during deploy, no ongoing monitoring.
7. **Rollback on failure** — Old containers are removed before new ones start.
8. **Automatic golden image rebuild** — Need to rebuild when SDK changes.

---

## File Structure

```
helios/
├── engine/                    # Deployment engine
│   ├── app.py                 # FastAPI app, API endpoints
│   ├── orchestrator.py        # Docker lifecycle management
│   └── contracts.py           # Manifest validation, secret linting
├── images/
│   ├── app-base/              # Golden base image
│   │   └── Dockerfile
│   ├── auditor/               # Audit sidecar image
│   │   ├── Dockerfile
│   │   └── server.py          # FastAPI app, policy enforcement
│   └── core/                  # Platform tools image
│       ├── Dockerfile
│       └── server.py          # Mock tools, knowledge graph
├── sdk/helios_sdk/            # App Builder SDK
│   └── __init__.py            # propose_action, publish_event, subscribe_events
├── cli/helios.py              # Deployment CLI
├── examples/sample-app/       # Example app
│   ├── main.py
│   └── helios.yaml
├── bootstrap.sh               # Setup script
├── test-deploy.sh             # End-to-end test
├── requirements.txt           # Python dependencies
├── README.md                  # High-level overview
├── DEV-NOTES.md               # Design decisions, deviations
└── AGENT-BUILDERS-GUIDE.md    # Guide for agent builders
```

---

## Quick Start

```bash
# 1. Bootstrap the infrastructure
cd helios
./bootstrap.sh

# 2. Deploy the sample app
./test-deploy.sh

# 3. Access the app
curl http://localhost/apps/sample-collect-demo/health

# 4. Check the audit trail
curl http://localhost:8100/api/v1/apps/demo-sample-collect-app/logs
```

---

## Environment Variables

**For the deployment engine**:
- `DOCKER_HOST` — Docker daemon socket (default: `unix://$HOME/.orbstack/run/docker.sock`)

**For deployed apps** (injected automatically):
- `HELIOS_APP_NAME` — app name
- `HELIOS_TENANT` — tenant name
- `HELIOS_ACTION_TOKEN` — token for proposing actions
- `HELIOS_AUDIT_URL` — audit sidecar URL
- `HELIOS_REDIS_URL` — Redis connection string

**For audit sidecars** (injected automatically):
- `HELIOS_APP_NAME` — app name
- `HELIOS_TENANT` — tenant name
- `EXPECTED_ACTION_TOKEN` — token to validate app proposals
- `HELIOS_EXECUTION_SECRET` — secret to call helios-core
- `HELIOS_CORE_URL` — helios-core URL
- `HELIOS_ENGINE_URL` — deployment engine URL
- `ALLOWED_TOOLS` — comma-separated list of allowed tools

**For helios-core** (injected automatically):
- `HELIOS_EXECUTION_SECRET` — secret to authenticate tool calls
- `HELIOS_DB` — SQLite database path (default: `/tmp/helios_graph.db`)

# Agent Builders Guide

This guide is for teammates building the coding agent, action agent, and auditor agent. It covers deployment, inter-container communication, platform APIs, and the YAML contract.

---

## Deployment

### How to deploy an app

Your app needs two files:
1. `main.py` (or whatever your entry point is)
2. `helios.yaml` manifest

Then deploy via CLI:
```bash
cd /path/to/helios
./cli/helios.py deploy /path/to/your/app --tenant demo
```

The CLI zips your app folder and POSTs it to the deployment engine. The engine:
- Validates the manifest
- Injects a Dockerfile (you don't write one)
- Builds the image
- Runs a smoke test
- Attaches the audit sidecar
- Configures nginx routing

Your app is now live at `http://localhost/apps/<app-name>-<tenant>`.

### What you don't need to do

- Write Dockerfiles
- Manage containers
- Configure networking
- Handle secrets

The platform does all of that.

---

## Container Communication

### Network topology

Each tenant gets an isolated Docker network (e.g., `tenant_demo_net`). All containers for that tenant are on this network:
- Your app container
- Your audit sidecar
- `helios-core` (platform tools)
- `helios-redis` (event bus)

### How to reach other containers

Use the container name as hostname:
```python
# Reach helios-core (platform tools)
http://helios-core:9000/tools/payments

# Reach another app in the same tenant
http://<other-app-name>:3000/endpoint

# Reach Redis
redis://helios-redis:6379/0
```

DNS resolution is automatic within the tenant network.

### Example: calling another app

```python
import httpx

# Call another app in the same tenant
response = httpx.get("http://sample-collect:3000/health")
```

---

## Platform Communication

### Tool APIs (helios-core)

The platform exposes financial tools via helios-core. Your action agent proposes actions; the audit sidecar validates and forwards them.

**Available tools:**
- `payments` — send money
- `email` — send email
- `knowledge-graph` — read/write shared state
- `events` — publish to event bus

**How to use them:**

Use the SDK (pre-installed in the golden base image):
```python
from helios_sdk import propose_action

# Propose a payment
result = propose_action(
    "payments",
    to="Northwind Supplies",
    amount=250.0,
    invoice="INV-0042"
)
```

The SDK sends the proposal to your audit sidecar, which validates it and forwards to helios-core.

**Direct API access (if needed):**

```python
import httpx

# Call helios-core directly (requires EXECUTION_SECRET, which only the audit sidecar has)
response = httpx.post(
    "http://helios-core:9000/tools/payments",
    json={"to": "Northwind", "amount": 250.0},
    headers={"Authorization": "Bearer <EXECUTION_SECRET>"}
)
```

**Note:** Your app container does NOT have `EXECUTION_SECRET`. Only the audit sidecar has it. Your app must go through `propose_action()`.

---

## Database Interactions

### Knowledge Graph (shared state)

The knowledge graph is a shared database accessible via helios-core. Use it to store cross-app state (e.g., invoice status, customer info).

**Write to the knowledge graph:**
```python
from helios_sdk import propose_action

propose_action(
    "knowledge-graph",
    entity="invoice:INV-0042",
    props={"status": "paid", "amount": 250.0}
)
```

**Read from the knowledge graph:**

Query helios-core directly (requires `EXECUTION_SECRET`, so only the audit sidecar can do this):
```python
response = httpx.get(
    "http://helios-core:9000/kg/nodes",
    headers={"Authorization": "Bearer <EXECUTION_SECRET>"}
)
nodes = response.json()["nodes"]
```

### Tool call audit log

Every tool call is logged in helios-core's SQLite database (`/tmp/helios_graph.db`, table `tool_calls`). This is for auditing, not for your app to query directly.

---

## YAML Contract

### helios.yaml manifest

Your app must include a `helios.yaml` file at the root of your app folder:

```yaml
name: sample-collect
tenant: demo
entry: main:app
port: 3000
permissions:
  - payments
  - email
  - knowledge-graph
  - events
```

**Fields:**
- `name` — app name (used in URLs and container names)
- `tenant` — which tenant this app belongs to
- `entry` — Python module:app object (e.g., `main:app`)
- `port` — port your app listens on (default 3000)
- `permissions` — list of tools your app is allowed to use

**Permissions:**

The `permissions` list determines what your audit sidecar will allow. If you try to use a tool not in this list, the audit sidecar rejects it.

Example: if `permissions` includes `payments` but not `email`, your app can propose payments but not emails.

---

## Event Bus (Redis)

### Publish events

```python
from helios_sdk import publish_event

publish_event("receivable.rescheduled", invoice="INV-0042", new_due="2026-10-28")
```

### Subscribe to events

```python
from helios_sdk import subscribe_events

def handle_event(event):
    print(f"Received: {event}")

subscribe_events(handle_event)
```

Events are published to the tenant's Redis channel (`<tenant>:events`). All apps in the same tenant receive all events.

---

## Environment Variables

Your app container receives these environment variables automatically:
- `HELIOS_APP_NAME` — your app's name
- `HELIOS_TENANT` — your tenant
- `HELIOS_ACTION_TOKEN` — token for proposing actions to the audit sidecar
- `HELIOS_AUDIT_URL` — URL of your audit sidecar (e.g., `http://sample-collect-audit:8080/execute`)
- `HELIOS_REDIS_URL` — Redis connection string

You don't need to configure these. They're injected at deploy time.

---

## Quick Reference

### Deploy an app
```bash
./cli/helios.py deploy /path/to/app --tenant demo
```

### Access your app
```
http://localhost/apps/<app-name>-<tenant>
```

### Propose an action
```python
from helios_sdk import propose_action
propose_action("payments", to="...", amount=100.0)
```

### Publish an event
```python
from helios_sdk import publish_event
publish_event("invoice.paid", invoice="INV-0042")
```

### Call another app
```python
import httpx
httpx.get("http://<other-app>:3000/endpoint")
```

### Reach helios-core
```python
httpx.get("http://helios-core:9000/tools/payments")
```

---

## What's Next

The intent analysis layer is being built separately. Once it's ready, the audit sidecar will call it to validate that actions align with user intent. You don't need to do anything for this yet — it's transparent to your agents.

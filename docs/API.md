# Helios API Reference

Every HTTP surface and SDK call on the platform, as implemented. Money is
integer cents everywhere; dates are ISO strings.

## 1. Deployment engine — `http://127.0.0.1:8100`

| Method and path | Purpose | Notes |
|---|---|---|
| `GET /health` | liveness | no daemon needed |
| `GET /api/v1/doctor` | daemon, platform hash, base image, nginx, deployed apps | |
| `POST /api/v1/deploy` | multipart `bundle` (zip) + form `tenant` | 200 with the deploy record; 422 (or the stage's status) with `{stage, error, problems[], hint, logs}` |
| `GET /api/v1/apps` | deployed apps and containers | |
| `GET /api/v1/apps/{container}/logs?tail=` | a container's stdout | |
| `GET /api/v1/tenants/{tenant}/logs?app=&tail=` | **central evidence**: every app's `.jsonl` logs read from its data volume; hash-chained files carry `chain: {verified, intact, problems}` | |
| `DELETE /api/v1/apps/{name}?tenant=` | undeploy by container, app id or tenant; routes removed, volumes kept | |

Deploy stages, in order: `manifest`, `static_audit`, `build`, `smoke_test`,
`routing`, `execution`. `problems` is complete: fix everything in one pass.

### `helios.yaml`

```yaml
name: my-app                   # [a-z0-9-]; container name, DNS alias, URL path
tenant: ""                     # optional; --tenant overrides
entry: server:app              # module:attribute of the ASGI app
port: 3000
permissions: [email, collect]  # subset of payments, email, knowledge-graph, events,
                               # forecast, market-data, spreadsheets, collect; [] grants nothing
env: [DASHSCOPE_API_KEY]       # NAMES only; values come from the engine's environment
audit_manifest: agents/my-app-audit.yaml
data_dir: /data                # absolute; a named volume is mounted here in app and sidecar
```

Unknown fields are rejected. The app is reachable at
`http://localhost/apps/<name>-<tenant>/`; nginx strips the prefix and sends
`X-Forwarded-Prefix`, so build links from it.

### CLI

```
helios deploy <dir> [--tenant T] [--engine URL] [--include-tests]
helios apps | logs <container> [--tail N] | evidence --tenant T [--app A] [--json]
helios undeploy <name> [--tenant T] | doctor
```

## 2. Audit sidecar — `http://<app>-audit:8080` (tenant network)

The platform's gate in front of the tenant core. Holds `HELIOS_EXECUTION_SECRET`;
the app holds only `HELIOS_ACTION_TOKEN`.

| Method and path | Purpose |
|---|---|
| `GET /health` | status, granted families, manifest loaded, enforced and delegated rules, chain verification |
| `GET /audit-log` | recent decisions and chain status |
| `POST /execute` | bearer `HELIOS_ACTION_TOKEN`; body `{"action": "<tool>", "payload": {...}}` |

`/execute` outcomes:

| Response | Meaning |
|---|---|
| `{"status": "executed", "audit_id", "result"}` | forwarded to core, the tool ran |
| `{"status": "denied", "reason", "findings[]"}` | refused by a fact-free rule, the allow-list, duplicate detection, core's own gate, or the ledger (`core.ledger_rejected`) |
| `{"status": "allowed", "forwarded": false}` | an app-declared tool, adjudicated and handed back to the app |
| `502 {"stage": "execution", "retryable": true}` | core unreachable or unavailable: an outage, never a decision |

Rules needing a trusted fact are skipped here and listed under
`delegated_to_in_app_gate`; the in-app auditor enforces them and is the only gate
that can hold.

## 3. Tenant core — `http://helios-core:9000` (tenant network)

`python -m fin_tools`. Bearer `HELIOS_EXECUTION_SECRET` on every route except
`/health`. State persists to `/data/world_state.sqlite3`.

| Method and path | Purpose |
|---|---|
| `GET /health` | `{status, role, tools}` |
| `GET /tools` | the catalog: name, description, schemas, `side_effects` |
| `POST /call` | `{"tool": "payments.send", "args": {...}}` |
| `GET /kg/nodes` | verified knowledge-graph nodes |

`/call` always answers 200 with one of three envelopes:

```json
{"ok": true,  "result": {...}}
{"ok": false, "rejected": true, "error": "..."}        // a gate or validator refused THIS request
{"ok": false, "unavailable": true, "retryable": true, "error": "..."}   // nobody decided
```

A tool may run and still report `result.state == "rejected"` (for example
`payments.send` with no owner-confirmed `auth_code`); the sidecar records that as
a denial with the ledger's reason.

Tool families and the catalog: `entry-point/references/financial-tools.md`.

## 4. App SDK — `helios_sdk` (pre-installed in the app image)

```python
from helios_sdk import propose_action, publish_event, subscribe_events

propose_action("email.send", to=..., subject=..., body=...)   # -> sidecar /execute
publish_event("invoice.extension_agreed", invoice="INV-1", new_due="2026-11-20")
subscribe_events(handler)   # daemon thread; handler(dict) per tenant event
```

Environment the engine sets in an app container: `HELIOS_APP_NAME`,
`HELIOS_TENANT`, `HELIOS_ACTION_TOKEN`, `HELIOS_AUDIT_URL`, `HELIOS_REDIS_URL`,
`HELIOS_DATA_DIR`, plus the names listed under `env:` in `helios.yaml`.

Events are `{"event": "<entity>.<event>", "data": {...}, "app": "<name>"}` on the
Redis channel `<tenant>:events`.

## 5. Application HTTP contract

Every app serves `GET /health` → 200, constant, building nothing. The flagships
also expose `GET /` (dashboard), `GET /holds` (approval queue), `POST /run` or
`POST /runs` (run scenarios), and per-app pages. Cash Planner accepts
`POST /events` with a tenant event (the same shape the bus delivers) and lists
them at `GET /events`.

## 6. Builder CLI — `helios-app`

```
helios-app questions                              # the 16-question intake template
helios-app spec-from-intake <intake.yaml> [--app-id X] [-o helios-app.yaml]
helios-app new <dir> --spec helios-app.yaml [--intake intake.yaml] [--dry-run]
helios-app validate <dir>                         # exit 0 clean, 1 problems, 2 usage
```

`agent-toolkit inspect | validate [--audit] | demo` works on an action manifest.
`python -m agent_integration.cli demo | selfcheck | inspect | doctor | run` drives
the gated loop.

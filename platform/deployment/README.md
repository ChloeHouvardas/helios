# Helios Deployment Engine

Turns an app directory into a running, audited, routed container. Written fresh
from the Mac-mini build; every mismatch found while converting it is corrected
here and in the code.

```
helios.yaml + ASGI entry + GET /health   ->   zipped, linted, built on a golden
                                              image, smoke-tested, attached to an
                                              audit sidecar, routed behind nginx
```

## Quick start

```bash
./bootstrap.sh                                    # daemon + nginx + engine on :8100
../../.venv/bin/python cli/helios.py doctor       # what is up, what is not
../../.venv/bin/python cli/helios.py deploy ../../apps/helios-collect --tenant demo
open http://localhost/apps/helios-collect-demo/
```

`bootstrap.sh` is idempotent. It does **not** kill a process already on :8100 --
another session may own it. It prints how to check whether that process is
running *this* engine (`GET /api/v1/doctor` → 200; a 404 means it is older), and
`HELIOS_ENGINE_PORT` runs a second engine beside it.

## What an app must provide

Three things, and only three:

| | |
|---|---|
| `helios.yaml` | the deployment manifest (below) |
| an ASGI app | `entry: server:app` — uvicorn runs it |
| `GET /health` | **must return 200 with no dependencies** |

The engine injects its own `Dockerfile`, so an app supplies none.

### `helios.yaml`

```yaml
name: helios-collect          # sanitised to [a-z0-9-]; becomes container name, DNS alias, URL path
tenant: ""                    # optional; --tenant overrides it
entry: server:app             # module:attribute
port: 3000
permissions: [email, collect] # subset of: payments, email, knowledge-graph, events,
                              #           forecast, market-data, spreadsheets, collect
env:                          # NAMES only. The engine resolves values from its own
  - DASHSCOPE_API_KEY         # environment at container creation. Only names matching
  - HELIOS_LLM_MODEL          # ^(HELIOS_|DASHSCOPE_|OPENAI_|TUSHARE_|EMAILTOOL_) are
                              # accepted. Values never appear in the manifest, in a
                              # deploy response, or in a log line.
audit_manifest: agents/helios-collect-audit.yaml   # path inside the bundle
data_dir: /data               # must be absolute; a named volume is mounted here in
                              # BOTH the app and its audit sidecar
```

`helios.yaml` is **not** `helios-app.yaml`. That file is the app-builder manifest
describing the agents. Different schemas; an app may legitimately have both.

### Why `/health` must be constant

The smoke test runs the container on an isolated network with **no Redis, no
audit sidecar, no model key**, polling `/health` up to 45×1s. Build the app
lazily on first use of a real page, not at import and not in `/health`.

## Pipeline

1. **Ingest.** Unzip (zip-slip guarded), find `helios.yaml` at the root or one
   wrapper folder down.
2. **Validate + lint.** Every manifest problem is collected and reported at once,
   each naming the field, the problem and what to write instead. Then a secret
   scan over `.py .js .ts .json .yaml .yml .env .txt .sh .cfg .ini .toml`.
3. **Build.** On the golden base image, which already contains the platform
   packages -- the app layer is a single `COPY`, with no `pip install`.
4. **Smoke test.** Isolated network, `GET /health` must return 200.
5. **Attach.** Per-tenant internal bridge network, `helios-redis-<t>`,
   `helios-core-<t>` (the tool service, holds `HELIOS_EXECUTION_SECRET`), then the app's audit
   sidecar and the app itself. A named volume `helios-data-<t>-<a>` is mounted at
   `data_dir` in both, so action logs, the hash-chained audit log, holds and
   replay state survive a redeploy.
6. **Route.** nginx, prefix-stripped.

### Golden base image

`images/app-base/Dockerfile` is `python:3.13-slim` with all four platform
packages installed into `/opt/helios/platform` -- `audit-agent-builder` and
`agent-integration` declare `requires-python = ">=3.13"`, so 3.12 cannot host
them.

The image tag is a **content hash of `platform/`**: `helios-app-base:<hash>`,
also tagged `:latest`. A platform change therefore rebuilds it; an unchanged
platform reuses the cache. This replaces the never-rebuild rule, under which a
platform fix silently never reached a deployed app. Third-party deps are a cached
layer *above* the platform source, so a platform-only edit rebuilds in seconds
after the first build. Tests and `__pycache__` are excluded from the hash -- they
change on every run and cannot change runtime behaviour.

The build context is the **repository root**, because the base image embeds
`platform/*`, which does not live under this directory. `HELIOS_REPO_ROOT`
overrides discovery; `.dockerignore` at the repo root keeps `.git` and a ~100 MB
`agent-integration/.venv` out of every build.

### Blue/green redeploy

Candidates come up as `<t>-<a>-app-next` / `-audit-next` with `-next` aliases and
are health-checked **on the tenant network**. Only then is the live pair removed
and the candidates renamed into place. On any failure the temporaries are removed
and **the previous deployment keeps serving**. The old behaviour force-removed the
running pair first, so a bad redeploy took a working deployment down with it.

### Routing

Two files carry the app IP -- the per-slug `<slug>.conf` and the shared
`default.conf` -- and **both are rewritten every deploy**. The `default.conf`
block is marker-delimited (`# helios:begin <slug>` … `# helios:end <slug>`) so a
redeploy replaces exactly its own region: no duplicates accumulate, and one app's
route cannot be spliced into another's. `nginx -t` runs before reload, so a
broken generated config is refused rather than applied.

The ingress IP is read with retries (10×0.5s). Reading it once immediately after
`start()` can yield `""`, producing `proxy_pass http://:3000` -- a 502 with no
explanation, long after the deploy reported success. If it is still empty the
deploy fails with a named-field error instead.

Apps are reachable at `http://localhost/apps/<app>-<tenant>/` (prefix stripped
via `rewrite`) and `http://<app>-<tenant>.helios.test` (needs a hosts entry). The
engine sends `X-Forwarded-Prefix`; apps should build URLs from it, because
relative links break at nested routes.

## The tenant core

One per tenant: `platform/financial-tools` served by `python -m fin_tools` on
port 9000 of the tenant network. It is the same registry the apps compose
in-process -- payments, collect, email, forecast, market, sheets, graph -- behind
`GET /tools`, `POST /call` and `GET /kg/nodes`, bearer-authenticated with the
tenant execution secret that only the audit sidecar holds. Its ledger, balances,
outbox and verified graph nodes persist to SQLite on the named volume
`helios-data-<t>-core`, so a redeploy does not lose money. The intent-signing
secret (`HELIOS_INTENT_SECRET`) is separate from the execution secret and lives
only in core: authenticating a caller and authorising a payment are different
powers, and `payments.send` without an owner-confirmed `auth_code` is **rejected
by core** whatever the bearer token says.

Market data answers from the committed fixtures unless the engine's own
environment carries `TUSHARE_TOKEN`, in which case the core goes live; every
result says which mode answered. The core image is keyed by the platform hash
like `app-base`, so a tools change reaches it on the next deploy.

## The audit sidecar

One per app. It holds `HELIOS_EXECUTION_SECRET` and is the **only** container
that may reach the tenant core. The app holds only `HELIOS_ACTION_TOKEN`.

Policy comes from the app's own audit manifest, which the engine copies into
`/policy/audit.yaml` **before** the sidecar starts -- the app has no volume and no
exec access there, so it cannot rewrite its own policy afterwards. The sidecar
loads it with `audit_agent`'s own `AuditManifest.load` and writes decisions with
`audit_agent`'s own hash-chained `AuditLog`, so the two gates cannot drift.

Only the **fact-free subset** is enforced: tool allow-list, constant argument
ceilings, forbidden values, required arguments, `max_calls`, and duplicate
detection within a window. Rules needing a trusted fact are **skipped** --
`audit_agent` fails such a rule closed, so evaluating it here without a fact
source would deny every request it touched. Every skipped rule is disclosed at
`GET /health` under `delegated_to_in_app_gate`.

Two gates, one direction of authority: the in-app `audit_agent` (which has facts
and an owner) is authoritative for holds and resumes; the sidecar may **deny or
forward, never approve**. See `specs/decisions.md` §"Two audit gates".

A proposal names a **tool** (`payments.send`, `email.send`,
`graph.publish_verified`), and its family prefix must be one the manifest's
`permissions` grants; a tool the app's own audit manifest declares is adjudicated
and returned to the app to run itself. Granted tools are forwarded to the core's
`POST /call` and the three envelopes are kept distinct: `ok` is recorded as
executed, `rejected` (core's own gate refused *this* request, e.g. no intent
code) is recorded as a denial and returned as `status: denied`, and
`unavailable` is a 502 with `retryable: true` and no decision in the chain. The
platform's last-resort cap is on `amount_cents` for the payments family,
integer cents, `MAX_PAYMENT_AMOUNT_CENTS` (default 1,000,000).

`GET /health` also reports chain verification. `GET /audit-log` returns recent
decisions. The authoritative record is `<data_dir>/audit/sidecar-log.jsonl`.

A non-2xx from core is recorded as `error` and surfaced as a failure. The old
build checked no status code at all, so a 404 -- core not implementing the
requested tool -- was recorded as `executed`: a compliance log claiming a side
effect that never happened.

## API

| | |
|---|---|
| `GET /health` | liveness. Does not need a daemon. |
| `GET /api/v1/doctor` | daemon, platform hash, base image, nginx, deployed apps |
| `POST /api/v1/deploy` | multipart `bundle` + form `tenant` |
| `GET /api/v1/apps` | deployed apps and containers |
| `GET /api/v1/apps/{container}/logs?tail=200` | container logs |
| `DELETE /api/v1/apps/{name}?tenant=` | undeploy by container, app id, or tenant; every removed app's route goes with it |
| `GET /api/v1/tenants/{tenant}/logs?app=&tail=` | **central evidence**: every app's action log, audit chain and sidecar log, read from the data volumes, each chain verified |

Errors are 422 (or the status `DeployError` names) with a body FastAPI wraps
under `detail`:

```json
{"stage": "manifest|static_audit|build|smoke_test|routing|execution",
 "error": "one-line summary",
 "problems": ["each field, its problem, what to write instead"],
 "hint": "optional next action",
 "logs": "last 4000 chars (build/smoke only)"}
```

`problems` is plural and complete on purpose: an author should not redeploy four
times to find four errors. Note the key differs by stage -- `static_audit` uses
`problems`, `manifest` uses `error` plus `problems`.

`DELETE` resolves `name` as an exact container name, then an app id (scoped to
`tenant` if given), then a tenant id. Only containers labelled `helios.managed=true`
are ever touched. **Data volumes are not removed**: a volume holds the
hash-chained audit log, which is compliance evidence, and destroying it is not an
implied consequence of "undeploy the app".

## CLI

```bash
helios deploy <dir> [--tenant T] [--engine URL] [--include-tests]
helios apps   [--engine URL]
helios logs <container> [--tail N] [--engine URL]
helios undeploy <name> [--tenant T] [--engine URL]
helios evidence --tenant T [--app A] [--tail N] [--json] [--engine URL]
helios doctor [--engine URL]
```

Exit codes, because a coding agent reads them to decide what to do next: `0`
success, `1` the engine rejected it, `2` refused (a `.env` was present), `3`
unreachable, `4` bad usage.

Bundles exclude `__pycache__ .venv venv node_modules .git .helios-run
.pytest_cache .mypy_cache .ruff_cache .idea .vscode`, any `Dockerfile`, and by
default `runs/ tests/ test/` and `test_*.py` (`--include-tests` to override). A
`.env` anywhere in the bundle directory is a hard failure with exit 2 -- a real
key would otherwise be uploaded, and the secret linter would then reject it with
a confusing "possible hardcoded key" that does not name the actual cause.

`--engine` resolves flag > `$HELIOS_ENGINE_URL` > `http://127.0.0.1:8100`, and
works both before and after the subcommand.

## Layout

```
platform/deployment/
├── bootstrap.sh              daemon + nginx + engine, idempotent
├── cli/helios.py             the CLI above
├── engine/
│   ├── contracts.py          helios.yaml schema, env allow-list, secret lint
│   ├── app.py                FastAPI control plane
│   └── orchestrator.py       all Docker lifecycle; base-image hash; nginx; rollback
├── images/
│   ├── app-base/Dockerfile   python:3.13 + the four platform packages
│   ├── auditor/              the sidecar: manifest-driven, fact-free policy
│   └── core/                 the tenant core: fin_tools served over HTTP (python -m fin_tools)
├── sdk/helios_sdk/           propose_action / publish_event / subscribe_events
└── examples/sample-app/
```

## Tests

```bash
cd platform/deployment && PYTHONPATH=. ../../.venv/bin/python -m pytest tests
```

docker-py mocked throughout -- no daemon, no network, no image builds.
They cover nginx rewriting on redeploy, marker isolation between slugs, IP
retry, the rollback guarantee driven through the real `deploy()` path, base-image
hashing, and the sidecar's allow/deny/cap/forbidden/duplicate/chain behaviour.

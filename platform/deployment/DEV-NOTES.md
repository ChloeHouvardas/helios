# Dev notes — Helios local infra build

For the team (English). What we set up on the Mac mini, why, and what's open.

> **Corrections applied by session p2-deploy.** These are the original Mac-mini
> author's notes and are kept as written, but four statements no longer match the
> code. `platform/deployment/README.md` is authoritative.
>
> 1. The token env var is `HELIOS_ACTION_TOKEN` (what `helios_sdk` reads), not
>    `ACTION_PROPOSAL_TOKEN`. The sidecar verifies it against
>    `EXPECTED_ACTION_TOKEN`. (§3 below, and the sidecar's own docstring.)
> 2. "no outbound internet for user code" (§4) is **false** and is no longer
>    claimed. `tenant_<id>_net` is `internal=True`, but an app container also
>    joins `helios_ingress_net` because nginx must reach it, and that network has
>    egress. Making the claim true would mean putting app containers on the
>    tenant network only — an open hardening item.
> 3. §4 still says "for Traefik"; §1 already corrects this. The ingress is
>    **nginx**. The old `bootstrap.sh` also printed "traefik entry" and "traefik
>    dash" while starting `nginx:alpine`; it has been rewritten.
> 4. `HELIOS_ENGINE_URL` is **no longer injected** into the auditor. It was
>    injected and never read, so the sidecar no longer queries the engine for app
>    logs. It reads its policy from `/policy/audit.yaml`, which the engine copies
>    in *before* the sidecar starts.
>
> The sidecar's policy is also no longer hard-coded: it is derived from the app's
> own audit manifest (fact-free subset only), and decisions are written to a
> hash-chained JSONL under `data_dir` using `audit_agent`'s own log writer.
> See `specs/decisions.md` §"Two audit gates".

## What was built (maps to the LLM design doc section by section)

- **§1 Orchestration** — `engine/orchestrator.py` is the `OrchestratorDriver`
  wrapping docker-py. FastAPI control plane (`engine/app.py`). No K8s. No
  Portainer. **Reverse proxy is nginx, not Traefik** — the design doc
  recommended Traefik, but its embedded Docker client (API v1.24) is
  incompatible with OrbStack's engine (min v1.40). Nginx provides identical
  routing; the engine regenerates configs on each deploy and reloads
  automatically. **Redis is the event bus** (pub/sub on `<tenant>:events`),
  not a router.
- **§2 Golden base + manifest contract** — `helios-app-base:latest` pre-bakes
  FastAPI/uvicorn/redis/httpx + the `helios_sdk` module. Qwen-generated apps
  ship **only** `main.py` + `helios.yaml`; the engine injects the Dockerfile
  server-side (`FROM helios-app-base`) so builds are one COPY layer.
  `cli/helios.py` implements the "1-line deploy" (zip + POST); git ingestion
  not implemented — the zip path is strictly better for the Surprise Task.
- **§3 Container split** — App+Action Agent in one container (untrusted, gets
  only `ACTION_PROPOSAL_TOKEN`); Audit Agent is a **separate container from
  the official `helios-auditor` image** that the engine starts automatically
  per app — Qwen never builds it. The auditor holds `EXECUTION_SECRET` and is
  the only container that may call helios-core `/tools/*`. Policy implemented:
  tool allow-list from `helios.yaml`, per-payment amount cap, duplicate
  detection within a window → `held_for_review`. The audit sidecar can query
  app logs via `GET /api/v1/apps/{container}/logs` for post-deploy debugging
  (accessible via `host.docker.internal:8100` from any container).
- **§4 Multi-tenant** — per-tenant `tenant_<id>_net` created `internal=True`
  (no outbound internet for user code — security talking point), plus shared
  `helios_ingress_net` for Traefik. Redis + core run per tenant. Inter-app
  calls via embedded DNS (`http://<app-name>:3000`); cross-app reactivity via
  Redis pub/sub channel `<tenant>:events`.
- **§5 Pipeline** — ingest+lint → build → sandbox smoke test with
  crash-log capture (`container.logs()` piped back as JSON for Qwen's retry
  loop) → sidecar attach → Traefik labels, zero reload. All 422 responses are
  structured so a coding agent can self-correct.

## Deviations from the doc (intentional, hackathon-scoped)

1. **SQLite instead of Postgres** inside core — one less container per tenant;
   schema lives behind one service, swap cost is low.
2. **Colima instead of Docker Desktop** on this Apple Silicon Mac — CLI-only,
   scriptable, no GUI license prompts. Docker socket:
   `unix://$HOME/.colima/default/docker.sock` (exported by bootstrap.sh).
3. **Path-prefix routing** (`/apps/<app>-<tenant>`) as the primary URL so the
   demo works with zero `/etc/hosts` edits; host-rule routing
   (`*.helios.test`) is also configured and works if you add hosts entries.
4. **Static secret linting** is regex-based (sk-, AKIA, ghp_, private keys) —
   catches the obvious hardcoded-key rule violation; extend before judging.

## Failure-mode behavior worth demoing

- Missing `helios.yaml` / bad permissions → 422 `stage: manifest`.
- Hardcoded key in app code → 422 `stage: static_audit`.
- Build error → 422 `stage: build` with last 4000 chars of build log.
- App crashes at startup → 422 `stage: smoke_test` with container traceback.
- App tries an ungranted tool → auditor returns `held_for_review`, core never
  sees the call. Show `demo-sample-collect-audit` logs — the app container
  literally cannot reach `helios-core` auth, only propose.

## Next steps (candidates, not done)

- [ ] Second seed app under `examples/` (e.g. cash-planner subscribing to
      `receivable.rescheduled`) to show reactive cross-app behavior live.
- [ ] Rollback-on-failed-startup (doc step 4 mentions keeping previous tag).
- [x] `/apps/{container}/logs` GET — wired into the audit feedback loop.
      Qwen can query logs post-deploy via `GET /api/v1/apps/{container}/logs`.
      The audit sidecar can also access this endpoint via
      `host.docker.internal:8100` for post-deploy debugging.
- [ ] Tushare data endpoint in core (organizer data source per rules).
- [ ] Rebuild golden images automatically when `sdk/` changes
      (`orchestrator.ensure_base_images` currently caches by tag).

## Reset

```bash
cd helios
docker rm -f $(docker ps -aq --filter label=helios.managed=true)
docker network rm tenant_demo_net helios_ingress_net helios_sandbox_net
colima stop   # or `colima delete` to nuke the VM entirely
./bootstrap.sh
```

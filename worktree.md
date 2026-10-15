# worktree: `p2-deploy`

**Branch:** `worktree-p2-deploy` · **Base:** `chloe-dev` @ `cc96f0a` · **Worktree:** `.qwen/worktrees/p2-deploy`
**Scope:** 52 files changed, 7877 insertions(+), 5 deletions(-) across 7 commits.

> This document describes **what this worktree contains and what it does**. It is
> not a merge plan and says nothing about other worktrees — a separate agent owns
> merge sequencing and cross-worktree management. The only merge-relevant facts
> here are the five pre-existing files this branch modified (§6), listed so nothing
> is discovered by surprise.

---

## 1. What this worktree delivers

**Validation + Deployment for Helios.** Before this branch, deployment did not
exist in any form: `HANDOFF.md` §2 recorded it as *"Deployment: does not exist. No
hosting, no artifact format, no deployment engine."* The only deployment-adjacent
code was an untracked copy of a Mac-mini build sitting under
`OTHER-TEAMMATE-STUFF/`, invisible to git, absent from every worktree, and
unreviewable.

After it, an app directory becomes a running, audited, routed container:

```
app dir (helios.yaml + ASGI entry + GET /health)
  1. zipped and secret-linted
  2. built on a golden base image that already contains the platform packages
  3. smoke-tested on an isolated network
  4. attached to a per-app audit sidecar holding the execution secret
  5. routed behind nginx
  → http://localhost/apps/<app>-<tenant>/
```

Both existing apps (`apps/helios-collect`,
`entry-point/examples/invoice-payer`) were converted to this contract and were
deployed and verified live during this session.

**Verification state at handoff: 470 passed, 0 skipped** (`scripts/test-all.sh`).
Both apps reachable through nginx with working approval queues. Details in §7.

---

## 2. The deployment contract

Three things an app must provide, and only three:

| | |
|---|---|
| `helios.yaml` | the deployment manifest |
| an ASGI app | `entry: server:app` — uvicorn runs it |
| `GET /health` | **must return 200 with no dependencies** |

The engine injects its own `Dockerfile`, so an app supplies none.

```yaml
name: helios-collect          # sanitised to [a-z0-9-]; becomes container name, DNS alias, URL path
tenant: ""                    # optional; --tenant overrides it
entry: server:app             # module:attribute
port: 3000
permissions: [email, collect] # payments, email, knowledge-graph, events,
                              # forecast, market-data, spreadsheets, collect
env:                          # NAMES only. The engine resolves values from its own
  - DASHSCOPE_API_KEY         # environment at container creation. Only names matching
  - HELIOS_LLM_MODEL          # ^(HELIOS_|DASHSCOPE_|OPENAI_|TUSHARE_|EMAILTOOL_) are
                              # accepted. Values never appear in the manifest, in a
                              # deploy response, or in a log line.
audit_manifest: agents/helios-collect-audit.yaml   # path inside the bundle
data_dir: /data               # must be absolute; a named volume is mounted here in
                              # BOTH the app and its audit sidecar
```

`helios.yaml` is **not** `helios-app.yaml`. The latter already existed in both apps
and describes the agents; the two have different schemas and an app dir legitimately
holds both. This worktree added `helios.yaml` alongside it and did not overwrite
anything.

**Why `/health` must be a constant.** The smoke test runs the container on an
isolated network with **no Redis, no audit sidecar and no model key**, polling
`/health` up to 45×1s. Any health check needing those fails every deploy. Both
converted apps therefore build lazily on first use of a real page, under a lock.

**Field validation** reports every problem in one pass rather than raising on the
first, and fails closed. Unknown fields are rejected, so a typo surfaces loudly
instead of being silently ignored. Each message names the field, the problem and
what to write instead, because the usual author is a model that has to act on it.

---

## 3. Contents

### `platform/deployment/` — the engine (new subtree)

Imported verbatim from the untracked Mac-mini copy in its own commit (`ce3efbd`),
then fixed in subsequent commits. The import was separated on purpose: **the fixes
are reviewable as a diff against a known-good baseline** rather than arriving as a
wall of new code. `OTHER-TEAMMATE-STUFF/` itself was not modified.

| Path | Status | What it does |
|---|---|---|
| `engine/contracts.py` | modified | `helios.yaml` schema, env-name allow-list, secret lint |
| `engine/orchestrator.py` | modified | All Docker lifecycle: base-image hashing, tenant infra, blue/green deploy, nginx routing, rollback, undeploy |
| `engine/app.py` | modified | FastAPI control plane. **New:** `GET /api/v1/doctor`, `DELETE /api/v1/apps/{name}` |
| `cli/helios.py` | modified | Five subcommands: `deploy`, `apps`, `logs`, `undeploy`, `doctor`. Meaningful exit codes |
| `images/app-base/Dockerfile` | modified | Golden image: `python:3.13-slim` + all four platform packages |
| `images/auditor/Dockerfile` | modified | Sidecar image; installs `audit-agent-builder` so it can import the real manifest loader and log writer |
| `images/auditor/server.py` | modified | Manifest-driven, fact-free policy gate (§5) |
| `images/core/Dockerfile` | modified | Build-context path fix only; `server.py` untouched |
| `bootstrap.sh` | rewritten | Daemon detection + nginx + engine, idempotent |
| `README.md` | rewritten fresh | Authoritative deployment documentation |
| `SPEC-SHEET.md`, `DEV-NOTES.md`, `AGENT-BUILDERS-GUIDE.md` | corrected | Mismatches with the code fixed; the original author's notes are preserved under a corrections block |
| `tests/test_orchestrator.py`, `tests/test_auditor_sidecar.py` | **new** | 36 tests, docker-py mocked — no daemon, network or image builds needed |
| `engine/__init__.py`, `sdk/helios_sdk/`, `images/core/server.py`, `examples/sample-app/`, `requirements.txt`, `test-deploy.sh` | pristine | Imported, never modified |

### `scripts/` — new

- `bootstrap-venv.sh` — creates `.venv` on Python 3.13+, installs the engine's
  pinned requirements and the platform packages editable. Idempotent.
- `test-all.sh` — runs all six suites, reports per-suite counts, exits non-zero on
  any failure. This is the entry point the session rules refer to; **it did not
  exist anywhere in the repo before this branch.**

### Root

- `.dockerignore` — new, and required: the build context became the repo root
  (§4), so without it every build uploads `.git` and a ~100 MB
  `agent-integration/.venv` to the daemon.
- `.gitignore` — adds `.helios-run/`, which holds per-tenant execution secrets.

### The two converted apps

Each gained, **without changing its CLI, scenarios or existing tests**:
`helios.yaml`, `server.py`, six templates, `style.css`, and `tests/test_server.py`
(18 tests for helios-collect, 13 for invoice-payer).

The two `server.py` files are deliberately **different runtime shapes** — one
composes `build_integration()`, the other a hand-rolled `GatedRuntime` with a
`WebOwner` that aborts a run at a hold so the web queue has something to show. Both
are working reference implementations of the same contract.

---

## 4. The behaviour this worktree changed

Each item was verified against the imported source, not assumed.

**Routing reflects the running container.** The import inserted the
`/apps/<slug>` nginx block only when absent, so a redeploy onto a new container IP
kept proxying to a container that no longer existed. Two files carry the app IP —
the per-slug `.conf` and the shared `default.conf` — and only the first was
refreshed. Both are now rewritten every deploy, with the block delimited by
`# helios:begin/end <slug>` markers so a redeploy replaces exactly its own region
and can never splice into another app's. `nginx -t` runs before reload, so a broken
generated config is refused rather than applied.

**Ingress IP is retried, not guessed.** It was read once immediately after
`start()`, where Docker may not have assigned it yet, producing
`proxy_pass http://:3000` — a 502 with no explanation, long after the deploy
reported success. Now retried 10×0.5s, and if still empty the deploy fails with a
named-field error.

**A failed redeploy leaves the previous deployment serving.** Candidates come up
under `-next` names with `-next` aliases and are health-checked **on the tenant
network**. Only then is the live pair removed and the candidates renamed into
place. On failure the temporaries are removed and the old deployment keeps running.
The import force-removed the running pair *first*, so a bad redeploy took a working
deployment down with it.

**The golden base image tracks platform source.** Now `python:3.13-slim` (the
packages declare `requires-python = ">=3.13"`, so 3.12 cannot host them), embedding
all four platform packages, tagged `helios-app-base:<content hash of platform/>`.
A platform change rebuilds it; an unchanged platform reuses the cache. This
replaces the never-rebuild rule, under which a platform fix silently never reached
a deployed app. Third-party deps sit in a cached layer *above* the platform source,
so a platform-only edit rebuilds in seconds after the first build. Tests and
`__pycache__` are excluded from the hash — they change on every run and cannot
change runtime behaviour.

**The build context moved to the repository root**, because the base image embeds
`platform/*`, which does not live under `platform/deployment/`. `REPO` is resolved
via `HELIOS_REPO_ROOT` or by walking up for a directory containing both `platform/`
and `specs/`. A consequence worth knowing: **`.helios-run/` moved to the repo
root**, and nginx must mount that one.

**Core failures are no longer recorded as successes.** The import checked no HTTP
status code at all, so a 404 — core not implementing the requested tool — was
recorded as `executed`. A compliance log claiming a side effect that never happened
is worse than no log. Any non-2xx is now recorded as `error` and surfaced as a
failure.

**Data survives a redeploy.** A named volume `helios-data-<tenant>-<app>` is
mounted at `data_dir` in **both** the app and its audit sidecar, so action logs, the
hash-chained audit log and replay state persist. Undeploy removes containers but
**never the volume** — it holds compliance evidence, and destroying it is not an
implied consequence of "undeploy the app".

**`bootstrap.sh` can run on this machine.** The original hardcoded the OrbStack
socket (absent here), created a Python 3.12 venv (below the packages' floor), and
printed "traefik entry" / "traefik dash" while starting `nginx:alpine`. It now
detects the socket in a documented order, prefers starting Docker Desktop over
installing Colima beside it, uses 3.13, drops all Traefik text, and **does not kill
a process already on :8100** — another session may own it. It prints how to check
whether that process is running the current engine (`GET /api/v1/doctor` → 200; a
404 means older) and honours `HELIOS_ENGINE_PORT`.

**A deployment blocker in both apps, fixed.** Both derived the repo root as
`Path(__file__).parents[N]` at import time. The injected Dockerfile does
`COPY . /app` + `WORKDIR /app`, and `Path("/app")` has only one parent, so
`parents[1]` / `parents[2]` raise `IndexError` **at import** — before uvicorn can
bind. Both apps would have crashed inside the container and failed the smoke test
with an opaque traceback. The derivation is now guarded and every `sys.path` insert
is gated on `is_dir()`, since inside the base image the packages are pip-installed
and those directories do not exist.

**URLs are prefix-aware.** nginx strips `/apps/<slug>` before proxying, so the app
always sees `/`. Relative links break at nested routes: from `/runs/run-x`,
`../holds` resolves to `/runs/holds`. The engine sends `X-Forwarded-Prefix` and
both apps build every URL from it.

---

## 5. Two decisions a future agent must not silently reverse

**(a) The sidecar allow-list is a union, not an intersection.**
The session spec said `tools[].name ∩ permissions`. Verified against both real
apps, that intersection is **empty**: audit manifests name *app* tools
(`send_email`, `record_inbound_payment`) while `permissions` names *platform
families* (`payments`, `email`). A literal implementation denies every legitimate
action. The implemented rule accepts an action whose name is a granted family
**or** a tool the app's manifest declares. It still fails closed — a name in
neither set is refused, and only families can reach core because core routes on the
family name.

**(b) The sidecar enforces only the fact-free subset, and skips fact-bound rules.**
`deny_if_fact_true`, `approval_if_fact_true`, `max_contacts_in_days`, and limits
using `fact` / `max_from_fact` are **skipped**, not evaluated — `audit_agent` fails
those closed, so running them without a fact source would deny every request they
touched. The in-app `audit_agent` (which has facts and an owner) stays
authoritative for holds and resumption; the sidecar may **deny or forward, never
approve**, and can never reverse an in-app hold. Every skipped rule is disclosed at
`GET /health` under `delegated_to_in_app_gate`, so a reviewer can always see what
the platform gate does not check. Recorded in `specs/decisions.md` §"Two audit
gates, one direction of authority".

The policy itself comes from the app's own audit manifest, which the engine copies
into `/policy/audit.yaml` **before** the sidecar starts — the app has no volume and
no exec access there, so it cannot rewrite its own policy after deploy. The sidecar
loads it with `audit_agent.manifest.AuditManifest.load` and writes decisions with
`audit_agent.audit_log.AuditLog`: the same code the in-app gate uses, which is the
mechanism by which the two gates cannot drift.

Also worth knowing: `MAX_PAYMENT_AMOUNT` moved from `float` to `Decimal`, per the
repo rule that money is integer cents or decimal strings, never float.

---

## 6. Pre-existing files this branch modified

Everything else in §3 is new. These five existed before, so they are the only
places this branch can interact with someone else's work:

| File | Change | Notes |
|---|---|---|
| `apps/helios-collect/app.py` | 13 lines | `REPO` derivation guarded; `sys.path` inserts gated on `is_dir()`. No logic change |
| `entry-point/examples/invoice-payer/app.py` | 11 lines | Same two fixes |
| `specs/decisions.md` | +36, −0 | **Insertions only** — one new `### Two audit gates` entry appended under `## Decided`. No existing decision edited |
| `.gitignore` | +4, −0 | Adds `.helios-run/` |
| `.dockerignore` | new file | Required by the repo-root build context |

`platform/app-builder/` and `platform/financial-tools/` were **never touched** —
both were outside this worktree's remit and that held. Anything those packages need
from the deployment contract is written up in
`docs/sessions/p2-deploy/HANDOFF-TO-P1-P3.md` rather than edited into them.

---

## 7. How to verify this worktree

```bash
cd .qwen/worktrees/p2-deploy
scripts/bootstrap-venv.sh          # creates .venv on 3.13+; idempotent
scripts/test-all.sh                # 470 passed, 0 skipped; needs no Docker
```

The six suites: `audit-agent-builder` 79 · `financial-tools` 86 ·
`agent-integration` 123 · `helios-collect` 92 · `invoice-payer` 54 ·
`deployment-engine` 36. Sum 470.

Two traps when running tests:

- `audit-agent-builder` and `financial-tools` set `addopts = "-q"` in their own
  `pyproject.toml`. Passing `-q` again makes pytest doubly quiet and **suppresses
  the summary line entirely**, which looks exactly like a hang. `test-all.sh`
  already avoids this.
- `agent-integration` must run with `.venv/bin/python`, not bare `python3`.

Live deployment (needs a Docker daemon):

```bash
cd platform/deployment && ./bootstrap.sh
../../.venv/bin/python cli/helios.py doctor
../../.venv/bin/python cli/helios.py deploy ../../apps/helios-collect --tenant <t>
curl http://localhost/apps/helios-collect-<t>/
```

What was observed live this session, for comparison: both apps returned 200 with
correct titles, prefixed links and rendered invoices; `style.css` served as
`text/css`; the sidecar reported `manifest.loaded: true` with 5 tools and 8
fact-bound rules delegated; the audit chain verified `intact`; the named data
volume mounted at `/data` in both containers; and the approval queue worked end to
end — a hold appeared with both buttons, approve returned 303 and emptied the
queue, a **second** approve returned 404 (single-use, so no double execution), and
decline executed nothing.

---

## 8. Environment facts that are not in the code

These cost real time this session and will cost it again on a fresh machine:

- **Docker Hub is unreachable from this machine.** `registry-1.docker.io` times
  out while PyPI returns 200. Images were pulled via `docker.m.daocloud.io` and
  retagged to the names the Dockerfiles expect. `bootstrap.sh` does **not** do this
  automatically.
- **Docker Desktop is installed and is the active context**; Colima and OrbStack
  are absent. `/var/run/docker.sock` is a symlink into the Desktop socket.
- **`python3` here is Anaconda 3.13.9 with pytest present.** Under it, two
  `financial-tools` modules are module-skipped because `audit_agent` and
  `agent_toolkit` are not importable; in `.venv` they run and pass. That accounts
  for 25 of the tests.
- **`fin-tools` cannot be pip-installed.** Its `pyproject.toml` declares no
  `[build-system]`, so setuptools auto-discovery finds `fin_tools`, `fixtures`,
  `research` and `tests` side by side, refuses to guess, and the editable install
  fails. Worked around with `PYTHONPATH` in the base image and a `.pth` file in the
  venv. Adding a `[build-system]` to that package would let both workarounds go
  away.
- **A stale engine from another session held :8100** (running from `/tmp/helios-pre`,
  predating this work). It was not killed; this session's engine ran on **:8101**.
- **Runtime state is left running.** Seven containers were live at handoff:
  `helios-nginx`, `helios-redis-p2-deploy`, `helios-core-p2-deploy`, and the
  app/audit pair for each of the two apps. `.helios-run/` contains per-tenant
  execution secrets and is gitignored.

---

## 9. What is deliberately not done

- **Holds are still in-memory only.** A SQLite-backed `HoldStore` / `ReplayGuard`
  was specified but not implemented, so pending approvals and grant-replay
  protection are lost on container restart **even though `data_dir` is a persistent
  volume**. Deferred because `platform/agent-integration/` carries other agents'
  in-flight work and this session's rules forbid editing files it does not
  recognise. The seam to wire is `GrantVerifier` / `ReplayGuard` in
  `audit_agent.policy` — wire it, do not fork it — and it should stay opt-in behind
  `build_integration(..., persistence="sqlite")` so the existing 123 tests are
  untouched.
- **The `helios` console-script entry point does not exist.** No pyproject carries
  it. The CLI works via `.venv/bin/python cli/helios.py` and is documented that way.
- **App containers are not internet-isolated.** `tenant_<t>_net` is
  `internal=True`, but an app container also joins `helios_ingress_net` because
  nginx must reach it, and that network has egress. The old docs claimed otherwise;
  that claim was removed rather than left false. Making it true would mean putting
  app containers on the tenant network only and letting nginx reach them through a
  second interface on the sidecar — an open hardening item.
- **Part of the task specification was unavailable.** The prompt text was truncated
  mid-sentence, so everything named in the visible portion was delivered (the five
  CLI subcommands, `DELETE /api/v1/apps/{container}`, the fresh README) and nothing
  beyond it was invented. `docs/research/` does not exist, and the external prompt
  set at `../helios-qwen-prompts/`
  returned `Operation not permitted` even though its parent directory was granted.

Session notes with full reasoning are in `docs/sessions/p2-deploy/`: `PLAN.md`
(preflight findings and task plan), `PROGRESS.md` (running log),
`HANDOFF-TO-P1-P3.md` (what the parallel sessions need from this one).

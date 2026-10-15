# Session `p2-deploy` — Plan

**Objective (spec §E):** make Validation + Deployment real — a generated app is zipped,
linted, built on a golden image containing the platform packages, smoke-tested, attached to
a real audit sidecar, routed behind nginx, and reachable at
`http://localhost/apps/p2-deploy-<tenant>/`. Both existing apps convert to the contract and
deploy end to end with a working approval queue.

**Scope of this commit:** Task 1 only (preflight + plan), per the operator's instruction.
No engine code, no images, no installs, no app changes yet.

Status legend: **decided** · **tentative** · **open** · **BLOCKED**

---

## 1. Preflight findings (all observed with tools this session)

### 1.1 Repository and worktrees

| Fact | Observation |
|---|---|
| Main checkout | `<repo>`, branch `chloe-dev`, HEAD `cc96f0a` |
| Worktrees at session start | main (`cc96f0a`), `.qwen/worktrees/p0-foundation` (`cc96f0a`), `.qwen/worktrees/p3-builder` (`cc96f0a`) |
| `.qwen/worktrees/p2-deploy` | **did not exist.** Created this session: `git worktree add .qwen/worktrees/p2-deploy -b worktree-p2-deploy chloe-dev` → now at `cc96f0a` |
| `git diff --name-status chloe-dev...HEAD` in `p0-foundation` | **empty** — P0 has committed nothing |
| `git status --porcelain` in main | only `?? .DS_Store`, `?? .qwen/`, `?? OTHER-TEAMMATE-STUFF/` |

The work-in-progress list in `HANDOFF.md` §7 (`M platform/agent-integration/...`,
`?? SETUP.md`, etc.) is **gone** — that work has since been committed. `HANDOFF.md` §7 is
stale on that point; the rest of §7's rules still apply.

### 1.2 BLOCKED — P0's deliverables do not exist

The spec's premise "P0 already imported the engine to `platform/deployment/`" is **false on
this checkout**. Verified by repo-wide `find`:

| Spec expects at `$WT/...` | Reality |
|---|---|
| `platform/deployment/` | **absent** from the tracked tree (only `OTHER-TEAMMATE-STUFF/forecasting-simulation/platform/deployment/README.md`, a stub) |
| `scripts/test-all.sh` | **absent** — no `scripts/` directory at all |
| `scripts/bootstrap-venv.sh` | **absent** |
| `.venv/bin/python` (repo venv) | **absent** |
| `docs/research/*.md` | **absent** — `docs/` contains only `milestones/` |
| `docs/sessions/` | **absent**; created this session |

The raw engine **is** present, untracked and read-only, at
`OTHER-TEAMMATE-STUFF/deployment-stuff/fPT9INmvWJ/default/helios/`:

```
AGENT-BUILDERS-GUIDE.md  DEV-NOTES.md  README.md  SPEC-SHEET.md
bootstrap.sh  requirements.txt  test-deploy.sh  engine.log
cli/helios.py
engine/{__init__,app,contracts,orchestrator}.py
images/app-base/Dockerfile
images/auditor/{Dockerfile,server.py}
images/core/{Dockerfile,server.py}
sdk/helios_sdk/__init__.py
examples/sample-app/{helios.yaml,main.py}
.venv/  .helios-run/  .DS_Store      <- exclude from any import
```

**Consequence:** Tasks 2, 3, 5 and 8 (all engine-side) have **no files to edit inside
`$WT`**, because the engine was never added to git and untracked files are not carried into
a new worktree. This is the single gating dependency for the session.

**Decision (tentative, reversible):** add a **Task 0 — engine import**, performed by
`p2-deploy` rather than waited on. Copy the tree above into `platform/deployment/`,
excluding `.venv/`, `.helios-run/`, `engine.log`, `.DS_Store`, `__pycache__/`. Rationale:
it is purely additive, it is what P0 was contracted to deliver, it unblocks four tasks, and
it does not modify `OTHER-TEAMMATE-STUFF/` (which stays read-only per §C.3). `p0-foundation`
has a zero diff, so there is no conflicting in-flight work to clobber. **Not executed in
this commit** — held for operator go-ahead because it creates a large new tracked subtree
that another agent may also be about to create.

### 1.3 Python — spec assumption is wrong here

| Spec claims | Observed |
|---|---|
| system `python3` is 3.14 with no deps | `python3` → `/opt/anaconda3/bin/python3`, **Python 3.13.9**, `pytest 8.4.2` present |
| `python3.13` needed | `/opt/anaconda3/bin/python3.13` → **3.13.9** |
| `platform/agent-integration/.venv` | exists, **3.13.9** |

So the interpreter floor (≥3.13) is already satisfied without creating a venv.
**Decision (tentative):** still create `$WT/.venv` per spec §A for reproducibility, but the
claim "never use the system `python3`, which is 3.14" does not hold on this machine — record
the correction rather than silently following it.

### 1.4 Docker and host services — daemon down

| Check | Result |
|---|---|
| `DOCKER_HOST` | unset |
| `HELIOS_ENGINE_URL` | unset (so no remote engine; local bootstrap path applies) |
| `~/.orbstack/run/docker.sock` | absent |
| `~/.colima/default/docker.sock` | absent |
| `/var/run/docker.sock` | present, but a **symlink** → `~/.docker/run/docker.sock` |
| `docker info` (both default and explicit `unix:///var/run/docker.sock`) | **`Cannot connect to the Docker daemon`** |
| `docker version` | client **29.6.1** (API 1.55, darwin/arm64), context `default`; server unreachable |
| `docker context ls` | `default` (unix:///var/run/docker.sock) and `desktop-linux *` (unix://~/.docker/run/docker.sock) — **Docker Desktop is the active context and is not running** |
| `colima` / `orbstack` binaries | **both MISSING**; `brew` present at `/opt/homebrew/bin/brew` |
| host `nginx` | **MISSING** (spec routes via a `helios-nginx` container, so host nginx is not required) |
| `:80`, `:8100` | nothing listening |

**Diagnosis:** this is Docker **Desktop**, not Colima and not OrbStack. `bootstrap.sh`
defaults `DOCKER_HOST` to the OrbStack socket, which does not exist here — that is exactly
the §E bug list item, confirmed locally.

**Decision (tentative):** the correct fix is `open -a Docker` (or the operator starting
Docker Desktop), **not** `brew install colima docker` as spec Task 1 prescribes. Installing
Colima alongside an existing Docker Desktop would create two competing daemons and a second
`docker` binary — a harder-to-reverse change than starting the one already installed.
Until a daemon answers `docker info`, **every live step is BLOCKED**: image builds, smoke
tests, container runs, nginx reload, and the end-to-end `http://localhost/apps/...` check.
Code, mocked tests and docs can all proceed meanwhile (§Task 1 fallback).

### 1.5 Test baseline — recorded before any change

`scripts/test-all.sh` does not exist, so the baseline was reproduced from the five suites
`HANDOFF.md` §4 documents. Note: `addopts = "-q"` is set in both
`platform/audit-agent-builder/pyproject.toml` and `platform/financial-tools/pyproject.toml`,
so passing `-q` again **suppresses the summary line** — run those two without an extra `-q`.

| Suite | Command | Result |
|---|---|---|
| audit-agent-builder | `PYTHONPATH=src python3 -m pytest tests` | **79 passed** (rc 0) |
| financial-tools | `python3 -m pytest tests` | **61 passed, 2 skipped** (rc 0) |
| agent-integration | `.venv/bin/python -m pytest tests` | **123 passed** (rc 0) |
| helios-collect | `python3 -m pytest tests -q` | **74 passed** (rc 0) |
| invoice-payer | `python3 -m pytest tests -q` | **41 passed** (rc 0) |
| **Total** | | **378 passed, 2 skipped** — matches `HANDOFF.md` §4 |

`agent_integration.cli selfcheck` ran clean and printed:
`tools agree on names, side-effect flags and schemas` /
`auditor grants: ['lookup_invoice', 'send_email']` /
`gated tools are adjudicated per call`.
⚠️ Spec Task 6 asserts it must print the literal string `seam check: OK`; the observed
output above does **not** contain that exact line (only `tail -3` was captured). Verify the
full output before treating Task 6's acceptance string as satisfied.

### 1.6 Inaccessible path

`../helios-qwen-prompts/` →
**`Operation not permitted`**, even though the parent directory was granted. The prompt set
and research reports referenced by §A cannot be read this session. Proceeding without them;
recorded as **open**.

---

## 2. Engine facts verified from source (correcting the spec where it is wrong)

Read directly, not taken on trust. Line numbers are from the raw copy under
`OTHER-TEAMMATE-STUFF/deployment-stuff/fPT9INmvWJ/default/helios/`.

### 2.1 Spec claims that check out

- `engine/contracts.py`: `Manifest(name, tenant="", entry="main:app", port=3000,
  permissions=[])`; `ALLOWED_TOOLS = {payments, email, knowledge-graph, events}`; the five
  secret patterns and the eleven `TEXT_EXT` suffixes match the spec exactly; `lint()` skips
  files >1 MB and `.git/__pycache__/.venv/node_modules`.
- `engine/app.py`: port-agnostic FastAPI app with `GET /health`, `POST /api/v1/deploy`
  (multipart `bundle` + form `tenant`), `GET /api/v1/apps`,
  `GET /api/v1/apps/{container_name}/logs?tail=200`. `RUN` (`.helios-run`) is created **at
  import time**, which matters for tests.
- `orchestrator.py`: `ensure_base_images()` builds only on `ImageNotFound` → the
  **never-rebuild** rule, confirmed (`:145-155`). Injected Dockerfile is exactly the four
  lines the spec quotes (`:195-201`). Smoke = 45 × 1 s polling `/health` via
  `docker exec python -c urlopen(...)` on `helios_sandbox_net` (`:210-244`).
- **Bug (a)** confirmed: `if f"location /apps/{slug}" not in default_conf:` (`:332`) — the
  path block is inserted only when absent, so a redeploy keeps the **old ingress IP**.
  Note it is worse than the spec says: a *separate* `.helios-run/nginx/{slug}.conf` is also
  written every deploy with a hardcoded `proxy_pass http://{app_ip}:{port}` (`:302`), so
  there are **two** files carrying the IP and only one of them is refreshed.
- **Bug (b)** confirmed: `app_container.reload()` once, then read
  `attrs["NetworkSettings"]["Networks"][ingress.name]["IPAddress"]` (`:285-286`) — can be
  `""`, producing `proxy_pass http://:3000`.
- **Bug (c)** confirmed: `_container()` force-removes any container of the same name before
  creating the new one (`:118-122`), so the old deployment is destroyed **before** the new
  one is verified, with no rollback.
- **Bug (e)** confirmed, and worse: the auditor checks **no status code at all**.
  `httpx.post(...)` only lands in `except` on transport failure; a 404/500 from core falls
  straight through to `_record("executed", ...)` (`images/auditor/server.py:99-110`). So
  *any* non-2xx is recorded as `executed`, not just 404.
- **Bug (f)** confirmed: `HELIOS_ENGINE_URL` is injected into the auditor
  (`orchestrator.py:258`) and never read by `images/auditor/server.py`.
- `bootstrap.sh`: defaults `DOCKER_HOST` to the OrbStack socket (`:7`), runs `open -a
  OrbStack`, uses `python3.12 -m venv` (`:41`), and prints **"traefik entry" / "traefik
  dash"** (`:56-57`) while actually starting **nginx:alpine** (`:33-37`). The header comment
  also says "Traefik" and the prereq line says Colima — three-way inconsistency.
- `images/app-base/Dockerfile`: `python:3.12-slim-bookworm`; installs only fastapi,
  `uvicorn[standard]`, redis, httpx, PyYAML; copies `helios_sdk` into
  `python3.12/site-packages`. **No pydantic pin, no jsonschema, no jinja2, no numpy, no
  python-multipart, no platform packages** → Task 3's gap is real and larger than stated,
  since Task 7's server-rendered pages need `jinja2` + `python-multipart`.
- `cli/helios.py`: `argparse` with `choices=["deploy"]` only; `EXCLUDE_DIRS` =
  `{__pycache__, .venv, node_modules, .git, .helios-run}`; `EXCLUDE_FILES` = `{Dockerfile}`.
  Confirms it **ships** `runs/`, `tests/` and `.env` → Task 4's exclusion work is needed.
  No `apps`/`logs`/`undeploy`/`doctor`.

### 2.2 Where the spec is imprecise

1. **Error shape.** Spec §E says a lint hit is
   `422 {"detail":{"stage":"static_audit","problems":[...]}}`. In code the `HTTPException`
   detail is `{"stage": ..., "error": ...}` for manifest failures and
   `{"stage":"static_audit","problems":[...],"hint":...}` for lint. FastAPI wraps any
   `HTTPException` detail under a top-level `"detail"` key, so the **wire** shape does match
   — but `SPEC-SHEET.md`/`AGENT-BUILDERS-GUIDE.md` must document `detail.stage`,
   `detail.error` **and** `detail.problems`, since the key differs by stage. Task 4's doc
   work should say this precisely.
2. **`_find_app_root` allows one wrapper folder** (`app.py:45-57`) — undocumented in the
   spec, and it silently accepts a bundle nested two levels deep. Worth stating in the docs.
3. **Auditor holds are not resumable** — confirmed (`{"status":"held_for_review"}`, HTTP
   200, decisions in an in-memory list truncated to the last 500, `_seen` dedupe dict also
   in memory). This is the gap Task 5's `/data` JSONL persistence and Task 6's SQLite
   `HoldStore` both address, and they must agree on the hash-chain format.
4. **`MAX_PAYMENT_AMOUNT` is a `float`** (`server.py:24`) — violates decision B.6 (money is
   integer cents or decimal strings, never float). Task 5 must convert this to `Decimal`
   when it replaces the hardcoded policy; flag it explicitly so it is not "fixed" by
   accident or left behind.

### 2.3 A concrete Task 7 bug found by preflight

Both apps derive the repo root from `__file__` at **module import time**:

- `apps/helios-collect/app.py:76-77` → `HERE = Path(__file__).resolve().parent`,
  `REPO = HERE.parents[1]`
- `entry-point/examples/invoice-payer/app.py:73` → `REPO = HERE.parents[2]`

Verified with the interpreter: at the container path `/app`, `Path("/app").parents[1]`
raises **`IndexError: 1`** (and `parents[2]` likewise). Since the injected Dockerfile does
`COPY . /app` + `WORKDIR /app`, **both apps crash at import inside the container**, before
`/health` can ever answer — the smoke test would fail with a confusing traceback rather than
a missing-route error.

This makes Task 7's "import `build_app()` lazily, not at import time" **mandatory for
correctness, not a nicety**, and the `sys.path` inserts must be guarded by existence checks
(the dirs do not exist in the image; the packages are installed there instead).

### 2.4 App contract state

`apps/helios-collect` has `helios-app.yaml` (**not** `helios.yaml`) and
`agents/helios-collect-audit.yaml`; `entry-point/examples/invoice-payer` likewise has
`helios-app.yaml` + `agents/`. `HANDOFF.md` §3 notes "nothing parses `helios-app.yaml`
yet". Task 7 must add a **new** `helios.yaml` (the deployment manifest) alongside the
existing `helios-app.yaml` (the app-builder manifest) — the two are different schemas and
must not be conflated or overwritten.

---

## 3. Task plan

Ordered so each commit leaves the repo importable and its own tests green (§D).
Tasks 2/3/5/8 all depend on Task 0.

### Task 0 — import the engine (prerequisite, **BLOCKED on operator go-ahead**)
Copy the raw engine into `platform/deployment/`, excluding `.venv/`, `.helios-run/`,
`engine.log`, `.DS_Store`, `__pycache__/`. Add `platform/deployment/tests/` offline tests
(P0's stated deliverable) and `scripts/bootstrap-venv.sh` + `scripts/test-all.sh`, since
§A and §D depend on both. Commit: `chore: import the deployment engine into the repo`.
*Risk:* another agent (P0) may be about to create the same subtree. Coordinate before
executing.

### Task 1 — preflight and plan (**done**, this commit)
Daemon detection per §1.4; baseline per §1.5; PLAN.md + PROGRESS.md.
Outcome: **no daemon** → all live steps BLOCKED; no Colima install (see §1.4 decision).

### Task 2 — engine fixes (needs Task 0; tests mocked, live verify BLOCKED)
(a) Rewrite the `/apps/<slug>` location block on every deploy, marker-delimited
(`# helios:begin <slug>` / `# helios:end <slug>`), one block per slug; also regenerate the
per-slug `.conf` so **both** IP carriers refresh.
(b) Retry ingress-IP read 10 × 0.5 s; fail the deploy with a named-field error if still
empty rather than writing `http://:3000`.
(c) Rollback: create `{t}-{a}-app-next` / `{t}-{a}-audit-next`, wait for `/health` 200 **on
the tenant network**, then swap names and remove the old pair; on failure remove only the
temporaries and leave the running deployment untouched.
(d) `bootstrap.sh`: socket detection in the §1.4 order (`$DOCKER_HOST` → OrbStack → Colima →
`/var/run/docker.sock`), prefer the already-installed Docker Desktop, `python3.13`, drop all
Traefik text, idempotent.
(e) Auditor: check `resp.status_code`; a non-2xx from core is recorded `error` and returned
as an error, never `executed` (fixes more than the spec's 404 case — §2.2).
(f) **Decision:** drop the unused `HELIOS_ENGINE_URL` injection (simpler and removes a
dead, secret-adjacent env var) rather than wire it.
Tests: `docker` mocked via `unittest.mock` for (a)–(c); pure-function tests for (b)/(e).

### Task 3 — golden base image with the platform inside (needs Task 0; build BLOCKED)
`python:3.13-slim`; copy the four `platform/*` packages into `/opt/helios/platform`;
`pip install -e` each; pin `pydantic pyyaml jsonschema fastapi uvicorn[standard] httpx redis
jinja2 numpy python-multipart`; install `helios_sdk`. Engine hashes `platform/` (excluding
tests/caches) → tag `helios-app-base:<hash>` + `:latest`, rebuild on hash change (replaces
never-rebuild). Build context = repo root, located via `HELIOS_REPO_ROOT` or by walking up
from `platform/deployment/`. Layer order: deps first, package source last, so a platform
edit rebuilds in under a minute after the first build.
⚠️ Note `REPO = Path(__file__).resolve().parent.parent` (`orchestrator.py:23`) currently
resolves to the *engine* root, not the repo root — Task 3 must change this, and it also
governs where `.helios-run/` lives.

### Task 4 — `helios.yaml` extensions (needs Task 0; offline-testable)
Add optional `env: [NAMES]` (allow-list of names only, `^(HELIOS_|DASHSCOPE_|OPENAI_|
TUSHARE_|EMAILTOOL_)`, values never logged), `audit_manifest:`, `data_dir:` (→ named volume
`helios-data-<t>-<a>` on both app and audit containers). Extend `permissions` with
`forecast`, `market-data`, `spreadsheets`, `collect`. All validation in `contracts.py`,
model-actionable messages, all problems reported at once, fail closed. CLI: exclude
`runs/`, `tests/`, `.pytest_cache`, `.env` by default, `--include-tests` to override,
**exit 2** if a `.env` is present and not excluded. Update `SPEC-SHEET.md` and
`AGENT-BUILDERS-GUIDE.md` per §2.2 items 1–2 (error shapes incl. the `detail` wrapper;
token var is `HELIOS_ACTION_TOKEN`, not `ACTION_PROPOSAL_TOKEN` — note
`images/auditor/server.py`'s own docstring still says `ACTION_PROPOSAL_TOKEN`, so fix that
too). **Decision needed** on the "no outbound internet" claim — see §4.

### Task 5 — real audit sidecar (needs Tasks 0, 3, 4; live verify BLOCKED)
Replace the hardcoded policy with the app's `audit_manifest`, copied by the engine from the
bundle to `/policy/audit.yaml` in the auditor container (read-only to the app afterwards).
Enforce the fact-free subset deterministically: allow-list = `tools[].name` ∩
`permissions`; `limits` (`argument` + `max`); `forbidden_values`; `required_arguments`;
`max_calls` from `tool_overrides`; duplicate detection on the gated-tool payload hash within
a window. Load via `audit_agent.manifest.AuditManifest.load` so the two gates cannot drift.
Keep the execution secret and the forward to core. Persist decisions to
`/data/audit/sidecar-log.jsonl` using `audit_agent`'s **own** hash-chain log writer (import,
do not re-implement). Convert `MAX_PAYMENT_AMOUNT` to `Decimal` (§2.2 item 4). Record the
two-gates decision in `specs/decisions.md`: in-app `audit_agent` (with facts) is
authoritative for holds and owner decisions; the sidecar is the platform's last line and may
only deny or forward — it can never approve what the in-app gate held.
Tests: manifest-driven allow/deny, cap, forbidden value, duplicate, chain verification.

### Task 6 — persist held state in the platform (**partly BLOCKED — another agent's package**)
Add SQLite-backed `HoldStore` + `ReplayGuard` in `agent-integration`
(`<data_dir>/helios.sqlite`), selectable via `build_integration(..., persistence="sqlite")`,
default in-memory so existing tests are untouched; wire through the existing
`GrantVerifier`/replay-guard seam in `audit_agent.policy` — do not fork it. Add
`build_integration(..., audit_url=...)` for an external `audit_agent` HTTP transport, tested
against an in-process `HttpTransportServer` on loopback. Then run
`.venv/bin/python -m agent_integration.cli selfcheck`.
⚠️ **Boundary conflict:** `agent-integration` is outside the two dirs §E forbids
(`platform/financial-tools`, `platform/app-builder`), so it is technically in scope — but
`HANDOFF.md` §7 records that other agents are actively editing
`agent_integration/{__init__,cli,model_driver}.py`, and §C.3 forbids touching work I do not
recognise. **Decision (tentative):** implement Task 6 additively (new modules + new
keyword-only params with defaults), touch no existing behaviour, and coordinate the
`cli.py`/`__init__.py` surface via `HANDOFF-TO-P1-P3.md` rather than editing it blind.
`audit_agent.policy` lives in `platform/audit-agent-builder`, which is **not** forbidden and
whose suite is green at 79 — wiring the existing seam is in scope.

### Task 7 — convert both apps to the contract (offline-testable; live deploy BLOCKED)
For `apps/helios-collect` and `entry-point/examples/invoice-payer`, add without breaking
their CLIs or the 74/41 green tests:
- `helios.yaml` (new file, alongside the existing `helios-app.yaml` — §2.4): `name` = app id,
  `entry: server:app`, `port: 3000`, `permissions`, `env: [HELIOS_LLM_API_KEY,
  DASHSCOPE_API_KEY, HELIOS_LLM_MODEL, HELIOS_LLM_BASE_URL, HELIOS_AUDIT_SIGNING_KEY,
  HELIOS_AUDIT_SIGNING_KEY_ID]`, `audit_manifest`, `data_dir: /data`.
- `server.py`: FastAPI app importing `build_app()` **lazily** (§2.3 — otherwise
  `IndexError` at `/app`), using `HELIOS_DATA_DIR` (default `./runs`), serving
  Jinja2/HTMX pages with **relative** URLs (nginx strips the prefix): `GET /health`; `GET /`
  dashboard (existing `render_dashboard` data); `GET /holds` approval queue showing the
  auditor's own `owner_question`, rule id and reason, with Approve/Decline → `POST
  /holds/{hold_id}/{approve|decline}` calling `runtime.resume(...)`; `POST /runs`
  (`task`, `task_type`, optional `invoice_id`) driving one scenario scripted, or the real
  `ModelDriver` when `?driver=model` and a key is present; `GET /runs/{run_id}` action-log
  view; `GET /audit` chain verification. Guard every `sys.path` insert so it tolerates the
  container (packages installed; dirs absent) — fix `app.py:80-88` and the invoice-payer
  equivalent.
- Templates + ~60 lines of CSS in `apps/helios-collect/templates/`, copied for
  invoice-payer; tell P3 to lift them into the scaffolder via the handoff note.
- `tests/test_server.py` with `fastapi.testclient`: health 200 with no env; dashboard
  renders; a holding scenario appears in `/holds`; approving executes **exactly** the held
  action (assert via the action log); declining executes nothing. Existing scenario tests
  stay green.
⚠️ Depends on Task 4 (`env`, `audit_manifest`, `data_dir` fields must exist first) and, for
holds surviving a restart, on Task 6.

### Task 8 — CLI and docs (needs Task 0; **spec text truncated**)
`cli/helios.py` subcommands: `deploy <dir> [--tenant] [--engine] [--include-tests]`, `apps`,
`logs <container> [--tail]`, `undeploy p2-deploy [--tenant]` (requires a **new** engine
endpoint `DELETE /api/v1/apps/{container}` — absent today, `app.py` has only the four routes
in §2.1), `doctor` (daemon, nginx, engine health, base image hash). Installable as `helios`
via P0's pyproject — **which does not exist yet**, so Task 8 also depends on Task 0
delivering it. Rewrite `platform/deployment/README.md` from the Mac-mini README, correcting
every mismatch found this session (Traefik→nginx, OrbStack→detected socket, python3.12→3.13).
**BLOCKED-unknown:** the attached spec is cut off mid-sentence at *"correct every mismatch
you f…"*. The remainder of Task 8, any Task 9+, and §G (blocked-handling procedure) are
**missing**. Do not invent them; re-request the full text before completing Task 8.

### Final acceptance (spec §E)
`http://localhost/apps/p2-deploy-<tenant>/` reachable, both apps deployed end to end with a
working approval queue. **BLOCKED** on a Docker daemon (§1.4) and on Task 0.

---

## 4. Decisions taken this session (safer, reversible; recorded per §D)

1. **Do not install Colima.** Docker Desktop is already installed and is the active
   context; starting it is reversible, installing a second daemon is not. Deviates from
   Task 1's literal instruction — recorded here as required.
2. **Create `$WT/.venv` anyway**, but note the system `python3` here is 3.13.9 with pytest
   available, contradicting §A's "system python3 is 3.14 with no dependencies".
3. **Task 2(f): drop** `HELIOS_ENGINE_URL` rather than wire it.
4. **Task 5: `Decimal`** for the payment cap, not `float`.
5. **Task 6: additive only**, coordinate the shared `cli.py`/`__init__.py` surface through
   `HANDOFF-TO-P1-P3.md` instead of editing files carrying another agent's work.
6. **Task 0 held, not executed** — creating a large tracked subtree that P0 is contracted to
   deliver is the one irreversible-ish step here; it needs a nod, or P0 needs to land it.

## 5. Open questions

- **O1.** Who lands `platform/deployment/`, `scripts/test-all.sh`, `scripts/bootstrap-venv.sh`
  and the `helios` pyproject — P0, or may `p2-deploy` do it (Task 0)? Everything engine-side
  waits on this.
- **O2.** Task 4's "no outbound internet" claim: make it true (app containers on the tenant
  network only, nginx reaching them via a second interface on the auditor) or delete the
  claim? **Tentative:** make it true — the tenant network is already `internal=True`
  (`orchestrator.py:159`), and the app container is currently attached to **both** the tenant
  net and the non-internal `helios_ingress_net` (`:278-282`), which is precisely the hole the
  claim denies. Dropping the app's ingress attachment and letting nginx reach it through the
  auditor closes it and matches the documented architecture.
- **O3.** Full text of Task 8's tail, any Task 9+, and §G.
- **O4.** `HANDOFF.md` §6's Qwen-vs-GLM conflict: `platform/agent-integration/.env` and
  `model_driver.py` target **GLM-5.3** via a Gitee gateway, while the competition rule and
  spec §A say Qwen only, and any GLM/Zhipu/bigmodel.cn reference must be removed from files
  I touch. Task 7 touches `server.py`'s model-driver path. **Tentative:** when Task 7 needs
  the driver, point the config at DashScope OpenAI-compatible mode (decision B.3) and remove
  legacy GLM references **only in files this session edits**, recording each removal in the
  commit body — do not sweep the repo.
- **O5.** `helios-qwen-prompts/` is unreadable (§1.6); unknown whether it contains the
  missing spec text.

## 6. Do-not-touch list (from §C, verified present)

`OTHER-TEAMMATE-STUFF/` (read-only; also **untracked**, so absent from `$WT`),
`docs/milestones/`, `.env` (one exists at `platform/agent-integration/.env` — never read,
print, commit or copy it), `*.venv`. No `git add -A`/`git add .`; no push, force-push,
rebase, amend, reset, `--no-verify`. No edits to `platform/financial-tools/` or
`platform/app-builder/`.

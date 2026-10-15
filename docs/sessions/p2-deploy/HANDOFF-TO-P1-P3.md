# Handoff — p2-deploy → P1 (financial-tools) and P3 (app-builder/scaffolder)

Written by session `p2-deploy`. Per the session rules I did **not** edit
`platform/financial-tools/` or `platform/app-builder/`, so anything those two need
from the deployment contract is here instead.

Everything below was observed in this session, not assumed. Repo-wide suite at
handoff time: **457 passed, 0 skipped**.

---

## For P3 (scaffolder) — this is the larger half

### The template you generate `server.py` from

`apps/helios-collect/server.py` is the reference implementation and
`entry-point/examples/invoice-payer/server.py` is the second, deliberately
different example. Lift both, not one: they show the two runtime shapes that exist
today (`build_integration()`-based vs a hand-rolled `GatedRuntime`), and a
scaffolder that only knows one will generate broken apps for the other.

Three properties are load-bearing and are the ones a generated app gets wrong:

1. **`/health` must be a constant that builds nothing.** The deploy smoke test
   runs the container on an isolated network with **no Redis, no audit sidecar, no
   model key**, polling `/health` for up to 45×1s. Any health check that needs
   those fails every deploy. Build the app lazily on first use of a real page,
   under a lock.

2. **URLs must use `X-Forwarded-Prefix`, not be relative.** nginx strips
   `/apps/<slug>` before proxying, so the app always sees `/`. Relative links
   break at nested routes: from `/runs/run-x`, `../holds` resolves to
   `/runs/holds`. The engine sends `X-Forwarded-Prefix`; generate
   `{{ prefix }}/path` everywhere. See `_prefix()` in either server.py.

3. **Never derive the repo root as `Path(__file__).parents[N]` at import time.**
   Both existing apps did, and it was a real deployment blocker: the injected
   Dockerfile does `COPY . /app` + `WORKDIR /app`, and `Path("/app")` has only one
   parent, so `parents[1]` raises `IndexError` **at import** — before uvicorn can
   bind. The smoke test then fails with an opaque traceback instead of a readable
   missing-route error. Guard the derivation and gate every `sys.path` insert on
   `is_dir()`, because inside the base image the packages are pip-installed and
   those directories do not exist.

### Templates and CSS

`apps/helios-collect/templates/` — `base.html`, `dashboard.html`, `holds.html`,
`runs.html`, `run_detail.html`, `audit.html`, plus `style.css` (~110 lines).
These are copied verbatim into `entry-point/examples/invoice-payer/templates/`
with only the app name changed in `base.html`, which is evidence they are already
app-agnostic enough to lift.

The **approval queue (`holds.html`) is the page that matters** — without it a held
agent pauses invisibly. It shows the auditor's own `owner_question`, never a
summary the agent wrote about itself; that distinction is the architecture-defence
point, so do not let the scaffolder generate a page that renders the agent's own
`reason` instead.

### `helios.yaml` — the contract you must emit

Full field list and validation rules: `platform/deployment/README.md` §"What an
app must provide" and `platform/deployment/AGENT-BUILDERS-GUIDE.md` §"YAML
Contract". The short version:

```yaml
name: <app id>          # sanitised to [a-z0-9-]; becomes container name, DNS alias, URL path
entry: server:app
port: 3000
permissions: [...]      # payments, email, knowledge-graph, events,
                        # forecast, market-data, spreadsheets, collect
env: [NAMES]            # NAMES only; ^(HELIOS_|DASHSCOPE_|OPENAI_|TUSHARE_|EMAILTOOL_)
audit_manifest: agents/<id>-audit.yaml   # path inside the bundle
data_dir: /data         # must be absolute
```

`helios.yaml` is **not** `helios-app.yaml`. Different schemas; both may exist in
one app dir. Do not have the scaffolder overwrite one with the other. Validation
reports **all** problems at once and fails closed; unknown fields are rejected, so
a typo surfaces loudly rather than being ignored.

### Bundle contents

The CLI excludes `runs/`, `tests/`, `test_*.py`, `.pytest_cache` by default
(`--include-tests` to override) and **refuses with exit 2** if a `.env` is present
in the bundle dir. So a generated app should not ship a `.env` at all — declare
secret NAMES under `env:` and let the engine inject values.

### Two things I could not finish that affect you

- **`stacks.yaml` does not exist.** `apps/helios-collect` names
  `helios-python-web-1` and that name resolves to nothing (HANDOFF.md §5 trap 13).
  If the scaffolder emits a `stack.use`, it needs that registry to exist first.
- **Task 6 (SQLite-backed `HoldStore` / `ReplayGuard`) was deferred.** Holds are
  still **in-memory only**, so pending approvals and grant-replay protection are
  lost on container restart even though `data_dir` is a persistent volume. The
  seam to wire is `GrantVerifier` / `ReplayGuard` in `audit_agent.policy` — wire
  it, do not fork it. `build_integration(..., persistence="sqlite")` should stay
  opt-in so the existing 123 agent-integration tests are untouched. I did not do
  this because `agent-integration/{cli,__init__,model_driver}.py` carry other
  agents' in-flight work and the session rules forbid touching files I do not
  recognise; it needs coordination, not a drive-by edit.

---

## For P1 (financial-tools)

- **The base image installs `fin-tools` by path, not by pip.** Its
  `pyproject.toml` declares no `[build-system]`, so setuptools' auto-discovery
  finds `fin_tools`, `fixtures`, `research` and `tests` side by side, refuses to
  guess, and `pip install -e` fails. `images/app-base/Dockerfile` therefore sets
  `ENV PYTHONPATH=/opt/helios/platform/financial-tools`, and
  `scripts/bootstrap-venv.sh` writes a `.pth` file for the same reason. **Adding a
  `[build-system]` + explicit package config to that pyproject would let both drop
  their workaround** — that edit is yours, not mine.

- **The core image is a mock, and you own the real thing.** `images/core/server.py`
  serves bearer-gated `POST /tools/{payments|email|knowledge-graph}` plus SQLite at
  `/tmp` and `GET /kg/nodes`. Your HTTP service with persistence is meant to
  replace it. Contract to preserve: the audit sidecar forwards to
  `{HELIOS_CORE_URL}/tools/{action}` with `Bearer <HELIOS_EXECUTION_SECRET>`, and
  **a non-2xx is now recorded as `error`, not `executed`** — so returning 404 for
  an unimplemented tool is safe and honest, where before it silently claimed the
  action happened.

- **`permissions` gained four families:** `forecast`, `market-data`,
  `spreadsheets`, `collect` (alongside `payments`, `email`, `knowledge-graph`,
  `events`). If your service routes on family name, those four need handlers or a
  deliberate 404.

- **The `side_effects` / `side_effect` and `ToolSpec` / `ToolDefinition`
  mismatches** (HANDOFF.md §5 trap 1) are still live. `adapters.ToolContractView`
  and `fin_tools.toolkit_bridge` remain the only sanctioned bridges. Run
  `agent_integration.cli selfcheck` after any change to either package's tool
  contract — a gated tool registering as read-only means the app emails customers
  with **no audit at all**, and nothing raises.

---

## Environment facts worth knowing

- **Docker Hub is unreachable from this machine** (`registry-1.docker.io` times
  out; PyPI works fine). Images were pulled via `docker.m.daocloud.io` and
  retagged to the names the Dockerfiles expect. `bootstrap.sh` does not do this for
  you — if a fresh machine hits the same wall, pull and retag manually, or
  configure a daemon registry mirror.
- **Docker Desktop is installed and is the active context.** Colima and OrbStack
  are absent. `bootstrap.sh` detects the socket in the documented order and starts
  Desktop rather than installing Colima beside it, which would mean two competing
  daemons.
- **Python:** `python3` here is Anaconda **3.13.9** with pytest present — not 3.14
  without deps as the session prompt assumed. `scripts/bootstrap-venv.sh` still
  creates `.venv` for reproducibility, and it is what `scripts/test-all.sh` uses.
- **`.venv` is required for the full suite.** Under Anaconda's `python3`, two
  financial-tools modules (`test_gated_loop_integration.py`,
  `test_toolkit_bridge.py` = 25 tests) are module-skipped because `audit_agent`
  and `agent_toolkit` are not importable; in `.venv` they run and pass. That is the
  whole difference between the 378 baseline and 457 now.
- **A stale engine from another session was on :8100** (running from
  `/tmp/helios-pre`, predating this work). I did not kill it — the rules forbid
  touching processes I do not recognise — and ran mine on **:8101**.
  `bootstrap.sh` now detects an occupied port and tells you how to check whether
  that process is running the current engine (`GET /api/v1/doctor` → 200; 404 means
  older), plus `HELIOS_ENGINE_PORT` to run beside it.
- **`.helios-run/` holds per-tenant execution secrets.** It is now in `.gitignore`.
  It lives at the **repo root** (the engine resolves `REPO` upward), not under
  `platform/deployment/` — nginx must mount the repo-root one, or it serves its
  default page while the engine believes it routed an app. That mismatch cost a
  confusing 200-during-this-session and is the reason `bootstrap.sh` derives the
  path rather than hardcoding it.

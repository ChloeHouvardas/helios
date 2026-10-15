# Tech Stack

## Decided
- **Language:** Python 3.13. `audit_agent` and `agent_integration` both declare
  `requires-python = ">=3.13"`; the rest declare `>=3.10`. One interpreter for
  everything, via `scripts/bootstrap-venv.sh`.
- **Repo layout:** monorepo — `platform/`, `services/`, `apps/`, `entry-point/`, `scripts/`
- **Folder naming:** kebab-case (`action-agent-builder/`, `audit-agent-builder/`,
  `agent-integration/`, `financial-tools/`, `deployment/`); Python package names
  inside folders use snake_case (hyphens can't be imported). Note the folder
  names are not the package names: `action-agent-builder/` contains
  `agent_toolkit`, and `financial-tools/` contains several sibling packages
  (`fin_tools`, `helios_forecast`, `spreadsheets`, `email_tool`,
  `payments_collect`).
- **Model:** Qwen (the only generative model allowed for development), via
  DashScope's OpenAI-compatible endpoint. Default model `qwen3.8-max`.
- **Agent loop:** an OpenAI-compatible tool-calling loop over stdlib `urllib`,
  returning one tool call per turn as a proposal the audit bridge adjudicates
  before execution. Not built on Qwen-Agent — no file imports `qwen_agent`.
- **Secrets:** API keys are read from environment variables and never hard-coded or logged
- **App spec format:** YAML (`helios-app.yaml`), validated with pydantic under `extra="forbid"` — see `specs/app-builder.md`
- **Default generated-app stack:** FastAPI + SQLite (stdlib `sqlite3`, no ORM) + Jinja2 + HTMX, when a spec does not name one. Named `helios-python-web-1` in `platform/app-builder/stacks.yaml`
- **Web framework:** FastAPI — Python keeps holds and approvals in-process with the agent runtime
- **Frontend:** Jinja2 templates + HTMX, server-rendered by the app's own FastAPI process. *Changed 2026-10-15 from React built with Vite.* The property Vite was chosen for still holds — one process, one port, one origin, with no server/client runtime split and no CORS boundary on the approval flow — and server-rendered pages reach it without an install step, a build step, a second language, or a bundle that can build successfully and still render nothing. A generated app has to be buildable and debuggable by a model in minutes. See the CHANGED note in `specs/app-builder.md`
- **UI:** a shared kit of Jinja2 templates — one per pattern (`summary`, `table`, `detail`, `form`, `approval_queue`, `settings`), a base layout, and a held-action partial — over one `tokens.css` design-token file. Qwen assembles from it rather than writing UI from scratch. *Narrower than the component library originally planned*: the trade for dropping React is granularity, not consistency. Generated apps are still expected to be clean and visually credible, and every template ships its own empty/loading/error states
- **Data storage:** SQLite per app, accessed through generated parameterised SQL. *Changed 2026-10-15 from SQLAlchemy.* The generated data layer is a few dozen statements a model can read, debug and fix; an ORM is one more thing that can be subtly wrong in generated code, and it hides the parameterisation that stops SQL injection. Portability to Postgres now lives in the `stacks.yaml` type map rather than in an ORM, so moving database is still a mapping change and not a rewrite of generated code
- **Money representation:** integer cents, or `decimal` stored as `TEXT` with a parse CHECK. Never float, and never `REAL`. SQLite applies column affinity *before* a CHECK runs, so binding a Python float into a TEXT column arrives as a string and passes every constraint — no pure-SQL rule catches it, and the generated store refuses a float at the Python boundary instead
- **Package/build tooling:** hatchling with a `src/` layout, for every packaged platform module. `platform/financial-tools` and `platform/deployment` use setuptools with explicit package lists, and every package installs editable through `scripts/bootstrap-venv.sh`
- **Vendored frontend assets:** HTMX 2.0.11 is vendored into `app_builder/kit/static/` rather than loaded from a CDN, with its version, byte count and sha256 recorded in `stacks.yaml` and asserted by a test. A generated app must work offline and behind a host that strips `/apps/<slug>`
- **Deployment:** `platform/deployment` — `helios.yaml` manifest, an ASGI entry
  and `GET /health`, built and routed through the Docker Engine API (docker-py).
  Three golden images keyed by the platform hash: `helios-app-base` (the platform
  packages), `helios-auditor` (the sidecar), `helios-core` (`python -m fin_tools`,
  the tool service). One nginx on the ingress network; per tenant an internal
  network, a Redis bus and a core container with its own data volume.
- **Cross-app transport:** Redis pub/sub, one channel per tenant, through
  `helios_sdk`. Apps never call each other.
- **Testing:** pytest, run by `scripts/test-all.sh`. Every suite is offline: no
  network, Docker daemon, model token or provider credential required.
- **Dependency pinning:** `requirements-dev.txt` pins every version exactly, so
  two agents in two worktrees cannot drift into different dependency trees.
- **Action log storage:** append-only JSONL files, one per run under `.runs/<run_id>/`; the auditor's own log is hash-chained
- **Market data transport:** stdlib `urllib` against the Tushare Pro HTTP API, returning plain dicts. No `tushare` SDK and no pandas, so `fin_tools` keeps `dependencies = []` and the package stays importable with nothing installed beyond the standard library
- **Market data endpoint:** `https://api.tushare.pro`. The brief names the plain-HTTP form of the same host; HTTPS is the default because the token travels in the POST body and a plaintext request would put a live credential on the wire. HTTPS was verified working against the real API. The endpoint is overridable if plain HTTP is ever required
- **Market data credentials:** `TUSHARE_TOKEN`, read from the environment at call time only. Never hard-coded, never logged, never stored on an instance, never present in `ToolSpec.declare()` output, and redacted from every error message. There is deliberately no `set_token()` — a token in a process global is a token in a traceback
- **Market data endpoints are allow-listed:** `shibor_lpr`, `shibor`, `stock_basic`, `stock_company`, `fina_indicator`, `index_member_all`, `daily`, `trade_cal`, each hard-coded inside the tool that uses it. Offline fixtures under `fixtures/market/` serve every tool when `HELIOS_MARKET_MODE=fixture`. No tool accepts an `api_name`, so an agent cannot reach an endpoint nobody reviewed

## Tentative
- Which of the two audit-gate implementations stays importable long term: `agent_toolkit.audit.AuditGate` is legacy and out of the loop.

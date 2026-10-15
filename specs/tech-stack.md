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
- **Default generated-app stack:** FastAPI + SQLite + Jinja2/HTMX
  server-rendered pages, when a spec does not name one. Holds and approvals stay
  in-process with the agent runtime, so one process on one origin keeps the
  approval flow same-origin with no CORS boundary.
- **Web framework:** FastAPI — Python keeps holds and approvals in-process with the agent runtime
- **Frontend:** React built with Vite. Chosen over Next.js deliberately: Vite emits a static bundle that FastAPI serves, so a generated app is one process on one origin, with no server/client runtime split and no CORS boundary on the approval flow
- **UI:** a shared prebuilt component library — Qwen assembles from it rather than writing UI from scratch. Generated apps are expected to be clean and visually credible
- **Market data transport:** stdlib `urllib` against the Tushare Pro HTTP API, returning plain dicts. No `tushare` SDK and no pandas, so `fin_tools` keeps `dependencies = []` and the package stays importable with nothing installed beyond the standard library
- **Market data endpoint:** `https://api.tushare.pro`. The brief names the plain-HTTP form of the same host; HTTPS is the default because the token travels in the POST body and a plaintext request would put a live credential on the wire. HTTPS was verified working against the real API. The endpoint is overridable if plain HTTP is ever required
- **Market data credentials:** `TUSHARE_TOKEN`, read from the environment at call time only. Never hard-coded, never logged, never stored on an instance, never present in `ToolSpec.declare()` output, and redacted from every error message. There is deliberately no `set_token()` — a token in a process global is a token in a traceback
- **Market data endpoints are allow-listed:** `trade_cal`, `daily`, `shibor_lpr`, `shibor`, `stock_basic`, each verified against the live API before being listed. No tool accepts an `api_name`, so an agent cannot reach an endpoint nobody reviewed
- **Data storage:** SQLite per app, under the app's own data dir. Held state and
  replay guards persist there too (P2 implements).
- **Money representation:** integer cents or decimal strings, never float.
  Converted at boundaries only — a rounding error in an invoice total is a real
  financial error.
- **Deployment:** `platform/deployment` — `helios.yaml` manifest, an ASGI entry
  and `GET /health`, built and routed through the Docker Engine API (docker-py).
- **Testing:** pytest, run by `scripts/test-all.sh`. Every suite is offline: no
  network, Docker daemon, model token or provider credential required.
- **Dependency pinning:** `requirements-dev.txt` pins every version exactly, so
  two agents in two worktrees cannot drift into different dependency trees.

## Tentative
- **Action log storage:** append-only JSONL files, hash-chained on the audit side
- **Frontend:** Jinja2/HTMX is the default, but `specs/app-builder.md` still
  describes a Vite-built React component library for richer apps. Which one the
  scaffolder emits is not settled.
- Package/build tooling is mixed: hatchling for the three agent packages,
  setuptools for `financial-tools` and `deployment`.

# Tech Stack

## Decided
- **Language:** Python 3.13 (`requires-python = ">=3.13"` in the builder packages; the older three accept >=3.10 but the repo venv is a single 3.13). Build the venv from an explicit 3.13 — the system `python3` is 3.14 and has no dependencies installed
- **Repo layout:** monorepo — `platform/`, `services/`, `apps/`
- **Folder naming:** kebab-case (`agent-toolkit/`); Python package names inside folders use snake_case (hyphens can't be imported)
- **Model:** Qwen (the only generative model allowed for development)
- **Agent framework:** [Qwen-Agent](https://github.com/QwenLM/Qwen-Agent) as the base for the Agent Toolkit runtime
- **Secrets:** API keys are read from environment variables and never hard-coded or logged
- **App spec format:** YAML (`helios-app.yaml`), validated with pydantic under `extra="forbid"` — see `specs/app-builder.md`
- **Default generated-app stack:** FastAPI + SQLite (stdlib `sqlite3`, no ORM) + Jinja2 + HTMX, when a spec does not name one. Named `helios-python-web-1` in `platform/app-builder/stacks.yaml`
- **Web framework:** FastAPI — Python keeps holds and approvals in-process with the agent runtime
- **Frontend:** Jinja2 templates + HTMX, server-rendered by the app's own FastAPI process. *Changed 2026-10-15 from React built with Vite.* The property Vite was chosen for still holds — one process, one port, one origin, with no server/client runtime split and no CORS boundary on the approval flow — and server-rendered pages reach it without an install step, a build step, a second language, or a bundle that can build successfully and still render nothing. A generated app has to be buildable and debuggable by a model in minutes. See the CHANGED note in `specs/app-builder.md`
- **UI:** a shared kit of Jinja2 templates — one per pattern (`summary`, `table`, `detail`, `form`, `approval_queue`, `settings`), a base layout, and a held-action partial — over one `tokens.css` design-token file. Qwen assembles from it rather than writing UI from scratch. *Narrower than the component library originally planned*: the trade for dropping React is granularity, not consistency. Generated apps are still expected to be clean and visually credible, and every template ships its own empty/loading/error states
- **Data storage:** SQLite per app, accessed through generated parameterised SQL. *Changed 2026-10-15 from SQLAlchemy.* The generated data layer is a few dozen statements a model can read, debug and fix; an ORM is one more thing that can be subtly wrong in generated code, and it hides the parameterisation that stops SQL injection. Portability to Postgres now lives in the `stacks.yaml` type map rather than in an ORM, so moving database is still a mapping change and not a rewrite of generated code
- **Money representation:** integer cents, or `decimal` stored as `TEXT` with a parse CHECK. Never float, and never `REAL`. SQLite applies column affinity *before* a CHECK runs, so binding a Python float into a TEXT column arrives as a string and passes every constraint — no pure-SQL rule catches it, and the generated store refuses a float at the Python boundary instead
- **Package/build tooling:** hatchling with a `src/` layout, for every packaged platform module. `platform/financial-tools` is the exception — a flat layout with no `[build-system]`, so it cannot be installed editable and goes on the path via a `.pth` file written by `scripts/bootstrap-venv.sh`
- **Action log storage:** append-only JSONL files, one per run under `.runs/<run_id>/`; the auditor's own log is hash-chained
- **Vendored frontend assets:** HTMX 2.0.11 is vendored into `app_builder/kit/static/` rather than loaded from a CDN, with its version, byte count and sha256 recorded in `stacks.yaml` and asserted by a test. A generated app must work offline and behind a host that strips `/apps/<slug>`

The last four were "to be selected during implementation" and were settled on
2026-10-15 when `platform/app-builder` was built.

## Tentative
- **Testing framework:** pytest

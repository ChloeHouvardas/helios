# Tech Stack

## Decided
- **Language:** Python (version TBD)
- **Repo layout:** monorepo — `platform/`, `services/`, `apps/`
- **Folder naming:** kebab-case (`agent-toolkit/`); Python package names inside folders use snake_case (hyphens can't be imported)
- **Model:** Qwen (the only generative model allowed for development)
- **Agent framework:** [Qwen-Agent](https://github.com/QwenLM/Qwen-Agent) as the base for the Agent Toolkit runtime
- **Secrets:** API keys are read from environment variables and never hard-coded or logged
- **App spec format:** YAML (`helios-app.yaml`), validated with pydantic under `extra="forbid"` — see `specs/app-builder.md`
- **Default generated-app stack:** FastAPI + SQLite (via SQLAlchemy) + Vite-built React, when a spec does not name one
- **Web framework:** FastAPI — Python keeps holds and approvals in-process with the agent runtime
- **Frontend:** React built with Vite. Chosen over Next.js deliberately: Vite emits a static bundle that FastAPI serves, so a generated app is one process on one origin, with no server/client runtime split and no CORS boundary on the approval flow
- **UI:** a shared prebuilt component library — Qwen assembles from it rather than writing UI from scratch. Generated apps are expected to be clean and visually credible
- **Data storage:** SQLite per app, behind SQLAlchemy, so moving to Postgres is a connection string

## Tentative
- **Testing framework:** pytest
- **Action log storage:** append-only JSONL files
- Package/build tooling — to be selected during implementation.

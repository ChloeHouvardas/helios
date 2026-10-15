# Helios Development Platform

Tools Qwen uses to build, validate, and deploy applications. Implements part 1 of the architecture (`specs/architecture-mermaid.md`).

Six packages, all with code and tests:

- `action-agent-builder/` — the action side of the agent contract: tool definitions, registry, store, action log, holds, manifests. Contains the `agent_toolkit` package. See `specs/build1.md`.
- `audit-agent-builder/` — the standalone audit agent: signed scoped grants, deterministic fail-closed policy packs, fact sources, hash-chained evidence. Contains the `audit_agent` package.
- `agent-integration/` — the seam between the two builders, and the only package allowed to import both. The gated agent loop, the audit bridge, the contract adapters, the manifest generator, the Qwen model driver and the CLI. Contains the `agent_integration` package.
- `deployment/` — the deployment engine: the `helios.yaml` manifest contract, a FastAPI ingest endpoint, and a Docker Engine API orchestrator that builds app bundles on golden base images, smoke-tests them, attaches a per-app audit sidecar and routes them through nginx. Contains `engine`, `cli.helios` and `helios_sdk`.
- `financial-tools/` — the Financial Tools Developer Interface. Five sibling packages installed by one pyproject: `fin_tools` (payments, email, spreadsheets, knowledge graph, simulation, intent verification), `helios_forecast` (cash-flow projection), `spreadsheets` (workbook rendering and reconciliation), `email_tool` (templated SMTP sender) and `payments_collect` (receivables model). See `platform/financial-tools/README.md` and `platform/financial-tools/research/README.md`.
- `app-builder/` — the application builder: the 16-question intake, `helios-app.yaml` spec, scaffolder and validator. Contains the `app_builder` package and the `helios-app` CLI. See `specs/app-builder.md` and `app-builder/README.md`.

Folder names are kebab-case and are not the Python package names inside them:
`action-agent-builder/` contains `agent_toolkit`, and `financial-tools/` contains
five packages. Use `.venv/bin/python` from the repo root —
`scripts/bootstrap-venv.sh` installs all of these editable into one environment,
and `scripts/test-all.sh` runs every suite offline.

The two builders never import each other. That is not tidiness: the audit agent is
only trustworthy because the app it audits cannot modify the code that audits it.
`agent-integration/` is where that boundary is crossed, deliberately and in one
place. See `specs/decisions.md`.

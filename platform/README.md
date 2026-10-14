# Helios Development Platform

Tools Qwen uses to build, validate, and deploy applications. Implements part 1 of the architecture (`specs/architecture-mermaid.md`).

Directories are created when their build starts:

- `action-agent-builder/` — the shared agent runtime packaged into generated applications. See `specs/build1.md`.
- `audit-agent-builder/` — the framework for creating agents that audit app workflows.
- `app-builder/` — the spec, templates, and integration tooling Qwen uses to generate an application. See `specs/app-builder.md`.
- `financial-tools/` — the Financial Tools Developer Interface: payments, email, spreadsheets, knowledge graph, outcome simulation and intent verification prototypes. See `platform/financial-tools/README.md` and `platform/financial-tools/research/README.md`.

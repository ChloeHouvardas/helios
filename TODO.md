# TODO

## app-builder ↔ agent packages (dependency boundary)

`platform/app-builder/` is built against the **current** state of
`platform/action-agent-builder/` (package `agent_toolkit`) and
`platform/audit-agent-builder/` (package `audit_agent`). Both are still changing.

- Manifests are read as **plain data**, never imported. Each generated app runs its
  own copy of the shared runtime (`specs/decisions.md`).
- Known churn: `platform/agent-toolkit/` was renamed to
  `platform/action-agent-builder/` (2026-10-14). Its Python package is still
  `agent_toolkit` and its README still titles itself "Agent Toolkit" — terminology
  not yet settled.
- `agent_toolkit/store.py` is an in-memory JSON-fixture store hardcoded to
  `customers` / `invoices` for the Build 1 demo. It is **not** a reusable data
  layer; app-builder provides its own.
- If either manifest shape changes, re-check the validator's cross-file assertions —
  notably that action-manifest tools ⊆ audit-manifest declared tools.

## Stale reference (not ours)

`specs/tech-stack.md:6` still uses `agent-toolkit/` as its kebab-case naming
example. Belongs to whoever performed the rename.

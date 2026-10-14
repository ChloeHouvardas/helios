# Plan: Define the Application Builder SDK

A design + minimal-scaffold build. The deliverable is a component definition Qwen (and we) can build against later — not code, not apps, not the agents, not the tools.

## Non-interference guarantees
- All new files; **zero edits inside `platform/agent-toolkit/` or `platform/audit-agent-builder/`** (other agents are actively working there)
- New sibling folder `platform/app-builder/` — no overlap with their folders; at implementation time (later build) their manifests are read as plain data, never imported
- Shared-doc edits limited to three one-line-level touches (below)

## Deliverables

### 1. `specs/app-builder.md` — the component definition (main deliverable)
Follows the pattern of `specs/build1.md`. Sections:

- **What the Application Builder is** — the kit Qwen uses to turn a user's idea into a working app: spec + templates + integration tooling. Explicitly not the financial tools and not the agents.
- **App spec format (`helios-app.yaml`)** — YAML validated with pydantic, strict fail-closed validation, error messages written for Qwen to act on (house style of both sibling packages). Sketch of sections: `identity` (id, name, description), `stack` (a named Helios stack, **or "match existing" when the app joins an existing system**), `data` (local database entities/fields — or integration points to existing data), `graph` (entities read from / published to the shared knowledge graph), `agents` (references to action-agent manifest + audit manifest), `ui` (pages + UI patterns — or alignment with an existing system's UI), `tools` (financial tools agents may use).
- **Design principle: templates are adaptable, not fixed** — the builder must be able to generate an app that **fits into an existing system**. The question phase discovers what already exists; the scaffolder then generates what's missing and follows the existing system's conventions where they apply, rather than forcing a rigid cookie-cutter shape. A generated app is "Helios-compatible", not "Helios-identical".
- **Question phase** — the fixed intake questions Qwen works through (user's idea → answers → app spec): what data, what agents, what tools, what pages — **and what already exists** (existing system, stack, data, UI conventions) so the app integrates rather than duplicates.
- **Database schema tool** — reads the spec's `data` section, generates the app's local schema/DDL.
- **UI kit + guidelines** — documented layout/component conventions so generated apps look consistent; referenced by the `ui` section, used as defaults when the app is standalone.
- **Tech stack definitions** — named pinned stacks the spec can reference, plus the "match existing" option for integration cases; actual framework picks stay open (still TBD in `specs/tech-stack.md`) — this defines the mechanism, not the choice.
- **Knowledge graph setup** — read/publish declarations + hooks for the future shared services.
- **Scaffolder** — validated spec → app skeleton under `apps/<name>/`, skipping/adapting whatever the spec says already exists.
- **Validator** — app-spec shape + cross-file consistency (e.g. action-manifest tools ⊆ audit-manifest declared tools; referenced files exist).
- **Build order / done-when / not-in-scope** for the future implementation build — with "a generated app can fit an existing system" as an explicit done-when example.

### 2. `platform/app-builder/` — minimal scaffold
One `README.md` (what it is, component one-liners, pointer to the spec doc). No pyproject, no src, no code — added when the implementation build starts.

### 3. Small shared-doc updates
- `specs/tech-stack.md` — record: app spec is YAML validated with pydantic
- `specs/interfaces.md` — point "Generated application package format" at the new doc
- `platform/README.md` — add `app-builder/` and fix the stale "only agent-toolkit exists" line (tiny conflict risk if the audit-agent agent edits it too — flagged)

## Not doing
- No Python code, CLI, or lockfile; no apps generated; no `apps/` folder; no web-framework/DB picks; no edits in the two protected directories

## Verification
- You read `specs/app-builder.md` and confirm the component list matches what you meant — including the "fits into an existing system" caveat — the real acceptance test for a design build
- `git status` shows only: new `specs/app-builder.md`, new `platform/app-builder/README.md`, plus the three one-line doc edits
- `git status -- platform/agent-toolkit platform/audit-agent-builder` is clean

## Open default (veto anytime)
- Folder name: `app-builder` (matches sibling `audit-agent-builder`); alternative `application-builder` to match the spec's exact term

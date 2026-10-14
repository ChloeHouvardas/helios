# p3-builder — session plan

Objective: build `platform/app-builder` (package `app_builder`) per
`specs/app-builder.md`, and finish the Action Agent Builder's missing CLI.

## Environment reality (verified this session, differs from the prompt)

| Prompt said | Actual | Consequence |
|---|---|---|
| Worktree exists at `$MAIN/.qwen/worktrees/p3-builder` | Did not exist | Created it: `git worktree add .qwen/worktrees/p3-builder -b worktree-p3-builder chloe-dev` |
| `scripts/bootstrap-venv.sh` and `scripts/test-all.sh` exist (P0) | `scripts/` did not exist in any worktree | Wrote both. `scripts/` is new — P0 must reconcile, not overwrite. |
| `platform/deployment/` holds the engine | Does not exist. Engine is only in read-only `OTHER-TEAMMATE-STUFF/deployment-stuff/.../helios/` | Cannot read `engine/contracts.py` from `platform/`. Read it from `OTHER-TEAMMATE-STUFF` instead. |
| `helios.yaml` takes `audit_manifest`, `data_dir`, `env` | `Manifest` accepts only `name, tenant, entry, port, permissions` (pydantic default `extra="ignore"`) | Emit only those five. The rest would be silently dropped. |
| permissions include `forecast`, `market-data`, `spreadsheets`, `collect` | `ALLOWED_TOOLS = {"payments","email","knowledge-graph","events"}`; unknown raises `ContractError` | Derive permissions only from that closed set. |
| `ToolDefinition` has optional `records_contact` (after P0) | Fields are exactly `name, description, input_schema, output_schema, side_effect, handler` | Generated tools must pass `output_schema` (non-empty, required). No `records_contact`. |
| `fin_tools.graph.publish_verified` | Real API is `upsert_verified(kind, key, attrs, *, source)` and `add_observation(..., observer=)` | `graph.py` generation targets the real names. |
| Baseline from `scripts/test-all.sh` | Impossible — script did not exist | Baseline recorded in PROGRESS.md after writing the scripts. |
| venv at `$WT/.venv` | Did not exist | Created from `/opt/anaconda3/bin/python3.13` (3.13.9). System `python3` is 3.14. |
| `platform/financial-tools` installable via pip -e | Flat layout, no `[build-system]` → discovery fails | On the path via a `.pth` in the venv. P1 owns that dir; not edited. |

## Decisions taken without asking (safer/reversible, recorded per section D)

1. **Spec model rejects unsafe SQL identifiers.** Entity and field names become
   SQLite DDL. `^[a-z_][a-z0-9_]*$` only, refused rather than escaped — generated
   code interpolating a name into `CREATE TABLE` is an injection point.
2. **Money heuristic excludes `string`.** Fixes the `payer_behavior` false positive
   properly: the heuristic only fires on numeric-looking types, where a float
   really is a bug. `due_date` was already handled by the date exclusion.
3. **`extra="forbid"` + all-problems-at-once.** `AppSpecError` carries every
   problem; each line is `<yaml path>: <problem>; write <what instead>`.
4. **`app_builder` does not import the sibling packages at module scope.**
   `validate` imports them lazily so shape-checking works without them installed.
   The scaffolder never imports them — it reads manifests as plain data.
5. **Two validation layers.** `spec.py` = syntax (single file, no disk).
   `validate.py` = semantics (cross-file, reads the app). Keeps a malformed spec
   rejectable without touching the filesystem.
6. **Stack override recorded in the spec, not the code.** Decision 5 (FastAPI +
   SQLite + Jinja2/HTMX) overrides the spec's Vite/React; `specs/app-builder.md`
   is updated in Task 8 with the why.

## Task order

- [x] **Task 1** — package + spec model. `pyproject.toml`, `spec.py`, tests.
- [x] **Task 2** — `validate.py` over `AppSpec`; `check_app.py` becomes a wrapper.
- [x] **Task 3** — `stacks.yaml` + `schema.py` (DDL, `store.py`, seed).
- [x] **Task 4** — `kit/` (base layout, tokens.css, six patterns, held_action).
- [x] **Task 5** — `scaffold.py` + `helios-app new`.
- [x] **Task 6** — intake (`questions`, `spec-from-intake`).
- [x] **Task 7** — `agent_toolkit.cli` (inspect / validate / demo).
- [x] **Task 8** — BUILD.md rewrite, SKILL.md, platform-status.md, spec update.

## Out of scope (other sessions own these)

`platform/deployment/` (P2), `platform/financial-tools/` (P1),
`apps/helios-collect/` (P2), `OTHER-TEAMMATE-STUFF/` (read-only),
`docs/milestones/`, `.env`, `*.venv`.

Requests for P1/P2 live in `HANDOFF-TO-P1-P2.md`.

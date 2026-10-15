# worktree-p3-builder — what this worktree contains

Self-description for whoever integrates it. Everything below was verified on
2026-10-15 by running it, not inferred from the session prompt.

This document describes **only this worktree**. Merge ordering, other branches'
contents, and conflict resolution are not covered here.

## Goal of the session

Build the **Application Builder** that `specs/app-builder.md` specified but nothing
implemented, so that a coding agent given one sentence of intent can:

1. answer the 16-question intake into a file,
2. scaffold a complete, validator-clean app skeleton,
3. fill in the policy and the scenarios,
4. validate,
5. hand the folder to `helios deploy`.

Plus one side task: finish the Action Agent Builder's missing CLI.

The session is `p3-builder`; notes are in `docs/sessions/p3-builder/`.

## Footprint

**51 files: 43 added, 8 modified.** No deletions.

| Area | Files | Nature |
|---|---|---|
| `platform/app-builder/` | 38 | 36 new + `README.md` and `PLAN.md` rewritten |
| `platform/action-agent-builder/` | 2 | new `src/agent_toolkit/cli.py` + `tests/test_cli.py` |
| `scripts/` | 2 | new `bootstrap-venv.sh`, `test-all.sh` |
| `docs/sessions/p3-builder/` | 3 | new `PLAN.md`, `PROGRESS.md`, `HANDOFF-TO-P1-P2.md` |

The 8 **modified pre-existing** files — the only ones that can collide with another
branch:

```
entry-point/BUILD.md                          rewritten as an 11-step procedure
entry-point/SKILL.md                          updated build order + warnings
entry-point/checks/check_app.py               408 lines -> 56-line wrapper
entry-point/references/platform-status.md     corrected stale status claims
platform/app-builder/README.md                placeholder -> real documentation
platform/app-builder/PLAN.md                  289 lines -> open items only
specs/app-builder.md                          updated to what shipped
specs/tech-stack.md                           React/SQLAlchemy -> Jinja2/sqlite3
```

### Packages deliberately untouched

Zero files changed in `platform/agent-integration/`,
`platform/financial-tools/`, `platform/audit-agent-builder/`,
`platform/deployment/`, and `apps/`. Those belong to other sessions; this one read
them and imported them at runtime only.

The two exceptions to "new package only" are intentional and narrow:
`agent_toolkit/cli.py` (Task 7 asked for it explicitly) and the four entry-point /
spec docs (Task 8 asked for them explicitly).

## What it does

### `app_builder.spec` — the spec model

Pydantic v2 models for `helios-app.yaml`. `extra="forbid"` on every model, closed
enums for ui patterns and field types, `stack.use` xor `stack.match_existing`,
`data.local` xor `data.integration`, kebab-case `identity.id`, plus cross-section
checks for the three options that read from the `existing` block.

`AppSpecError` carries **every** problem at once, each rendered as
`<yaml path>: <problem>; write <what instead>`.

Entity and field names are constrained to `^[a-z_][a-z0-9_]*$`. They are
interpolated into generated DDL, so an unsafe name is **refused rather than
escaped** — escaping would still let a spec author create an arbitrarily-named
column, and would have to stay correct in every generated statement forever.

`float` is rejected as a field type with a message naming `decimal` and integer
cents, because money in a binary float is a real financial error, not a display
artefact.

### `app_builder.validate` — the validator

`validate_app(dir) -> Report(errors, warnings, exit_code)`. Two layers on purpose:
`spec.py` is syntax (single file, no filesystem), this is semantics (cross-file).

Checks, in the order they matter:

- **action-manifest tools ⊆ audit-manifest declared tools.** The one that protects
  money. An undeclared tool is not denied — it is never looked at, so it runs with
  no gate at all.
- spec `tools` is a superset of both manifests' tools.
- `identity.id` == audit `app_id`.
- every audit task's allowlist ⊆ declared tools; at least one task allows something
  the agent offers.
- a gated tool in the mandate declares an `input_schema`.
- audit `side_effect` flags agree with the app's own `ToolDefinition`s — reported in
  **both directions, named separately**, because manifest-says-read-only over
  app-says-write is a control bypass while the reverse is only a nuisance.
- every fact key a rule binds is produced by `facts.py`.
- every argument a rule binds exists in that tool's `input_schema`.
- a gated tool with no `SUBJECT_KEYS` argument is warned about.

**Why the fact check reads the AST instead of calling the fact source.** The obvious
implementation — import `facts.py`, call `extra_facts_for("dummy")` — returns an
empty dict for a *correct* app. A fact source must return `None`/`{}` for a subject
it does not know (unknown is None and never zero, or a fail-closed control becomes
fail-open), so a dummy subject yields no keys and every correct app is reported
broken. `probe_fact_keys` therefore reads the key set from the AST — a superset of
what any one call could return, which is the question actually being asked. Modules
are still imported, in a subprocess with `-B`, to prove they import and to resolve
constant names.

Two branches are **unreachable through `AuditManifest.load()`**, which already raises
on the same conditions (undeclared task/rule tool; side-effecting tool with no
schema). Kept as documented defence in depth for a manifest built in memory.

### `app_builder.stacks` + `stacks.yaml`

Defines `helios-python-web-1`: FastAPI 0.143.1, stdlib `sqlite3` (no ORM), Jinja2
3.1.6, HTMX 2.0.11, Python 3.13. Every version was read from an installed
distribution or the vendored file's own banner, not recalled.

`stacks.yaml` lives at `platform/app-builder/stacks.yaml` — **outside `src/`** — and
is copied into the wheel by a `force-include`. Resolution tries the package
directory first, then the repo layout, because an editable install hides this class
of bug entirely (the source tree *is* the install). Verified by building a wheel and
installing it non-editable.

Three SQLite properties shaped the type map and are easy to get wrong:

- **Column affinity is applied before a CHECK runs.** Binding the float `12.5` into a
  TEXT column arrives as the string `'12.5'` and passes every constraint. No
  pure-SQL rule can catch it, so the float refusal lives in the coercion layer at the
  Python boundary.
- **An unguarded CHECK rejects NULL**, because `typeof(col) = 'text'` is false for
  NULL. Optional columns get `CHECK (col IS NULL OR (...))`; required ones do not.
- **GLOB is not regex.** `?` is exactly one character and never "optional"; it matches
  digit *shape* only, so `'2026-13-45'` satisfies a date pattern unless explicit
  `BETWEEN` range clauses are added. The fragments were probed against SQLite 3.51.0.

### `app_builder.schema`

`ddl_for(spec)`, `store_module_for(spec)`, `seed_from_json(conn, data_dir)`.

`decimal` → `TEXT` with a parse CHECK, never `REAL`. `date`/`datetime` → ISO `TEXT`.
`json` → `TEXT` with `json_valid`. `boolean` → `INTEGER` constrained to 0/1.

`data.integration` produces **no DDL** and a store whose accessors raise naming the
source — data the app does not own is not created by it, and an empty result would
read as "there is nothing" when the truth is "this is not wired up".

The generated `store.py` is standalone (it cannot import `app_builder`), but the
seeder runs inside `app_builder` and needs identical value rules. Two copies would
mean a fixture accepted at seed time and refused at write time — or the reverse,
which is how a wrong number reaches a database without an error. So the rules exist
once, in `_runtime_coerce.py`, and `schema.py` embeds that file's **source text** via
`inspect.getsource`.

Identifiers are always emitted quoted, because the spec's identifier pattern still
admits SQL keywords: a spec naming a field `order` is legal and would otherwise
generate DDL that does not parse.

### `app_builder.kit`

Jinja2 templates plus HTMX rather than the React/Vite library the spec originally
described — the reason is recorded in a CHANGED note in `specs/app-builder.md`. One
`tokens.css` (light and dark from `prefers-color-scheme` over one variable set), a
base layout, one template per pattern (`summary`, `table`, `detail`, `form`,
`approval_queue`, `settings`), and `_held_action.html.j2`.

- **Relative URLs only**, because the deployment host strips `/apps/<slug>`. The
  consequence is that generated pages must be served at flat routes. Asserted against
  the template *source*, not rendered output, because an absolute URL inside a
  conditional block would not appear in every render.
- Every template renders with an empty context **and** under `StrictUndefined`, and
  carries its own empty state.
- HTMX is **vendored**, not loaded from a CDN, with version/bytes/sha256 recorded in
  `stacks.yaml` and asserted by a test. A generated app must work offline and behind
  a path-stripping host.
- Masking **extends** `audit_agent.contracts.REDACTED_KEYS` rather than reusing it.
  `auth_code` is not in the platform set and is exactly what a payment proposal
  carries; the approval queue is the one page where a mistake puts a credential on a
  screen in front of a human. A test asserts the platform keys stay a subset.

### `app_builder.scaffold`

`scaffold(app_dir, spec, ...)` writes 23 files. Reads the spec only — never imports
the agent builders; manifests are read as plain data.

- **Never overwrites.** An existing file is reported `skipped: <path> (exists)`.
- **Idempotent.** A second run writes nothing.
- **Everything is rendered before anything is written**, so an aborted run cannot
  leave a half-generated app that passes some checks and fails others.
- The skeleton **validates clean** (0 errors) and its own generated tests pass.

Generated content: `helios-app.yaml` (copied **verbatim**, so the author's comments
survive), `helios.yaml`, both manifests, `facts.py`, `tools.py`, `graph.py`,
`store.py`, `app.py`, `server.py`, `data/schema.sql`, `data/README.md`, the declared
templates and static assets, `tests/test_controls.py`, `README.md`.

**No audit rules are generated** — only the five rule shapes as a commented block
with the exact YAML keys. A generated rule with a made-up ceiling passes validation,
reads like a control, and caps the wrong thing, so the owner believes the app is
governed when the number came from a generator. Policy is the product.

`side_effect` flags are **guessed from the tool name** and each carries `# REVIEW`.
Only the segment carrying the operation counts: the last segment of a namespaced name
(`payments.send`), the first of a snake_case one (`record_payment`). Checking every
segment looks equally reasonable and is wrong — `read_record` contains the segment
`record`. All nine tools across both reference apps are guessed correctly.

Tool handler stubs **raise** `NotImplementedError` naming the tool, the arguments it
received and the file to edit. A stub returning a plausible empty result would let a
demo succeed while doing nothing.

`app.py` composes `build_integration` in the same order as `apps/helios-collect/app.py`
(build, then swap in the app's own `FactResolver`), and carries `ScriptedOwner`,
`ScriptedDriver`, `run_scenario`, `_summarize` and the page renderers, so
`python app.py` drives scenarios through the gate. `_summarize` derives the outcome
from the **recorded actions**, not the loop's status, because a run whose only side
effect was refused still reports COMPLETED.

`server.py` exposes `/health`, `/`, `/holds`, `POST /holds/decide`, `/runs`,
`/audit`, with `build_app` imported lazily so the engine's smoke test does not depend
on data being present. The decide endpoint resolves through the audit bridge and
returns **409** with the auditor's own message for an unknown or already-decided hold
— never a 200 for a decision it did not record. It parses the body with
`urllib.parse` rather than `request.form()`, because Starlette asserts
`python-multipart` is installed for any form, urlencoded included, and that package
is not a Helios dependency.

### `app_builder.intake` + `cli`

`helios-app questions | spec-from-intake | new | validate`. Exit codes 0 clean,
1 problems, 2 usage — the same ones `check_app.py` has always used.

An unanswered question is recorded as `not sure` and becomes a `# TODO` naming the
question that would fill it, **never a guess**: a plausible-looking wrong entity is
worse than an absent one, because the absence gets noticed and the guess gets built
on. An `approval_queue` page is added when the answers omit one, since without it
held actions are invisible to the owner.

### `app_builder.deployment`

Reads `platform/deployment/engine/contracts.py` **at generation time** (AST) and
emits only the fields that model declares. The engine's `Manifest` does not forbid
extra keys, so an unrecognised one is dropped **silently** — emitting `data_dir` or
`env` would produce a manifest that looks configured and is not.

Permissions are filtered against the engine's `ALLOWED_TOOLS`, because an unknown
permission is a hard `ContractError` at deploy time. Tool families with no mapping
are dropped and named in a `# REVIEW` comment.

Falls back to the field set recorded from the read-only engine copy when
`platform/deployment/` is absent, and says so in the generated file. **When the real
engine lands, generated manifests widen automatically** — a test builds a fake engine
with an extra `data_dir` field to pin that behaviour.

### `agent_toolkit.cli` (Task 7)

`inspect`, `validate [--audit]`, `demo`.

`pyproject.toml` had declared `agent-toolkit = "agent_toolkit.cli:main"` since the
package was created but the module did not exist, so the installed console script
crashed with `ModuleNotFoundError` on every invocation. **No `pyproject.toml` change
was needed** — only the module.

`audit_agent` is imported **inside** the validate handler, not at module scope, to
preserve the boundary that `agent-integration` is the only package importing both.

`demo` drives the store, gate and action log through four proposals — allowed, held,
denied, undeclared — with a fixed date, no model, no network. It shows the whole
decision space rather than only the happy path, because a demo of approvals alone
would not demonstrate that the control works when the agent misbehaves.

## Tests

411 in `platform/app-builder/tests`, 36 in `platform/action-agent-builder/tests`.

`tests/test_boot.py` is the one to know about. A generated `server.py` raised
`AssertionError: Routed paths must start with '/'` at import time and passed **386
green tests**, because every other test treated generated output as *text* — grepping
strings or compiling a module. None imported it into a running app. That file
scaffolds, imports `server`, wraps it in Starlette's `TestClient` in-process, and
requests every route for both reference apps. Verified it can fail: reintroducing the
mount bug makes 16 of 21 fail.

`tests/fixtures/broken-app/` is wrong **on purpose** — see its README before
"fixing" it. It produces exactly 8 errors and 8 warnings, one per check, and each is
asserted by name so a check that stops running fails on its own row.

## What this worktree assumes about other packages

These are the integration assumptions. If any changed elsewhere, re-verify here.

- **`audit_agent`**: `AuditManifest.load()`; the `FactSource` protocol's five methods
  plus `record_contact`; `FactResolver.SUBJECT_KEYS` ==
  `(invoice_id, bill_id, customer_id, supplier_id, subject_id, payment_id)` — a test
  asserts the local copy has not drifted. `contracts.REDACTED_KEYS`, and the fact that
  `auth_code` is **not** in it. `HoldRecord` fields (`hold_id`, `tool`, `arguments`,
  `findings`, `owner_question`, `status`, `created_at`, `decided_by`, `decided_at`),
  and `resolve_hold(hold_id, *, approve, decided_by, note)`.
- **`agent_toolkit`**: `ToolDefinition` fields are exactly `name`, `description`,
  `input_schema`, `output_schema`, `side_effect`, `handler` — `output_schema` is
  **required and non-empty**, and there is **no `records_contact` field**.
  `manifest.validate_manifest(dict)` vs `load_manifest(path)` (different signatures;
  conflating them fails as a `TypeError` from `pathlib`). `ALLOWED_FIELDS` rejects
  even a `_comment` key. `MandateLimits` accepts only four receivables-shaped keys.
  `ActionLog`, `HoldStore`, `Store.from_fixtures`.
- **`agent_integration`**: `build_integration(...)` signature; `Integration.bridge`,
  `.audit_agent`, `.runtime`; `runtime.run(driver=, task=, task_type=, run_id=,
  requested_tools=)` and `runtime.resume(...)`; `RunResult.{status,actions,holds}`.
- **`fin_tools`**: `build_world()`; `KnowledgeGraph.upsert_verified(kind, key, attrs,
  *, source)` and `add_observation(..., observer=)`. Generated `graph.py` targets
  those names — an earlier note in this repo called the method `publish_verified`,
  which does not exist (there is a registry *tool* by that name, not a method).
- **Verdict vocabulary**: the runtime writes `approve` / `reject` / `hold` into
  `actions[].verdict.decision`, while the auditor's `Decision` enum is `allow` /
  `deny`. Two vocabularies at two layers. `_summarize` and its tests use the runtime's.

## Known limitations of this worktree

- **No generated app has been deployed through the engine**, because
  `platform/deployment/` did not exist here. Everything short of that has been
  exercised locally: the server boots under uvicorn, every route answers 200, static
  assets serve, and the decide endpoint returns correct 400/409 on the error paths.
- **`stack.match_existing` scaffolding is refused, not generated.** An integration
  stack has no `stacks.yaml` entry and therefore no type map to generate DDL from;
  emitting SQLite tables for an app joining a Postgres system would be wrong in a way
  that looks fine until it runs.
- **`ui.align_with`** is validated (requires `existing.ui`) but otherwise ignored;
  token substitution is not implemented.
- **Graph subscribe** is a stub that raises, because no subscription transport is
  settled. Publish works against `fin_tools`.
- **`identity.id` uniqueness across apps** is not enforced — nothing in the repo
  holds a registry of app ids to check against.
- Generated **policy is a TODO block** and tool handlers raise. That is deliberate,
  but it means a scaffolded app is a skeleton until someone writes both.

## Verification

```bash
scripts/bootstrap-venv.sh
scripts/test-all.sh                     # exit 0; 6 suites, 822 tests, per-suite counts

.venv/bin/helios-app validate apps/helios-collect                     # exit 0
.venv/bin/helios-app validate entry-point/examples/invoice-payer      # exit 0
.venv/bin/helios-app validate platform/app-builder/tests/fixtures/broken-app
                                                                    # exit 1, 8 errors

rm -rf /tmp/smoke
.venv/bin/helios-app new /tmp/smoke \
    --spec entry-point/examples/invoice-payer/helios-app.yaml         # 23 files
.venv/bin/helios-app validate /tmp/smoke                              # exit 0
.venv/bin/helios-app new /tmp/smoke --spec ...                        # 0 written, 23 skipped

# Boot it. Not redundant — see the test_boot.py note above.
(cd /tmp/smoke && PYTHONPATH=. <venv-python> -c "import server")      # must not raise

.venv/bin/agent-toolkit demo --run-dir /tmp/demo                      # exit 0, four outcomes
```

`test-all.sh` passes `--override-ini="addopts="` to pytest. Four packages declare
`addopts = "-q"` themselves, and adding `-q` on the command line makes it `-qq`,
which suppresses the "N passed" summary — so without the override the runner reports
green with no counts at all.

## Session notes

`docs/sessions/p3-builder/PLAN.md` — the plan, including a table of four places the
environment differed from the task description and the decisions taken.
`docs/sessions/p3-builder/PROGRESS.md` — running log with the acceptance outputs
pasted verbatim and the defects found along the way.
`docs/sessions/p3-builder/HANDOFF-TO-P1-P2.md` — requests left for other sessions.

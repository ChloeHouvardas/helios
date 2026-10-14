# p3-builder — progress log

Three lines per entry: what changed, why, what it affects.

## 0. Environment bootstrap

- Created the worktree and branch (neither existed), wrote `scripts/bootstrap-venv.sh`
  and `scripts/test-all.sh` (P0 had not landed them), and built `.venv` from
  `/opt/anaconda3/bin/python3.13`.
- Why: without them there is no interpreter with the dependencies and no way to
  record a baseline, and section D requires both before any edit.
- Affects: every later task. `scripts/` is a shared path — P0 must reconcile rather
  than overwrite; noted in `HANDOFF-TO-P1-P2.md`.

## Baseline

`scripts/test-all.sh` did not exist, so no baseline could be taken before writing it.
The counts below were taken immediately after, before any existing file was modified
— which for this session is the same thing, since Task 1 only adds files.

`scripts/test-all.sh` exits 0. Counts are recorded per suite so a later regression is
attributable to a named package rather than to "something broke".

| suite | result |
|---|---|
| audit-agent-builder | 79 passed |
| agent-integration | 123 passed |
| financial-tools | 86 passed |
| app-builder | 57 passed (new this session) |
| helios-collect | 74 passed |
| action-agent-builder | skipped — no `tests/` directory |
| entry-point | skipped — no `tests/` directory |

Pre-existing total excluding app-builder: **362 passed**.

One bug found and fixed while establishing this baseline: four packages declare
`addopts = "-q"` in their own `pyproject.toml`, and the script passed `-q` too. The
two combine to `-qq`, which suppresses pytest's "N passed" summary — so the script
reported green with no counts at all, which is the worst of both outcomes.
`test-all.sh` now clears per-package `addopts` with `--override-ini="addopts="` and
applies exactly one `-q` itself, so the counts do not depend on each package's own
configuration.

## 1. Task 1 — package and spec model

- Added `platform/app-builder/{pyproject.toml,src/app_builder/{__init__,spec}.py,tests/test_spec.py}`.
  `spec.py` is the pydantic v2 model of `helios-app.yaml`: `extra="forbid"` everywhere,
  closed enums for ui patterns and field types, `stack.use` xor `stack.match_existing`,
  `data.local` xor `data.integration`, kebab `identity.id`, and `AppSpecError` carrying
  every problem at once as `<yaml path>: <problem>; write <what instead>`.
- Why: the spec is the contract the whole builder reads. Shape validation is kept
  separate from cross-file validation so a malformed spec is rejectable without
  touching the filesystem, and so `validate.py` can assume the shape is sound.
- Affects: nothing yet — no existing file was modified. Both real apps' specs load
  unchanged; 57 new tests pass.

### Two safety properties added beyond the prompt

1. **SQL identifier validation.** Entity and field names are interpolated into
   generated DDL by Task 3, so `spec.py` refuses anything outside
   `^[a-z_][a-z0-9_]*$`. Refusing is not the same as escaping: an escaped name would
   still let a spec author create an arbitrarily-named column, and the escaping would
   have to be correct in every generated statement forever.
2. **Money heuristic excludes `string`.** Fixes the `payer_behavior` false positive
   named in Task 2. The heuristic now only fires where a float would genuinely be a
   bug, because a checker that cries wolf on prose fields gets ignored — and an
   ignored checker misses the real one.

## 2. Task 2 — validator

- Added `app_builder/validate.py` (`validate_app(dir) -> Report` with `errors`,
  `warnings`, `exit_code`) on top of `AppSpec`, carrying over every cross-file check
  `check_app.py` had and adding four: `side_effect` agreement with `tools.py`,
  fact-key coverage against `facts.py`, rule arguments present in the tool's
  `input_schema`, and a subject-key warning for gated tools. Rewrote
  `entry-point/checks/check_app.py` as a 56-line wrapper (was 408); it keeps the
  same interface, the same exit codes 0/1/2 and `--strict`.
- Why: the cross-file checks are the reason a validator exists separately from the
  schema. Each new one closes a failure mode that is silent and safe-directioned,
  which is exactly the kind that survives to the demo.
- Affects: both real apps still validate with 0 errors (2 and 4 warnings). The new
  `broken-app` fixture produces exactly 8 errors and 8 warnings, each check firing
  once. Suite count 362 -> 457 passed; `test-all.sh` exits 0.

### Two defects found and fixed while building this task

1. **A relative app path was resolved twice.** `_import_module` runs a subprocess
   with `cwd=app_dir`; a relative path handed in by the caller was resolved against
   the caller's cwd and then again against the subprocess cwd, producing
   `apps/helios-collect/apps/helios-collect/facts.py`. The AST half worked, so only
   the import probe failed — and it failed *as a warning*, which is how it survived
   the first run against both real apps looking clean. Now resolved to absolute
   before use, with a regression test that cds to the repo root.
2. **Two error branches were unreachable, and one was dead.** `AuditManifest.load()`
   already raises `UnknownToolError` for an undeclared task/rule tool and
   `PolicyError` for a side-effecting tool with no `input_schema`, so those two
   validator branches cannot fire on a manifest read from disk. Separately, the
   "monetary field in a non-monetary type" error became unreachable once the money
   heuristic excluded `string`: `FieldType` is closed and excludes `float`, so the
   only types reaching that loop are `decimal` and `integer` — both correct. The
   dead branch was removed rather than left behind; code that looks like a control
   but cannot run is worse than no code, because it reads as coverage that does not
   exist. The two unreachable-by-`load()` branches were kept and documented as
   defence in depth, because they do fire for a manifest built in memory.

### Why the fact-coverage check reads the AST instead of calling the fact source

The task description says to import `facts.py` and call `extra_facts_for` with a
dummy subject, treating `None` as fine and a missing key as an error. That
implementation reports errors against a **correct** app, so it was not used.

A fact source must return `None`/`{}` for a subject it does not know. That is
required behaviour rather than a limitation: unknown must be None and never zero, or
a fail-closed control becomes fail-open (an unknown balance read as "no exposure").
So a dummy subject yields no keys at all, every key the rules bind looks missing, and
`helios-collect` — whose `extra_facts_for` produces 11 keys — fails validation.

`probe_fact_keys` instead reads the key set from the AST: every string literal used
as a dict key or subscript target inside `extra_facts_for`. That is a superset of
what any single call can return, which is the right question — "could this key ever
be produced" rather than "was it produced for a subject I made up". The module is
still imported, in a subprocess, to prove it imports and to resolve constant names.
Verified non-vacuous: injecting a bogus `fact:` key into helios-collect's real
manifest produces exactly one error naming it.

`tools.py` is scanned the same way, because real apps construct `ToolDefinition`s
inside factory functions that need a store and a bundle
(`make_record_payment(book, bundle.payments, bundle.graph)`), so nothing can be
enumerated from module globals without building the whole app.

### Fixture design constraint worth knowing

`broken-app`'s spec and audit manifest are both **valid on purpose**.
`validate_app` stops after shape errors (the cross-file checks would be
meaningless), and `AuditManifest.load()` raises on several defects before the
validator sees them. So every defect in the fixture is one that only shows up as a
disagreement *between* files. Both preconditions are pinned by tests
(`test_the_broken_fixture_spec_is_itself_valid`,
`test_the_broken_fixture_tools_and_facts_both_import`) so a well-meaning edit cannot
quietly disable the checks. See `tests/fixtures/broken-app/README.md`.

## 3. Task 3 — stacks.yaml and the schema tool

- Added `stacks.yaml` (`helios-python-web-1`: FastAPI 0.143.1 + stdlib `sqlite3` +
  Jinja2 3.1.6 + HTMX 2.0.11, Python 3.13, with the type map and the CHECK
  fragments), `stacks.py`, `schema.py` (`ddl_for`, `store_module_for`,
  `seed_from_json`) and `_runtime_coerce.py`. Vendored `htmx.min.js` under
  `kit/static/`.
- Why: the spec's `stack.use` resolved to nothing, so both real apps named a stack
  that did not exist. Money is TEXT with a parse CHECK rather than REAL, because a
  rounding error in an invoice total is a real financial error, not a display
  artefact.
- Affects: no existing file modified. Every version was read from an installed
  distribution or the vendored file's own banner, never recalled.

### Three findings that look correct on a first read

1. **SQLite applies column affinity BEFORE a CHECK runs.** Binding the float `12.5`
   into a TEXT column arrives as the string `'12.5'` and satisfies every
   constraint, so no pure-SQL rule can distinguish a decimal string from a float
   that stringifies. The float refusal therefore lives in the coercion layer at
   the Python boundary, where the value is still a float and can be refused with
   its field named.
2. **An unguarded CHECK rejects NULL.** A CHECK passes when it evaluates to NULL,
   but `typeof(col) = 'text'` is FALSE for NULL — so every *optional*
   date/decimal/json column was unwritable. `helios-collect.last_contacted` hit
   this immediately. Optional columns now emit `CHECK (col IS NULL OR (...))`.
3. **GLOB matches digit SHAPE only**, so `'2026-13-45'` satisfied the date pattern
   and `'2026-10-15T99:99:99'` satisfied the datetime one. Explicit `BETWEEN`
   range clauses were added for month, day, hour, minute and second.

### One implementation of the coercion rules, embedded

The generated `store.py` cannot import `app_builder`, but the seeder runs inside
`app_builder` and needs identical rules. Two copies would mean a fixture accepted
at seed time and refused at write time — or the reverse, which is how a wrong
number gets into a database without an error. So the rules exist once in
`_runtime_coerce.py` and `schema.py` embeds that file's *source text* into each
generated store via `inspect.getsource`. Same code, two places, no drift.

### A packaging bug that editable installs hide completely

`stacks.yaml` lives outside `src/`, so it was absent from the built wheel and an
installed `app-builder` could not generate any schema. Every test passed anyway,
because an editable install *is* the source tree. Found by building a wheel and
installing it non-editable into a scratch venv, then generating DDL from
site-packages. Added `force-include` plus a test asserting the resolved path is one
of the searched candidates. **Anything that must work installed needs this check**;
the repo's own test suite can never catch it.

## 4. Task 4 — the UI kit

- Added `app_builder/kit/` (the package) and `kit/__init__.py` (the module):
  `base.html.j2`, `tokens.css`, one template per pattern
  (`summary, table, detail, form, approval_queue, settings`) and the
  `_held_action.html.j2` partial. Relative URLs throughout; light/dark from one
  token set via `prefers-color-scheme`.
- Why: P2 had not landed `apps/helios-collect/templates/` (checked), so there was
  nothing to start from. The kit is written to be copied into a generated app
  rather than imported by it.
- Affects: no existing file modified. 91 new tests; suite total 362 -> 620 passed.

### `StrictUndefined` is what caught the real template bugs

Rendering each pattern under `StrictUndefined` found two classes of defect that the
lenient render hid completely:

1. **An optional variable in `{% if %}` raises under strict mode.** The `default`
   filter is safe because it receives the Undefined object without touching it, but
   `{% if subtitle %}` calls `__bool__` and raises. All five patterns that extend
   `base.html.j2` failed on this one line in the layout. Each template now
   normalises its optional context up front with `{% set x = x | default(...) %}`.
2. **`default('...')` does not catch an EMPTY value.** A hold whose
   `owner_question` was `""` rendered nothing for the owner to decide on — a blank
   question on the one card whose entire purpose is to ask one. Fixed with
   `default(..., true)`.

### The kit's masking extends the platform set rather than reusing it

`auth_code` — a single-use credential bound to (amount, currency, payee), and
exactly what a payment proposal carries — is **not** in
`audit_agent.contracts.REDACTED_KEYS`. Relying on the platform set would have
printed it on the approval queue, in front of a human, on the one page where a
mistake is most visible. `SENSITIVE_KEYS` is the union of the platform set and the
kit's own additions, and a test asserts the platform's keys are a subset so the
kit cannot fall behind if the platform list grows.

Masking stays shallow for the same reason `audit_agent.redact` does: a deep walk
would rewrite a nested key that happens to be named `token` for a legitimate
business reason (a queue token, a pagination token), and silently mangling real
data is its own kind of bug.

### Coverage gap closed

The pattern-level strict test renders `approval_queue` with **no holds**, so
`_held_action.html.j2` — the template where masking happens — was never exercised
under `StrictUndefined`. Added explicit tests for it with a complete record, a
minimal one and an empty one. A queue that errors on one malformed hold would hide
every other pending action behind it, which is the worst failure available for that
page.

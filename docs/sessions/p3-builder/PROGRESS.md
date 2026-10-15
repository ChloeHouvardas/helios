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

## 5. Task 5 — scaffolder

- Added `app_builder/scaffold.py` and `app_builder/deployment.py`. `scaffold()` writes
  23 files from a validated spec; `deployment.py` reads the engine's accepted
  `helios.yaml` field set at generation time.
- Why: this is the piece that turns a spec into something runnable. The generated
  skeleton validates with 0 errors and its own generated tests pass, so it is a
  working starting point rather than a pile of stubs.
- Affects: no existing file modified. Verified against both real apps' specs and via
  a `--dry-run` into `apps/helios-collect` that listed only files absent there and
  wrote nothing.

### Four bugs found while building it

1. **`helios-app.yaml` was not among the generated files.** The scaffolded app could
   not be validated at all, because the validator resolves both manifest paths
   relative to that file. The spec is now copied **verbatim** rather than re-dumped
   from the model, because a spec carries the author's comments — the reasoning behind
   a data model, why an entity is integrated rather than local — and round-tripping
   through pydantic destroys all of it.
2. **`yaml.safe_dump(model_dump())` raises `RepresenterError`.** The closed sets are
   `StrEnum` members, which YAML cannot represent. `mode="json"` emits the plain
   string values. This only affects the in-memory fallback path, which is why the
   first smoke test — which passed a `spec_path` — did not catch it.
3. **`guess_side_effect` matched a verb anywhere in the name**, so `read_record`
   looked like a write because it contains the segment `record`. Now only the segment
   carrying the *operation* counts: the last one for a namespaced name
   (`payments.send`), the first for snake_case (`record_payment`). All nine tools
   across both real apps are guessed correctly under this rule — verified by comparing
   against their audit manifests, which is the only available evidence the rule is
   right rather than merely plausible.
4. **A generated test called `load_manifest(dict)`.** That function takes a *path*;
   `validate_manifest` takes the document. The failure surfaced as a `TypeError` from
   inside `pathlib`, which reads like a broken install.

## 6. Task 6 — intake

- Added `app_builder/intake.py` and `app_builder/cli.py` (`questions`,
  `spec-from-intake`, `new`, `validate`). Exit codes match `check_app.py`'s existing
  0/1/2 so pipelines can treat all three alike.
- Why: this is the on-ramp from one sentence to a spec. An unanswered question is
  recorded as `not sure` and becomes a `# TODO`, never a guess.
- Affects: no existing file modified. Verified end to end: intake -> spec -> scaffold
  -> `validate` with 0 errors, nothing in between hand-written.

### Three parsing bugs, all found by writing the tests

1. **Page patterns were matched by scanning the whole answer for members of
   `UI_PATTERNS`.** That set is a `frozenset`, so match order is arbitrary — and the
   pattern list is zipped against the page list *positionally*. The result mapped
   `holds` to `settings` and `policy` to `approval_queue`. Now matched per chunk,
   preserving the order the user wrote, with a chunk that names no pattern still
   consuming a slot so later pages do not shift.
2. **A structured answer was flattened by dumping each element and joining with
   `"; "`.** The result is not valid YAML, so entity parsing fell through to line
   scanning and turned `[{"name": "invoice", ...}]` into an entity called `name`. The
   whole structure is now dumped once.
3. **`--app-id` was passed through unvalidated**, so `!!!` produced a spec that failed
   inside `AppSpec.parse` with a message about the *spec* rather than about the
   argument, with no way to connect the two. Now validated, offering the derived form
   as an example.

The emptiness check for a structured answer must not recurse through the cleaner: the
first version passed the dict back into the function calling it and hit
`RecursionError` on any dict-shaped answer.

## 7. Task 7 — `agent_toolkit.cli`

- Added `platform/action-agent-builder/src/agent_toolkit/cli.py` with `inspect`,
  `validate [--audit]` and `demo`, plus `tests/test_cli.py` (36 tests) — the first
  `tests/` directory in that package, so `test-all.sh` went from 5 suites to 6.
- Why: `pyproject.toml` has declared `agent-toolkit = "agent_toolkit.cli:main"` since
  the package was created, but the module did not exist, so the installed console
  script crashed with `ModuleNotFoundError` on every invocation. **No `pyproject.toml`
  change was needed** — the task description said P0 had removed the script entry, but
  it was still declared and simply broken. A regression test invokes the entry point
  as a subprocess, because calling `main()` in-process cannot detect broken console
  wiring.
- Affects: one new module and one new test file in another agent's package. Nothing
  existing was modified. `audit_agent` is imported *inside* the validate handler, not
  at module scope, to preserve the boundary that `agent-integration` is the only
  package importing both.

`demo` drives the store, gate and action log through four proposals — allowed, held,
denied, undeclared — with a fixed date, no model and no network. It shows the whole
decision space rather than only the happy path, because a demo of approvals alone
would not demonstrate that the control works when the agent misbehaves, which is the
only case the control exists for. Fixture data made the four outcomes available
without inventing any: INV-1003 is 12,500 against a 10,000 threshold, and CUST-002 was
contacted 1 day ago against a 3-day minimum.

## 8. Task 8 — the coding-agent on-ramp

- Rewrote `entry-point/BUILD.md` as an 11-step procedure around the real commands,
  with a Traps section preserving 32 items from the old BUILD.md and HANDOFF.md §5.
  Updated `entry-point/SKILL.md` and `entry-point/references/platform-status.md` to
  the real status, `specs/app-builder.md` and `specs/tech-stack.md` to what shipped,
  and `platform/app-builder/{README,PLAN}.md`.
- Why: BUILD.md told the reader "you are the scaffolder", which is no longer true, and
  platform-status.md claimed there was no agent loop, no scaffolder, no validator and
  no `agent_toolkit` CLI — all four exist.
- Affects: seven existing files, all documentation. No code changed. Suite stayed at
  784 tests, exit 0.

Two things were **verified against source rather than trusted** from the old text
before being carried into the rewrite, because both are specific enough to be wrong:
`RunResult.summary()` really does count `len(self.actions)` for
`side_effects_attempted` while read-only calls are appended there too
(`agent_integration/runtime.py:131-132`), and `ToolDescriptor.from_contract()` really
does read `has_side_effect` where `ToolDefinition` names it `side_effect`
(`audit_agent/policy.py:92`, closed by `ToolContractView` at
`agent_integration/adapters.py:66`).

`specs/app-builder.md` gained two honest corrections rather than only good news:
`identity.id` **uniqueness across apps is not implemented** (it needs a registry of app
ids and nothing here holds one), and two cross-file checks are unreachable through
`AuditManifest.load()` because that loader already raises on the same conditions — kept
as defence in depth for an in-memory manifest and documented as such rather than
presented as live coverage.

`PLAN.md` was **shrunk, not deleted** (289 -> ~110 lines). It described Vite-built
React and SQLAlchemy, so keeping it whole meant keeping a second copy of the design
that had already drifted from both the spec and the code. Six items in it are genuinely
open and three decide whether the surprise task can be met, so the open items stayed.

## Done-when — outputs pasted

**1. `scripts/test-all.sh` exits 0 with the new suite counted**

```
=== action-agent-builder (platform/action-agent-builder) ===
36 passed in 0.22s
=== audit-agent-builder (platform/audit-agent-builder) ===
79 passed in 0.88s
=== agent-integration (platform/agent-integration) ===
123 passed in 10.65s
=== financial-tools (platform/financial-tools) ===
86 passed in 8.87s
=== app-builder (platform/app-builder) ===
386 passed in 2.94s
=== helios-collect (apps/helios-collect) ===
74 passed in 1.26s
===============================================
test-all: 6 suite(s) run
test-all: skipped — entry-point (no tests/)
test-all: all suites green
EXIT CODE = 0
```

784 tests total at the end of Task 8; **822** after §9 added `tests/test_boot.py`.
Baseline at the start of the session was 362 across 5 suites.

**2. `helios-app validate`**

| target | exit | errors | warnings |
|---|---|---|---|
| `apps/helios-collect` | 0 | 0 | 2 |
| `entry-point/examples/invoice-payer` | 0 | 0 | 4 |
| `platform/app-builder/tests/fixtures/broken-app` | 1 | **8** | 8 |

The broken fixture's 8 errors are 8 *distinct* checks: undeclared tool, spec-tools not
a superset, identity mismatch, no task allows anything, rule binds an unknown argument,
rule needs a missing fact, and the two directions of side-effect disagreement. Each is
asserted by name in `test_validate.py`, so a check that stops running fails on its own
row rather than quietly dropping the total.

**3. `helios-app new /tmp/p3-smoke --spec entry-point/examples/invoice-payer/helios-app.yaml`**

```
23 file(s) written to /private/tmp/p3-smoke
EXIT CODE = 0
```

Then, on the result: `validate` -> `0 error(s), 4 warning(s)`; re-running `new` ->
`0 file(s) written, 23 skipped`.

## Left undone, deliberately

**GLM / Zhipu / bigmodel.cn references were not removed.** The competition rule says
any reference to them is legacy and must be removed *when you touch that file*. Every
occurrence is in a file no task here touched: `SETUP.md` (1 line), `HANDOFF.md` (4
lines), and — overwhelmingly — `platform/agent-integration/`, where a case-insensitive
grep counts 48 lines across `model_driver.py` (10), `.env.example` (10),
`tests/test_model_driver.py` (16), `README.md` (8), `cli.py` (2), `__init__.py` (1)
and `tests/conftest.py` (1).

The two load-bearing lines are `model_driver.py:54-55`:
`DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"` and
`DEFAULT_MODEL = "glm-5.3"`. One thing that makes this cheaper than it looks: the
driver's env-var list at `model_driver.py:64-67` **already includes
`DASHSCOPE_API_KEY`** alongside `GLM_API_KEY` and `ZHIPUAI_API_KEY`, so the credential
side needs no change — only the two defaults and the tests that hard-code them.

Editing `platform/agent-integration/` is outside this session's scope — it is another
agent's package, and section C forbids editing directories not assigned here. The
driver is OpenAI-compatible, so reconciling it to DashScope is a base-URL and model-id
change rather than a rewrite, but its tests hard-code the old values and would all need
updating together. Recorded in `HANDOFF-TO-P1-P2.md`, in `BUILD.md`'s
non-negotiables, and in `platform-status.md` §1 instead of done. **This is the highest
priority item left in the repo** and it is not this session's to close.

Related: **`qwen_agent` is installed (0.0.34) but does not import**, because `numpy` is
absent. So the model-driven path is not runnable as-is even after the driver is
repointed — verified by attempting the import, not inferred from the dependency list.

**`identity.id` uniqueness across apps** is not enforced — there is no registry of app
ids anywhere in the repo to check against. Documented as open in
`specs/app-builder.md`.

**No generated app has been deployed through the engine.** `platform/deployment/` does
not exist in the repo, so there is nothing to deploy against. Everything short of that
has now been exercised locally — see §9: the generated server boots, serves every
route, and runs scenarios through the gate.

## 9. Continuation — make it actually run

The first eight tasks produced a generator whose output compiled and validated but had
never been started. Four defects were found by running it rather than by reading it.

**`server.py` could not be imported at all.** `app.mount("static", ...)` — Starlette
requires a mount path to begin with `/`, so every generated app raised
`AssertionError: Routed paths must start with '/'` at import time and would have failed
the engine's smoke test instantly. This passed 386 green tests, because every existing
test treated generated output as TEXT: it grepped for strings or compiled a module, and
none imported the module into a running app. `tests/test_boot.py` (25 tests) closes that
gap — it scaffolds, imports `server`, wraps it in Starlette's `TestClient` in-process,
and requests every route for both reference apps. Verified it can fail: reintroducing
the mount bug makes 16 of 21 fail.

**`POST /holds/decide` raised `NotImplementedError`.** Now wired through the audit
bridge, which owns the decision on the file and HTTP transports too. Returns 409 with
the auditor's own message for an unknown or already-decided hold, never a 200 — an
endpoint that reports success for a decision it did not record leaves the owner
believing they approved. It parses the body with `urllib.parse` rather than
`request.form()`, because Starlette asserts `python-multipart` is installed for ANY
form, urlencoded included, and that package is not a Helios dependency.

**`python app.py` demonstrated nothing.** It declared `ScriptedOwner`, `Scenario` and
`Outcome` but had no driver and no runner, so it printed a TODO and returned 0 — a
prototype that exits successfully having shown nothing. Added `ScriptedDriver`,
`run_scenario`, `_summarize` and the two page renderers, mirroring
`apps/helios-collect/app.py`. `_summarize` derives the outcome from the recorded
actions rather than the loop's status, because a run whose only side effect was REFUSED
still reports COMPLETED — a denial goes back to the model as an observation and the loop
finishes normally. Reporting the loop's status would call a blocked payment a success.

The runner is a PLAIN string concatenated into `app_module`, not part of its f-string,
so its braces stay literal. Doubling braces is the easiest way to corrupt generated
code silently: a missed brace is interpolated by the generator instead of the generated
module, and the failure surfaces as a NameError in `scaffold.py` while the real fault is
in text nothing ever compiled. Two such bugs were hit this session.

**`Outcome` carried two fields for one idea.** `expects` (set from the Scenario) and
`decided` (never set, so always `"unknown"`), and `main()` compared against `decided`.
The CI exit code therefore reported `got done, expected unknown` and only failed by
accident — the comparison could never match a declared expectation. One field now.

Verified against a real run, not only unit tests: seeded the generated store, gave the
gated tool a handler, drove `payments.send` through the gate. Allowed -> `done`,
executed exactly once. Undeclared tool -> refused with the auditor's message. Mismatch
against a declared expectation -> exit 1. `--json` and `--dashboard` both render.
Booted under uvicorn: `/health`, `/`, `/holds`, `/runs`, `/audit`, `/static/*` all 200,
and the decide endpoint returns 400/409 with named fields on the error paths.

The verdict vocabulary in the runner is the RUNTIME's (`approve`/`reject`/`hold`), not
the auditor's `Decision` enum (`allow`/`deny`). Two vocabularies at two layers; my first
test asserted the wrong one and failed on correct code.

Suite: 822 tests, exit 0. `fastapi`/`httpx`/`uvicorn` were installed but declared
nowhere in the repo, so they are now in the `dev` extra — the boot test would otherwise
pass here and fail on a fresh checkout.

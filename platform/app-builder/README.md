# App Builder

The kit that turns one sentence of intent into a working application on Helios.
Implements the Application Builder in part 1 of the architecture
(`specs/architecture-mermaid.md`). Design detail and status:
`specs/app-builder.md`.

```bash
helios-app questions > intake.yaml                        # the 16-question intake
helios-app spec-from-intake intake.yaml > helios-app.yaml  # a first spec
helios-app new apps/my-app --spec helios-app.yaml          # 23 files, validator-clean
helios-app validate apps/my-app                            # shape + cross-file
```

Design principle: **templates are adaptable, not fixed** — the question phase
discovers what already exists, and the scaffolder generates what is missing rather
than forcing one shape of app. A generated application is Helios-compatible, not
Helios-identical.

## Layout

```
stacks.yaml                     named stacks, the type map, and the CHECK fragments
src/app_builder/
  spec.py                       pydantic models for helios-app.yaml
  validate.py                   validate_app(dir) -> Report(errors, warnings, exit_code)
  stacks.py                     loads stacks.yaml; resolves a spec's stack
  schema.py                     ddl_for, store_module_for, seed_from_json
  _runtime_coerce.py            the value rules, in one place (embedded into store.py)
  kit/__init__.py               Jinja2 env, pattern map, mask_arguments
  kit/*.html.j2                 base layout, six patterns, _held_action partial
  kit/static/                   tokens.css, vendored htmx.min.js
  intake.py                     the 16 questions; spec_from_intake
  scaffold.py                   spec -> application skeleton
  deployment.py                 reads the engine's accepted helios.yaml fields
  cli.py                        the helios-app command
tests/
  test_spec.py                  one fixture per invariant
  test_validate.py              both real apps clean; the broken fixture fires once per check
  test_schema.py                DDL, CHECK semantics, generated store, seeding
  test_kit.py                   every pattern renders with a minimal context
  test_scaffold.py              23 files, idempotency, never overwrites, dry-run
  test_intake.py                questions, spec-from-intake, the CLI's exit codes
  fixtures/broken-app/          wrong on purpose — see its README before "fixing" it
```

## What it does

**Spec model.** `helios-app.yaml` under pydantic v2 with `extra="forbid"` on every
model, closed enums for ui patterns and field types, `stack.use` xor
`stack.match_existing`, `data.local` xor `data.integration`, kebab-case
`identity.id`. `AppSpecError` carries **every** problem at once, each as
`<yaml path>: <problem>; write <what instead>` — a revision loop should not pay one
round trip per error.

Entity and field names must match `^[a-z_][a-z0-9_]*$`. They are interpolated into
generated DDL, so they are **refused** rather than escaped: an escaped name would
still let a spec author create an arbitrarily-named column, and the escaping would
have to stay correct in every generated statement forever.

**Validator.** Two layers, because the risk is split. `spec.py` is syntax and needs
no filesystem. `validate.py` is semantics: it reads both manifests under their own
builders' validators and cross-checks them against each other and against
`facts.py` and `tools.py`. The load-bearing check is that action-manifest tools are
a subset of audit-declared tools — an undeclared tool is not denied, it is never
looked at, so it runs with no gate at all.

Fact coverage and `side_effect` agreement are read from the AST, not by importing
and calling. That is not a shortcut: a fact source **must** return `None`/`{}` for a
subject it does not know (unknown is None and never zero, or a fail-closed control
becomes fail-open), so calling `extra_facts_for("dummy")` returns no keys and
reports every *correct* app as broken. Modules are still imported — in a subprocess,
with `-B` — to prove they import and to resolve constant names.

**Schema tool.** `data.local` to `CREATE TABLE` plus a standalone `store.py`.
`decimal` becomes `TEXT` with a parse CHECK, never `REAL`. Three properties of
SQLite shaped this and are easy to get wrong:

- Column affinity is applied **before** a CHECK runs, so a bound float `12.5`
  arrives as `'12.5'` and passes every constraint. No pure-SQL rule can catch it, so
  the store refuses floats at the Python boundary.
- An unguarded CHECK **rejects NULL**, because `typeof(col) = 'text'` is false for
  NULL. Optional columns get `CHECK (col IS NULL OR (...))`.
- `GLOB` is not regex — `?` is exactly one character, and it matches digit *shape*
  only, so `'2026-13-45'` satisfies a date pattern without explicit range clauses.

`data.integration` produces no DDL and a store whose accessors raise naming the
source. Data the app does not own is not created by it.

**UI kit.** Jinja2 templates plus HTMX rather than the React/Vite library the spec
originally described — see the CHANGED note in `specs/app-builder.md`. Relative URLs
only (the deployment host strips `/apps/<slug>`), one `tokens.css` with light and
dark from `prefers-color-scheme`, vendored HTMX whose sha256 is recorded in
`stacks.yaml` and asserted by a test. Every template renders with an empty context
and under `StrictUndefined`, and each carries its own empty state.

The held-action card masks credentials using a set that **extends**
`audit_agent.contracts.REDACTED_KEYS`. `auth_code` — a single-use credential bound to
(amount, currency, payee), and exactly what a payment proposal carries — is not in
the platform's set, so inheriting it would print a credential on the one page where
a mistake is most visible.

**Scaffolder.** 23 files: both manifests, `facts.py`, `tools.py`, `store.py`,
`graph.py`, `app.py`, `server.py`, `data/schema.sql`, the declared templates, the
generated tests and the README. Reads the spec only; never imports the agent
builders. Never overwrites — an existing file is reported as
`skipped: <path> (exists)`. Idempotent. Everything is rendered before anything is
written, so an aborted run cannot leave a half-generated app.

Generated policy is a commented TODO block showing the five rule shapes, and
**no rules**. That is deliberate: a generated ceiling passes validation, reads like
a control, and caps the wrong thing, so the owner believes the app is governed when
the number came from a generator. Policy is the product. Write it.

Tool handler stubs raise `NotImplementedError` naming the tool, the arguments it
received and the file to edit. A stub returning a plausible empty result would let a
demo succeed while doing nothing at all.

## Boundaries

- **The scaffolder never imports `audit_agent` or `agent_toolkit`.** Manifests are
  read as plain data; each generated app runs its own copy of the shared runtime.
  `validate.py` imports them lazily, inside the functions that need them, so a spec
  can be shape-checked without the sibling packages installed. `agent-integration`
  remains the only package that imports both.
- **`helios.yaml` is field-conservative.** The engine's `Manifest` accepts `name`,
  `tenant`, `entry`, `port`, `permissions` and does **not** forbid extra keys, so
  anything else is dropped silently — a manifest that looks configured and is not.
  `deployment.py` reads the engine's `contracts.py` at generation time and emits only
  what it declares. `ALLOWED_TOOLS` is a closed set of four and an unknown permission
  is a hard `ContractError`, so unmappable tool families are dropped with a `# REVIEW`
  note.
- `platform/deployment/` is not in the repo yet; the engine is only in the read-only
  `OTHER-TEAMMATE-STUFF/` copy. When it lands, regenerate and the field set widens
  automatically.

## Status

Implemented and tested: the spec model, both validation layers, `stacks.yaml`, the
schema tool, the UI kit, the scaffolder, the intake, the CLI. 386 tests in this
package; `scripts/test-all.sh` runs 6 suites, all green.

Still open: `ui.align_with` token substitution; graph **subscribe** (no transport is
settled); scaffolding a `stack.match_existing` app (refused rather than guessed,
because an integration stack has no type map to generate DDL from); serving a
generated app end to end, which needs `platform/deployment/`.

See the status table at the end of `specs/app-builder.md`, and
`docs/sessions/p3-builder/HANDOFF-TO-P1-P2.md` for what other sessions need to change
for some of these to close.

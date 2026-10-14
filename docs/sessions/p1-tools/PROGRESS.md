# Session p1-tools — Progress log

Three lines per entry. Newest last.

## 0. Foundation

**Did:** Created the worktree `.qwen/worktrees/p1-tools` on a new branch
`worktree-p1-tools` off `chloe-dev`; wrote `scripts/bootstrap-venv.sh` and
`scripts/test-all.sh`; created `.venv` (Python 3.13.9); imported the four teammate
packages as siblings of `fin_tools` (`email_tool` from `email-tool/sender`,
`payments_collect` from `payments-tool/collect`, `helios_forecast` from the
forecasting fork, `spreadsheets` via `git archive origin/payments`); made
`spreadsheets.create_app` a lazy `__getattr__` so FastAPI is no longer needed to
import the tools; rewrote the copied teammate tests' import lines to the new
package names; gave `platform/financial-tools/pyproject.toml` explicit package
discovery, pinned dependencies and the three teammate test paths.

**Learned:** The brief's P0 had not run anywhere — no worktree, no `scripts/`, no
venv, packages unimported, no `docs/research/`, no `specs/forecasting-research-claims.md`,
no `platform/deployment/`, and `helios_forecast/market.py` does not exist (the
adapter is at `services/financial-api/financial_api/market.py`). `p0-foundation`
is running concurrently with uncommitted edits to the same two `pyproject.toml`
files in its own worktree — no shared file today, but a guaranteed merge conflict.
My first import-rewrite regex was too broad and renamed a local variable
(`sender.send(...)` → `email_tool.send(...)`), causing 19 failures; fixed by
restricting the substitution to lines starting with `from`/`import`.
`platform/action-agent-builder` has no `tests/` at all, so pytest exit 5 must read
as SKIP.

**State:** `scripts/test-all.sh` green. Baseline 607 passed (financial-tools 364,
audit-agent-builder 79, agent-integration 123, invoice-payer 41),
action-agent-builder SKIP, `seam check: OK`, 12 tools registered.

## 1. One contract

**Did:** Added `ToolSpec.input_json_schema` (wins over the flat map),
`tenant_scoped`, `records_contact`, and `effective_input_schema()`; `declare()`
emits all three. Added `ToolSpec.from_definition()` and `from_teammate()`, both
accepting `side_effect` singular, both wrapping a `handler(Mapping)` into
`handler(**kwargs)`, both reporting every contract problem in one pass. Added
`flat_from_json_schema()` as the lossy summary so existing `input_schema`
consumers keep working. The bridge now passes a declared JSON Schema through
untouched and converts only when there is nothing but a flat map. Replaced the
module-level singleton binding: `payments.register(reg, payments)`,
`email_sender.register(reg, sender)` and `simulation.register(reg, profiles)`
take their own instance, and `build_world()` passes it. `bind()` and
`set_profiles()` stay as deprecated shims. `Bundle` gained `tenant`, `data_dir`
and `profiles`; added `require_tenant()` in `contracts.py` (not `__init__.py`,
which would have been a cycle for the Task 3 module).

**Learned:** `set_profiles()` was never called anywhere, so the third singleton
was already dead weight — the refactor was safe. Only one direct `ToolSpec(...)`
construction exists (inside the registry decorator), so the new fields could be
keyword-defaulted without breaking positional callers. `test_registry.py` asserts
a `<=` subset on `declare()` keys, so adding keys is compatible. `helios_forecast`
exposes `list_tools()`, so its five real contracts adapt directly rather than
through fixtures.

**State:** green — 393 passed in financial-tools (364 baseline + 29 new contract
tests), audit-agent-builder 79, agent-integration 123, invoice-payer 41;
636 across the four suites. `seam check: OK`; still 12 tools (no new families
yet). Two worlds in one process verified to keep two ledgers, two outboxes and
two payer-profile dicts.

## 2. Email family

**Did:** Added `fin_tools/email_delivery.py` — the seam to the imported
`email_tool`. Free-form `email.send` delivers through `SmtpTransport` directly;
`email.send_template` goes through `EmailSender` (rendering, schema validation,
idempotency). Both default to `world.outbox` and go live only when
`EMAILTOOL_<TENANT>_SMTP_HOST` is set for the bundle's tenant, recording the
provider message id on the outbox entry. Added `email.send_template` (gated,
nested JSON Schema built from the teammate's own template registry) and
`email.templates` (read-only catalog of the five templates + required/optional
fields). Both send tools carry `records_contact=True` and `tenant_scoped=True`.
Idempotency store owned by the `EmailSender` instance so an offline replay is
detectable across calls; a replay does not append a second outbox entry.
`build_world()` now passes `tenant`/`data_dir` into the sender.

**Learned:** `EmailSender.send()` validates against a JSON Schema that requires
`template_id` (5-value enum) and `invoice_ref` and allows no unknown properties,
so free-form sends CANNOT route through it — that forced the two-path split.
The teammate's response carries no `replayed` flag, so the delivery layer
computes the enforced idempotency key and checks the store BEFORE sending;
finding out afterwards would already mean a second email. An offline in-memory
store must be reused, not rebuilt per call, or every replay looks like a first
send. Fixed two self-inflicted bugs: `resolved_tenant` used before assignment in
`build_world`, and a Task 1 test fixture that collided with the now-real
`email.send_template` name (renamed to `example.nested_schema`).

**State:** green — financial-tools 410 passed + 1 skipped (the opt-in live SMTP
test), 14 tools registered, `seam check: OK`. Verified the nested enum schema
survives end-to-end into `build_validated()` (the auditor's strict validator
accepts it, `additionalProperties: false` intact). Gated tools in the manifest:
email.send, email.send_template, graph.observe/publish_verified,
payments.receive/send, spreadsheet.save.

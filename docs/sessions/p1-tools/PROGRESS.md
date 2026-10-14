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

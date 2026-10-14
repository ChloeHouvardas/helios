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

## 3. Receivables family (`collect.*`)

**Did:** Added `fin_tools/collect.py` — a `Collect` facade over the imported
`payments_collect.PaymentStore`, bound to the bundle's tenant, `Payments` and
`IntentService`. Registered five tools: `collect.add_receivable` (gated),
`collect.list_receivables` (read), `collect.create_payment_link` (gated, returns
id + `https://pay.helios.test/l/<id>` + token), `collect.status` (read, with
outstanding balance + links + attempts), `collect.refund` (gated). All
`tenant_scoped=True` with real JSON Schemas (enums, nullable fields).
`Collect.apply_provider_event(...)` is the webhook-only entry point — NOT
registered, so an agent cannot confirm its own payment. A captured payment
mirrors into the fin_tools ledger via `payments.receive`; a refund mirrors out
via a new ungated `Payments.record_refund_mirror` (ungated because the auth
already happened in the tool; exposing it as a tool would be an ungated outbound
payment). Added `Payments.record_refund_mirror`. Wired `Collect` into `Bundle`
and `build_world`.

**Learned:** `PaymentStore.record_refund` has NO authorization parameter — it
refunds whatever it's told — so `collect.refund` had to add the intent gate
itself: verify an auth code bound to (amount, currency, customer) before
calling, consume it only after the store accepts. The customer is derived from
the attempt's receivable, so the human authorizes a refund to a specific party.
The store's `record_payment_success` has no callback, but `subscribe()` exists;
I did not need it — the webhook facade drives success directly and guards the
ledger mirror with an `out_trade_no` set so a replayed event OR a second event id
reporting one capture posts once. The store's clock is a `datetime` while
fin_tools uses a float clock; they stay separate. One test I introduced
(`test_collections_rules_are_absent`) had used a bare `"customer_id" not in
manifest` substring check as a proxy for "no collections mandate leaked"; now
that `collect.add_receivable` legitimately declares `customer_id`, I re-expressed
the real intent (never a blanket requirement on a tool that lacks the field)
rather than weakening it.

**State:** green — financial-tools 437 passed + 1 skipped (410 + 27 new), audit
79, agent-integration 123, invoice-payer 41; 680 across the four suites. 19 tools
registered. `seam check: OK`. Verified `build_validated()` accepts the manifest
with the collect tools; the three gated collect tools fall under the
`fin.large_payment_approval` ceiling (they declare `amount_cents`) and get
per-tool required-argument rules. NOTE for HANDOFF: that means creating a
receivable ≥ ¥10,000 now needs owner approval — a consequence of the existing
manifest derivation (ceiling scopes to any `amount_cents` tool), flagged for the
audit owner rather than changed here. Also flagged: the cashier URL uses the
enumerable link id per the p1-tools spec, not the unguessable token; a production
cashier URL should use the token. And `simulation.sandbox_run` still has a branch
for a tool named `email.send_template` that now collides with the real registered
tool (different signature); no test exercises it, left for a follow-up.

## 4. Forecasting family

**Did:** Added `fin_tools/forecast.py` — the cents↔float boundary over the
imported `helios_forecast`. Six read-only tools: `forecast.project`,
`forecast.simulate`, `forecast.scenario`, `forecast.query` (one tool, `query`
arg selects balance_at/min_balance/first_shortfall/expected_total/
probability_below), `behavior.profile`, `behavior.promise_rate`. Money is
integer cents at the tool edge, converted to float only at the engine call via
`to_engine_units`/`to_cents` (ROUND_HALF_EVEN back). JSON Schemas lifted from
the teammate's registry shape with every money field retyped to integer cents.
Documented the D12 `common_shock` symmetry caveat on `forecast.simulate`'s
description. Re-boded the `simulation.forecast` tool to translate the persona
invoice book into engine flows and delegate to `forecast.project`/`simulate`,
keeping its signature and `{summary, curve}` shape; module-level
`simulate`/`what_if`/`sandbox_run` untouched (their own tests + the shadow
sandbox depend on them). Added per-day `inflows_cents`/`outflows_cents` to
`forecast.project` for the cash-in curve.

**Learned:** Conversion MUST be field-specific, never a blind numeric walk — a
flow mixes money (`amount`, `amount_distribution[i][0]`) with non-money that must
not be scaled (`offset_days` day counts, `probability`, `resize` factors). Three
tests pin that a 7-day offset stays 7 days, a 0.5 probability halves not x100s,
and a 0.5 resize factor halves. The engine tolerates an empty `opening` (accounts
derive from flows) and `percentile_levels` is generic, so the shim requests
p10/p50/p90. The persona→flow translation preserves expected value exactly
(Gaussian delay discretised into offset weights, dispute folded as weighted
buckets, partial-payer as a near flow + a `probability`-gated deferred
remainder); the two-instalment case is EV-exact but only approximates the
correlated original in the tails — noted since the shim is compatibility-only.
Two of my own test assertions were wrong, not the code: `250_00` is 25000 in
Python (underscore separator), and percentile bands are seed-invariant over a
discrete timing distribution at large n_paths (they converge to
weight-determined thresholds) — replaced the brittle "different seed → different
percentile" with a proof that shortfall probability is strictly between 0 and 1.

**State:** green — financial-tools 470 passed + 1 skipped (437 + 33 new), 25
tools registered. `seam check: OK`. Verified all six forecast/behavior tools
enter `build_validated()` as read-only (none gated), and `simulation.forecast`
still registered and read-only. Full four-suite run green.

## 5. Market data (`market.*`)

**Did:** Added `fin_tools/market_data/` (`__init__.py`, `client.py`, `cache.py`,
`rates.py`, `registry.py`, `tools.py`), stdlib only. Four read-only tools, tags
`("market",)`: `market.rate` (series ∈ lpr_1y/lpr_5y/shibor_1m/shibor_3m; publishes
a VERIFIED graph node `market_rate:<series>@<date>`; gated because it writes shared
graph state), `market.borrowing_cost_compare` (four option kinds; returns each
option's formula+inputs so the auditor recomputes; always carries the "not
financial advice" disclaimer), `market.company_lookup` (name/USCC/ts_code →
registry evidence; a miss is an empty list, never an invented match),
`market.collection_period` (national average arturn_days + industry peer median
via index_member_all; the prior for a new customer, stated as such in `basis`).
One `MarketDataSource` protocol, four sources: `TushareSource` (HTTPS only, token
from `TUSHARE_TOKEN`, redacted from every error, 40/min token bucket, retries with
backoff), `CoreFileSource`, `FixtureSource`, `CachedSource` (SQLite TTL: 24 h rates,
7 d registry). Mode via `HELIOS_MARKET_MODE` (fixture|live|auto). Every result
carries `provenance{publisher, api_name, as_of, fetched_at, mode, stale, source_url}`;
a cache/fixture/core hit reports its own mode, never `live`. Nothing found →
`MarketDataUnavailable` naming the series and `TUSHARE_TOKEN`. Wrote labelled sample
fixtures under `fixtures/market/<api>/<sha1>.json`. Wired into `build_world`.

**Learned:** The brief's corrected Tushare facts (`shibor_lpr` fields `date,1y,5y`;
`shibor` full tenor set) are implemented directly in my `client.py`/`rates.py` — I
never imported the buggy teammate adapter (`services/financial-api/financial_api/
market.py`, which uses `api_name="lpr"`, `TRADE_DATE/LPR1Y/ON`, plain http). That
file is NOT in my worktree and NOT in my owned scope, so I did not edit it; the
corrected implementation supersedes it. `specs/forecasting-research-claims.md` is
absent, so T5/T6 cannot be marked there literally — recorded in the reference doc
I own instead. Bug caught: I initially passed `as_of` into the fetch `params`,
which changed the fixture/cache key and missed snapshots; fixed by fetching the
series with no date param and filtering `as_of` locally (`_pick_row`). Another:
`FixtureSource` default root used `parents[1]` (fin_tools/) not `parents[2]`
(package root). `company_lookup` only attempts the direct `stock_company` lookup
when the query looks like a ts_code or an 18-char USCC, so a plain name does not
burn a live call. Verified arithmetic: 2/10 net 30 → 37.24%/yr; credit_line
2.9%+150bps → 2411 cents over 20 days on ¥10,000; national collection period 20.0d.

**State:** green — financial-tools 511 passed + 1 skipped (470 + 41 new), 29 tools.
`seam check: OK`. `build_validated()` accepts the manifest with all four market
tools; only `market.rate` is gated. NOTE for HANDOFF: the `fin.large_payment_approval`
ceiling scopes to ANY tool declaring `amount_cents`, so the read-only
`market.borrowing_cost_compare` (and `collect.add_receivable`/`create_payment_link`)
are pulled into owner-approval. Fail-safe direction, not a hole; the manifest
derivation is shared infra I don't own, so flagged not rescaled.

# Financial tools: the real catalog

The tools an app's agents may call. Generated with the commands at the bottom of
this file — re-run them rather than trusting this snapshot, because the tool set
is still growing.

**38 tools**, in nine families. The set grew from 12 to 38 in session `p1-tools`,
which wrapped four teammate packages (`email_tool`, `payments_collect`,
`helios_forecast`, `spreadsheets`) into this registry and added a market-data
family.

**Mostly offline, and deliberately so.** Without any environment variables set,
`fin_tools` touches no network and no database: its `World` is an in-memory
dataclass, and the market family serves labelled sample fixtures. That lets a
generated app be built, tested and demoed without credentials. Three things opt
into real infrastructure *only* when you set the matching env var:

| Env var | What goes live |
| --- | --- |
| `EMAILTOOL_<TENANT>_SMTP_*` | real SMTP delivery through `email_tool` |
| `TUSHARE_TOKEN` | live LPR/SHIBOR and company-registry lookups |
| `HELIOS_DATA_DIR` | SQLite persistence for the world snapshot, plus the email idempotency store |

Unset, each of those falls back to a local no-op — never to a network call, and
never to an invented value.

Money is **integer cents everywhere**. Never a float. The imported
`helios_forecast` engine uses floats internally; the `forecast.*` wrapper converts
at that boundary only and rounds half-even back to cents.

## Entry point

```python
from fin_tools import build_world

bundle = build_world(tenant="acme")            # tenant optional but recommended
bundle.registry.catalog()                      # what an agent sees
bundle.registry.call("payments.balance", account="acme")   # dispatch by name
bundle.world                                   # the in-memory state
bundle.intents                                 # Intent Verification service
bundle.collect                                 # the receivables facade
bundle.tenant                                  # resolved tenant (or None)
```

`build_world(registry=None, secret=..., clock=None, tenant=None, data_dir=None)`
returns a `Bundle(world, registry, intents, payments, email, graph, collect)` and
registers all 38 tools onto a fresh `ToolRegistry`.

`tenant` resolves from the argument, else `HELIOS_TENANT`, else `None`.
`data_dir` resolves from the argument, else `HELIOS_DATA_DIR`, else `None`.

**Two worlds in one process now work correctly.** `build_world()` used to bind
module-level singletons (`payments.bind`, `email_sender.bind`,
`simulation.set_profiles`), so the *last* call in a process won dispatch for every
registry — a real trap for tests and for a multi-tenant app. It now registers
closures bound to its own `Bundle`. `bind()` remains as a deprecated shim.

## Catalog

`side_effects=True` means **every call must be gated** — it becomes a proposal
the audit agent adjudicates. `False` means it runs directly.

### payments

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `payments.balance` | no | `account: str` | `{balance_cents: int}` |
| `payments.send` | **yes** | `payer, payee: str`; `amount_cents: int`; `currency: str`; `auth_code: str\|None`; `idempotency_key: str\|None`; `memo: str` | `{payment_id, state, reject_reason}` |
| `payments.receive` | **yes** | `payer, payee: str`; `amount_cents: int`; `currency: str`; `reference: str` | `{payment_id, state}` |

`payments.send` **fails without a valid `auth_code`**, returning
`state="rejected"`, `reject_reason="missing verified intent auth code"`. This is
the one tool where a control is enforced by the implementation as well as by
policy — worth knowing, because it means an app can get this wrong twice and
still not lose money.

`state` values observed: `settled`, `rejected`.

### collect — receivables over `payments_collect.PaymentStore`

| Tool | Side effect | Tenant-scoped | Arguments | Returns |
|---|---|---|---|---|
| `collect.add_receivable` | **yes** | yes | `customer_id: str`; `amount_cents: int`; `currency: str`; `invoice_no: str\|None`; `due_date: str\|None`; `meta: dict\|None` | `{receivable_id, status, ...}` |
| `collect.list_receivables` | no | yes | `status: str\|None` | `{receivables: list}` |
| `collect.create_payment_link` | **yes** | yes | `receivable_id: str`; `amount_cents: int\|None`; `expires_at: str\|None`; `one_time: bool`; `idempotency_key: str\|None` | `{link_id, url, amount_cents, ...}` |
| `collect.status` | no | yes | `receivable_id: str` | `{receivable: dict\|None}` |
| `collect.refund` | **yes** | yes | `attempt_id: str`; `amount_cents: int`; `reason: str`; `auth_code: str\|None` | `{attempt_id, state, refunded_amount_cents, ...}` |

`collect.refund` requires an intent `auth_code` exactly like `payments.send` does —
the teammate store's `record_refund` has **no** authorization parameter, so the
gate is added here. Without a valid code it is refused, not executed.

`create_payment_link` returns a **simulated** URL, `https://pay.helios.test/l/<id>`.
There are no real payment rails anywhere in this system (decision B.4). Pass an
`idempotency_key` to make link creation repeatable; the store caches the first
result per `(tenant, key)` and never caches a failure, so a retry after an error
re-runs.

**Tenancy is never defaulted.** The teammate models default `business_id="dev"`.
That default is not used here: every call passes the bundle's tenant explicitly,
and a tool that needs one raises rather than silently mixing two SMEs' books.

#### Webhooks are not tools

`record_payment_success` and provider events are **not** registered as tools — an
agent must not be able to declare that a customer paid. They are reachable only
through the facade, for an app's webhook handler:

```python
bundle.collect.apply_provider_event(provider="alipay", event_id="evt_1",
                                    payload={...})          # append-only
bundle.collect.record_payment_success(attempt_id, provider_order_ref=...)
```

A successful capture **also posts to the `fin_tools` ledger** via
`payments.receive`, so one ledger holds the truth instead of two disagreeing.
The mirror is guarded against double-posting: a replayed webhook (identical
payload, same `event_id`) and a duplicate capture both leave the ledger unchanged.
Note that the append-only event log rejects a *different* payload for an
already-seen `event_id` (`event_conflict`) — that is the store protecting its
audit trail, not a bug.

`apply_provider_event` is deliberately not in the registry, and stays out of
`NOT_AGENT_CALLABLE`'s way because it is never registered at all.

### email

| Tool | Side effect | Tenant-scoped | Arguments | Returns |
|---|---|---|---|---|
| `email.send` | **yes** | yes | `to, subject, body: str`; `template: str\|None` | `{message_id, to, subject, delivered, transport}` |
| `email.send_template` | **yes** | yes | `to: str`; `template: str`; `fields: dict`; `idempotency_key: str\|None` | `{message_id, template, to, delivered, replayed, ...}` |
| `email.templates` | no | — | *(none)* | `{templates: [{name, required_fields, optional_fields}]}` |
| `email.outbox` | no | — | *(none)* | `{messages: list}` |

Side-effecting because it is visible to a human outside the system — the
threshold that matters for gating is *external visibility*, not persistence.

Two send paths, deliberately different, because the imported `email_tool` forces
the split:

- `email.send` is free-form. It writes to `world.outbox`. When
  `EMAILTOOL_<TENANT>_SMTP_HOST` is set for the bundle's tenant it *also* delivers
  through the teammate's `SmtpTransport` and records the provider message id in
  `transport`. It cannot route through `EmailSender.send()`, because that method's
  schema requires a `template_id` from its five-template enum and rejects a
  free-form request outright.
- `email.send_template` routes through `EmailSender` and the five templates:
  `invoice_delivery`, `reminder_pre_due`, `notice_overdue`, `negotiation_offer`,
  `escalate_to_owner`. Call `email.templates` first to get each template's
  required fields — every one of them needs several, and `escalate_to_owner`
  needs ten.

Idempotency uses the teammate's JSONL store under `HELIOS_DATA_DIR`. A repeated
`idempotency_key` replays the original response and does not send a second email;
the result carries `replayed: true` so a UI can say "already sent" rather than
implying a fresh send. Replays do not even need a working transport.

Both send tools carry the `records_contact` marker, so an app can tell which
tools write to the customer-contact history.

### forecast — over `helios_forecast`

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `forecast.project` | no | `opening: list[dict]`; `flows: list[dict]`; `start: str`; `days: int` | `{days, final_balance_cents, min_balance_cents, min_balance_date, first_shortfall, expected_inflows_cents, expected_outflows_cents, total_cents, inflows_cents, outflows_cents, inputs_hash}` |
| `forecast.simulate` | no | the above + `n_paths: int`; `seed: int`; `threshold_cents: int`; `common_shock: dict\|None` | `{days, n_paths, seed, percentiles, min_balance_percentiles, probability_below, first_shortfall_probability, first_shortfall_median_date, inputs_hash}` |
| `forecast.scenario` | no | the project args + `delta: dict` | `{baseline_final_balance_cents, scenario_final_balance_cents, final_balance_change_cents, ..., scenario_resolves_baseline_shortfall}` |
| `forecast.query` | no | the project args + `query: str`; `on: str\|None`; `window: list\|None`; `threshold_cents: int\|None`; `direction: str\|None` | `{query, value}` (shape depends on `query`) |

`forecast.query` folds five read queries into one tool behind a `query` argument:
`balance_at`, `min_balance`, `first_shortfall`, `probability_below`,
`expected_total`. The first three run on the deterministic projection; the last
two need a Monte Carlo run, which the tool performs internally.

**Cents in, cents out.** The engine is float-based; the wrapper divides by 100 on
the way in and rounds half-even on the way out. Conversion is field-by-field and
explicit — never a blind walk over numbers — because a flow mixes money (`amount`,
`amount_distribution[i][0]`) with non-money (`offset_days` day counts,
`probability`, resize factors). Getting that wrong would silently turn a
probability into a payment.

`common_shock` carries a caveat worth repeating to any user who sees it, from the
teammates' own decision record (D12): it draws **one** timing offset per simulated
path and shifts **all** flows in that path by the same amount — inflows *and*
outflows together. It models correlated timing, not a one-sided shock. It can
therefore *lower* measured shortfall risk as easily as raise it. It is not a
stress test.

`inputs_hash` is on every result, so a forecast can be tied to the exact inputs
that produced it — which is what lets a user ask "why did this change?" and get an
answer.

### behavior — over `helios_forecast.behavior`

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `behavior.profile` | no | `records: list[dict]`; `customer_id: str`; `segment: str`; `prior_strength: float` | `{offset_days, n, level}` |
| `behavior.promise_rate` | no | `records: list[dict]`; `customer_id: str`; `segment: str`; `prior_strength: float` | `{promise_kept, n, kept, segment, level}` |

Empirical-Bayes payment timing and Beta-Binomial promise rates. `records` are
`{customer_id, segment, due, paid}` for the profile and
`{customer_id, segment, kept}` for the promise rate.

Read `level` before trusting the number: it reports where the estimate actually
came from — `customer`, `segment`, `global`, or `default`. **`default` means there
was no evidence at all** and the value is the built-in prior
(`[[0, 0.5], [30, 0.5]]`), not a measurement of this customer. A UI that shows a
`default` profile as if it were learned is misleading its user.

`behavior.profile`'s `offset_days` output is directly consumable as a flow's
`date.offset_days` input, so a learned profile can drive `forecast.simulate`
without reshaping.

### market — public reference rates and registry evidence

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `market.rate` | **yes** | `series: str`; `as_of: str\|None` | `{series, as_of, value_pct, value_bps, provenance, node_id}` |
| `market.borrowing_cost_compare` | no | `amount_cents: int`; `days: int`; `options: list[dict]` | `{options, cheapest, rate_used, disclaimer}` |
| `market.company_lookup` | no | `query: str` | `{matches: list, provenance}` |
| `market.collection_period` | no | `industry: str\|None`; `ts_code: str\|None` | `{national_avg_days, industry_median_days, basis, provenance}` |
| `market.daily_quotes` | no | `ts_code: str`; `start_date, end_date, trade_date: str\|None` | `{ts_code, count, quotes[], units, provenance}` |
| `market.trading_calendar` | no | `start_date, end_date: str`; `exchange: str\|None` | `{exchange, count, open_days[], days[], provenance}` |

`series` ∈ `lpr_1y`, `lpr_5y`, `shibor_1m`, `shibor_3m`.

`market.rate` is the one side-effecting tool in this family, and the reason is
worth stating: it **publishes a verified graph node**
`market_rate:<series>@<date>` carrying `source` and `stale`. That writes shared
state other apps read, so it is gated — not because reading a rate is dangerous.

This is deliberately *not* a general market-data catalog. There is no FX, no
CPI/PPI/PMI, no yield curves, no credit-score tiers. Six tools: four tied to a
decision an owner already faces, plus daily quotes and the trading calendar for
apps that value a holding or need "the last session before this date":

1. **"Can I make payroll on the 25th?"** → `forecast.query(first_shortfall)` for
   the gap, then `market.borrowing_cost_compare` to price the real options: draw
   the credit line, take a supplier's early-payment discount, delay a bill, or
   chase a customer for free.
2. **"funds are short for this bill"** → the same comparison, `delay_supplier` vs
   `credit_line`. **"supplier announced a new bank account"** →
   `market.company_lookup` adds registry evidence to the hold: does the supplier on
   file match the sender of the change?
3. **"new customer, no payment history"** → `market.collection_period` gives the
   national average collection period as the prior, and says so in `basis`.

`market.borrowing_cost_compare` accepts four option kinds —
`credit_line {base: lpr_1y|shibor_3m, spread_bps}`,
`early_payment_discount {discount_pct, discount_days, net_days}`,
`delay_supplier {late_fee_pct|None}`,
`chase_customer {customer_id, expected_days}` — and returns each option's
`formula` and `inputs` alongside its `annualised_pct` and `cost_cents`, plus a
`cheapest`. **The arithmetic is returned so the auditor can recompute it** rather
than take the number on faith. Discount cost is
`(d/(100−d)) × (365/(net−disc))` (2/10 net 30 ≈ 37%/yr); loan cost is
`amount × (base+spread) × days/365`. The disclaimer — "Indicative cost from public
reference rates; not financial advice" — is always present, on every call.

#### Provenance is not optional

Every market result carries:

```python
{"publisher": ..., "api_name": ..., "as_of": ..., "fetched_at": ...,
 "mode": "live"|"cache"|"core"|"fixture", "stale": bool, "source_url": ...}
```

**Surface `mode` and `stale` in the UI.** A fallback is never presented as live.
If the demo is running on fixtures, the page should say so — showing sample data
as today's LPR is exactly the failure this field exists to prevent.

Resolution is by `HELIOS_MARKET_MODE` = `fixture | live | auto`. Default `auto`:
live when `TUSHARE_TOKEN` is set, else the organizers' core files under
`data/core/`, else fixtures; and on any live error it falls back to the newest
cache/core/fixture rather than failing the call.

When nothing is found anywhere, the tool raises `MarketDataUnavailable` with a
message naming the series and the env var to set. These tools are read-only, so
that is an honest **"unknown"** — never an invented number. An app should render
it as unavailable, not as zero.

The Tushare token comes only from `TUSHARE_TOKEN`, is never logged, and is
redacted from every error string. Transport is HTTPS only, with a 40 calls/min
token bucket and retries with backoff.

Two endpoints the teammate adapter got wrong are implemented correctly here:
`shibor_lpr` with fields `date, 1y, 5y`, and `shibor` with
`date, on, 1w, 2w, 1m, 3m, 6m, 9m, 1y`. The stale adapter
(`api_name="lpr"`, fields `TRADE_DATE/LPR1Y/ON`, plain `http://`) lives at
`services/financial-api/financial_api/market.py`, is **not** imported by this
package, and should be deleted or redirected by whoever owns that tree. This file
is the corrected record of those claims (T5/T6); `specs/forecasting-research-claims.md`
does not exist in this repo.

`market.company_lookup` accepts a name, an 18-character Unified Social Credit
Code, or a `ts_code`. A miss returns an empty `matches` list — never a fuzzy
substitute. It only attempts the direct company lookup when the query looks like a
`ts_code` or a USCC, so a plain company name does not burn a live API call.

### spreadsheet

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `spreadsheet.load` | no | `path: str` | `{rows: list[dict], count: int}` |
| `spreadsheet.aggregate` | no | `rows: list[dict]`; `group_by: str`; `sum_column: str` | `{totals: dict[str,int]}` |
| `spreadsheet.save` | **yes** | `path: str`; `rows: list[dict]`; `columns: list[str]\|None` | `{path: str}` |

`spreadsheet.save` is side-effecting because it touches the filesystem. A
generated app that lets an agent write files should gate it and constrain `path`.

These three are standalone stdlib CSV helpers. They are **not** the imported
`spreadsheets` package — that is the `sheets.*` family below.

### sheets — statement reconciliation over the imported `spreadsheets` package

The "which payment settles which invoice?" question a bookkeeper does by hand:
import a bank or wallet statement, match its transactions against known
obligations, and render the result.

| Tool | Side effect | Tenant-scoped | Arguments | Returns |
|---|---|---|---|---|
| `sheets.records` | no | yes | `record_type: str`; `limit: int` | `{record_type, count, items: list, cursor}` |
| `sheets.list_proposals` | no | yes | `view_id: str\|None`; `status: str\|None` | `{count, items: list}` |
| `sheets.upload` | **yes** | yes | `filename, content: str`; `encoding: str` | `{file_id, filename, size, sha}` |
| `sheets.import_sheet` | **yes** | yes | `file_id, record_type: str`; `column_map: dict\|None`; `dry_run: bool`; `archive_password: str\|None`; `idempotency_key: str\|None` | `{imported, mapping, record_type, rejected_rows}` (+`preview` when `dry_run`) |
| `sheets.reconcile` | **yes** | yes | `max_step: int`; `amount_drift: int`; `date_window_days: int`; `dry_run: bool`; `scope: dict\|None`; `idempotency_key: str\|None` | `{applied, suggested, unmatched_transactions, unmatched_obligations, dry_run}` |
| `sheets.resolve_match` | **yes** | yes | `match_id: str`; `decision: str`; `actor: str` | `{match_id, decision, method, rejected}` |
| `sheets.render_sheet` | **yes** | yes | `query: dict`; `columns: list`; `target: str`; `editable_columns: list\|None`; `protected_columns: list\|None`; `group_by`, `sort`, `view_id`, `idempotency_key` | `{view_id, target, file_id, rows, columns}` |
| `sheets.write_cell` | **yes** | yes | `view_id, record_id, field: str`; `new_value`; `actor: str`; `actor_kind: str`; `reason: str` | `{applied, proposal_id?, record_id?, status?}` |
| `sheets.resolve_proposal` | **yes** | yes | `proposal_id: str`; `decision: str`; `resolved_by: str`; `resolved_by_kind: str` | proposal payload incl. `status` |

**Money is integer fen, with no conversion.** Unlike `forecast.*` there is no
float boundary: the package already stores minor units, so `1000.00` in a CSV
becomes `100000` and passes through unchanged. `record_type` is one of
`Obligation`, `Transaction`, `Counterparty`.

**Model-free by default.** The package can call a model in three places. This
facade injects `NullQwen` unconditionally, so no `sheets.*` tool reaches a model
or reads `QWEN_API_KEY`. Column mapping is resolved deterministically; when the
headers cannot be mapped the tool fails with `mapping_ambiguous` rather than
guessing. Pass an explicit `column_map` (`{"金额": "amount", ...}`) to resolve it.
`dry_run=True` shows the inferred mapping and a preview before writing anything.

**`target` defaults to `"csv"`, not the package's own `"feishu"`**, because a live
target needs credentials the offline path does not have. `"xlsx"` also works
without credentials. Note that `rows` in the result is a **count**, not the row
data — the rows go to the view or exported file, addressed by `view_id` and
`file_id`.

**`query` filters records**, e.g. `{"type": "Obligation", "status": ["open",
"overdue"]}`. `record_type` is accepted as an alias for `type` so this family
speaks one vocabulary. Any other key must be a real record field; an unknown key
raises rather than silently rendering an empty sheet, because zero rows otherwise
reads as "this tenant has no obligations" instead of "your query was misspelled".
Omit `type` to search every record type.

**Protected columns are the human checkpoint.** A rendered view is read-only
unless `editable_columns` names a column. A column listed in `protected_columns`
(money, dates and payee details should be) cannot be written directly: the edit is
queued as a pending proposal and `write_cell` returns `applied: false`. That is
the whole propose→approve loop, and all three steps are tools:

```python
view = call("sheets.render_sheet", query={"type": "Obligation"}, columns=["amount"],
            editable_columns=["amount"], protected_columns=["amount"])
w = call("sheets.write_cell", view_id=view["view_id"], record_id=rid,
         field="amount", new_value=99900, actor="agent", actor_kind="agent")
# w == {"applied": False, "proposal_id": "prop_...", "status": "pending"}
p = call("sheets.list_proposals")["items"][0]     # needs_human is True
call("sheets.resolve_proposal", proposal_id=p["proposal_id"], decision="approve",
     resolved_by="owner", resolved_by_kind="user")
# only now is the value written
```

**`resolved_by_kind` defaults to `"agent"`.** That is deliberate and it matters:
the engine refuses to let an agent approve a proposal that `needs_human`, so a
caller must assert human authority explicitly. Defaulting the other way would let
an agent approve its own edit to an invoice amount.

Decisions accept either tense — `confirm`/`confirmed`, `approve`/`approved`,
`reject`/`rejected` — because rejecting a correct decision on grammar alone looks
like a real refusal.

`reconcile`'s ladder is deterministic and ordered (exact reference, then
counterparty/alias, then amount and date proximity, then partial, then
combinations). `applied` matches settle obligations immediately, which is why the
tool is gated; `suggested` matches do not, and are settled by
`sheets.resolve_match`. With `NullQwen` the model-assisted step never fires — a
deliberate loss of recall in exchange for identical behaviour with and without
credentials.

> Two upstream bugs were fixed while wiring this family, both of which produced
> silently wrong financial answers rather than errors: a scoped `reconcile` used
> to drop every transaction, and an approved proposal used to record "approved"
> without writing the value. See the commit messages and
> `docs/sessions/p1-tools/PROGRESS.md` §7.

Uploaded files and imported records live in the package's in-memory store, so
they do **not** survive a restart even with `SqlitePersistence` on — only the
`fin_tools` `World` is snapshotted. Recorded in `HANDOFF-TO-P2-P3.md`.

### knowledge graph

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `graph.read` | no | `node_id: str` | `{node: dict\|None}` |
| `graph.publish_verified` | **yes** | `kind, key: str`; `attrs: dict`; `source: str` | `{node_id}` |
| `graph.observe` | **yes** | `kind, key: str`; `attrs: dict`; `observer: str` | `{node_id}` |

The verified/observation split is the platform's answer to the open question in
`specs/decisions.md` about distinguishing authoritative records from agent
observations: `graph.observe` is **rejected if it would overwrite a verified
fact** (`GraphWriteError`). An agent cannot promote its own guess to a record of
truth.

This is a local prototype, not the shared service. `specs/interfaces.md` still
lists the cross-app graph read/write interface as to be defined.

### simulation

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `simulation.forecast` | no | `invoices: list[dict]`; `policy: dict\|None`; `n_runs: int`; `seed: int` | `{summary: dict, curve: dict}` |

**Re-boded in session `p1-tools`.** Signature and return shape are unchanged, but
the body now delegates to `forecast.simulate` over the `helios_forecast` engine
instead of running its own persona-based Monte Carlo. The persona invoice book is
translated into engine flows with expected value preserved exactly, so a given
invoice book produces the same expected cash-in as before — what changed is which
engine does the arithmetic. One consequence: the payment-timing distribution is
discretised into day buckets, and buckets whose weight rounds to zero are dropped
(the engine rejects a zero-weight bucket).

Also importable directly, not only via the registry:

```python
from fin_tools import simulate, what_if, sandbox_run, PayerProfile, SimPolicy
```

These module-level functions are unchanged and still back their own tests.

`sandbox_run(world, intents, tool, **kwargs)` executes a tool against a **shadow
world** and reports the diff (`ShadowReport(tool, executed, result, effects)`).
This is how an app previews a side effect for the owner without performing it —
useful for the `approval_queue` UI, because the owner can see what would happen.

`what_if(invoices, profiles, baseline=SimPolicy(...), variant=SimPolicy(...))`
compares two policies. This is the capability behind Helios Cash Planner's
plain-language what-ifs.

> Naming trap: `sandbox_run` has a branch for a tool literally named
> `email.send_template`, which is now *also* a real registered tool with a
> different signature and a different template system. Nothing is broken — no
> test exercises that branch and the class method it calls still exists — but the
> two should not share a name.

### Intent Verification — not in the registry

Reached through `bundle.intents`, not `registry.call`. This matters: capturing and
confirming an intent is an **owner-facing act**, not a tool an agent may call. If
it were a tool, the agent could mint its own payment authorization and the whole
control would be decorative.

```python
intent = bundle.intents.capture_intent(
    principal="owner", amount_cents=125000, currency="USD",
    payee="sup-northwind", purpose="invoice INV-1001")
# intent.intent_id, intent.state == IntentState.CAPTURED

code = bundle.intents.confirm(intent.intent_id, by="owner")   # -> AuthCode
# code.code, code.expires_at (~300s)

bundle.intents.verify(code.code, amount_cents=125000,
                      currency="USD", payee="sup-northwind")   # -> Intent
bundle.intents.consume(code.code)      # single-use
bundle.intents.revoke(intent.intent_id)
```

The code lives **300 seconds** and `IntentState` runs
`CAPTURED → CONFIRMED → CONSUMED | EXPIRED | REVOKED`.

`bundle.intents.summarize(intent)` returns a plain-language line —
`"owner authorizes 1250.00 USD to sup-northwind: invoice INV-1001"` — which is
what an approval queue should show the owner rather than a raw code.

The code is an **HMAC bound to (amount, currency, payee)**. Changing any of the
three invalidates it, so an authorization for $1,250 to Northwind cannot be
replayed as $48,000 to Delta. `VerificationError` is raised on a bad, expired, or
already-consumed code. `python3 -m fin_tools.demo` exercises all four rejections.

Two tools enforce this in their implementation, not just in policy:
`payments.send` and `collect.refund`. Both refuse without a valid code.

## The HTTP service

```python
from fin_tools import ToolServer, HttpToolClient, free_port

srv = ToolServer(bundle, port=free_port(), execution_secret="...").start()
client = HttpToolClient(srv.base_url, execution_secret="...")
client.catalog()                                  # GET /tools
client.call("payments.balance", account="acme")   # POST /call
srv.stop()
```

Routes: `GET /health` (open), `GET /tools`, `POST /call {"tool","args"}` →
`{"ok","tool","result"}` or `{"ok":false,"rejected":true,"error"}`, and
`GET /kg/nodes`. Run it with `python -m fin_tools --host --port` (default 8080).

**Bearer auth.** When `HELIOS_EXECUTION_SECRET` is set (or `execution_secret` is
passed), every route except `GET /health` requires
`Authorization: Bearer <secret>`, compared with `hmac.compare_digest`. A missing
or wrong token is a 401. When no secret is configured the service keeps its
historical open behaviour, so existing local callers are unaffected.

`HttpToolClient` distinguishes the two failure modes: 401/403 raises
`PermissionError`, and a network failure raises `ServiceUnavailable` — it **fails
closed** rather than pretending the call succeeded.

**The bearer token is not the signing secret.** It authenticates the HTTP caller;
it does not authorize any particular action. An authenticated caller that guesses
an `auth_code` is still refused by `payments.send`. Authenticating the caller does
not authorize the action. This is tested in
`tests/test_service_auth_persistence.py`.

**Persistence.** `SqlitePersistence(data_dir=...)` saves and restores the `World`
snapshot — ledger, balances, outbox, graph — to `world_state.sqlite3`. With no
data dir it is a disabled no-op. `python -m fin_tools` wires it up from
`HELIOS_DATA_DIR` and saves on shutdown, so a container restart does not forget
payments or verified facts. Note that if you deploy this, `HELIOS_DATA_DIR` must
be a real volume; `/tmp` loses state on restart.

## Adding a tool

An app may add its own tools (like the example's `lookup_invoice`, which reads
that app's database). Two requirements:

1. Register it with a `side_effect` flag that is **honest**. This flag is what
   routes a call to the gate. Marking a money-moving tool read-only is how an app
   acquires an ungated side effect.
2. Declare it in the **audit manifest** too, with an `input_schema` if it has
   side effects. The audit agent is the registry of what may be granted at all;
   an action agent offering a tool the auditor never declared must be rejected.

```python
from fin_tools.contracts import ToolSpec

spec = ToolSpec(
    name="myapp.lookup_contract",
    description="Look up one contract. Read-only.",
    input_schema={"contract_id": "str"},
    output_schema={"contract": "dict|None"},
    side_effects=False,
    handler=lambda contract_id: {"contract": ...},
    tags=("myapp",),
    # optional, all added in session p1-tools:
    input_json_schema=None,   # full JSON Schema; wins over the flat map
    tenant_scoped=False,      # True => the tool operates on one tenant's data
    records_contact=False,    # True => writes customer-contact history
)
bundle.registry.register(spec)
```

### The contract mismatch is now bridged

`fin_tools.ToolSpec` uses `side_effects` (plural), a flat `{name: "type"}` schema
and `handler(**kwargs)`. `agent_toolkit.ToolDefinition` uses `side_effect`
(singular), a JSON Schema object and `handler(args: Mapping)`. The imported
teammate packages use *both* spellings.

They are still not interchangeable by hand, but they now convert:

```python
from fin_tools.contracts import ToolSpec

ToolSpec.from_definition(tool_definition)   # agent_toolkit -> ToolSpec
ToolSpec.from_teammate({"name": ..., "side_effect": True, ...})
                                             # teammate dict -> ToolSpec
```

Both accept either `side_effect` or `side_effects`. And `input_json_schema` lets a
tool declare a **full JSON Schema** — including nested objects, which the flat
`{name: "type"}` map cannot express. `toolkit_bridge` passes a JSON Schema
through untouched and still converts flat maps, so `email.send_template`'s nested
`fields: dict` and `forecast.*`'s nested `flows` survive the round trip to the
agent runtime.

See `platform-status.md` §4 for the remaining drift (tenancy naming, cents vs
floats) and `docs/sessions/p1-tools/HANDOFF-TO-P2-P3.md` for open cross-boundary
requests.

## Regenerate this table

```bash
cd platform/financial-tools
python3 -c "
from fin_tools import build_world
b = build_world(tenant='acme')
print('%-34s %-12s %-8s %s' % ('TOOL','SIDE_EFFECT','TENANT','TAGS'))
for t in sorted(b.registry.list(), key=lambda s: s.name):
    print('%-34s %-12s %-8s %s' % (t.name, t.side_effects, t.tenant_scoped, list(t.tags)))
print('total:', len(b.registry.list()))
"
python3 -m fin_tools.demo     # end-to-end: simulate -> intent -> shadow -> pay -> email -> graph
```

`python3 -m fin_tools.demo` is the fastest way to see every family used
correctly together. Read it before writing an app that moves money.

To exercise the market family offline without a token:

```bash
HELIOS_MARKET_MODE=fixture python3 -c "
from fin_tools import build_world
b = build_world(tenant='acme')
print(b.registry.call('market.rate', series='lpr_1y'))
"
```

# Financial tools: the real catalog

The tools an app's agents may call. Generated with the commands at the bottom of
this file — re-run them rather than trusting this snapshot, because the tool set
is still growing.

**Money movement here is an offline prototype.** `fin_tools` uses no database and
no real money; its `World` is an in-memory dataclass. That is deliberate: it lets
a generated app be built, tested and demoed without credentials, and any
implementation following the tool contract can be swapped in without changing the
agent runtime.

**One exception:** the `market.*` tools may call the Tushare HTTP API for live
quote and reference-rate data, and only when `TUSHARE_TOKEN` is set. No other
module opens a socket, and that data is read-only — it never reaches a balance or
the ledger. Without a token those tools raise `ServiceUnavailable` rather than
returning invented numbers.

## Entry point

```python
from fin_tools import build_world

bundle = build_world()
bundle.registry.catalog()                                   # what an agent sees
bundle.registry.call("payments.balance", account="acme")    # dispatch by name
bundle.world                                                # the in-memory state
bundle.intents                                              # Intent Verification service
```

`build_world()` returns a `Bundle(world, registry, intents, payments, email,
graph)` and registers all 16 tools onto a fresh `ToolRegistry`.

It also accepts two optional keyword arguments for the market tools:
`market_client=` (a preconfigured `TushareClient`) and `post_json=` (shorthand for
injecting only the transport). Both default to `None`. Injecting either is how the
market tools are tested offline — pass `post_json=<callable>` and no socket is
opened.

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

Money is **integer cents everywhere**. Never a float. A rounding error in an
invoice total is a real financial error.

`state` values observed: `settled`, `rejected`.

### email

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `email.send` | **yes** | `to, subject, body: str`; `template: str\|None` | `{message_id, to, subject}` |
| `email.outbox` | no | *(none)* | `{messages: list}` |

Side-effecting because it is visible to a human outside the system — the
threshold that matters for gating is *external visibility*, not persistence.

### spreadsheet

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `spreadsheet.load` | no | `path: str` | `{rows: list[dict], count: int}` |
| `spreadsheet.aggregate` | no | `rows: list[dict]`; `group_by: str`; `sum_column: str` | `{totals: dict[str,int]}` |
| `spreadsheet.save` | **yes** | `path: str`; `rows: list[dict]`; `columns: list[str]\|None` | `{path: str}` |

`spreadsheet.save` is side-effecting because it touches the filesystem. A
generated app that lets an agent write files should gate it and constrain `path`.

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

Monte-Carlo cash-in forecast over an invoice book using `PayerProfile` payer
behaviour. Read-only and seeded, so it is reproducible — important for an app
that shows a forecast to a user who will ask why it changed.

Also importable directly, not only via the registry:

```python
from fin_tools import simulate, what_if, sandbox_run, PayerProfile, SimPolicy
```

`sandbox_run(world, intents, tool, **kwargs)` executes a tool against a **shadow
world** and reports the diff (`ShadowReport(tool, executed, result, effects)`).
This is how an app previews a side effect for the owner without performing it —
useful for the `approval_queue` UI, because the owner can see what would happen.

`what_if(invoices, profiles, baseline=SimPolicy(...), variant=SimPolicy(...))`
compares two policies. This is the capability behind Helios Cash Planner's
plain-language what-ifs.

### market — live quote data

| Tool | Side effect | Arguments | Returns |
|---|---|---|---|
| `market.reference_rate` | no | `series: str`; `as_of: str\|None` | `{series, value_pct, unit, as_of, requested_as_of, provenance}` |
| `market.daily_quotes` | no | `ts_code: str`; `start_date, end_date, trade_date: str\|None` | `{ts_code, count, quotes, units, provenance}` |
| `market.trading_calendar` | no | `start_date, end_date: str`; `exchange: str\|None` | `{exchange, count, open_days, days, provenance}` |
| `market.company_basic` | no | `query: str\|None`; `ts_code: str\|None`; `list_status: str\|None` | `{count, matches, provenance}` |

All four are read-only and require `TUSHARE_TOKEN`. `series` ∈ `lpr_1y`,
`lpr_5y`, `shibor_on`, `shibor_1w`, `shibor_1m`, `shibor_3m`, `shibor_6m`,
`shibor_1y`.

Four things an app author needs to know:

- **This is quote data, not money.** Values are floats denominated in their own
  unit, and every result says which: `CNY per share`, `手 / lots of 100 shares`,
  `千元 / thousands of CNY`, `percent per annum`. No field is named `*_cents`,
  because that name would imply integer money and invite adding a share price to a
  balance. If your app needs to *use* a price, convert explicitly at your own
  boundary and own the rounding — see `specs/decisions.md`.
- **`as_of` may not be the date you asked for.** These series publish on their own
  schedule, and asking for an unpublished date returns zero rows. A rate resolves
  to the newest publication **on or before** your `as_of`, and
  `provenance.as_of` tells you the date it was actually published — so "today's
  LPR" may correctly come back as one published six weeks ago. Read that field
  before presenting a rate as current.
- **Every result carries `provenance`** — publisher, `api_name`, `as_of`,
  `retrieved_at`, `source_url`, `mode`. Surface it. A rate with no provenance is
  not a rate you can defend.
- **Unavailable is not a refusal.** A missing token, a timeout, or an upstream
  error returns `{"ok": false, "unavailable": true, "retryable": true}` — *not*
  `rejected`. Treat it as an outage worth retrying or reporting, not as a gate
  denying your action. `{"rejected": true}` still means a validator or gate looked
  at this specific request and said no.

Endpoints are allow-listed to `trade_cal`, `daily`, `shibor_lpr`, `shibor` and
`stock_basic`. **No tool accepts an `api_name` argument** — the endpoint is
hard-coded per tool, so an agent cannot reach an endpoint nobody reviewed. A
company-lookup miss returns an empty `matches` list rather than a fuzzy
substitute, because for a counterparty check an invented match is worse than no
answer.

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
)
bundle.registry.register(spec)
```

Note the **contract mismatch** between packages: `fin_tools.ToolSpec` uses
`side_effects` (plural) and a flat `{name: "type"}` schema with
`handler(**kwargs)`, while `agent_toolkit.ToolDefinition` uses `side_effect`
(singular), a JSON Schema object, and `handler(args: Mapping)`. They are not
interchangeable — **but you do not write the adapter, it already exists.**

- `fin_tools.toolkit_bridge.as_tool_definitions(bundle)` (or
  `agent_tool_definitions(bundle)`, which excludes the intent-issuing operations an
  agent must never hold) returns the financial tools as real
  `agent_toolkit.ToolDefinition`s, converting the flat schema map to JSON Schema
  and the kwargs handler to a Mapping-taking one.
- `agent_integration.adapters.ToolContractView` closes the gap in the other
  direction, so the auditor registers each tool with the correct side-effect flag
  and schema rather than silently treating a gated tool as read-only.

`fin_tools.audit_manifest` likewise derives the audit manifest's tool declarations
and per-tool required-argument rules from the real schemas, so the manifest cannot
drift from the contract. See `platform-status.md` §4.

### The sibling packages

`platform/financial-tools/` holds four more packages beside `fin_tools`, all
installed by the same pyproject and all offline-testable:

- `helios_forecast` — Monte-Carlo cash-flow projection, scenario deltas and
  conformal calibration. `helios_forecast.market` is the stdlib market-data
  reader (P1 wires it to the real service).
- `spreadsheets` — render a query to a sheet; live engines are Feishu (default),
  WPS and Grist, but they are served in-process by a local engine, so tests need no
  credentials. `spreadsheets/app.py` is a FastAPI service.
- `email_tool` — a templated SMTP sender with JSON-Schema-validated requests and an
  idempotency store. Credentials come from the environment; it degrades offline.
- `payments_collect` — a pydantic receivables model and in-memory `PaymentStore`,
  integer minor units, no provider yet.

## Regenerate this table

```bash
# From the repo root, with the repo venv (scripts/bootstrap-venv.sh).
.venv/bin/python -c "
from fin_tools import build_world
b = build_world()
print('%-24s %-12s %s' % ('TOOL','SIDE_EFFECT','TAGS'))
for t in b.registry.list():
    print('%-24s %-12s %s' % (t.name, t.side_effects, list(t.tags)))
print('total:', len(b.registry.list()))
"
.venv/bin/python -m fin_tools.demo     # end-to-end: simulate -> intent -> shadow -> pay -> email -> graph
```

`python3 -m fin_tools.demo` is the fastest way to see every family used
correctly together. Read it before writing an app that moves money.

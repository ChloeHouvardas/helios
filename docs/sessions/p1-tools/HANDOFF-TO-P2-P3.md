# HANDOFF — from `p1-tools` to P2 (deployment) and P3 (app-builder)

Status of this session: Tasks 1–5 complete and committed, plus bearer auth and
persistence on the tool service. `scripts/test-all.sh` exit 0, all suites green,
`seam check: OK`. **29 tools** registered. Baseline at session start was 12 tools.

I own `platform/financial-tools/` and `entry-point/references/financial-tools.md`.
I did not edit `platform/deployment/` or `platform/app-builder/`. The requests
below need a decision from whoever owns those, or from the group.

---

## 1. BLOCKING DECISION for P2: the core container's execution routes bypass the audit gate

This is the one item that genuinely cannot be resolved from my side, and it is a
control conflict, not a cosmetic one.

The deployment core container (`platform/deployment/images/core/server.py`, which I
read from the untracked teammate copy at
`OTHER-TEAMMATE-STUFF/deployment-stuff/fPT9INmvWJ/default/helios/images/core/server.py`)
exposes `POST /tools/{payments|email|knowledge-graph}` behind
`Bearer HELIOS_EXECUTION_SECRET`, and its auditor sidecar calls those routes with:

- `payments`: `{to, amount, invoice, note}` — **`amount` is a float**
- `email`: `{to, subject, body}`
- `knowledge-graph`: `{entity, props}`

Three conflicts with decisions already made (§B):

| Core container | Helios decision it contradicts |
| --- | --- |
| No auth code; bearer token is the sole gate | **B.2 / B.7**: `payments.send` must require an owner-confirmed intent auth code. A bearer token authenticates the *caller*; it does not authorize *this payment*. |
| `amount` as float | **B.6**: money is integer cents or decimal strings, never float. Float arithmetic on money is a correctness bug, not a style choice. |
| No tenant field | **B.7 / Task 3**: tenancy is explicit; a missing tenant is an error. My `collect.*` family refuses a defaulted tenant. |

**What I did:** I did not silently adopt those routes, and I did not weaken the
intent gate to make a mock pass (§C.7 forbids exactly that). Instead:

- My service now accepts the bearer token as an *authentication* layer
  (`HELIOS_EXECUTION_SECRET`), which is compatible with the core container's
  expectation and is a strict improvement over the previous no-auth service.
- `payments.send` and `collect.refund` still require a real `auth_code` from the
  intent service. Authenticated caller ≠ authorized action. This is tested
  explicitly in `tests/test_service_auth_persistence.py`:
  `test_a_valid_bearer_token_cannot_forge_an_auth_code`.
- I exposed `GET /health` and `GET /kg/nodes` so two of the core container's
  routes are already satisfiable by my service without change.

**What I need from P2 — pick one:**

- **(a) Preferred:** the sidecar adopts the tool-service contract — call
  `POST /call` with `{"tool": "payments.send", "args": {...}}`, integer
  `amount_cents`, an explicit tenant, and an `auth_code` it obtained from
  `intents.capture_intent` + `intents.confirm`. The sidecar holds the execution
  secret and the owner confirms; the gate stays intact. I can add a thin
  compatibility shim on my side once the shape is agreed.
- **(b)** If the sidecar must keep its current three routes, then those routes
  need to be **read-only or pre-authorized only** — i.e. they may query balance,
  forecast, and market data, but they may not move money. A separate, gated path
  carries the actual payment with an auth code.

I can implement (a) or the read-only subset of (b) quickly; what I will not do is
accept a float-amount, no-auth-code, no-tenant money-moving route as-is.

Also note for P2: replacing the core image with my service requires
`HELIOS_DATA_DIR` to be a real volume if state must survive restarts. My
`SqlitePersistence` writes `world_state.sqlite3` there and is a no-op when the
variable is unset. The core mock used `/tmp`, which loses state on restart.

---

## 2. For P2/P3: `amount_cents` pulls read-only tools into owner-approval

`build_financial_manifest` derives the `fin.large_payment_approval` ceiling from
**any** tool declaring an `amount_cents` argument. Three of my new tools declare
one without moving money:

- `market.borrowing_cost_compare` — read-only cost arithmetic
- `collect.add_receivable` — records an invoice, moves nothing
- `collect.create_payment_link` — issues a link, moves nothing

So a ¥10,000 receivable or a borrowing-cost comparison now needs owner approval.
**This is fail-safe, not a hole** — nothing unsafe slips through — but it is
friction in the pitch demo: the owner gets asked to approve a calculation.

The derivation lives in `fin_tools/audit_manifest.py`, which is shared infra
feeding the auditor. I flagged rather than rescaled it, because narrowing a
control is P2's call, not mine. Suggested fix if you want it: scope the ceiling
rule to tools that are `side_effects=True` **and** in a payments/collect family,
or add an explicit `moves_money: bool` marker on `ToolSpec` so the manifest keys
off intent rather than argument name. Say the word and I'll implement it.

---

## 3. For P3 (app-builder): what is now available to you

Read-only, safe to call from a page without a gate:

- `forecast.project`, `forecast.simulate`, `forecast.scenario`, `forecast.query`
  (one tool, `query` ∈ `balance_at|min_balance|first_shortfall|probability_below|expected_total`),
  `behavior.profile`, `behavior.promise_rate` — all integer cents in and out.
- `market.rate` (series ∈ `lpr_1y|lpr_5y|shibor_1m|shibor_3m`),
  `market.borrowing_cost_compare`, `market.company_lookup`,
  `market.collection_period`.
- `email.templates`, `collect.list_receivables`, `collect.status`.

The three moments the market family exists for (§E) map to tools as follows, so
P4 can build the pages without re-deriving this:

1. **"Can I make payroll on the 25th?"** → `forecast.query(first_shortfall)` for
   the gap, then `market.borrowing_cost_compare` with all four option kinds. The
   tool returns each option's `formula` and `inputs`, and a `cheapest`, so the
   page can show the arithmetic rather than just a number.
2. **"funds are short for this bill" / "supplier announced a new bank account"**
   → `market.borrowing_cost_compare` (`delay_supplier` vs `credit_line`) for the
   first; `market.company_lookup` for the second — it returns registry evidence
   (`com_id`, `chairman`, `province`, `website`, `email`) so the page can show
   whether the supplier on file matches the sender of the change. A miss returns
   an empty `matches` list, never an invented match.
3. **"new customer, no payment history"** → `market.collection_period` returns
   the national average `arturn_days` plus an industry peer median, and a `basis`
   field stating that this is a prior, not a learned rate. `behavior.profile`
   reports `level: "default"` when there is no evidence for that customer.

Two things worth knowing when you render any of this:

- **Every market result carries `provenance{publisher, api_name, as_of,
  fetched_at, mode, stale, source_url}`.** `mode` is one of
  `live|cache|core|fixture`. Please surface `mode` and `stale` in the UI. A
  fallback is never presented as live, and the pitch should not show fixture
  numbers as if they were today's rates — that is exactly what `mode` is for.
- **`market.borrowing_cost_compare` always returns a `disclaimer`** ("Indicative
  cost from public reference rates; not financial advice"). Keep it visible.

For offline demoing: set `HELIOS_MARKET_MODE=fixture` and the four market tools
serve the labelled sample fixtures committed under
`platform/financial-tools/fixtures/market/`. No `TUSHARE_TOKEN`, no network.
In `auto` mode (default) it uses live when the token is set, then core files,
then fixtures, and falls back on any live error.

---

## 4. Corrections the brief asked for that I could not land where it said

- **The buggy Tushare adapter.** The brief says to fix
  `helios_forecast/market.py` (copied from
  `services/financial-api/financial_api/market.py`). Confirmed facts: it calls
  `api_name="lpr"` with fields `TRADE_DATE/LPR1Y/LPR5Y`, SHIBOR field `ON`, over
  plain `http://`. All wrong; the real endpoints are `shibor_lpr` (`date, 1y, 5y`)
  and `shibor` (`date, on, 1w, 2w, 1m, 3m, 6m, 9m, 1y`), over HTTPS.
  **That file is not in my worktree and not in my owned scope** — `helios_forecast/`
  as imported has no `market.py`, and the source lives under read-only
  `OTHER-TEAMMATE-STUFF/`. So I implemented the corrected endpoints from scratch
  in `fin_tools/market_data/client.py`, which is authoritative and tested. The
  stale adapter still exists at
  `OTHER-TEAMMATE-STUFF/forecasting-simulation/services/financial-api/financial_api/market.py`
  and should be deleted or redirected to my module by whoever owns that tree.
- **`specs/forecasting-research-claims.md` does not exist** in this repo, so
  claims T5/T6 cannot be marked corrected there. I recorded the correction in
  `entry-point/references/financial-tools.md` (which I do own) instead.
- **`docs/research/` does not exist**, so §B.1/§B.2/§B.5/§E of
  `research-data.md` could not be read. I built the market family strictly to the
  verified endpoint list in the brief and to nothing more — no FX, no CPI/PPI/PMI,
  no yield curves, no credit-score tiers, no calendars.

---

## 5. Contract drift: reconciled where I could, still open where I could not

Reconciled in `fin_tools/contracts.py` (Task 1):

- `side_effects` (fin_tools) vs `side_effect` (agent_toolkit, email_tool,
  helios_forecast): `ToolSpec.from_definition` and `ToolSpec.from_teammate` both
  accept either spelling.
- Flat schemas vs JSON Schema: `input_json_schema` now wins over the flat map when
  present, and `toolkit_bridge` passes a full JSON Schema through untouched while
  still converting flat maps. Nested objects (which the flat map cannot express)
  now round-trip.
- The module-level singleton trap: `build_world()` used to call
  `payments.bind`/`email_sender.bind`/`simulation.set_profiles`, so the **last**
  `build_world()` in a process won for every registry. It now registers closures
  bound to its own `Bundle`. `bind()` remains as a deprecated shim. Two worlds in
  one process now have two independent ledgers — tested.

Still open, needs a group decision:

- **Tenancy has no single name.** `tenant_id` (email_tool), `business_id`
  (payments_collect), `HELIOS_TENANT` (mine). I standardised on `HELIOS_TENANT`
  → `Bundle.tenant` → passed explicitly as `business_id`/`tenant_id` at each
  boundary, and `require_tenant()` fails closed when it is missing. But the three
  packages still disagree underneath, so any new package needs the same
  translation. Worth settling on one name.
- **I removed reliance on the silent `business_id="dev"` default** rather than
  the default itself (it is in the teammate's pydantic models, which I don't own).
  My `Collect` facade always passes an explicit tenant, and `require_tenant`
  raises rather than defaulting. If anyone constructs those models directly, the
  `"dev"` default is still live and will silently mix tenants.

---

## 6. Environment / harness notes (P0 was never run)

The brief assumed P0 had already created the worktree, `scripts/`, the venv, and
imported the teammate packages. **None of that existed.** I built it, inside my
own worktree only:

- Created `.qwen/worktrees/p1-tools` on branch `worktree-p1-tools` from
  `chloe-dev`. Never switched branches, never touched `main`.
- `scripts/bootstrap-venv.sh` and `scripts/test-all.sh` did not exist; I wrote
  both. `test-all.sh` treats pytest exit 5 (no tests collected) as a skip, because
  `platform/action-agent-builder` declares `testpaths = ["tests"]` but has no
  `tests/` directory. **That is a real gap in that package — it has zero tests.**
  Not mine to fix; flagging it.
- The venv is `.venv` (Python 3.13.9) created with `--system-site-packages`,
  because the machine interpreter already has numpy 2.4.5, pydantic 2.12.5,
  jinja2 3.1.6, jsonschema 4.26.0, PyYAML 6.0.3, openpyxl 3.1.5, pytest 8.4.2.
  System `python3` is Anaconda 3.13.9, not 3.14 as the brief said.
- **`fastapi` is not installed anywhere.** Only `spreadsheets/app.py` needs it, so
  I made that import lazy (PEP 562 `__getattr__` in `spreadsheets/__init__.py`).
  The tools import fine without it. If P2 wants the ASGI app, `fastapi` must be
  added and pinned — I deliberately did not add a dependency for a module I don't
  use.
- I imported four teammate packages as siblings of `fin_tools`: `email_tool`,
  `payments_collect`, `helios_forecast`, `spreadsheets`. **211 of their tests came
  with them and pass** (38 payments_collect, 105 email_tool, 68 helios_forecast,
  plus spreadsheets). `platform/financial-tools/pyproject.toml` now uses explicit
  package discovery, because seven top-level packages broke flat-layout detection.
- **A concurrent agent is working in this repo.** `.qwen/worktrees/p0-foundation`
  and `.qwen/worktrees/p3-builder` both exist with uncommitted changes, including
  their own `bootstrap-venv.sh`. I stayed entirely inside my worktree and did not
  touch theirs, but **there will be a merge collision on `scripts/` and on
  `platform/financial-tools/pyproject.toml`.** Whoever merges should expect to
  reconcile both.
- One naming trap for anyone extending this: `simulation.sandbox_run` has a branch
  for a tool literally named `email.send_template`, which is now also a real
  registered tool with a different signature and template system. No test
  exercises the sandbox branch and I preserved the class method, so nothing is
  broken today, but the two should not share a name. Renaming the sandbox branch
  is a one-line fix I left alone to avoid scope creep.

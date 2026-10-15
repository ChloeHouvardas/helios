# worktree.md — `chloe-dev`, main checkout

**Path:** `<repo>`
**Branch:** `chloe-dev` (the main checkout, not a `.qwen/worktrees/` worktree)
**Base:** `cc96f0a` = `origin/chloe-dev` · **5 local commits, 0 pushed** (0 behind / 5 ahead)

This document describes **this branch's work and functionality only**. Merging and
the management of other worktrees are out of scope here and belong to the merge
agent. Where a fact below could matter to a merge (the shape of a file, a public
signature, an invariant a test defends), it is stated as a property of *this*
branch — nothing here characterizes or prescribes work done elsewhere.

---

## 1. Goal of this session

Give `fin_tools` **live Tushare market data** — real quote and reference-rate data
— while money movement stays mock and untouched.

In scope: four read-only market tools, an injectable transport so they test
offline, one service-layer bug fix that market data surfaced, and the spec/doc
updates those changes required.

Out of scope, and not touched: payments, ledger, balances, intent verification,
the audit gate, and anything inside `OTHER-TEAMMATE-STUFF/` (read for convention
only).

---

## 2. Commits

Five commits, in dependency order. `b98ba36` must precede `7ff4b74`, because
`MarketDataError` subclasses the `UpstreamError` that `b98ba36` introduces.

| Commit | Type | Summary |
|---|---|---|
| `b98ba36` | `fix:` | stop reporting an outage as an audit-gate rejection |
| `7ff4b74` | `feat:` | live Tushare market data behind an allow-list |
| `dcbdae5` | `docs:` | record market-data decisions, retire stale guarantees |
| `8434107` | `docs:` | flag teammate `financial-api` as promote-and-share candidate |
| `26f5609` | `docs:` | add this file |

Every commit leaves the tree importable and the suite green.

### Files touched (12)

```
platform/financial-tools/fin_tools/market_data.py       NEW  (587 lines)
platform/financial-tools/tests/test_market_data.py      NEW  (43 tests)
platform/financial-tools/fin_tools/service.py           MOD  (UpstreamError, _wrap)
platform/financial-tools/fin_tools/__init__.py          MOD  (build_world wiring)
platform/financial-tools/PLAN.md                        MOD
platform/financial-tools/README.md                      MOD
entry-point/references/financial-tools.md               MOD
specs/decisions.md                                      MOD
specs/financial-tools-service.md                        MOD
specs/interfaces.md                                     MOD
specs/tech-stack.md                                     MOD
worktree.md                                             NEW  (this file)
```

`pyproject.toml` was **not** modified: `dependencies = []` still holds — stdlib
`urllib` only, no `tushare` SDK, no pandas.

---

## 3. What was built

### 3.1 `fin_tools/market_data.py` — a single module, four read-only tools

`market_data` is a **single module file** (`fin_tools/market_data.py`), not a
package directory. Registered by `register(registry, client=None)`, following the
existing `spreadsheet_tools.py` pattern.

| Tool | Args | Returns |
|---|---|---|
| `market.reference_rate` | `series: str`, `as_of: str\|None` | `{series, value_pct, unit, as_of, requested_as_of, provenance}` |
| `market.daily_quotes` | `ts_code: str`, `start_date/end_date/trade_date: str\|None` | `{ts_code, count, quotes[], units, provenance}` |
| `market.trading_calendar` | `start_date: str`, `end_date: str`, `exchange: str\|None` | `{exchange, count, open_days[], days[], provenance}` |
| `market.company_basic` | `query: str\|None`, `ts_code: str\|None`, `list_status: str\|None` | `{count, matches[], provenance}` |

All four: `side_effects=False`, tags `("market",)`. Tool count went **12 → 16**.

`series` ∈ `lpr_1y`, `lpr_5y`, `shibor_on`, `shibor_1w`, `shibor_1m`,
`shibor_3m`, `shibor_6m`, `shibor_1y`.

### 3.2 Public API added

```python
from fin_tools import TushareClient, MarketDataError, UpstreamError, build_world
from fin_tools.market_data import ALLOWED_APIS, RATE_SERIES, TOKEN_ENV

TushareClient(endpoint=..., post_json=..., timeout=...)    # frozen-ish dataclass
TushareClient.fetch(api_name, params=None, fields="") -> list[dict]
build_world(..., market_client=None, post_json=None)       # two NEW kwargs
```

`build_world`'s two new keyword parameters are the test seam and mirror its
existing `clock=`: pass `post_json=<callable>` and no socket is opened. Both
default to `None`, so every pre-existing call site is unaffected.

Internally the module aliases itself as `_market_mod` inside `__init__.py` and is
registered as `_market_mod.register(reg, market_client)` — a two-argument call
(registry, client).

### 3.3 `service.py` — three distinguishable call outcomes

Added `UpstreamError` as a shared base class. `ServiceUnavailable` now subclasses
it, and `MarketDataError` subclasses it too. `_wrap()` returns three outcomes
instead of two:

| Envelope | Meaning | Agent should |
|---|---|---|
| `{"ok": true, "result": ...}` | the tool ran | use the result |
| `{"ok": false, "rejected": true, "error": ...}` | a gate/validator refused *this request* | change the request |
| `{"ok": false, "unavailable": true, "retryable": true, "error": ...}` | nobody decided anything; unreachable | retry, or report the outage |

Previously every exception mapped to `rejected: true`, so a network timeout was
written into the audit log as a policy denial of an action no gate ever
adjudicated — a false record that also sends an agent "fixing" a call that was
never wrong. `VerificationError`, `KeyError` and `ValueError` still report as
`rejected`, so nothing that was correctly a denial changed meaning.

---

## 4. Invariants this branch's tests defend

Seven properties, all asserted in `tests/test_market_data.py` unless noted. Two
would fail **silently** rather than as a red test if broken — they are marked ⚠️.

1. **Closed allow-list.** `ALLOWED_APIS` is exactly `{trade_cal, daily,
   shibor_lpr, shibor, stock_basic}`. **No tool accepts an `api_name` argument** —
   a test asserts that across the whole registry, so an agent cannot reach an
   endpoint nobody reviewed.

2. **The token is never observable.** Read from `TUSHARE_TOKEN` at call time only;
   sent only in the POST body; never stored on an instance; never present in
   `ToolSpec.declare()` output; redacted from every error string. **There is
   deliberately no `set_token()`** and a test asserts its absence — a token in a
   process global is a token in a traceback.

3. **Quote data is not money.** `market_data.py` never imports `World` or
   `Payments`; every tool is `side_effects=False`; **no output field is named
   `*_cents`.** Amounts carry real units instead (`CNY per share`,
   `手 / lots of 100 shares`, `千元 / thousands of CNY`). A test greps the module
   source for forbidden imports and greps every result blob for `_cents`.

4. **⚠️ Unavailable ≠ rejected.** If `UpstreamError` is lost from the MRO of
   `ServiceUnavailable` or `MarketDataError`, or `_wrap` reverts to two outcomes,
   outages go back to being logged as rejections — while everything still imports
   and passes. Covered here and in `tests/test_service_boundary.py`.

5. **⚠️ Date resolution.** Two behaviours the live API forced:
   - A point query for an unpublished date returns **zero rows** (`shibor_lpr` for
     20261015 came back empty while the newest publication was 20260820). So rates
     resolve by looking back `RATE_LOOKBACK_DAYS = 400` and taking the newest
     publication **on or before** `as_of`; `provenance.as_of` reports the date
     actually published, so a caller can see the number is six weeks old rather
     than assume it is current. Swapping this for a `date=<as_of>` point query
     returns nothing.
   - **Upstream ordering is not a contract.** The live range came back
     newest-first; `_latest_on_or_before` sorts explicitly. Trusting upstream order
     would silently return the **oldest** rate. A reversed-payload test pins it.

6. **Nulls are skipped, never coerced to zero.** A `None` rate must not become
   `0.0` — that is an invented number. No data at all raises `MarketDataError`.

7. **A company-lookup miss returns an empty list**, never a fuzzy substitute. For a
   counterparty check, an invented match is worse than no answer.

---

## 5. Verification

```bash
cd <repo>/platform/financial-tools

# offline — all 43 market tests use an injected fake transport
python3 -m pytest -p no:cacheprovider --override-ini="addopts=" -q
#   -> 104 passed, 2 skipped

python3 -m fin_tools.demo     # runs offline with no token; prints "16 tools"
```

**Baseline before this branch: 61 passed, 2 skipped. After: 104 passed, 2
skipped** (43 new).

The 2 skips are **pre-existing and unrelated**: `test_gated_loop_integration.py`
and `test_toolkit_bridge.py` `importorskip` on `audit_agent` / `agent_toolkit`,
which are not installed in this checkout. They are not regressions.

Also run: `entry-point/examples/invoice-payer` → **41 passed**. That example
consumes `service.py`, so it is the check that the `_wrap` change is
backward-compatible.

### Live check (needs `TUSHARE_TOKEN`)

The token is in `.env` at the repo root and is gitignored (`.gitignore:12`). The
line has **no `export`**, so a bare `source .env` will not reach child processes —
use `set -a`:

```bash
cd <repo>
set -a && . ./.env && set +a
cd platform/financial-tools
python3 -c "
from fin_tools import build_world
b = build_world()
print(b.registry.call('market.reference_rate', series='lpr_1y'))
print(b.registry.call('market.trading_calendar',
                      start_date='2026-10-09', end_date='2026-10-15')['open_days'])
print(b.world.balances, b.world.ledger)   # must stay {} {}
"
```

Verified this way during the session: all four tools returned real data,
`World.balances` and `World.ledger` were untouched, and the token appeared in no
result or catalog blob.

---

## 6. Decisions recorded

### Ratified (in `specs/decisions.md`, under **Decided**)

- **Market data is quote data and never becomes money.** A price or rate is a
  float with a unit and an `as_of`; a balance or ledger entry is integer cents.
  Both rules hold because they describe different things. The separation is
  structural, not conventional — see invariants #3 above. An app that needs to use
  a price converts explicitly at its own boundary and owns the rounding; the
  platform never silently coerces a quote into money.
- **An unavailable quote is an outage, not a decision.** The rule behind the
  `_wrap` fix, recorded so nobody "simplifies" the distinction away later.

### Deliberately left open

"Which financial operations should be managed APIs versus packaged SDK tools?"
remains an **Open Question**. This branch implemented market data as a *packaged*
adapter and says so in the annotation, but that is an implementation choice for
one capability, not a resolution. The teammate proposal in
`OTHER-TEAMMATE-STUFF/forecasting-simulation/services/financial-api/` ("math is
packaged, data-fed defaults are managed") is **unratified** — its §10 spec updates
never landed, so it must not be cited as precedent. The practical asymmetry is
noted: a packaged adapter means every deployed app holds its own `TUSHARE_TOKEN`
and its own quota.

`8434107` flags that same teammate service as a candidate to promote into
`platform/` and share rather than duplicate. Not acted on — `OTHER-TEAMMATE-STUFF/`
was read, never modified.

### Deviation from the brief

The brief names `http://api.tushare.pro`. This branch defaults to **HTTPS** on the
same host, because the token travels in the POST body and a plaintext request
would put a live credential on the wire. HTTPS was verified working against the
real API. `DEFAULT_ENDPOINT` is overridable via `TushareClient(endpoint=...)` if
plain HTTP is ever required. Every other decided constraint held: stdlib urllib,
no SDK, no pandas, plain dicts, token from env only, no `set_token()`,
`dependencies = []`.

### Endpoint names corrected against the live API

The teammate adapter's names were self-declared unverified and are **wrong**:
`lpr` with `TRADE_DATE`/`LPR1Y`/`ON` returns Tushare error **40101
"请指定正确的接口名"**. The verified endpoints are `shibor_lpr` (fields
`date,1y,5y`) and `shibor` (fields `date,on,1w,2w,1m,3m,6m,9m,1y`). A test
asserts the stale names are absent from `ALLOWED_APIS`.

### Not built

No cache, no rate limiting, no retry/backoff, no offline fixture or core-file
source. A caller with no token gets `ServiceUnavailable`, never invented numbers.
Also not built: FX, macro (CPI/PPI/PMI), yield curves, credit tiers.

---

## 7. Shared-file edit shape

Recorded because four of the files above are shared docs that other work may also
touch. All `specs/` edits on this branch are **pure insertions, zero deletions**
(verified via `git diff --numstat cc96f0a HEAD`), so they sit alongside other
insertions cleanly unless adjacent lines were edited.

| File | Hunks | Insertion anchor |
|---|---|---|
| `specs/decisions.md` | 2 | new `### Market data is quote data…` under **Decided**, just before `## Tentative`; plus an indented sub-list appended to the existing open-question bullet about managed APIs vs packaged SDK tools |
| `specs/interfaces.md` | 1 | new `### Call outcomes` and `### Market data tools` inside §6, before `## Interfaces to Define Later` |
| `specs/tech-stack.md` | 1 | five new bullets appended under **Decided**, after the `Data storage:` line |
| `specs/financial-tools-service.md` | 1 | new `### Market data sits outside this boundary, deliberately` at the end of §6, before the `---` preceding §7 |

Three files are **not** pure insertions, because they replace statements this
branch made false:

| File | +/− | What was replaced |
|---|---|---|
| `platform/financial-tools/PLAN.md` | +32/−2 | the "no network calls, no secrets" guarantee and the "no HTTP server" line in *Not doing* |
| `platform/financial-tools/README.md` | +42/−9 | "no network, no DB, no real money"; "44 tests"; "12 tools"; "no HTTP" |
| `entry-point/references/financial-tools.md` | +61/−6 | "touches no network"; "registers all 12 tools"; added a `### market` section in family order |

All three counts and claims were re-verified against the tree after editing:
16 tools, 104 passed + 2 skipped, `build_world` signature matches what the
entry-point doc now documents.

---

## 8. Notes for whoever picks this up

- `worktree.md` itself is committed (`26f5609`) and tracked at the repo root.
- `000demo-flow.md` is untracked at the root and is **not** this branch's work; it
  was left untouched and unstaged.
- `demo.py` computes its tool count dynamically, so it self-corrected to 16 with no
  edit. Three docs hardcoded counts or an offline guarantee that this branch made
  false and were corrected: `platform/financial-tools/README.md` (12 → 16 tools,
  44 → 104 tests, the "no network" and "no HTTP" claims) and
  `entry-point/references/financial-tools.md` (12 → 16 tools, the "touches no
  network" claim, plus a new `### market` section). `PLAN.md` likewise.
- Untracked and not this branch's: `.DS_Store`, `.env.example`, `.qwen/`,
  `OTHER-TEAMMATE-STUFF/`, `000demo-flow.md`. The last was left untouched and
  unstaged.

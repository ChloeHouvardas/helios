# worktree.md — `chloe-dev` @ main checkout

**Scope of this document:** describes the work and functionality of **this** branch
(`chloe-dev`, main checkout at
`<repo>`). A
separate agent owns merging and the other worktrees; this file is written for that
agent and deliberately does not describe or prescribe work done elsewhere.

**Goal of this session:** give `fin_tools` **live Tushare market data** — real
quote and reference-rate data — while leaving money movement mock. Plus one
adjacent service-layer bug fix that market data surfaced, and the spec/doc updates
those changes required.

---

## 1. Commits

Four local commits, in dependency order, on `chloe-dev`. Base `cc96f0a`
(= `origin/chloe-dev`). **0 behind / 4 ahead. Nothing pushed.**

| Commit | Type | Summary |
|---|---|---|
| `b98ba36` | `fix:` | stop reporting an outage as an audit-gate rejection |
| `7ff4b74` | `feat:` | live Tushare market data behind an allow-list |
| `dcbdae5` | `docs:` | record market-data decisions, retire stale guarantees |
| `8434107` | `docs:` | flag teammate `financial-api` as promote-and-share candidate |

Each commit leaves the tree importable and the suite green. `b98ba36` must come
before `7ff4b74`: `MarketDataError` subclasses the `UpstreamError` that `b98ba36`
introduces.

### Files touched (10 total)

```
platform/financial-tools/fin_tools/market_data.py       NEW  (587 lines)
platform/financial-tools/tests/test_market_data.py      NEW  (43 tests)
platform/financial-tools/fin_tools/service.py           MOD  (_wrap, UpstreamError)
platform/financial-tools/fin_tools/__init__.py          MOD  (build_world wiring)
platform/financial-tools/PLAN.md                        MOD
platform/financial-tools/README.md                      MOD
specs/decisions.md                                      MOD
specs/financial-tools-service.md                        MOD
specs/interfaces.md                                     MOD
specs/tech-stack.md                                     MOD
```

`dependencies = []` in `pyproject.toml` was **preserved** — stdlib `urllib` only,
no `tushare` SDK, no pandas.

---

## 2. What was built

### 2.1 `fin_tools/market_data.py` — four read-only tools

| Tool | Args | Returns |
|---|---|---|
| `market.reference_rate` | `series: str`, `as_of: str\|None` | `{series, value_pct, unit, as_of, requested_as_of, provenance}` |
| `market.daily_quotes` | `ts_code: str`, `start_date/end_date/trade_date: str\|None` | `{ts_code, count, quotes[], units, provenance}` |
| `market.trading_calendar` | `start_date: str`, `end_date: str`, `exchange: str\|None` | `{exchange, count, open_days[], days[], provenance}` |
| `market.company_basic` | `query: str\|None`, `ts_code: str\|None`, `list_status: str\|None` | `{count, matches[], provenance}` |

All four are `side_effects=False`, tags `("market",)`. Registered by
`market_data.register(registry, client=None)`, following the
`spreadsheet_tools.py` pattern.

`series` ∈ `lpr_1y`, `lpr_5y`, `shibor_on`, `shibor_1w`, `shibor_1m`,
`shibor_3m`, `shibor_6m`, `shibor_1y`.

### 2.2 Public API added

```python
from fin_tools import TushareClient, MarketDataError, UpstreamError, build_world
from fin_tools.market_data import ALLOWED_APIS, RATE_SERIES, TOKEN_ENV

TushareClient(endpoint=..., post_json=..., timeout=...)   # dataclass
TushareClient.fetch(api_name, params=None, fields="") -> list[dict]
build_world(..., market_client=None, post_json=None)      # two NEW kwargs
```

`build_world`'s two new parameters are the test seam, mirroring its existing
`clock=`: pass `post_json=<callable>` and no socket is opened.

### 2.3 `service.py` — three-way call outcome

Added `UpstreamError` as a shared base; `ServiceUnavailable` now subclasses it and
`MarketDataError` subclasses it too. `_wrap()` returns three distinguishable
outcomes instead of two:

| Envelope | Meaning |
|---|---|
| `{"ok": true, "result": ...}` | tool ran |
| `{"ok": false, "rejected": true, "error": ...}` | a gate/validator refused *this request* |
| `{"ok": false, "unavailable": true, "retryable": true, "error": ...}` | nobody decided anything; unreachable |

Previously every exception mapped to `rejected: true`, so a network timeout was
recorded in the audit log as a policy denial of an action no gate ever
adjudicated — a false record that also sends an agent "fixing" a call that was
never wrong. `VerificationError`, `KeyError` and `ValueError` still report as
`rejected`, so nothing that was correctly a denial changed meaning.

---

## 3. Invariants a merger must not break

These are the properties this branch's tests assert. A merge that drops any of
them will show up as a test failure, but two would fail **silently** instead —
those are called out.

1. **Closed allow-list.** `ALLOWED_APIS` is exactly `{trade_cal, daily,
   shibor_lpr, shibor, stock_basic}`. **No tool accepts an `api_name`
   argument.** A test asserts that across the whole registry; keep it.

2. **Token never observable.** Read from `TUSHARE_TOKEN` at call time only; sent
   only in the POST body; never stored on an instance; never in
   `ToolSpec.declare()` output; redacted from every error string. **There is
   deliberately no `set_token()`** — a test asserts its absence. Do not add one,
   and do not "tidy up" by caching the token on `TushareClient`.

3. **Quote data ≠ money.** `market_data.py` never imports `World` or `Payments`;
   every tool is `side_effects=False`; **no output field is named `*_cents`.**
   Amounts carry real units instead (`CNY per share`, `手 / lots of 100 shares`,
   `千元 / thousands of CNY`). A test greps the module source for forbidden
   imports and greps every result blob for `_cents`.

4. **⚠️ Unavailable ≠ rejected — fails silently if broken.** If a merge restores
   the old two-way `_wrap`, or drops `UpstreamError` from the MRO of
   `ServiceUnavailable`/`MarketDataError`, outages go back to being logged as
   rejections. This is the single most likely thing to be lost in a merge, because
   a naive conflict resolution that takes one side's `class ServiceUnavailable(...)`
   line reintroduces the bug while everything still imports and passes.
   `tests/test_market_data.py` and `tests/test_service_boundary.py` both cover it.

5. **⚠️ Date resolution — fails silently if broken.** Two behaviours the live API
   forced:
   - A point query for an unpublished date returns **zero rows** (`shibor_lpr` for
     20261015 was empty; newest publication 20260820). So rates are resolved by
     looking back `RATE_LOOKBACK_DAYS=400` and taking the newest publication
     **on or before** `as_of`, and `provenance.as_of` reports the date actually
     published. Replacing this with a `date=<as_of>` point query returns nothing.
   - **Upstream ordering is not a contract.** The live range came back
     newest-first; `_latest_on_or_before` sorts explicitly. Trusting upstream
     order would silently return the **oldest** rate. A reversed-payload test
     pins this.

6. **Nulls are skipped, never coerced to zero.** A `None` rate must not become
   `0.0` — that is an invented number. No data at all raises `MarketDataError`.

7. **Company lookup misses return an empty list**, never a fuzzy substitute. For a
   counterparty check an invented match is worse than no answer.

---

## 4. ⚠️ Known collision surface

Reported because it is *this* branch's files that collide; resolving it is the
merge agent's call. Two facts, both verified against git objects:

### 4.1 `market_data` is a **file here** and a **directory** on `worktree-p1-tools`

```
chloe-dev:           100644 blob  platform/financial-tools/fin_tools/market_data.py
worktree-p1-tools:   040000 tree  platform/financial-tools/fin_tools/market_data/
```

Git cannot reconcile a path that is a blob on one side and a tree on the other —
this is a hard conflict, not a textual one. They are also **different designs**:

| | this branch (`chloe-dev`) | `worktree-p1-tools` |
|---|---|---|
| Shape | single module `market_data.py` | package `market_data/` (client, cache, rates, registry, tools) |
| Endpoints | `trade_cal`, `daily`, `shibor_lpr`, `shibor`, `stock_basic` | `shibor_lpr`, `shibor`, `stock_basic`, `stock_company`, `fina_indicator`, `index_member_all` |
| Data sources | live only; no token → `ServiceUnavailable` | `fixture` / `core` / `live` / cached, via `HELIOS_MARKET_MODE` |
| Caching | none | SQLite TTL under `HELIOS_DATA_DIR` |
| Tools | `market.reference_rate`, `.daily_quotes`, `.trading_calendar`, `.company_basic` | `market.rate`, `.borrowing_cost_compare`, `.company_lookup`, `.collection_period` |
| Tool names overlap | **none — the four names are disjoint** | |

Because the tool **names** are disjoint, both families can in principle coexist
after a rename; only the `market_data` path and the shared plumbing collide. Note
that `daily_quotes`/`trading_calendar` (this branch) and
`rate`/`borrowing_cost_compare` (theirs) are not duplicates of each other.

### 4.2 Two files changed on both sides

`git diff` against merge-base `cc96f0a` shows overlap in exactly:

```
platform/financial-tools/fin_tools/__init__.py
platform/financial-tools/fin_tools/service.py
```

- **`service.py`** — changes are **complementary**, not competing. This branch adds
  `UpstreamError` + the three-way `_wrap`. The other side adds
  `EXECUTION_SECRET_ENV`, bearer auth in `make_handler`, `/health`, `/kg/nodes`,
  and a `persistence=` hook. Both should survive; the risk is invariant #4 above.

- **`__init__.py`** — ⚠️ **silent-wrong-winner risk.** Both sides bind the *same
  alias* to *different modules* and then call it positionally:

  ```python
  # chloe-dev
  from . import market_data as _market_mod
  _market_mod.register(reg, market_client)            # (registry, client)

  # worktree-p1-tools
  from .market_data import tools as _market_mod
  _market_mod.register(reg, market_resolver, graph)   # (registry, resolver, graph)
  ```

  A merge that keeps one import line and the other call site yields an
  `ImportError`, or — worse — a signature that happens to accept the arguments and
  registers the wrong tool family. **These two lines must be resolved together,
  and the `_market_mod` alias must be renamed on at least one side.** The other
  side also extends `Bundle` with `tenant`, `data_dir`, `sheets`, `profiles`,
  `collect`; this branch adds no `Bundle` fields.

### 4.3 Shared `specs/` files

All four `specs/` edits on this branch are **pure insertions — zero deletions**
(verified with `git diff --numstat cc96f0a HEAD`). They should merge cleanly
against other insertions unless another branch edited the immediately adjacent
lines. Hunk counts: `decisions.md` **2**, the other three **1 each**. Anchors:

- `specs/decisions.md` — new `### Market data is quote data...` under **Decided**,
  inserted just before `## Tentative`; plus an indented sub-list appended to the
  existing open-question bullet "Which financial operations should be managed APIs
  versus packaged SDK tools?" (that question is left **open**, deliberately).
- `specs/interfaces.md` — new `### Call outcomes` and `### Market data tools`
  inside §6, inserted before `## Interfaces to Define Later`.
- `specs/tech-stack.md` — five new bullets appended under **Decided**, after the
  `Data storage:` line.
- `specs/financial-tools-service.md` — new `### Market data sits outside this
  boundary, deliberately` at the end of §6, before the `---` preceding §7.

`platform/financial-tools/PLAN.md` and `README.md` are **not** pure insertions —
they replace lines that had become false ("no network calls, no secrets", "no
HTTP", "44 tests", "12 tools"). Expect real conflicts there if another branch also
edited them.

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
skipped** (43 new). Tool count **12 → 16**.

The 2 skips are **pre-existing and unrelated**: `test_gated_loop_integration.py`
and `test_toolkit_bridge.py` `importorskip` on `audit_agent` / `agent_toolkit`,
which are not installed in this checkout. Do not read them as regressions.

`entry-point/examples/invoice-payer`: **41 passed** (it consumes `service.py`, so
this is the check that the `_wrap` change is backward-compatible).

### Live check (needs `TUSHARE_TOKEN`)

The token is in `.env` at the repo root and is gitignored (`.gitignore:12`). Note
the line has **no `export`**, so a bare `source .env` will not reach child
processes — use `set -a`:

```bash
cd <repo>
set -a && . ./.env && set +a
cd platform/financial-tools
python3 -c "
from fin_tools import build_world
b = build_world()
print(b.registry.call('market.reference_rate', series='lpr_1y'))
print(b.registry.call('market.trading_calendar', start_date='2026-10-09', end_date='2026-10-15')['open_days'])
print(b.world.balances, b.world.ledger)   # must stay {} {}
"
```

Verified this way during the session: all four tools returned real data,
`World.balances`/`World.ledger` were untouched, and the token appeared in no
result or catalog blob.

---

## 6. Decisions recorded, and what is still open

**Recorded in `specs/decisions.md` as Decided:** market data is quote data and
must never reach `World.balances` or `World.ledger`; and an unavailable quote is
an outage, not a decision.

**Left open on purpose:** "Which financial operations should be managed APIs
versus packaged SDK tools?" remains an **Open Question**. This branch implemented
market data as a *packaged* adapter and says so in the annotation, but that is an
implementation choice for one capability, not a resolution. The teammate proposal
in `OTHER-TEAMMATE-STUFF/forecasting-simulation/services/financial-api/` ("math is
packaged, data-fed defaults are managed") is **unratified** — its §10 spec updates
never landed, so it must not be cited as precedent. The practical asymmetry is
noted: a packaged adapter means every deployed app holds its own `TUSHARE_TOKEN`
and its own quota.

**Flagged for another owner** (`8434107`): that same teammate `services/financial-api`
is a candidate to be promoted into `platform/` and shared rather than duplicated.
Not done here — `OTHER-TEAMMATE-STUFF/` was read, never modified.

### Deliberate deviation from the brief

The brief names `http://api.tushare.pro`. This branch defaults to **HTTPS** on the
same host, because the token travels in the POST body and a plaintext request
would put a live credential on the wire. HTTPS was verified working.
`DEFAULT_ENDPOINT` is overridable via `TushareClient(endpoint=...)` if plain HTTP
is ever required. Every other decided constraint held: stdlib urllib, no SDK, no
pandas, plain dicts, token from env only, no `set_token()`, `dependencies = []`.

### Endpoint names corrected against the live API

The teammate adapter's names were self-declared unverified and are **wrong**:
`lpr` with `TRADE_DATE`/`LPR1Y`/`ON` returns Tushare error **40101
"请指定正确的接口名"**. The verified endpoints are `shibor_lpr` (fields
`date,1y,5y`) and `shibor` (fields `date,on,1w,2w,1m,3m,6m,9m,1y`). A test
asserts the stale names are not present in `ALLOWED_APIS`.

### Not built (out of scope here)

No cache, no rate limiting, no retry/backoff, no offline fixture or core-file
source. A caller with no token gets `ServiceUnavailable`, never invented numbers.
Also not built: FX, macro (CPI/PPI/PMI), yield curves, credit tiers.

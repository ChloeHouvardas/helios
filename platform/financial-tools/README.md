# Financial Tools Developer Interface (prototype)

The tool layer of Helios Layer 1 (whiteboard box ①): what Qwen-as-coding-agent and
in-app agents call to do financial work. Money movement is an offline,
deterministic prototype — **no DB, no real money, payments stay mock**.

One exception, and it is deliberate: `fin_tools/market_data.py` may call the
Tushare HTTP API to fetch **quote and reference-rate data**, and only when a
`market.*` tool is invoked with `TUSHARE_TOKEN` set. No other module opens a
socket. That data is read-only and never reaches a balance or the ledger — see
`specs/decisions.md`.

## Run

```bash
cd platform/financial-tools
python3 -m pytest          # 104 tests; all offline (market tests inject a fake transport)
python3 -m fin_tools.demo  # end-to-end story
```

## Components

| Module | What it is | Key property |
|---|---|---|
| `fin_tools/contracts.py` | Tool Contract + registry (`specs/interfaces.md` §6) | every tool declares input/output schema + `side_effects` flag |
| `fin_tools/intent_verification.py` | PSD2-style dynamic linking: HMAC auth code bound to (amount, currency, payee); TTL; single-use | any field change, replay, or expiry → reject |
| `fin_tools/payments.py` | mock ledger, send + receive, integer cents, idempotency keys | **settles only with a verified intent**; otherwise `rejected` |
| `fin_tools/email_sender.py` | compose/render/queue to local outbox | no SMTP; outbox is inspectable by audits/sims |
| `fin_tools/spreadsheet_tools.py` | CSV load/save/filter/aggregate/schema-check (stdlib only) | `*_cents` columns coerced to int |
| `fin_tools/market_data.py` | live Tushare quotes, reference rates, trading calendar, listed-company registry (stdlib urllib) | **closed allow-list**; no tool takes an `api_name`; token never observable; output is quote data, never `*_cents` |
| `fin_tools/knowledge_graph.py` | nodes/edges with `verified` vs `observation` provenance | an observation can never overwrite a verified fact |
| `fin_tools/simulation.py` | (a) persona-driven Monte-Carlo cash forecast + what-if; (b) copy-on-write shadow sandbox | seeded/deterministic; sandbox never mutates the real world |
| `fin_tools/demo.py` | scripted story: simulate → preview → verify → pay → email → graph | shows tamper/replay/no-auth rejections live |

## Developer interface (the surface agents see)

`build_world()` returns a `Bundle` (world + services) with 16 tools registered:
`payments.send`, `payments.receive`, `payments.balance`, `email.send`,
`email.outbox`, `spreadsheet.load`, `spreadsheet.save`, `spreadsheet.aggregate`,
`graph.publish_verified`, `graph.observe`, `graph.read`, `simulation.forecast`,
and the four read-only market tools `market.reference_rate`,
`market.daily_quotes`, `market.trading_calendar`, `market.company_basic`.
`bundle.registry.catalog()` is the JSON-able declaration an action agent or the
audit gate consumes; side-effecting calls are the ones that must carry intent
proof / go through proposals. The `market.*` tools are all read-only and never
carry a token in their declaration.

## Design decisions (research-grounded, cited in `research/README.md`)

- **Intent verification = PSD2 dynamic linking applied to agents.** The human
  confirmation is the SCA moment; the HMAC binds amount+payee+currency, so an
  agent cannot widen its own authority by editing the call (arXiv:2605.16976,
  arXiv:2604.02767, EBA Q&A 2020_5133/2020_5366).
- **Probabilistic proposes, deterministic disposes.** An optional LLM
  `intent_assessor` may write the human-readable confirmation text; it never
  decides (VeriGuard/AgentGuard pattern).
- **Simulation has two halves.** Behavioural: payer personas as parameter cards
  (Park et al. arXiv:2411.10109 at pocket scale; `PersonaSimulator`-style LLM
  upgrade is a documented protocol swap). Mechanical: shadow execution of a
  proposed call on a deep copy of the world (ToolEmu's idea, arXiv:2309.15817,
  deterministic variant; `EmulatedSandbox` is the seam for an LM emulator).

- **Market data is quote data, and structurally cannot become money.** Tushare
  returns floats (verified live: `close` 11.5, `pct_chg` -0.605), which collides
  with "everything money-shaped is integer cents, no floats ever". Both are right:
  a share price is a *quote*, not a balance. So `market_data.py` never imports
  `World` or `Payments`, every tool is `side_effects=False`, and no output field
  is named `*_cents` — that name would imply integer money and invite a caller to
  add a price to a balance. Amounts carry their real unit instead (`CNY per
  share`, `手 / lots of 100 shares`, `千元 / thousands of CNY`). An app converts
  explicitly at its own boundary and owns the rounding.
- **Endpoint names were verified live, not inherited.** The teammate adapter's
  `lpr` / `TRADE_DATE` / `LPR1Y` names were self-declared unverified and return
  Tushare error 40101 "请指定正确的接口名". The real endpoints are `shibor_lpr`
  (`date,1y,5y`) and `shibor` (`date,on,1w,2w,1m,3m,6m,9m,1y`). A point query for
  an unpublished date returns zero rows, so rates resolve as the newest
  publication on or before the requested date, and `provenance.as_of` reports the
  date actually published — a caller can see the number is six weeks old rather
  than assume it is current.

## Known simplifications (say these out loud in the pitch)

- Personas are fixture cards, not interview-grounded; fidelity claims do not transfer.
- Shadow sandbox is copy-on-write, not LM-emulated; it reports deterministic effects.
- Single-process, in-memory; no persistence, no real SCA device. (`service.py`
  does ship an HTTP boundary — `ToolServer`/`HttpToolClient` — so "no HTTP" is no
  longer true; that line was stale and is corrected here.)
- Market data has no cache, no rate limiting, no retry/backoff and no offline
  fixture source. No token means `ServiceUnavailable`, never invented numbers.
- The Action-gate vs Audit-auditor seam (open team question) is untouched: these
  tools emit proposals/records that *either* gate could consume.

## Layout

```
financial-tools/
  PLAN.md               build plan for this package
  README.md             this file
  research/README.md    citations for intent verification + simulation choices
  fin_tools/            the package (contracts, intent, payments, email,
                        spreadsheets, graph, simulation, market_data, demo)
  fixtures/invoices.csv demo + test data (integer cents)
  tests/                104 offline pytest tests (43 for market_data, all via an
                        injected fake transport)
```

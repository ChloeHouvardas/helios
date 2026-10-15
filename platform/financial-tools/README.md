# Financial Tools Developer Interface (prototype)

The tool layer of Helios Layer 1 (whiteboard box ①): what Qwen-as-coding-agent and
in-app agents call to do financial work. Everything is an offline, deterministic
prototype — **no real money, no live provider credentials needed to test**. The
tools run in-process by default and can also be reached over a service boundary;
see "Known simplifications" below.

One exception, and it is deliberate: `fin_tools/market_data` may call the
Tushare HTTP API to fetch **quote and reference-rate data**, and only when a
`market.*` tool is invoked with `TUSHARE_TOKEN` set. That data is read-only and
never reaches a balance or the ledger — see `specs/decisions.md`.

## Run

```bash
# from the repo root; scripts/bootstrap-venv.sh builds .venv first
.venv/bin/python -m pytest platform/financial-tools/tests -q   # all offline; market tests inject a fake transport
.venv/bin/python -m fin_tools.demo                             # end-to-end story
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
- No persistence and no real SCA device. `fin_tools` itself is in-process by default — but it is not HTTP-free: `fin_tools/service.py` provides `HttpToolClient` + `ToolServer` over stdlib `http.server`, so the tools can be reached across a process or container boundary with only the server holding the signing secret. `spreadsheets/app.py` is a separate FastAPI service. No test needs either one running.
- The Action-gate vs Audit-auditor seam is **settled**, not open: the standalone `audit_agent` is authoritative and `agent_toolkit.audit.AuditGate` is legacy (see `specs/decisions.md`). These tools emit proposals/records that the standalone auditor consumes, and `fin_tools.toolkit_bridge` adapts them to the action side's contract.
- Market data serves no invented numbers: no token and no fixture means
  `ServiceUnavailable`, never a made-up rate.

## Layout

```
financial-tools/
  PLAN.md               build plan for this package
  README.md             this file
  pyproject.toml        installs all five sibling packages below
  research/README.md    citations for intent verification + simulation choices
  fin_tools/            the core package (contracts, intent, payments, email,
                        spreadsheets, graph, simulation, service, toolkit_bridge,
                        audit_manifest, market_data, demo)
  helios_forecast/      Monte-Carlo cash-flow projection + scenarios + calibration
  spreadsheets/         sheet rendering/reconciliation; FastAPI app; live engines
                        (Feishu, WPS, Grist) served locally so tests need no creds
  email_tool/           templated SMTP sender: schemas/, templates/, idempotency
  payments_collect/     pydantic receivables model + in-memory PaymentStore
  fixtures/invoices.csv demo + test data (integer cents)
  tests/                offline pytest tests; count printed by scripts/test-all.sh
```

Only `fin_tools` is described in the component table above; the four sibling
packages are teammates' work imported into this directory and installed by the
same `pyproject.toml`. Each keeps its own tests in `tests/`, prefixed with the
package name (`test_forecast_*`, `test_email_*`, `test_payments_collect_*`,
`test_spreadsheets.py`). See `entry-point/references/financial-tools.md`.

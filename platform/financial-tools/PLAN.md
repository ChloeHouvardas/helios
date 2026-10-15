# Plan: Financial Tools Developer Interface + Intent Verification & Outcome Simulation prototypes

Owner: Chloe's session agent (whiteboard box ① "App Creation Platform — Financial Tools Developer Interface").
Status: approved by user instruction "make a plan before implementing then execute the plan automatically".

## Scope (from whiteboard + task brief)

Scaffold the **Financial Tools Developer Interface** as a new sibling package
`platform/financial-tools/` (Python pkg `fin_tools`), containing bite-sized working
prototypes of:

1. **Tool registry / developer interface** — implements the Tool Contract from
   `specs/interfaces.md` §6 (name, description, input schema, output schema,
   side-effect flag). Every other tool registers through it.
2. **Simulating outcomes** (`simulation.py`) — seeded Monte-Carlo cash-flow
   simulation over payer-behaviour profiles + what-if comparison + a
   copy-on-write **shadow-execution sandbox** (preview a side-effecting tool call
   against a deep copy of world state before running it for real).
3. **Email sender** (`email_sender.py`) — compose/render/queue to a local
   append-only outbox. No SMTP, no network.
4. **Spreadsheet tools** (`spreadsheet_tools.py`) — CSV load/save, filter,
   aggregate, schema-validate over row dicts (stdlib `csv` only).
5. **Payments** (`payments.py`) — mock ledger, send + receive, integer cents,
   idempotency keys, state machine; **refuses to settle without a verified intent**.
6. **Intent verification** (`intent_verification.py`) — PSD2-style dynamic linking:
   an HMAC auth code bound to (amount, payee, currency, intent id); read-back
   confirmation; TTL; single-use consumption; intent-to-execution binding check
   before any payment settles.
7. **Knowledge graph stub** (`knowledge_graph.py`) — nodes/edges with the
   verified-vs-agent-observation distinction from `specs/interfaces.md` §4;
   observations cannot overwrite verified facts.

Plus: `demo.py` (one scripted end-to-end story), `fixtures/invoices.csv`,
pytest suite (offline, deterministic), package `README.md`, and
`research/README.md` citing the sources below.

## Research basis (citations live in `research/README.md`)

**Intent verification**
- SentinelAgent: Intent-Verified Delegation Chains (arXiv:2604.02767, 2026) — bind
  each agent action to verifiable user intent; verify intent *before* execution.
- "Securing LLM Agents Need Intent-to-Execution Integrity" (arXiv:2605.16976, 2026) —
  execution must faithfully reflect the captured intent; we enforce this as a
  hard gate in `payments.send_payment`.
- PSD2 Art. 97(2) + RTS dynamic linking (EBA Q&A 2020_5133; OneSpan explainer) —
  authentication code unique per transaction, linked to amount + payee, invalidated
  on any change. This is the direct model for `auth_code`.
- Runtime-verification context: VeriGuard (arXiv:2510.05156), AgentGuard
  (arXiv:2509.23864), survey arXiv:2608.14590.

**Simulating outcomes**
- ToolEmu (Ruan et al., ICLR 2024 Spotlight, arXiv:2309.15817) — emulate tool
  execution in a sandbox before/instead of real execution; 68.8% of failures it
  flagged were valid real-world risks. Our `sandbox_run` is the deterministic
  (copy-on-write) variant, with a documented hook for an LM emulator.
- Generative Agent Simulations of 1,000 People (Park et al., arXiv:2411.10109) —
  interview-grounded personas reproduce real behaviour (~85%); motivates the
  `PayerProfile`-as-persona design and the `PersonaSimulator` hook point.
- Generative Agents (Park et al., arXiv:2304.03442); Stanford HAI policy brief
  "Simulating Human Behavior with AI Agents" (2025).

## Non-interference guarantees

- All new files under `platform/financial-tools/`.
- **Zero edits** inside `platform/action-agent-builder/`, `platform/audit-agent-builder/`,
  `platform/app-builder/`.
- Shared-doc edits limited to one line in `platform/README.md` (list the new folder)
  and one line in `specs/interfaces.md` (point "financial tool schemas" at this package).
- No real money movement anywhere in the package. Payments, ledger and balances
  remain mock end to end.
- **Network is confined to `fin_tools/market_data.py`.** That one module may call
  the Tushare HTTP API, and only when a `market.*` tool is invoked with
  `TUSHARE_TOKEN` set. Every other module stays offline. Nothing else in the
  package opens a socket.
- **Secrets:** `TUSHARE_TOKEN` is read from the environment at call time, sent
  only in a request body, never stored on an instance, never logged, never placed
  in `ToolSpec.declare()` output, and redacted from every error message. There is
  no `set_token()` — a token in a process global is a token in a traceback.

## Build order

1. `PLAN.md` (this file), `research/README.md`
2. `fin_tools/contracts.py` → registry
3. `fin_tools/intent_verification.py`, `payments.py`, `email_sender.py`,
   `spreadsheet_tools.py`, `knowledge_graph.py`, `simulation.py`
4. `fixtures/invoices.csv`, `demo.py`
5. `tests/` (7 files) + `conftest.py` + `pyproject.toml`
6. Run pytest until green; run `demo.py`
7. `README.md` + the two one-line shared-doc edits

## Done-when

- `python -m pytest` green from `platform/financial-tools/`
- `python -m fin_tools.demo` prints the end-to-end story (simulate → verify intent →
  shadow preview → pay → email → graph) with a tampered-intent case correctly rejected
- `git status` shows only new files under `platform/financial-tools/` plus the two
  one-line shared-doc edits; the three protected folders untouched

## Not doing (veto anytime)

- No Qwen/LLM calls at runtime (offline-first; LLM hooks are documented interfaces only)
- No DB, no packaging beyond a minimal pyproject
- No resolution of the Action-gate vs Audit-auditor seam (open team question, unchanged)

### Superseded since this plan was written

- ~~No HTTP server~~ — `service.py` ships `ToolServer`/`HttpToolClient`, the
  boundary in `specs/financial-tools-service.md` §5. The line above was already
  stale when market data landed; corrected here rather than left misleading.
- ~~No network calls~~ — see the non-interference guarantees above. Network is
  confined to `fin_tools/market_data.py`.

### Market data scope (added with `market.*`)

- **Closed allow-list, and no tool takes an `api_name`.** The endpoint is
  hard-coded per tool, so an agent cannot reach an arbitrary Tushare endpoint.
  `ALLOWED_APIS` holds exactly `trade_cal`, `daily`, `shibor_lpr`, `shibor`,
  `stock_basic` — each verified against the live API before being listed.
- **Read-only, and quote data, not money.** Every `market.*` tool is
  `side_effects=False`; the module never imports `World` or `Payments`; no output
  field is named `*_cents`. See `specs/decisions.md` for the ratified rule.
- **Still not doing:** no caching layer, no rate limiting, no retry/backoff, no
  fixture or offline data source, no FX/macro/credit endpoints. A caller with no
  token gets `ServiceUnavailable`, not invented numbers.

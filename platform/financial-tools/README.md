# Financial Tools Developer Interface (prototype)

The tool layer of Helios Layer 1 (whiteboard box ①): what Qwen-as-coding-agent and
in-app agents call to do financial work. Everything is an offline, deterministic
prototype — **no network, no DB, no real money**.

## Run

```bash
# from the repo root; scripts/bootstrap-venv.sh builds .venv first
.venv/bin/python -m pytest platform/financial-tools/tests -q   # 86 tests, offline
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
| `fin_tools/knowledge_graph.py` | nodes/edges with `verified` vs `observation` provenance | an observation can never overwrite a verified fact |
| `fin_tools/simulation.py` | (a) persona-driven Monte-Carlo cash forecast + what-if; (b) copy-on-write shadow sandbox | seeded/deterministic; sandbox never mutates the real world |
| `fin_tools/demo.py` | scripted story: simulate → preview → verify → pay → email → graph | shows tamper/replay/no-auth rejections live |

## Developer interface (the surface agents see)

`build_world()` returns a `Bundle` (world + services) with 12 tools registered:
`payments.send`, `payments.receive`, `payments.balance`, `email.send`,
`email.outbox`, `spreadsheet.load`, `spreadsheet.save`, `spreadsheet.aggregate`,
`graph.publish_verified`, `graph.observe`, `graph.read`, `simulation.forecast`.
`bundle.registry.catalog()` is the JSON-able declaration an action agent or the
audit gate consumes; side-effecting calls are the ones that must carry intent
proof / go through proposals.

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

## Known simplifications (say these out loud in the pitch)

- Personas are fixture cards, not interview-grounded; fidelity claims do not transfer.
- Shadow sandbox is copy-on-write, not LM-emulated; it reports deterministic effects.
- Single-process, in-memory; no persistence, no HTTP, no real SCA device.
- The Action-gate vs Audit-auditor seam (open team question) is untouched: these
  tools emit proposals/records that *either* gate could consume.

## Layout

```
financial-tools/
  PLAN.md               build plan for this package
  README.md             this file
  research/README.md    citations for intent verification + simulation choices
  fin_tools/            the package (contracts, intent, payments, email,
                        spreadsheets, graph, simulation, demo)
  fixtures/invoices.csv demo + test data (integer cents)
  tests/                44 offline pytest tests
```

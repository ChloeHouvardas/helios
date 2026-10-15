# Helios — Platform Status

Written 2026-10-15 for a cold reader, against the `integration` branch (every
overnight worktree merged). Every claim here was observed in tool output on that
branch; where something was not run, it says so. `docs/developer-context/DEMOFLOW.md` holds the demo
stages and the test ladder; `scripts/demo-check.sh` runs that ladder.

Two claims carry the architecture. **The model proposes, code decides**: an action
agent may ask for anything, but what it gets is bounded by policy written as data
and enforced by deterministic code that fails closed, in a package the app cannot
modify. **Facts are not claims**: every value a rule depends on is resolved from a
trusted fact source the agent does not control — the recorded invoice amount, the
supplier's change date, and now the platform's own cash projection.

---

## The numbers

```
scripts/bootstrap-venv.sh && scripts/test-all.sh   -> all 14 suites green: 1876 passed
.venv/bin/python -m agent_integration.cli selfcheck -> seam check: OK
fin_tools.build_world()                             -> 40 tools in 9 families
```

| Suite | Tests |
|---|---|
| platform/financial-tools (package + 3 teammate suites) | 581 + 105 + 68 + 38 |
| platform/app-builder | 420 |
| platform/deployment | 140 |
| platform/agent-integration | 139 |
| platform/audit-agent-builder | 81 |
| platform/action-agent-builder | 36 |
| apps/helios-collect · helios-payables · helios-cash-planner | 93 · 75 · 41 |
| entry-point/examples/invoice-payer · checks | 54 · 5 |

---

## Component status

### Qwen Coding Agent — the on-ramp
- **Implemented:** `entry-point/SKILL.md`, `BUILD.md` (eleven steps), `references/`, the worked example `examples/invoice-payer`, and the builder CLI it drives.
- **Run:** `helios-app questions`, `spec-from-intake`, `new`, `validate`, then `platform/deployment/cli/helios.py deploy`.
- **Gaps:** every run is on the scripted driver; the live Qwen path is wired, not demonstrated.

### Financial Tools Interface + SDK
- **Implemented:** `fin_tools` — one registry with side-effect flags over payments, collect, email, forecast, behavior, market (six tools incl. daily quotes and the trading calendar, fixture/core/live sources with provenance), spreadsheet, sheets, graph; intent verification; an HTTP service (`python -m fin_tools`) with bearer auth and SQLite persistence.
- **Where:** `platform/financial-tools/`
- **Gaps:** the sheets and receivables stores are not in the SQLite snapshot (an imported statement does not survive a core restart).

### Application Builder
- **Implemented:** `app_builder` — 16-question intake, strict spec (tools may state their side-effect flag), scaffolder, validator (cross-file checks, fact probe that follows helpers), UI kit, `stacks.yaml`, deployment-manifest generation from the engine's real field set.
- **Verified:** a fresh scaffold validates clean on arrival and its generated tests pass; the surprise-task rehearsal defects are fixed.
- **Gaps:** `spec-from-intake` maps prose answers poorly (entities and pages often need hand editing — expected, it is step 3 of the procedure); app-local tool names give the scaffolder no permission families, so `helios.yaml` `permissions` is edited by hand.

### Agent Toolkit: action side, audit side, the seam
- **Implemented:** `agent_toolkit` (contract, store, action log, holds, CLI), `audit_agent` (YAML rules, signed grants, trusted facts, hash-chained log), `agent_integration` (the gated loop, the bridge, adapters, model driver, CLI). The bridge now names the rule that decided when a deny and a hold fire together.
- **Gaps:** holds and replay guards are in memory; a container restart loses pending approvals. The in-process `AuditGate` is legacy and out of the loop.

### Validation + Deployment
- **Implemented:** `platform/deployment` — `helios.yaml` + ASGI + constant `/health`; secret lint; golden images keyed by the platform hash (`helios-app-base`, `helios-auditor`, `helios-core`); smoke test; per-tenant internal network, Redis bus and **tenant core running `python -m fin_tools`**; audit sidecar forwarding named tools to core's `/call` with the three outcomes kept distinct; blue/green redeploy; nginx routing; `helios deploy / apps / logs / evidence / undeploy / doctor`; `GET /api/v1/tenants/<t>/logs` central evidence with chain verification; `permissions: []` grants nothing; tenant undeploy strips routes.
- **Verified live:** see "Live run" below.

### The three flagships
| App | Scenarios | Rules | Tests | The moment |
|---|---|---|---|---|
| Helios Collect | 10 | 11 app + 8 baseline | 93 | INV-1006 refused in code; INV-1009 over-claim held against a ceiling the agent cannot move |
| Helios Payables | 16 | 18 | 75 | BILL-2002 refused although the owner approves; BILL-2012 lying agent held; BILL-2016 held because the platform's forecaster says payroll would be short |
| Helios Cash Planner | 11 | 12 | 41 | WARN-INVENTED refused (a shortfall in a fine week); WARN-INFLATED held against the projection; DRAW always the owner's call, rate and provenance shown |

All three carry `helios.yaml`, build lazily behind a constant `/health`, and announce their verified changes on the tenant bus.

### Shared services
- **Knowledge graph:** in the tenant core, persisted; verified vs observation enforced.
- **Tool service:** the tenant core; a bearer token authenticates, never authorises; `payments.send` without an intent code is rejected by core.
- **Events:** Redis per tenant; Collect and Payables publish, Cash Planner subscribes and re-projects.
- **Evidence:** central, read from the volumes, chains verified.

---

## Live run

`scripts/demo-check.sh all` on this machine (Docker Desktop), 2026-10-15:
T0 5/5 · T1 10/10 · T2 8/8 · T3 17/17 · T4 8/8 · T5 4/4. Observed: the three
golden images built from this tree; the sample app and all three flagships
deployed under tenant `demo` at once; redeploy without downtime; a failing smoke
test left the previous deployment serving; a bundled `.env` refused before upload;
a hard-coded key caught by the static audit; hold approve then a second approve
404; a container restart kept its route and its logs; the tenant core answered
40 tools behind a bearer token and refused a payment with no intent code; the
central evidence endpoint returned every app's logs with intact chains; Collect's
extension event reached the cash planner over the bus and it re-projected;
Payables held a payment on the forecaster's payroll fact.

---

## Known gaps, honestly

1. Holds are in memory (DEMOFLOW G7, optional Phase 8).
2. Apps compose the tools in-process for local runs; when deployed, money movement still executes in-process inside the app container rather than through the sidecar and core — the sample app shows the platform path, the flagships show the policy. Wiring the flagships' `pay_bill` / `send_email` through `propose_action` is the remaining step of "tools on the platform".
3. Payables declares a read of `cashplan[shortfall_warning]` and does not act on it yet; its payroll fact is computed from its own book.
4. No live model run in the demo.
5. Base images were pulled through a registry mirror on this machine; verify digests for a judged demo.

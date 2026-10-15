# INVESTMENT-CONTEXT.md — Helios, explained for a pitch writer

Written 2026-10-15 for an LLM (or a person) drafting the investment pitch. It
describes what Helios is, every component, what is proven today versus what is
scheduled, and the claims that are safe to make. Status tags: **[built]** verified
running in this repo · **[in progress]** scheduled in the current build plan
(`DEMOFLOW.md`, the plan file) · **[design]** specified, not implemented.

Competition: Chengdu 80 2026 Fintech Hackathon, 80 hours, team **Maple Queen's**.
Scoring: 50% investment pitch, 25% engineering verification, 25% architecture and
technical defence. Judges' guiding question: *what new financial capability becomes
possible when AI, data and an extensible platform work together?*

---

## 1. The one-paragraph version

Helios is a platform where a small business describes the financial app it needs in
one sentence, and an AI coding agent (Qwen) builds it, the platform validates and
deploys it, and AI agents inside the running app then do the financial work: chasing
invoices, paying suppliers, planning cash. The platform's distinctive property is
**governed autonomy**: an agent may ask for anything, but every action that moves money,
sends an email or writes shared records is a *proposal* that deterministic,
fail-closed code approves, holds for the owner, or denies, using facts the agent
cannot forge. Apps are plug-ins on a shared platform that owns the tools, the
records, the audit trail and the deployment, so each new app is cheap to build and
inherits the same guarantees.

## 2. The problem and the customer

Small and medium businesses spend significant time and money on recurring financial
work across tools that do not connect: collecting receivables, paying payables,
forecasting cash. Off-the-shelf software is generic; custom software is unaffordable;
and handing money movement to an unconstrained AI agent is unacceptable to any owner,
accountant or regulator. Helios' bet is that the unlock is not "an AI that pays bills"
but "a platform on which an AI can build a custom financial app that is safe to run".

Customer: owners, finance staff and bookkeepers of SMBs, initially in China (the
market data is Chinese: Tushare LPR, SHIBOR, listed-company registry).

## 3. Two kinds of AI, kept apart on purpose

| | Coding agent | In-app agents |
|---|---|---|
| Who | Qwen, driven by the platform's docs and CLI | action agent + audit agent packaged into every app |
| Job | turn one sentence into a deployable app | run the app's financial workflow after deploy |
| Can it move money? | no, it writes code and policy | the action agent can only *propose*; code decides |

This separation is the architecture's core defence: the thing that writes the app
cannot grant the app permission, and the thing that runs the app cannot change its
own policy.

## 4. The two governing principles (pitch and defence material)

**The model proposes, code decides.** Policy is data (a YAML audit manifest). It is
enforced by a deterministic engine that fails closed: unknown tool, malformed
argument, unrecognised constraint, or unreachable service all mean "no". No LLM is
in the decision path.

**Facts are not claims.** Every value a rule depends on (invoice amount, whether a
supplier's bank details changed, how many times a customer was contacted) is resolved
from a trusted fact source the agent cannot write to. The agent names *which* record;
it never states *what the record says*. Demonstrated live: an owner **approves** a
payment to a supplier whose banking details changed five days ago, and the payment is
**still refused**. The control depends neither on the human noticing nor on the agent
behaving.

## 5. Architecture, component by component

Three parts, matching `specs/architecture-mermaid.md`.

### Part 1 — Development platform (what the coding agent uses)

**Financial Tools Interface + SDK** **[built]** — `platform/financial-tools`. One
registry of ~40 tools in nine families, each declaring input/output schema and a
side-effect flag (the flag decides whether a call runs directly or becomes a
proposal). Families: payments (ledger, send, receive, integer cents, settles only with
a verified intent code), collect (receivables and payment links), email (templated,
idempotent, SMTP optional), forecast and behavior (seeded Monte-Carlo cash projection,
what-if scenarios, payer behaviour profiles), market (public LPR/SHIBOR reference
rates, borrowing-cost comparison, listed-company registry lookup, industry collection
periods, all with provenance and a disclaimer, fixture mode for offline demos),
spreadsheet and sheets (statement import, reconciliation, propose-then-approve edits),
knowledge graph (verified records versus agent observations; an observation can
never overwrite a verified fact), intent verification (PSD2-style dynamic linking: an
HMAC auth code bound to amount, currency and payee, single use, short lived). Served
in-process or over an authenticated HTTP service with SQLite persistence.

**Application Builder** **[built]** — `platform/app-builder`. A fixed 16-question
intake, a strict app spec (`helios-app.yaml`, pydantic, unknown keys rejected, money
never float), a scaffolder that writes a complete validator-clean skeleton (both agent
manifests, fact source, tools, SQLite store, FastAPI server, Jinja2/HTMX pages, control
tests, deployment manifest), and a validator with cross-file checks. The check that
protects money: every tool the agent may call must be declared in the audit manifest,
or it is never gated at all. Known defects from a surprise-task rehearsal are being
cleared **[in progress]**.

**Agent Toolkit** — two packages that never import each other:
- *Action Agent Builder* **[built]** — tool contract, store, action log, holds, CLI.
- *Audit Agent Builder* **[built]** — YAML rules, HMAC-signed scoped single-use grants,
  trusted fact sources, hash-chained append-only evidence log, fail-closed
  adjudication returning approve / hold / deny with the rule that fired.
- *Agent integration* **[built]** — the only package allowed to see both. The gated
  loop: the auditor narrows the tool list *before* the model sees it; read-only calls
  run directly; every side effect is adjudicated first; on approval the auditor's
  granted arguments execute, never the model's originals. Runs on a scripted driver
  (deterministic, offline) or a Qwen model driver via DashScope.

**Validation + Deployment** **[built]** — `platform/deployment`. An app provides
three things: `helios.yaml`, an ASGI entry point, `GET /health`. The engine zips,
secret-lints, builds on a golden base image that already contains the platform
(seconds, no dependency install), smoke-tests on an isolated network, attaches an
audit sidecar, routes through nginx, and returns a URL or a structured error the
coding agent can act on. Blue/green redeploys; a failed redeploy never takes down the
running one. Errors list every problem at once, each naming the field and what to
write instead, because the author is a model.

### Part 2 — Generated applications (plug-ins)

Each app is its own container with UI, logic, action agent and audit agent. Each gets
a per-app audit sidecar that holds the execution secret the app never sees. Two
gates, one direction of authority: the in-app auditor has facts and an owner and can
hold; the sidecar may deny or forward, never approve. Data lives on a named volume
that survives redeploys and is never deleted on undeploy, because the audit log is
compliance evidence.

### Part 3 — Shared services (per tenant, i.e. per business)

- **Tool service in the tenant core** **[in progress]** — the financial tools run on
  the platform, bearer-authenticated, persisted; apps request, the platform executes.
  Today apps still compose the tools in-process; the switch is scheduled.
- **Knowledge graph + event bus** **[built at bus level, apps wiring in progress]** —
  verified records and observations, plus a Redis channel per tenant. Apps never call
  each other; they publish and subscribe.
- **Central logs** **[in progress]** — one engine endpoint per tenant returning every
  app's action log, hash-chained audit log and sidecar log, chains verified.
- **Intent and trust** **[built as a library, not yet a separate service]** — the
  auth-code authority; an agent is structurally unable to confirm its own intent.

## 6. The three flagship apps

| App | Status | What it does | The moment to show |
|---|---|---|---|
| **Helios Collect** (receivables) | **[built]**, deployed live, 10 scenarios | chases overdue invoices, negotiates extensions within a capped authority, routes inbound money by configuration | disputed invoice refused in code before any human sees it; an agent claiming 20,000 arrived on a 12,500 invoice is held against a ceiling it cannot move |
| **Helios Payables** | **[built]**, deploy-ready, 15 scenarios, 17 YAML rules | collects supplier bills, checks funds, pays on time, emails remittance, escalates exceptions; duplicates, changed payee details and unusual amounts are held or denied | owner approves a payment to a supplier whose bank details just changed and it is still refused; an agent that understates the amount to dodge a threshold is still held |
| **Helios Cash Planner** | **[in progress]** | 13-week cash forecast from how customers actually pay, early shortfall warnings, plain-language what-if, borrowing options ranked with the public rate cited | "you will be short on the 25th"; the forecast also becomes a *fact* inside Payables' gate, so paying a bill that creates a payroll shortfall is held with the date |

Plus a worked example, `invoice-payer`, built from the sentence "make me an app
that manages and pays my invoices", used as the on-ramp reference.

Cross-app story (bonus, after each app works alone): Collect agrees an extension →
verified graph node + event → Cash Planner re-forecasts → Payables holds a non-urgent
bill for rescheduling. No app calls another app.

## 7. The surprise task (platform extensibility)

Judges give a new app requirement after code lock. The path is: answer the intake,
generate the spec, scaffold, write the policy, validate, deploy. A rehearsal with the
sentence "approve employee expense claims against a department budget and reimburse
the approved ones" reached a validated app with all four controls passing (budget
ceiling held, duplicate claim refused, reimbursement needs owner intent, lying agent
caught) using only the two on-ramp documents. The defects it exposed are being fixed.

## 8. Data

Organisers' data plus public data; Tushare for Chinese market, company and macro
data. Used for reference rates (LPR, SHIBOR), company registry evidence when a
supplier announces new bank details, and industry collection periods as a prior for
customers with no history. Every market result carries provenance (publisher, date,
live/cache/fixture, stale flag). Quote data is structurally separated from money: a
rate is a float with a unit and a date; a balance is integer cents; the market module
cannot touch the ledger.

## 9. Safe claims and claims to avoid

Safe:
- Every side effect is gated by deterministic code; no LLM decides.
- Facts come from trusted sources; the agent cannot assert them.
- Policy is data, reviewed, and the app cannot rewrite its own policy after deploy.
- Hash-chained audit evidence; a single-use approval cannot be replayed (second
  approve of the same hold returns 404).
- Credentials never appear in manifests, logs, deploy responses or packages.
- Two apps built and run end to end through the platform; one deployed live through
  the engine with a working approval queue; the platform's own sidecar caught a
  duplicate payment on a live deploy.
- Hundreds of offline tests across the platform (800+ at last count on one branch).

Avoid, or qualify:
- Real money moves. It does not; payments are simulated with a mock ledger.
- Live Qwen in the loop. The driver exists and is pointed at Qwen; demos run on a
  deterministic scripted driver.
- Cross-app coordination between flagships. Proven at the bus level with a sample
  app; the flagship chain is scheduled.
- Investment advice. Rate comparisons are informational with a disclaimer; the
  platform gives no licensed advice.

## 10. Business framing hooks (facts the pitch can build on)

- **Platform, not app.** Each flagship is a plug-in; the surprise task is the proof
  that a new app costs an intake, a policy file and a deploy.
- **Governance as the product.** The audit manifest is the thing an owner, accountant
  or regulator reads. Apps that cannot explain a refusal do not qualify.
- **Per-tenant isolation**: one business = one tenant = its own network, core,
  bus, secrets and data volumes.
- **Compliance evidence by construction**: hash-chained logs that survive redeploys
  and undeploys.
- **Model-agnostic by design**: the driver is OpenAI-compatible; Qwen is the
  competition model, not a dependency.

## 11. Glossary

- **Proposal** — a side-effecting tool call awaiting adjudication.
- **Verdict** — approve / hold / deny, with the rule id and a reason.
- **Mandate / audit manifest** — the YAML policy: allowed tools, limits, what needs
  the owner.
- **Fact source** — trusted reader of the app's own data used by rules.
- **Hold / approval queue** — a paused action waiting for the owner; resume executes
  exactly that action.
- **Grant** — a signed, scoped, short-lived, single-use authorisation for a task.
- **Intent auth code** — single-use credential bound to amount, currency and payee;
  no settlement without it.
- **Sidecar** — the per-app platform gate that holds the execution secret.
- **Tenant core** — the per-business container running the tool service.
- **Golden base image** — prebuilt image with the platform; app layer is one copy.

## 12. Where to read more

`README.md`, `specs/helios-context.md` (competition and flagships),
`specs/architecture-mermaid.md`, `specs/decisions.md`, `DEMOFLOW.md` (demo stages,
test ladder, gaps), `HANDOFF.md` (verified flows and traps), `apps/*/README.md`
(scenario tables).

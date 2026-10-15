# Architecture Decisions

This document tracks current architectural decisions and unresolved questions.

## Decided

### Applications run independently
Generated applications contain their own UI, business logic, action agents, and audit agents.

Agents execute within their deployed applications rather than in a centralized Helios agent runtime.

### Agent builders are frameworks
The Action Agent Builder and Audit Agent Builder are reusable development toolkits used by Qwen to create agents.

The toolkit includes a shared agent runtime (agent loop, action interception, audit gate, logging, holds) that is packaged into each generated application. Helios maintains the runtime code; each application runs its own copy.

### Actions are proposals, decided by code
Action agents never perform side effects directly. Any tool call with a side effect becomes a **proposal**, which the audit gate approves, holds for the owner, or rejects.

Read-only tool calls run directly.

### Audit decisions are deterministic
The audit gate is plain, testable code checked against the app's **mandate**, not an LLM. It fails closed: unknown tools, malformed arguments, or unrecognized constraints are not allowed through.

### Agents operate under a mandate
Each agent has a mandate defining what it may do: allowed tools, limits (e.g. maximum extension length, contact frequency), and which actions need owner approval.

### Tools declare side effects
Every tool follows a shared contract: name, input schema, output schema, and a side-effect flag. The side-effect flag determines whether a call is gated.

### App-specific agent behavior is data
What differs between agents (instructions, tool list, mandate) is defined as data in an agent manifest, separate from the runtime. Qwen generates the manifest rather than writing agent logic from scratch.

### Agent loop is an OpenAI-compatible tool-calling loop driven by Qwen
The runtime is its own loop, not a Qwen-Agent subclass: `model_driver.py` posts
one chat completion over stdlib `urllib`, parses a single tool call per turn, and
hands the proposal to the audit bridge before anything executes. No file in the
repo imports `qwen_agent`, so "built on Qwen-Agent" was never true of the code.
Interception is structural — the driver returns a proposal and stops — rather
than a hook into someone else's loop, which is what keeps the gate unavoidable.

### The standalone audit agent is the authoritative in-app gate
The YAML-driven `audit_agent` (policy packs, deterministic rules, signed grants,
hash-chained evidence) decides what an app's agents may do.
`agent_toolkit.audit.AuditGate` is retired from the loop: the class stays
importable and is marked legacy in its docstring, so existing references do not
break, but no run path consults it. Two gates that can disagree is worse than
one. The engine's audit sidecar (`platform/deployment`) is a separate,
fact-free second gate holding the execution secret — it is not a substitute for
the in-app auditor, and the two are not interchangeable.

### Deployment contract is `platform/deployment`'s `helios.yaml`
One engine, imported from the Mac-mini build: a manifest plus an ASGI entry plus
`GET /health`, built and routed through the Docker Engine API. Apps adopt the
contract; where an app cannot, the engine is adapted rather than forked. This
answers "where are generated applications hosted" and "what is the deployment
artifact format" below.

### Qwen via DashScope for everything, legacy vendor removed
Qwen is the only generative model allowed, for coding and for in-app agents. The
driver targets DashScope's OpenAI-compatible endpoint (default model
`qwen3.8-max`), with `enable_thinking: false` sent only for `qwen3` hybrid ids.
All references to the previous vendor — its endpoints, model ids and vendor
env-var names — are removed wherever a file is touched.

### Money is simulated end to end
No real payment rails, anywhere. `fin_tools` runs a mock ledger with integer
cents; the payment tools imported from teammates ship with no provider. This is
a deliberate competition and safety constraint, not a placeholder to be filled in
later.

### Default app stack is FastAPI + SQLite + Jinja2/HTMX
Server-rendered pages over HTMX, not a Vite-built React bundle. Holds and
approvals live in-process with the agent runtime, so a single process on a single
origin keeps the approval flow same-origin with no CORS boundary — the property
the React choice was made for, achieved with less machinery. Money is integer
cents or decimal strings, never float, converted at boundaries only.

### Held state and replay guards persist to SQLite under the app's data dir
A hold survives a restart, and so does the guard that stops a spent grant being
replayed; both live in the app's own SQLite database rather than in memory or in
a shared platform service. Keeps an app independently deployable, which is what
"applications run independently" requires. **P2 implements this** — decided here
so the storage shape is not re-litigated then.

### Knowledge graph is shared
Helios hosts a central knowledge graph that applications can use to exchange relevant state and context.

### Qwen handles application generation
Qwen uses Helios's development interfaces to generate applications and initiate deployment.

### Market data is quote data, and never becomes money
Live market data — prices, rates, index levels — is **quote data**: a float denominated in its own unit, meaningful only as of a timestamp. It is categorically distinct from **money**, which in this system is integer cents and never a float.

This was resolved because the two rules collided in practice. Tushare returns floats (verified live: `close` 11.5, `pct_chg` -0.605), while `contracts.World` mandates "everything money-shaped is integer cents. No floats, ever." Both are correct, because they describe different things:

- A share price, interest rate or index level is a **quote**. It is a float, it carries a unit and an `as_of` date, and it is read-only.
- A **balance** or a **ledger entry** is money. It is integer cents. It is what `World.balances` and `World.ledger` hold.

**Market data must never reach `World.balances` or `World.ledger`.** The separation is enforced structurally in `fin_tools/market_data.py`, not by convention:

1. The module never imports `World`, `Payments`, or anything able to write ledger or balance state.
2. Every `market.*` tool declares `side_effects=False`, so it runs directly and can never be a proposal that mutates state.
3. No output field is named `*_cents`. That name would imply integer money and invite a caller to add a share price to a balance. Amounts carry their real unit instead (`CNY per share`, `手 / lots of 100 shares`, `千元 / thousands of CNY`).

An application that needs to *use* a price — say, to value a holding — converts explicitly at its own boundary, in its own code, and owns the rounding. The platform never silently coerces a quote into money.

Corollary, ratified alongside it: an unavailable quote is an **outage**, not a decision. A missing token, a timeout, or an upstream refusal is reported as `unavailable`, never as `rejected`, so the action log never records a gate decision that no gate made.

## Tentative

### Hybrid financial tools
Some financial capabilities may be packaged into applications, while others are provided through centrally managed APIs.

Managed APIs are preferred for operations that benefit from centralized maintenance, security, or regulatory updates.

### Shared trust services
Helios may centrally enforce sensitive financial permissions and approval requirements, while app-specific audit agents run locally.

The local audit gate checks each action against the app's mandate. Sensitive operations may additionally require a check by Helios's Intent & Trust Service.

### Shared ledger
A shared ledger may store verified financial events and records used across applications.

### Append-only action log
Every tool call, proposal, decision, and result is recorded in an append-only
log. **Decided in practice:** the audit side is hash-chained and verified
(`AuditLog.verify_chain`), so tampering or deletion is detectable. What remains
tentative is retention — how long logs are kept and who may read them.

### Holds and resumption
When an action is held, the agent's state (conversation history and the
proposal) is saved. Once the owner decides, the agent resumes from where it
stopped. An approval applies only to the exact action that was held.
**Decided in practice:** `GatedAgentRuntime.resume` implements this, and
`selfcheck` asserts the two packages agree. What remains is where the held state
lives across a restart — see *Held state and replay guards persist to SQLite*
above (P2).

## Open Questions

- Where is held-agent state stored — **answered**: SQLite under the app's data
  dir (P2 implements).
- Where will generated applications be hosted — **answered**: the
  `platform/deployment` engine, containers via the Docker Engine API.
- What is the deployment artifact format — **answered**: a bundle with
  `helios.yaml`, an ASGI entry and `GET /health`; the engine injects the
  Dockerfile.
- How will the knowledge graph distinguish authoritative records from agent observations?
- How will applications subscribe to cross-app updates?
- Which financial operations should be managed APIs versus packaged SDK tools?
  - **Still open.** This session implemented live market data as a *packaged*
    adapter inside `fin_tools/market_data.py` rather than as a managed API, for
    the reasons recorded in §6 of `specs/financial-tools-service.md` and
    `specs/tech-stack.md`. That is an implementation choice for one capability,
    not a resolution of the general question.
  - The teammate proposal in
    `OTHER-TEAMMATE-STUFF/forecasting-simulation/services/financial-api/` — "math
    is packaged, data-fed defaults are managed" — remains **unratified**. Their
    §10 spec updates never landed in `specs/`. Do not treat that split as
    decided, and do not cite it as precedent.
  - Note the practical asymmetry that makes this question worth answering soon:
    a packaged data adapter means every deployed app holds its own `TUSHARE_TOKEN`
    and its own quota. A managed API would hold the credential once. That is a
    security and cost question, not only an architectural preference.
  - **Coordination flag for whoever owns the teammate packages:**
    `OTHER-TEAMMATE-STUFF/forecasting-simulation/services/financial-api/` is
    already a data-fetching service with the same job as `fin_tools/market_data.py`
    (its `market.py` adapter). It is a candidate to be **promoted into `platform/`
    and shared** rather than left duplicated across sessions. Not done here — this
    session built its own adapter (OPTION 1) and did not edit inside
    `OTHER-TEAMMATE-STUFF`. If the group decides market data belongs behind a
    managed API, that service is the thing to promote; its endpoint/field names
    would still need the live-API correction recorded in `specs/tech-stack.md`
    first.
- How will applications authenticate with shared Helios services?
- What platform-wide limits should mandates be capped by?

## Updating Decisions

When a design choice is resolved, move it to **Decided** and update the relevant architecture or interface documentation.

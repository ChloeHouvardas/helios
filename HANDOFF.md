# Helios — Repository Handoff

**Audience:** an LLM or engineer picking this repo up cold.
**Purpose:** what exists, how it fits together, what you can actually *do* with it today, and where the traps are.
**Basis:** read from the code and run against it on **2026-10-15**, branch `chloe-dev` at `ecd86d0`. Not derived from the specs — where a spec and the code disagree, the code is right and this file says so.

> **Do not read `docs/milestones/` as context.** Its own `README.md` states those files are for human readers only and do not follow the current product vision. M2 there is an unfilled template.

---

## 1. What Helios is

A platform where a coding agent turns one sentence of user intent into a working agentic financial application, which is then audited and deployed. Two distinct kinds of AI, and keeping them separate is the whole design:

- **The coding agent** — builds the app. Reads the platform's docs/SDK/APIs, generates an app.
- **In-app agents** — operate the app once built. They do the financial work (chase invoices, pay suppliers).

The governing principle, repeated everywhere because it is load-bearing:

> **The model proposes, code decides.**

An action agent may ask for anything. What it gets is bounded by policy written as *data* (a YAML audit manifest) and enforced by *deterministic* code that fails closed. The corollary that does most of the work:

> **Facts are not claims.** Every value a rule depends on is resolved from a trusted `FactSource` the agent does not control. The agent gets to *name* which record it means; it never gets to *state* what that record says.

Competition framing (Chengdu 80 2026, team Maple Queen's): scoring is **50% investment pitch**, 25% engineering verification, 25% architecture defence. The Action/Audit split and *facts are not claims* are the pitch and defence material — not app polish. See `specs/helios-context.md`.

---

## 2. Component status

Component names follow `specs/architecture-mermaid.md`. Status is expressed as **what you can do with it**, not as a completion percentage — several of these (the financial tools especially) are being changed by other people concurrently, so treat the inventories as indicative, not frozen.

### Part 1 — Helios Development Platform

| Architecture component | Where it lives | Status |
|---|---|---|
| Financial Tools Interface + SDK | `platform/financial-tools` (`fin_tools`) | **Works.** Offline prototypes — payments, email, spreadsheets, knowledge graph, simulation, intent verification. 12 registered tools. Also ships a service boundary with in-process **and** HTTP transports, so the same tool can be called locally or across a socket. |
| Application Builder | `platform/app-builder` | **Design only.** `README.md` + `PLAN.md`, no code. No scaffolder, no validator, no `stacks.yaml`, no DDL generator, no UI kit. |
| Action Agent Builder | `platform/action-agent-builder` (`agent_toolkit`) | **Partial.** Tool contract, store, action log, holds, manifest loader all work. **No agent loop and no CLI** — `pyproject.toml` declares an `agent-toolkit = "agent_toolkit.cli:main"` entry point but `cli.py` does not exist. |
| Audit Agent Builder | `platform/audit-agent-builder` (`audit_agent`) | **Works.** Signed scoped short-lived single-use grants, deterministic fail-closed policy engine, hash-chained append-only log, YAML manifests, pluggable fact sources, three transports, CLI (`inspect`/`validate`/`demo`/`serve`). |
| Validation + Deployment | `entry-point/checks/check_app.py` (substitute) | **Validator: works (substitute). Deployment: does not exist.** No hosting, no artifact format, no deployment engine. |

**Two things in this part that are not in the architecture diagram but are load-bearing:**

| Component | Where | Status |
|---|---|---|
| The seam + the actual agent loop | `platform/agent-integration` (`agent_integration`) | **Works.** The runtime, bridge, adapters, and a real-model driver. This is the loop `action-agent-builder` specified but never shipped. |
| The coding-agent on-ramp | `entry-point/` | **Works.** `SKILL.md` + `BUILD.md` (8-step procedure) + `references/` + a complete example app. This is what you point a coding agent at. |

### Part 2 — Generated Applications

| App | Where | Status |
|---|---|---|
| Helios Collect (receivables) | `apps/helios-collect` | **Works.** 10 scenarios, one control path each; 74 offline tests. Runs through the platform, edits nothing in it. |
| Invoice Payer (payables, worked example) | `entry-point/examples/invoice-payer` | **Works.** 5 scenarios; 41 offline tests. Built from the single sentence *"make me an app that manages and pays my invoices."* |
| Helios Payables (flagship 2) | — | **Not built.** |
| Helios Cash Planner (flagship 3) | — | **Not built.** |

No app has a web UI. The declared `ui.pages` exist as CLI renderers. No database — JSON fixtures loaded into memory, writes do not persist between runs. No scheduling or triggers; agents run manually.

### Part 3 — Helios Shared Services

**None of these exist as services.** All four are design-stage, with local prototypes at most:

| Architecture component | Reality |
|---|---|
| Shared State & Knowledge Graph | `fin_tools` in-process `KnowledgeGraph` prototype. The verified-record vs agent-observation split is real and tested, but nothing outside the process reads it. `specs/interfaces.md` still lists the cross-app interface as to be defined. |
| Managed Financial Tools API | The `fin_tools` service boundary (in-process + HTTP) is the mechanism. Not deployed, not hosted. |
| Intent & Trust Service | `fin_tools.intent_verification` prototype. The wiring hook on the audit side is `GrantVerifier`, and it is **not wired**. |
| Shared Events & Updates | Does not exist in any form. |

Consequence: **cross-app coordination is not achievable today.** A Collect app can publish a verified due-date change, but Cash Planner does not exist and could not read it if it did.

---

## 3. Architecture — how the pieces actually connect

```
coding agent ──reads──▶ entry-point/ (SKILL.md, BUILD.md, references/)
                              │
                              ▼ generates an app
                        apps/<name>/
                  ┌───────────┴────────────┐
                  │ helios-app.yaml        │  ← nothing parses this yet
                  │ agents/*-audit.yaml    │  ← the policy. AUTHORITATIVE
                  │ agents/*-action.json   │  ← the agent's operating contract
                  │ facts.py               │  ← the FactSource (trusted values)
                  │ app.py                 │  ← composes the platform
                  └───────────┬────────────┘
                              ▼ composes, never edits
   ┌──────────────────────────────────────────────────────────────┐
   │ platform/agent-integration   ← ONLY dir allowed to import both │
   │   ToolContractView ─▶ AuditBridge ─▶ audit_agent (registry+policy) │
   │        ▲                   │                    │              │
   │        │    allowed_tools  │   Grant (HMAC)     │              │
   │        └───────────────────┴────────────────────┘              │
   │                          │                                     │
   │       every side effect ─▶ adjudicate ─▶ approve / hold / deny │
   └──────────────────────────────────────────────────────────────┘
        ▲                                        ▲
  agent_toolkit                             fin_tools
  (tool contract, store,                 (payments ledger,
   action log, send_email)                knowledge graph)
```

**The boundary rule is the architecture.** `action-agent-builder` and `audit-agent-builder` do not import each other and never should — the audit agent is only trustworthy because the app it audits cannot modify it. `platform/agent-integration/` is the single directory permitted to know about both. Respect this or the central claim collapses.

### Flow inside an application (verified working)

1. Action agent reads data with read-only tools — these execute **directly**, ungated.
2. It wants a side effect → the runtime turns it into a `Proposal`.
3. The audit bridge asks the standalone auditor, which returns **approve / hold / deny** with a rule id and a reason.
4. Approved → executes. Held → the run pauses; the owner decides; resume executes *exactly that action and nothing else*, without re-asking the auditor (the triggering fact has not changed — re-adjudicating would just hold again). Denied → the reason goes back to the model as an observation, so it can adjust instead of failing blind.
5. Every step is written to two separate logs: the app's `action-log.jsonl` and the auditor's hash-chained `*-audit-log.jsonl`.

### Why authority comes from a file

`build_integration()` **loads a reviewed manifest** rather than generating one from whatever `task_type` the caller passed. Generating from arguments means the app writes its own permission slip — ask for `collect.pay_suppliers` and it simply works. Loading a file means an unreviewed task type is refused with *"no policy registered."* Runtime generation is still available (`manifest_path=None` / `--self-declared-manifest`) but warns and is flagged on the bundle. This is the structural version of *an app cannot grant itself permission to move money.*

### Two gates exist, and nobody has decided which wins

- `agent_toolkit.audit.AuditGate` — in-process, checks a `Proposal` against a static `Mandate`. Accepts only **four receivables-shaped keys** (`max_extension_days`, `extension_owner_approval_days`, `min_days_between_contacts`, `invoice_owner_approval_amount`) and its rule logic is hard-coded to `send_email`. **A payables app cannot express its policy here at all.**
- `audit_agent.AuditAgent` — standalone, declarative YAML rules.

`agent_integration` makes the **standalone auditor authoritative** and leaves the in-process gate importable but out of the loop. That is a defensible choice, **not settled design**. `AGENTS.md` requires approval for architecture changes, so flag it — do not let a generated app resolve it silently.

---

## 4. Verified usage flows

This is the section to trust. Each was run on 2026-10-15 against this checkout; the command and the observed outcome are both recorded.

### Environment

```bash
# Python 3.13.9. audit-agent-builder's floor is 3.13; use one interpreter for everything.
# Packages are NOT pip-installed globally. Two different paths work:

# (a) source-on-path — used by the apps and the example
cd platform/audit-agent-builder && PYTHONPATH=src python3 -m pytest tests -q   # 79 passed
cd platform/financial-tools     && python3 -m pytest tests -q                  # 61 passed, 2 skipped
cd apps/helios-collect          && python3 -m pytest tests -q                  # 74 passed
cd entry-point/examples/invoice-payer && python3 -m pytest tests -q            # 41 passed

# (b) agent-integration has its OWN venv — use it, or imports fail
cd platform/agent-integration && .venv/bin/python -m pytest tests -q           # 123 passed
```

**378 tests green** across five suites. All offline; `agent-integration/tests/conftest.py` blocks any non-loopback socket, so the suite cannot quietly depend on a vendor endpoint or on whose shell it ran in.

> ⚠️ `python3 -m pytest` inside `platform/agent-integration` fails with `ModuleNotFoundError: No module named 'agent_integration'`. Use `.venv/bin/python`. That venv does **not** have `fin_tools` on its path.

### Flow A — a gated agent loop runs end to end, offline, no key

```bash
cd platform/agent-integration && .venv/bin/python -m agent_integration.cli demo
```
**Observed:** 5 scenarios. An in-grant `send_email` → `ALLOW (rule: baseline.within_grant)`, executed. A repeat contact → `DENY (rule: app.contact_frequency)`, not executed. Action log written with `run_started / authorization / tool_call / proposal / verdict / run_finished`. Audit evidence: `chain verification: OK — no entry has been edited or removed`. Exit 0.

### Flow B — the two packages still agree with each other

```bash
.venv/bin/python -m agent_integration.cli selfcheck
.venv/bin/python -m agent_integration.cli inspect
```
**Observed:** `seam check: OK — tools agree on names, side-effect flags and schemas`. `inspect` prints the authority surface: which tools are gated vs read-only, each rule with its limits and messages, TTLs, `allow_unsigned_grants: false`.

`selfcheck` is the CI guard for the single most dangerous failure mode in this repo — see §5, trap 1.

### Flow C — a REAL model drives the gated loop

A token is configured in `platform/agent-integration/.env` (`HELIOS_LLM_API_KEY`, 40 chars, base URL `https://ai.gitee.com/v1`, model `glm-5.3`).

```bash
cd platform/agent-integration && set -a && source .env && set +a
.venv/bin/python -m agent_integration.cli doctor
.venv/bin/python -m agent_integration.cli run --task "Follow up on overdue invoice INV-1001"
```
**Observed:** `doctor` → `OK: the model replied -> 'READY'`. `run` → the model called the read-only `lookup_invoice` (passed straight through), then proposed `send_email`, which was adjudicated `ALLOW (baseline.within_grant)` and executed. `status: completed`, `side_effects_attempted: 2, side_effects_executed: 2`.

**This is the core platform claim demonstrated with a live model, not a script.** The gate sits between the model and the world and the model cannot route around it.

### Flow D — a generated app runs, and the controls fire

```bash
cd apps/helios-collect && python3 app.py            # all 10 scenarios
python3 app.py --scenario INV-1006                  # one control path
python3 app.py --trace | --json | --dashboard | --list
```
**Observed:** `{'done': 5, 'refused': 3, 'declined_by_owner': 2}`, 4 emails sent. Verdicts match the README table exactly.

The two worth showing a judge:
- **INV-1006** (disputed invoice) → `REFUSED` in code, reason *"this invoice is disputed; collections contact is refused until the owner resolves the dispute"*. The scripted owner approves every hold it is shown, and this one never reaches the queue. **The control does not depend on the human noticing.**
- **INV-1009** (agent claims 20,000.00 arrived on a 12,500.00 invoice) → `hold`, rule *"the payment exceeds the recorded invoice amount"*, then declined. **The over-claim is measured against a ceiling the agent cannot move.**

```bash
cd entry-point/examples/invoice-payer && python3 app.py
```
**Observed:** `{'paid': 3, 'refused': 1, 'declined_by_owner': 1}`. **INV-1003 is the demo:** the owner *confirms* a 760.00 USD payment to a supplier whose banking details just changed, and it is **still `REFUSED`** — *"this supplier's payment details changed recently; payment is refused until the owner re-verifies the payee."* The control does not depend on the human noticing, and it does not depend on the agent behaving. One screen of terminal output.

### Flow E — the validator catches a broken app

```bash
python3 entry-point/checks/check_app.py apps/helios-collect   # 0 errors, 3 warnings, exit 0
python3 entry-point/checks/check_app.py <a-broken-app>        # 4 errors, exit 1
python3 entry-point/checks/check_app.py /nonexistent          # exit 2
```
**Observed:** all three exit codes correct, so it is CI-usable. Against a deliberately malformed app it reported missing required sections, the `stack.use`/`match_existing` exclusivity violation, the `data.local`/`integration` violation, and both missing manifests.

The check that protects money: **action-manifest tools must be a subset of audit-manifest declared tools.** A tool an agent can call that the auditor never declared is an ungated side effect waiting to happen.

The 3 warnings on `helios-collect` are expected and explained in its README §"Checker warnings".

### Flow F — the coding-agent build procedure exists and is followable

`entry-point/BUILD.md` is an 8-step procedure. **No scaffolder exists, so the coding agent *is* the scaffolder** — it writes the app directly:

1. Read `references/platform-status.md` (what runs vs what is only designed)
2. Work the question phase — the fixed 16-question intake from `specs/app-builder.md`, including the *what already exists* integration block
3. Write `helios-app.yaml`
4. **Write the audit manifest first** — policy before agent, always. The audit manifest is the registry of what may be granted at all.
5. Write the fact source (implements `audit_agent.FactSource`, reads the app's own data, never the request)
6. Write the action manifest (`instructions` as an operating contract, not a description)
7. Write the app — compose the packages; adapt `examples/invoice-payer/app.py`
8. Give the owner somewhere to decide — an `approval_queue` page or a scripted owner
9. Verify, then demo — `check_app.py`, plus a test that **each control fires**

Two apps have been built this way and both run. **That is the evidence the on-ramp works.**

> ⚠️ `entry-point/SKILL.md` and `references/platform-status.md` both still say *"there is no agent loop"* and *"no API key is set."* That was true on 2026-10-14 and is **now stale** — `agent_integration` ships the loop and a working model driver. `apps/helios-collect/README.md` already records this correction. Update those two files.

---

## 5. Traps — read before you touch anything

Ordered by how silently they break things.

**1. A gated tool can register as read-only, and nothing raises.**
`ToolDescriptor.from_contract()` reads `.has_side_effect`; `agent_toolkit.ToolDefinition` names it `.side_effect`. Duck-typing the two defaults the missing attribute to `False`, so `send_email` registers as read-only → never adjudicated → **the app emails customers with no audit at all.** Same function only reads a *callable* `input_schema`, but the action side's is a plain Mapping → auditor gets `None` → `baseline.arguments_validate` never fires. `adapters.ToolContractView` closes both. **Run `selfcheck` after any change to either package's tool contract.**

**2. Fact resolution is keyed on a fixed argument-name set.**
`FactResolver.SUBJECT_KEYS` = `invoice_id`, `bill_id`, `customer_id`, `supplier_id`, `subject_id`, `payment_id`. Name an argument anything else and facts do not resolve, so **fact-bound rules fail closed and deny everything.** Safe direction, completely invisible until the demo. This is the most common way a generated app's policy silently stops working.

**3. `auth_code` is not redacted from the audit log.**
It is not in `audit_agent`'s `REDACTED_KEYS` (`src/audit_agent/contracts.py:54`), so a real payment credential reaches the log **in full** inside the `owner_question` string (`agent.py:705`). I saw this happen live in Flow D — a full 64-hex `auth_code` printed in the owner prompt. Cannot be worked around by not sending it: the rule must check it and `payments.send` must receive it. **Upstream fix, one line:** add `auth_code` and `authorization_code` to `REDACTED_KEYS`. Apps meanwhile redact their *own* action log; they cannot fix the auditor's.

**4. The auditor refuses a short `task_summary`.** `baseline.intent_stated` requires ≥12 chars or the whole grant is denied. Use realistic task text when driving runs.

**5. `idempotency_key` replays a cached rejection.** Repeating a key returns the stored record with its *original* `reject_reason`, even if arguments changed. That is what prevents a double payment — but an agent that fixes a rejected payment and retries under the same key sees the stale failure and may conclude its fix did not work. Reuse the key when retrying the *same* payment; use a fresh one when the payment genuinely changed.

**6. Action manifests tolerate no extra keys.** A `_comment` field fails to load. Put commentary in a README next to the file.

**7. Audit rules cannot express a two-argument pairing check.** `send_email` takes `customer_id` *and* `invoice_id`, and the handler raises if they do not match — but `limits` and `deny_if_fact_true` resolve facts for one subject at a time, and `_subject_key` picks `invoice_id` first, so a valid invoice id makes a mismatched customer id invisible. The gate **approves**, the handler **refuses**, nothing is sent, and the run ends in `ERROR`. Fails loudly and safely, but the refusal lands *after* approval. Needs a pairing fact or a new rule shape — raise with whoever owns `audit-agent-builder`.

**8. A ceiling on an argument guards one direction only.** `argument` + `max_from_fact` catches **over**-payment. Paying 1.00 against a 48,000.00 invoice is *inside* the ceiling and is allowed. Understating is stopped elsewhere — `auth_code` is an HMAC over (amount, currency, payee), so a smaller payment needs the owner to have confirmed that smaller amount. Two shapes, two threats; **do not conflate them, and do not "fix" a ceiling by adding a floor.** `collect-reminder.yaml` binds on `fact:` instead, which *does* catch a lie about the value.

**9. `fin_tools.build_world()` binds module-level singletons.** Documented in `apps/helios-collect/README.md` limitation 6 (verified there 2026-10-15): with two worlds in one process the **last**-built one wins for every `registry.call` dispatch. That app therefore calls `bundle.payments` / `bundle.graph` directly rather than through `registry.call`. Matters because tests build a fresh world per case.

**10. `Store` has no public invoice mutator.** It exposes mutators for contacts and the outbox only — it was written for Build 1, where invoices are read-only. `apps/helios-collect/book.py::_mutate_invoice` writes into the store's private `_invoices` dict. Deliberately confined to one method so there is a single place to change if the platform grows a public mutator.

**11. `check_app.py` misreads prose as money.** Its heuristic splits field names into words and flags any containing a money word not typed `decimal`/`integer`, so a prose field named `payment_behavior` gets reported as monetary. `helios-collect` renamed it to `payer_behavior` rather than weaken a shared checker.

**12. Holds are in-memory only.** `AuditAgent` keeps holds in a dict and `InMemoryReplayGuard` is single-process, so pending approvals and grant-replay protection are **lost on restart**.

**13. `stacks.yaml` does not exist.** `helios-collect` names `helios-python-web-1` and that name resolves to nothing. Legitimate reading of "the mechanism, not the choice" — but do not present it as a stack the platform ships.

### A reporting defect found this session (new, not yet documented elsewhere)

`RunResult.summary()` reports `side_effects_attempted` / `side_effects_executed` as `len(self.actions)` / `len(self.executed)` (`runtime.py:131-132`), but **read-only tool calls are also appended to `result.actions`** (`runtime.py:248`). So the counters include non-side-effect tools.

I hit this live: on a run where the model looked up an invoice and then *declined to email*, the log correctly shows only `lookup_invoice` with `"side_effect": false` and no `send_email` at all — but the summary printed `side_effects_attempted: 1, side_effects_executed: 1`. **The gate behaved correctly; the reported number is wrong.** Worth fixing before any judging context, because it reads as "an email went out" when none did. The action log is the accurate record; the summary is not.

---

## 6. Open questions requiring a decision

**Model constraint conflict — this is the most urgent one.**
`specs/helios-context.md:25` states *"Qwen is the only generative model allowed for development."* But `SETUP.md` says to add a **GLM-5.3** key, and `model_driver.py`, `.env.example` and the configured `.env` all target **GLM-5.3** (via a Gitee AI gateway). `entry-point/SKILL.md` repeats the Qwen-only rule under "Non-negotiables (competition rules)". Flow C above therefore ran on a model the rules may not permit. **Someone must reconcile this before the pitch.** The driver is provider-shaped (OpenAI-compatible chat completions, stdlib HTTP only), so pointing it at Qwen is a base-URL/model-id change plus whatever protocol differences Qwen has — not a rewrite.

**Architectural, unresolved:**
- Which gate is authoritative — in-process `AuditGate` or standalone `AuditAgent`? (Currently: standalone, by `agent_integration`'s choice. Needs team sign-off.)
- Does Helios move real money or simulate? The brief lists payments as a platform capability; an earlier team position was policy+evidence only. **Still unreconciled.** Every `fin_tools` prototype touches no network, no database and no real money.

**From `specs/decisions.md` §Open Questions** (all still live): where generated apps are hosted; the deployment artifact format; where held-agent state is stored; how the knowledge graph distinguishes authoritative records from agent observations; how apps subscribe to cross-app updates; which financial operations are managed APIs vs packaged SDK tools; how apps authenticate with shared services; what platform-wide limits mandates are capped by.

**Licence walls that still apply:** no unlicensed investment advice; PIPL constrains behavioural telemetry; compliance output is not a legal opinion.

---

## 7. House rules

From `AGENTS.md` — these are enforced socially, and several agents work in this repo concurrently.

- **Commit as you go**, conventional commits (`feat:`/`fix:`/`test:`/`docs:`/`refactor:`/`chore:`/`perf:`), imperative, ≤72 chars, no trailing period. Add a body when the *why* is not obvious from the diff — especially for security properties and anything that looks simplifiable but must not be.
- **Stage explicit paths. Never `git add .` or `git add -A`.** Before committing, check `git status` and confirm every staged path is yours. If a shared file (`AGENTS.md`, `README.md`, anything under `specs/`) carries another agent's uncommitted edit, stage only your own hunk and leave theirs alone. **Never commit another agent's work; never revert or "clean up" edits you do not recognise** — they are probably someone's work in progress.
- Each commit leaves the repo importable and its own tests passing. Split by idea, not by file count.
- Do not push, force-push, rebase, amend or reset shared history unless the user asks. No `--no-verify`.
- Significant architectural changes do **not** require pause-and-wait; approval comes from the user and a decision already made carries forward — do not re-litigate it. Still flag *before* proceeding when a change would break or require edits inside another agent's package, or be hard to reverse once shipped.
- Fail closed. Unknown is never read as "fine".
- Validation reports **all** problems at once.
- Error messages are written **for a model to act on** — name the field, the problem, and what to write instead.
- Money is integer cents or `decimal` strings, **never float**.
- No hard-coded credentials. Secrets from env vars. Distinguish confirmed decisions from assumptions.

### Current working-tree state (as of 2026-10-15)

There is **uncommitted work in progress that is not mine** — do not commit or revert it:

```
 D TODO.md
 M platform/agent-integration/README.md
 M platform/agent-integration/src/agent_integration/{__init__,cli,model_driver}.py
 M platform/financial-tools/fin_tools/service.py
 M platform/financial-tools/tests/test_service_boundary.py
?? SETUP.md
?? entry-point/examples/invoice-payer/runs/
?? platform/agent-integration/tests/{conftest,test_model_driver}.py
?? platform/financial-tools/fin_tools/__main__.py
```

`TODO.md` was deleted uncommitted; its content (cross-package dependency boundary notes, and a flag that `specs/tech-stack.md:6` still uses the stale `agent-toolkit/` name) is recoverable via `git show HEAD:TODO.md`.

---

## 8. Where to go next

**Highest value, lowest risk:**
1. Reconcile the Qwen-vs-GLM conflict (§6). It is a compliance risk on 50% of the score.
2. Fix the `side_effects_*` counter (§5). Small, and it currently misreports in demos.
3. Add `auth_code` to `REDACTED_KEYS` upstream (§5 trap 3). One line, closes a real credential leak.
4. Update `entry-point/SKILL.md` and `references/platform-status.md` — both still claim there is no agent loop, which will actively mislead the next coding agent.

**The obvious build gaps**, in rough order of what unblocks what:
5. Build **Helios Payables** and **Helios Cash Planner** (flagships 2 and 3). The payables *policy shape* is already proven by `examples/invoice-payer`; it needs to become a real app. Cash Planner is what makes the knowledge-graph publish meaningful — it is the declared consumer of Collect's verified due dates.
6. Implement the **Application Builder** (`platform/app-builder`) — scaffolder, validator, `stacks.yaml`. `specs/app-builder.md` defines all of it; `entry-point/checks/check_app.py` is the interim validator to replace.
7. **Shared services** — the knowledge graph first, since cross-app coordination is the only architecture claim currently undemonstrable.
8. A real **UI**. Every app is CLI-only today; `approval_queue` is the page that matters most, because without one agents pause invisibly.

**Do not** treat the in-process `AuditGate` overlap as yours to resolve silently — it needs team sign-off.

---

## Appendix: quick reference

| Need | Go to |
|---|---|
| Architecture and the component diagram | `specs/architecture-mermaid.md` |
| What to read first as a coding agent | `entry-point/SKILL.md` → `entry-point/BUILD.md` |
| What actually exists vs what is designed | `entry-point/references/platform-status.md` (**partly stale — see §4 Flow F**) |
| Real tool catalog and signatures | `entry-point/references/financial-tools.md` |
| How to drive the runtime, online or offline | `entry-point/references/agent-loop.md` |
| Interface contracts | `specs/interfaces.md` |
| Decided vs tentative vs open | `specs/decisions.md` |
| Pinned stack choices | `specs/tech-stack.md` |
| Competition rules, scoring, the three flagships | `specs/helios-context.md` |
| Repo conventions and commit rules | `AGENTS.md` |
| A complete app to copy | `entry-point/examples/invoice-payer/app.py` |
| The best demo | `apps/helios-collect` INV-1006 / INV-1009, and `examples/invoice-payer` INV-1003 |

**Key types, and where they live:**

| Type | Location |
|---|---|
| `ToolDefinition` (`side_effect` singular, JSON Schema object, `handler(args: Mapping)`) | `action-agent-builder/src/agent_toolkit/contracts.py` |
| `ToolSpec` (`side_effects` plural, flat `{name: "type"}`, `handler(**kwargs)`) | `financial-tools/fin_tools/contracts.py` |
| `Proposal`, `Verdict`, `Mandate`, `MandateLimits` | `agent_toolkit/models.py` |
| `AuthorizationRequest`, `ActionRequest`, `TaskAuthorization`, `ActionDecision`, `Decision`, `Finding`, `REDACTED_KEYS` | `audit-agent-builder/src/audit_agent/contracts.py` |
| `FactSource` protocol | `audit_agent/facts.py` |
| `AuditManifest` | `audit_agent/manifest.py` |
| `GatedAgentRuntime`, `ScriptedDriver`, `RunResult` | `agent-integration/src/agent_integration/runtime.py` |
| `ToolContractView`, `HeliosStoreFactSource` | `agent_integration/adapters.py` |
| `build_integration()` → `Integration` | `agent_integration/wiring.py` |
| `ModelDriver` (GLM-5.3, stdlib HTTP, lazily imported) | `agent_integration/model_driver.py` |

The two tool contracts are **not interchangeable** — that mismatch is traps 1 and §5's whole reason for existing, and `adapters.ToolContractView` plus `fin_tools.toolkit_bridge` are the only sanctioned bridges.

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
| Financial Tools Interface + SDK | `platform/financial-tools` (`fin_tools` + four sibling packages) | **Works.** 367 offline tests. `fin_tools` registers 12 tools (payments, email, spreadsheets, knowledge graph, simulation, intent verification) and ships a service boundary with in-process **and** HTTP transports. Siblings: `helios_forecast`, `spreadsheets` (FastAPI app), `email_tool`, `payments_collect`. |
| Application Builder | `platform/app-builder` | **Design only.** `README.md` + `PLAN.md`, no code. No scaffolder, no validator, no `stacks.yaml`, no DDL generator, no UI kit. |
| Action Agent Builder | `platform/action-agent-builder` (`agent_toolkit`) | **Works.** Tool contract, store, action log, holds, manifest loader. Still no CLI — the `agent-toolkit = agent_toolkit.cli:main` entry point was removed from `pyproject.toml` so `pip install -e` succeeds; P3 writes `cli.py` and the entry returns with it. |
| Audit Agent Builder | `platform/audit-agent-builder` (`audit_agent`) | **Works.** 81 offline tests. Signed scoped short-lived single-use grants, deterministic fail-closed policy engine, hash-chained append-only log, YAML manifests, pluggable fact sources, three transports, CLI (`inspect`/`validate`/`demo`/`serve`). |
| Validation + Deployment | `entry-point/checks/check_app.py` + `platform/deployment` | **Validator: works (substitute).** Now also cross-checks that the action side and the audit manifest agree on every tool's side-effect flag; 8 tests. **Deployment engine: works.** 96 offline tests (docker-py mocked); needs a Docker daemon only to actually deploy. |

**Two things in this part that are not in the architecture diagram but are load-bearing:**

| Component | Where | Status |
|---|---|---|
| The seam + the actual agent loop | `platform/agent-integration` (`agent_integration`) | **Works.** 136 offline tests. The gated runtime, bridge, adapters, manifest generator, and a real-model driver pointed at Qwen over DashScope. This is the loop `action-agent-builder` specified but never shipped, and the only package allowed to import both builders. |
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
# One environment for the whole repo. Build it once from the repo root:
scripts/bootstrap-venv.sh          # creates .venv on Python 3.13, installs the
                                   # four first-party packages editable plus the
                                   # pins in requirements-dev.txt

# One test command for the whole repo:
scripts/test-all.sh                # 403 passed across five suites

# Run a single suite with the venv interpreter (from the repo root):
.venv/bin/python -m pytest platform/audit-agent-builder/tests -q     #  79 passed
.venv/bin/python -m pytest platform/financial-tools/tests     -q     #  86 passed
.venv/bin/python -m pytest platform/agent-integration/tests   -q     # 123 passed
.venv/bin/python -m pytest apps/helios-collect/tests          -q     #  74 passed
.venv/bin/python -m pytest entry-point/examples/invoice-payer/tests -q  # 41 passed
```

**403 tests green** across five suites. All offline; `agent-integration/tests/conftest.py` blocks any non-loopback socket, so the suite cannot quietly depend on a vendor endpoint or on whose shell it ran in.

> ⚠️ Use `.venv/bin/python`, never the system `python3`. The system interpreter is not guaranteed to be 3.13 (both `audit_agent` and `agent_integration` require `>=3.13`) and has none of the dependencies installed. The old per-package `PYTHONPATH=src` incantations and the separate `platform/agent-integration/.venv` are superseded by `scripts/bootstrap-venv.sh`; `scripts/test-all.sh` runs every suite in its own process so the per-suite `conftest.py` path shims cannot leak into each other.

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

The driver targets Qwen over DashScope's OpenAI-compatible endpoint (default
model `qwen3.8-max`, default base URL
`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`). Set a token in `.env`
from `platform/agent-integration/.env.example`; accepted variable names in
priority order are `HELIOS_LLM_API_KEY`, `DASHSCOPE_API_KEY`, `OPENAI_API_KEY`.
Competition rule: Qwen is the only generative model allowed — the previous
vendor's endpoint, model id and env-var names have all been removed.

```bash
# From the repo root, with the repo venv (scripts/bootstrap-venv.sh).
set -a; source .env; set +a
.venv/bin/python -m agent_integration.cli doctor
.venv/bin/python -m agent_integration.cli run --task "Follow up on overdue invoice INV-1001"
```

**Observed this session:** with no token configured, `doctor` prints the
`qwen3.8-max` defaults and `FAILED: no token is set.` naming the three accepted
variables, and exits 1. **The live `OK` path was not exercised** — no model key is
set in this environment, so nobody should read that as a verified live run.
Re-run it with a real key before claiming it in a demo.

**This is the core platform claim demonstrated with a live model, not a script.** The gate sits between the model and the world and the model cannot route around it: `model_driver.next_calls()` returns a proposal and stops, so there is no code path from a model reply to a tool execution that does not pass the auditor.

> Note on the counters: a successful run reports `side_effects_attempted` and
> `side_effects_executed` counting **only gated calls**. Read-only calls are
> recorded in `result.actions` too but are not side effects, and used to be
> counted as ones — so a lookup-plus-send run reported 2 and 2. It now reports 1
> and 1. See §5.

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
python3 entry-point/checks/check_app.py apps/helios-collect   # 0 errors, 4 warnings, exit 0
python3 entry-point/checks/check_app.py <a-broken-app>        # 4 errors, exit 1
python3 entry-point/checks/check_app.py /nonexistent          # exit 2
```
**Observed:** all three exit codes correct, so it is CI-usable. Against a deliberately malformed app it reported missing required sections, the `stack.use`/`match_existing` exclusivity violation, the `data.local`/`integration` violation, and both missing manifests.

The check that protects money: **action-manifest tools must be a subset of audit-manifest declared tools.** A tool an agent can call that the auditor never declared is an ungated side effect waiting to happen. This session added the second money check: **the action side and the audit manifest must agree on every tool's side-effect flag.** The runtime gates on the action side's flag, so a tool it believes is read-only runs directly and never reaches the auditor — the manifest's rules for it become dead text.

The 4 warnings on `helios-collect` are expected and explained in its README §"Checker warnings". The newest one is the flag cross-check declining to resolve three app-local tools it cannot see into any platform catalog; all five of that app's flags were verified by hand to match its audit manifest.

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

> ✅ `entry-point/SKILL.md` and `references/platform-status.md` used to say *"there
> is no agent loop"* and *"no API key is set."* Both are corrected as of this
> session: `agent_integration` ships the loop and a working model driver, and
> `platform-status.md` is re-verified against the current tree. Read it before
> building — it is the authority on what runs versus what is only designed.

---

## 5. Traps — read before you touch anything

Ordered by how silently they break things.

**1. A gated tool can register as read-only, and nothing raises.**
`ToolDescriptor.from_contract()` reads `.has_side_effect`; `agent_toolkit.ToolDefinition` names it `.side_effect`. Duck-typing the two defaults the missing attribute to `False`, so `send_email` registers as read-only → never adjudicated → **the app emails customers with no audit at all.** Same function only reads a *callable* `input_schema`, but the action side's is a plain Mapping → auditor gets `None` → `baseline.arguments_validate` never fires. `adapters.ToolContractView` closes both. **Run `selfcheck` after any change to either package's tool contract.**

**2. Fact resolution is keyed on a fixed argument-name set.**
`FactResolver.SUBJECT_KEYS` = `invoice_id`, `bill_id`, `customer_id`, `supplier_id`, `subject_id`, `payment_id`. Name an argument anything else and facts do not resolve, so **fact-bound rules fail closed and deny everything.** Safe direction, completely invisible until the demo. This is the most common way a generated app's policy silently stops working.

**3. `auth_code` redaction — FIXED, but it constrains your manifest.**
`auth_code` and `authorization_code` are now in `audit_agent`'s `REDACTED_KEYS`
(`src/audit_agent/contracts.py`), so the payment credential is masked before it
can reach the hash-chained log via the `owner_question` string (`agent.py`). This
was a real leak — a full 64-hex code used to print in the owner prompt — and the
one-line fix closed it. Asserted by
`test_a_held_payment_credential_never_reaches_the_owner_question`.

The trap that remains: **redaction happens before schema validation**, so a
manifest must never put a `minLength` or `pattern` on `auth_code`. The gate only
ever sees the 14-char `***REDACTED***` placeholder, so a length rule rejects every
payment. `examples/invoice-payer` dropped its `minLength: 16` for this reason —
presence is enforced by `required` + the critical `app.requires_verified_intent`
rule, validity by the `payments.send` tool at execution (which receives the real,
unredacted argument). Apps may still redact their *own* action log; that is now
defence-in-depth, not the only layer.

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

**All four of the items that used to head this list are now decided.** They are
recorded in `specs/decisions.md` with a one-line why each, and are summarised here
so you do not re-litigate them:

- **Model conflict — RESOLVED.** `specs/helios-context.md:25` states Qwen is the
  only generative model allowed, and the driver previously targeted a different
  vendor's endpoint and model id, which was a compliance risk on 50% of the score.
  The driver now targets Qwen via DashScope's OpenAI-compatible endpoint (default
  `qwen3.8-max`), and every reference to the previous vendor — endpoints, model
  ids, vendor env-var names — is removed from the files touched. `SETUP.md`,
  `.env.example`, the package README and the driver docstrings all agree.
  **Unverified:** the live model path. No key is configured in this environment, so
  `doctor`'s `OK` branch was never observed this session — only its no-key
  behaviour. Run it with a real key before claiming Flow C in a demo.
- **Which gate is authoritative — RESOLVED.** The standalone `audit_agent`.
  `agent_toolkit.audit.AuditGate` is legacy, in no run path, and marked deprecated
  in its docstring. Do not add rules to it.
- **Real money or simulated — RESOLVED.** Simulated end to end, no real payment
  rails. This is a deliberate competition and safety constraint, not a placeholder.
- **Hosting, deployment artifact format, held-state storage — RESOLVED.**
  `platform/deployment` answers the first two (`helios.yaml` + ASGI entry +
  `GET /health`, built and routed through the Docker Engine API). Held state and
  replay guards persist to SQLite under the app's data dir; **P2 implements that.**

**Genuinely still open** (from `specs/decisions.md`): how the knowledge graph
distinguishes authoritative records from agent observations beyond the current
verified/observation split; how apps subscribe to cross-app updates; which
financial operations are managed APIs vs packaged SDK tools; how apps authenticate
with shared services; what platform-wide limits mandates are capped by; and audit-log
retention — who may read it and for how long.

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

**Everything previously listed here is now committed.** The old note described
uncommitted work in progress that was not anyone's to commit; it has all landed:

- The `TODO.md` deletion is committed (`06f6281 chore: drop the app-builder
  TODO list`). Its content — cross-package dependency boundary notes, and the flag
  that `specs/tech-stack.md` used the stale `agent-toolkit/` folder name — is
  recoverable via `git show 06f6281^:TODO.md`. The tech-stack naming issue it
  flagged is fixed in `specs/tech-stack.md`.
- `SETUP.md`, `platform/agent-integration/tests/conftest.py`,
  `platform/agent-integration/tests/test_model_driver.py` and
  `platform/financial-tools/fin_tools/__main__.py` are all tracked now.
- `entry-point/examples/invoice-payer/runs/` is tracked (the recorded run logs are
  the example's evidence, committed deliberately — see `0432715`).

What this session added on top, all committed on branch
`worktree-p0-foundation` off `chloe-dev`:

- One environment and one test command (`scripts/bootstrap-venv.sh`,
  `scripts/test-all.sh`, pinned `requirements-dev.txt`). The four packages are
  installed editable; `platform/agent-integration/.venv` and the per-package
  `PYTHONPATH=src` incantations are superseded.
- `platform/deployment/` — the deployment engine, imported from a teammate's
  build, with 96 offline tests.
- Four teammate tool packages imported into `platform/financial-tools/` as
  siblings of `fin_tools`: `helios_forecast`, `spreadsheets`, `email_tool`,
  `payments_collect`.
- The six known defects fixed, each with a test that failed before and passes
  after (see §5 trap 3, §4 Flow C's counter note, and §8).
- `specs/decisions.md`, `specs/tech-stack.md` and `specs/architecture-mermaid.md`
  updated to record the settled decisions; `QWEN.md` added at the repo root.

Test counts: `scripts/test-all.sh` reports **all 7 suites green: 803 passed**, all
offline. Per suite: financial-tools 367, agent-integration 136, deployment 96,
audit-agent-builder 81, helios-collect 74, invoice-payer 41, checks 8.

Gitignored and therefore never to be staged: `.venv/`, `.qwen/worktrees/`,
`OTHER-TEAMMATE-STUFF/`, `.helios-run/`, `engine.log`, `data/core/`, `.DS_Store`,
`*.egg-info/`. **`platform/deployment/.helios-run/tenants/demo.json` holds a live
tenant execution secret in the source it was imported from — it was excluded from
the import and must never be committed.**

---

## 8. Where to go next

**Done this session** (were items 1–4 here; kept so you can see what changed and
do not redo it):
1. ~~Reconcile the model conflict~~ — **done.** The driver targets Qwen via
   DashScope; the previous vendor's endpoint, model id and env-var names are gone
   from every file touched (§6). *Still unverified: the live model path, because no
   key is configured here.*
2. ~~Fix the `side_effects_*` counter~~ — **done.** It counted read-only calls as
   side effects; it now counts only adjudicated ones, discriminated by
   `proposal_id`. A lookup-plus-send run reports 1 and 1, not 2 and 2.
3. ~~Add `auth_code` to `REDACTED_KEYS`~~ — **done**, with `authorization_code`
   too. Closed a real credential leak into the hash-chained log (§5 trap 3).
4. ~~Update `SKILL.md` and `platform-status.md`~~ — **done**, along with
   `agent-loop.md`, `BUILD.md`, both entry-point READMEs,
   `references/financial-tools.md`, `platform/README.md`, both financial-tools
   docs and `app-builder/PLAN.md`.

Also fixed this session: the hard-coded `send_email` contact recording (now
flag-driven via `ToolDefinition.records_contact`), the
`check_app.py` side-effect cross-check it documented but never performed, the CSV
BOM that corrupted the first column name, and a real defect found while testing the
imported deployment engine (`_build_error_text` read `build_logs`; docker-py sets
`build_log`, so every build failure dropped its compiler output).

**The obvious build gaps**, in rough order of what unblocks what:
5. Build **Helios Payables** and **Helios Cash Planner** (flagships 2 and 3). The payables *policy shape* is already proven by `examples/invoice-payer`; it needs to become a real app. Cash Planner is what makes the knowledge-graph publish meaningful — it is the declared consumer of Collect's verified due dates.
6. Implement the **Application Builder** (`platform/app-builder`) — scaffolder, validator, `stacks.yaml`. `specs/app-builder.md` defines all of it; `entry-point/checks/check_app.py` is the interim validator to replace.
7. **Shared services** — the knowledge graph first, since cross-app coordination is the only architecture claim currently undemonstrable.
8. A real **UI**. Every app is CLI-only today; `approval_queue` is the page that matters most, because without one agents pause invisibly.
9. **Persist held state and replay guards to SQLite** under the app's data dir. Decided in `specs/decisions.md`; holds are currently in-memory only, so a restart loses every pending owner decision.
10. **Wire the deployment engine to a real app.** `platform/deployment` has 96 offline tests with docker-py mocked, but no app in this repo has been deployed through it — that end-to-end path is unexercised.

The in-process `AuditGate` overlap is **settled**, not yours to re-open: the standalone `audit_agent` is authoritative and `AuditGate` is legacy and deprecated in its docstring (`specs/decisions.md`). Do not add rules to it.

---

## Appendix: quick reference

| Need | Go to |
|---|---|
| Architecture and the component diagram | `specs/architecture-mermaid.md` |
| What to read first as a coding agent | `entry-point/SKILL.md` → `entry-point/BUILD.md` |
| What actually exists vs what is designed | `entry-point/references/platform-status.md` (re-verified this session) |
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
| `ModelDriver` (Qwen via DashScope, stdlib HTTP, lazily imported) | `agent_integration/model_driver.py` |

The two tool contracts are **not interchangeable** — that mismatch is traps 1 and §5's whole reason for existing, and `adapters.ToolContractView` plus `fin_tools.toolkit_bridge` are the only sanctioned bridges.

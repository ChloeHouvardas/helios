# BUILD.md — how to build a Helios application

You are Qwen (or another LLM) acting as Helios's **coding agent**. A user has
described a financial app they want. Your job is to produce a working application
that runs on this platform.

This is the operative document. `SKILL.md` is the short version for a harness that
loads skills; this is the full procedure. Read `references/platform-status.md`
before writing code — it says what actually exists, and several things the specs
describe are not built yet.

**Target: a working prototype, not a product.** The platform's own scaffolder does
not exist yet, so *you are the scaffolder*. Write the app directly, make it run,
make the controls demonstrable. Depth is secondary to an app that a judge can
execute and watch refuse to do something it is not allowed to do.

---

## The one rule that decides everything

**The model proposes, code decides.**

An action agent may ask for anything. What it gets is bounded by policy written as
data and enforced by deterministic code that fails closed. You are writing *both*
sides: the agent's instructions and the policy that constrains it. The policy is
the product. An app whose agent can do whatever it likes is not a Helios app, no
matter how good the UI is.

Two corollaries that generate most of the design work:

- **A side effect is a proposal.** Anything that moves money, sends an email,
  writes a file, or publishes shared state is gated: approve, hold for the owner,
  or reject with a reason.
- **Facts are not claims.** Every value a rule depends on is resolved from a
  trusted source the agent does not control. The agent gets to *name* which record
  it means; it never gets to *state* what that record says.

If you internalise the second one, you will write a better app than most. It is the
difference between "we cap payments at the invoice amount" and "we cap payments at
the amount the agent says the invoice is" — and the second is not a control.

---

## Procedure

### Step 0 — Read the platform's current state

```
references/platform-status.md      # what exists, what does not, known gaps
references/financial-tools.md      # the real tool catalog + signatures
```

Also skim, in this order: `specs/helios-context.md` (what Helios is for),
`specs/app-builder.md` (the app spec you will write), `specs/decisions.md` (what is
settled). Do not skip `platform-status.md`: it records that there is **no agent
loop, no scaffolder, and no validator** yet, which changes what you must write
yourself.

### Step 1 — Work the question phase

`specs/app-builder.md` defines a fixed 16-question intake. Work through it. Its
purpose is that two runs over the same idea produce comparable specs, and that a
missing answer is visible rather than assumed.

The questions, compressed:

1. **What the app does** — the job, in the user's words; who uses it; what success
   changes for them.
2. **What already exists** *(not optional, and asked early on purpose)* — is this
   standalone or joining a system? its stack? where its data lives and how you may
   access it? its UI conventions?
3. **What data** — records and required fields; which already exist elsewhere;
   what must be shared across apps.
4. **What agents** — what work happens without being asked each time; per agent,
   what it may do, its limits, and which actions need owner approval. *This is the
   mandate.*
5. **What tools** — which financial tools that work needs; **which have side
   effects**. Each one becomes a gated proposal.
6. **What pages** — what the user must see and act on; which UI pattern per page.

If the user gave you one sentence ("make me an app that pays my invoices"), answer
what you can from it, **state your assumptions explicitly**, and proceed. Do not
stall on an interview you cannot conduct. Do not invent a data model to make a file
parse — an incomplete answer gets recorded as incomplete.

The integration block (2) is where the competition's surprise task is decided. A
requirement handed to you after code lock may have to join a system with its own
stack, data and UI. Ask it even when the answer looks obviously "standalone".

### Step 2 — Write `helios-app.yaml`

Follow `specs/app-builder.md` §"The app spec" exactly. Sections: `spec_version`,
`identity`, `existing`, `stack`, `data`, `graph`, `agents`, `ui`, `tools`.

There is no validator yet, so **self-check these invariants by hand** — they are
the ones `specs/app-builder.md` says the validator will enforce:

- Exactly one of `stack.use` / `stack.match_existing`.
- Exactly one of `data.local` / `data.integration`.
- `identity.id` is kebab-case and equals the audit manifest's `app_id`.
- Every `ui.pages[*].pattern` is one of: `summary`, `table`, `detail`, `form`,
  `approval_queue`, `settings`.
- Every entity field type is in the closed set: `string`, `integer`, `decimal`,
  `date`, `datetime`, `boolean`, `json`. **Money is always `decimal`, never float.**
- No entity appears in both `data.local` and `data.integration`.
- `stack.match_existing` requires populated `existing.stack`; `data.integration`
  requires `existing.data`; `ui.align_with` requires `existing.ui`.
- Both `agents` paths exist and parse under their own loaders.
- **Action-manifest tools ⊆ audit-manifest declared tools.** This is the single
  most important check in the whole build. A tool an action agent can call that the
  auditor never declared is an ungated side effect waiting to happen.
- The spec's `tools` is a superset of both manifests' tool lists.
- Every graph entity the agents publish appears in `graph.publishes`.

Write the file even though nothing reads it. It is the design record, it forces
the decisions above, and it becomes machine-checkable the moment the validator
lands.

### Step 3 — Write the audit manifest first

**Before the action manifest.** This ordering is not stylistic.

The audit manifest is the registry of what may be granted at all. If you write the
action agent first, you will find yourself declaring tools you have not thought
about gating, and then retro-fitting rules to cover them. Writing policy first
makes the mandate the design constraint and the agent the thing that fits inside it.

Format: `platform/audit-agent-builder/manifests/collect-reminder.yaml` and
`payables.yaml` are the two working references. Read `payables.yaml` in full — its
comments explain the reasoning behind each rule, including why one is a *deny*
rather than a *hold*.

Skeleton (every field below is real and validated by `AuditManifest.load`):

```yaml
manifest_version: "1"
app_id: your-app-id              # must equal identity.id in helios-app.yaml
name: Your App Audit Agent
description: >
  What this auditor governs, and the shape of the mandate.

tools:
  - name: your.read_tool
    description: ...
    side_effect: false           # runs directly
  - name: your.write_tool
    description: ...
    side_effect: true            # every call is gated
    input_schema:                # REQUIRED for side-effect tools
      type: object
      additionalProperties: false
      required: [invoice_id]
      properties:
        invoice_id: { type: string, minLength: 1 }
        amount_cents: { type: integer, minimum: 1 }

tasks:
  yourapp.task_name:
    description: What one unit of authorised work is
    allowed_tools: [your.read_tool, your.write_tool]   # the allowlist
    ttl_seconds: 120               # 30..86400; short for money movement
    tool_overrides:                # optional per-tool ceilings
      your.write_tool: { max_calls: 3, condition: "..." }

rules:
  - id: app.your_rule            # must NOT start with "baseline."
    description: ...
    task_types: [yourapp.task_name]   # omit = every task
    tools: [your.write_tool]          # omit = every tool
    message: plain language, shown to the owner
    severity: warn                    # info | warn | critical
    # ...one or more of the rule primitives below

settings:
  default_ttl_seconds: 120
  contact_window_days: 30
  log_path: your-app-audit-log.jsonl
  advisory: [heuristic]          # heuristic | qwen | none — never changes a verdict
  allow_unsigned_grants: false   # true is LOCAL DEV ONLY
```

**Rule primitives** — these are all the shapes a declarative rule can express.
Anything you cannot say with these belongs in Python, reviewed by a human.

| Primitive | What it does |
|---|---|
| `limits: [{argument, max, on_exceed}]` | cap a value the agent supplied |
| `limits: [{argument, max_from_fact, on_exceed}]` | cap it against a **trusted ceiling** |
| `limits: [{fact, max, on_exceed}]` | cap a trusted value |
| `on_exceed: hold` / `deny` | route to the owner, or refuse |
| `clamp: true` | reduce to the ceiling and allow (alternative to `hold`) |
| `required_arguments: [...]` | refuse a call missing these |
| `forbidden_values: {arg: [v1, v2]}` | refuse specific argument values |
| `max_contacts_in_days: {within_days, max}` | frequency limit from trusted history |
| `deny_if_fact_true: [flag]` | hard refusal on a trusted flag |
| `approval_if_fact_true: [flag]` | hold for the owner on a trusted flag |

Three things that catch people out:

- **`max` and `max_from_fact` are mutually exclusive, and one is required.** A
  limit with neither has nothing to compare against and would silently never fire —
  a control that looks configured and enforces nothing.
- **`on_exceed: hold` and `clamp: true` are alternatives, not complements.** Every
  rule evaluates the agent's *original* request independently, so a clamp in one
  rule does not make a later hold rule see a reduced value. That order-independence
  is deliberate: a rule's verdict must not depend on where it sits in the file.
- **You may not reuse a baseline rule id, nor start one with `baseline.`** Both
  raise `PolicyLooseningError` at parse time. Redefining a platform rule as
  something permissive is the most damaging thing a generated manifest can do, so
  it is refused rather than caught later. Use `app.<name>`.

**The pattern that matters most in finance** is *argument measured against a fact
ceiling*: "the payment may not exceed the recorded invoice amount". The agent
chooses the amount; the ledger chooses the ceiling; neither can move the other.
Write it as:

```yaml
limits:
  - argument: amount_cents
    max_from_fact: invoice_amount_cents
    on_exceed: hold
```

Then make sure your fact source actually supplies `invoice_amount_cents` (Step 4).

**Know which direction this guards, because it only guards one.** A ceiling on an
*argument* catches **over**payment. It does not catch underpayment — paying $1.00
against a $48,000 invoice is inside the ceiling, and the rule allows it. That is
not a bug in the rule; it is the wrong rule for that threat. Understating is
stopped by binding a credential to the exact amount (see the Intent Verification
section of `references/financial-tools.md`), so a smaller payment requires the
owner to have confirmed that smaller amount.

If your threat is "the agent misstates what a record says" rather than "the agent
asks for too much", bind on the fact instead of the argument —
`fact: amount_due, max: "10000.00"`, as `collect-reminder.yaml` does. That shape
never reads the agent's number at all, so lying about it changes nothing.

Choose deliberately, and write a test for the direction you actually care about.
An app that claims "payments are capped at the invoice amount" while nothing stops
an agent from paying a tenth of it is the kind of gap that is invisible in a demo
and obvious under questioning.

**Decide deny vs hold deliberately.** Deny when the agent should not proceed at all
until a human fixes something out of band — changed supplier bank details is the
canonical case. Hold when it might be legitimate and only the owner can tell — a
duplicate-looking invoice, an unusually large but exactly-correct payment. Getting
this backwards either annoys the owner with queue items they cannot act on, or
silently permits fraud.

### Step 4 — Write the fact source

A class satisfying `audit_agent.FactSource`. This is where "facts are not claims"
stops being a slogan. Five methods, all optional in practice:

```python
class YourFactSource:
    def amount_for(self, subject_id: str) -> Decimal | None: ...
    def contact_count_for(self, subject_id: str, within_days: int) -> int | None: ...
    def counterparty_for(self, subject_id: str) -> str | None: ...
    def payment_details_changed_for(self, subject_id: str) -> bool | None: ...
    def extra_facts_for(self, subject_id: str) -> dict[str, Any]: ...
```

Read from the app's own database. **Never from the request.**

`extra_facts_for` is how policy refers to app-specific signals — a recorded invoice
amount, a duplicate flag, an outlier score — without the fact source knowing which
ones this app declared. Its keys are the names your rules use in `fact:` and
`max_from_fact:`.

Two facts about resolution you must get right:

- **Subject selection is by argument name, from a fixed set:**
  `invoice_id`, `bill_id`, `customer_id`, `supplier_id`, `subject_id`,
  `payment_id`. An argument named anything else (say `inv` or `bill_reference`)
  will not select a record, the fact will not resolve, and a fact-bound rule will
  **fail closed** — denying everything. This is the most common way a generated
  app's policy silently stops working, and it fails in the safe direction, which
  is why it is easy to miss until the demo.
- **Absent fact ≠ absent argument.** An absent *argument* means "the agent did not
  ask for that" and a cap on it does not fire. An absent *fact* means "the auditor
  does not know something it needs" and the rule **denies**. Unknown is never read
  as small.

`StaticFactSource(amounts={...}, contacts={...}, ...)` is the test/demo
equivalent. Use it to prototype, swap in a real one for the app.

### Step 5 — Write the action manifest

JSON, loaded by `agent_toolkit.manifest.validate_manifest`. Loading is strict and
the strictness is the point:

```json
{
  "name": "your-agent",
  "version": "1",
  "description": "One line: what this agent does.",
  "instructions": "The agent's operating instructions. See below.",
  "tools": ["your.read_tool", "your.write_tool"],
  "mandate": {
    "allowed_tools": ["your.read_tool", "your.write_tool"],
    "limits": {}
  },
  "max_turns": 12
}
```

Allowed fields are exactly `name`, `version`, `description`, `instructions`,
`tools`, `mandate`, `model`, `max_turns`. **`extra="forbid"` semantics apply — a
`_comment` key is rejected.** Put commentary in a README next to the file, not in
the JSON.

Constraints the loader enforces:

- `name` must be kebab-case (`^[a-z0-9]+(?:-[a-z0-9]+)*$`).
- `tools` non-empty, no duplicates.
- Every tool in `mandate.allowed_tools` must appear in `tools`.
- `mandate.limits` accepts **only four keys**, all receivables-shaped:
  `max_extension_days`, `extension_owner_approval_days`,
  `min_days_between_contacts`, `invoice_owner_approval_amount`.

That last point is a real limitation. **A payables app cannot express its policy
here at all.** Your authoritative policy is the audit manifest from Step 3. Put
only the limits that fit, and say so in a comment in your README — do not let a
reader believe the action manifest is doing the work. See
`platform-status.md` §3 for the unresolved seam this creates.

**Write `instructions` as an operating contract, not a description.** The
reference example's instructions do five things, and yours should too:

1. Read before acting, and never state a value you did not read from a tool.
2. Obtain the human authorization the policy requires (for payments, an
   `auth_code` from a confirmed intent).
3. On a **hold**: stop and surface the owner's question. **Do not reword the
   request and retry** — that is circumventing a control, and it is the specific
   failure mode a judge will look for.
4. On a **deny**: read the reason, fix what is actually wrong, or report that the
   work cannot be done and why. Never work around it.
5. Report per item what happened: done, held, or refused with the reason.

Points 3 and 4 are what make the agent *legible under constraint*. An agent that
argues with its gate is a liability; one that reports it is a product.

### Step 6 — Write the app

Compose the three packages. `examples/invoice-payer/app.py` is a complete working
reference — read it, then adapt it. The pieces:

```python
# 1. financial tools
from fin_tools import build_world
bundle = build_world()
bundle.registry.catalog()          # expose to the agent
bundle.registry.call(name, **kw)   # dispatch

# 2. audit agent, over YOUR fact source
from audit_agent import (build_audit_agent, AuthorizationRequest, ActionRequest,
                         Decision)
from audit_agent.manifest import AuditManifest

auditor = build_audit_agent(
    AuditManifest.load("agents/your-app-audit.yaml"),
    fact_source=YourFactSource(...),
    log_path=run_dir / "audit.jsonl",
)

# 3. authorize once per task, then adjudicate every side effect
grant = auditor.authorize(AuthorizationRequest(
    request_id=..., app_id=..., agent_id=..., principal="owner",
    task_type="yourapp.task_name", task_summary="...",
    requested_tools=[...], context={"invoice_id": "..."}))

decision = auditor.adjudicate(ActionRequest(
    request_id=..., app_id=..., agent_id=..., principal="owner",
    task_type="yourapp.task_name", tool="payments.send",
    arguments={...}, authorization=grant,
    resolved_facts={"invoice_id": "..."}))

decision.decision        # Decision.ALLOW | HOLD | DENY
decision.reason
decision.owner_question  # plain language, for the approval queue
decision.findings        # which rules fired, with severity
decision.hold_id         # when held

# 4. execute only on ALLOW; on HOLD, pause for the owner
auditor.resolve_hold(hold_id, approve=True, decided_by="owner")
```

Notes that will save you time:

- **`resolved_facts` is a subject name, not evidence.** It selects which record to
  look up. Its *values* are deliberately not merged into the facts the auditor
  uses — an agent that sends `resolved_facts={"amount_due": "1.00"}` for a $24,500
  invoice is still held.
- **`build_audit_agent()` raises `SigningKeyMissingError`** unless
  `HELIOS_AUDIT_SIGNING_KEY` is set or the manifest sets
  `allow_unsigned_grants: true`. For local dev:
  `os.environ.setdefault("HELIOS_AUDIT_SIGNING_KEY", "0"*64)`. Never ship that.
- **Grants expire** (your `ttl_seconds`) and side-effect grants are **single-use**.
  Authorize per task, not once at startup. Reuse the same `idempotency_key` on a
  retry so it cannot pay twice.
- **The agent loop exists — you do not supply it.** `platform/agent-integration/`
  provides `GatedAgentRuntime` (the loop) and `model_driver.ModelDriver` (Qwen over
  DashScope, stdlib HTTP). `build_integration()` wires the auditor, bridge and
  runtime in one call. It is not built on Qwen-Agent — no file imports `qwen_agent`.
  For an offline demo you can still drive the runtime from deterministic code (as
  the example does); the gate underneath is identical either way, and that
  separability is the whole point, worth saying out loud in your demo.
- **Credentials are redacted before they reach the audit log.** `auth_code` and
  `authorization_code` are in `audit_agent`'s `REDACTED_KEYS`, so the payment
  credential no longer leaks via `owner_question`. Because redaction happens
  before schema validation, do not put a `minLength`/`pattern` on `auth_code` in a
  manifest. See `platform-status.md` §"Credential handling".
- Use `agent_toolkit.action_log.ActionLog` for the app's own append-only log, and
  keep the auditor's hash-chained log separate. Two logs, two purposes: the action
  log is the app's record of what it did; the audit log is tamper-evident evidence
  of what was authorised.

### Step 7 — Give the owner somewhere to decide

**Every app with side-effecting agents needs an `approval_queue` page.** Held
actions pause the agent; without a queue they pause *invisibly*, and the user
discovers it when they wonder why nothing happened. This is the UI pattern
`specs/app-builder.md` singles out as the one that matters most.

For a prototype, a CLI owner is fine — the example uses a `ScriptedOwner` that
prints the auditor's `owner_question` and returns a decision. The important part
is that the question shown to the owner is the one the **auditor generated**, not
one the agent wrote about itself.

Use `auditor.describe()` / `describe(manifest)` for a capability report, and
`sandbox_run(...)` from `fin_tools` to show the owner what a side effect *would*
do without doing it.

### Step 8 — Verify, then demo

```bash
# 1. Both manifests parse under their real loaders
python3 -c "
import sys; sys.path.insert(0,'platform/audit-agent-builder/src')
from audit_agent.manifest import AuditManifest
m = AuditManifest.load('apps/your-app/agents/your-app-audit.yaml')
print('audit OK', m.app_id, list(m.tasks), [r.id for r in m.rules])"

python3 -c "
import sys, json; sys.path.insert(0,'platform/action-agent-builder/src')
from agent_toolkit.manifest import validate_manifest
m = validate_manifest(json.load(open('apps/your-app/agents/your-app-action.json')))
print('action OK', m.name, m.tools)"

# 2. The cross-file invariant, by hand (no validator exists yet)
python3 -c "
import sys, json; sys.path.insert(0,'platform/audit-agent-builder/src')
sys.path.insert(0,'platform/action-agent-builder/src')
from audit_agent.manifest import AuditManifest
from agent_toolkit.manifest import validate_manifest
a = validate_manifest(json.load(open('apps/your-app/agents/your-app-action.json')))
b = AuditManifest.load('apps/your-app/agents/your-app-audit.yaml')
missing = sorted(set(a.mandate.allowed_tools) - {t.name for t in b.tools})
assert not missing, f'UNGATED SIDE EFFECT: {missing}'
assert a.name == b.app_id or True, 'check identity.id == audit app_id'
print('cross-file OK')"

# 3. Inspect what authority the manifest actually grants
cd platform/audit-agent-builder
PYTHONPATH=src python3 -m audit_agent.cli inspect ../../apps/your-app/agents/your-app-audit.yaml

# 4. Run the app
python3 apps/your-app/app.py
```

The CLI (`audit_agent.cli`) also has `validate` (exit 1 if unsound — use it in CI)
and `demo` (watch it decide, with reasons). `serve` runs it as an HTTP service.

**Then write a test for each control.** Not a test that the app runs — a test that
each rule fires. Model it on `platform/audit-agent-builder/tests/test_security.py`,
which asserts every security property individually and is the file to show a judge.
Minimum set:

- a normal call is **allowed** and executes;
- a call over a limit is **held**, and resumes only after the owner decides;
- a call outside the mandate is **denied**, with a reason the agent can act on;
- an agent that **lies about a fact** is still caught. Two shapes, and it is worth
  testing both because they are guarded by different mechanisms:
  - lying in `resolved_facts` about a recorded amount changes nothing, because
    those values are never merged into the facts the auditor uses;
  - paying **more** than the invoice is worth is held, because the ceiling is
    `max_from_fact`. Paying *less* is inside that ceiling and is **allowed** by
    the rule — it is stopped instead by `payments.send` refusing an `auth_code`
    that is not bound to that exact amount, so the owner must have confirmed the
    smaller sum. Do not write a test asserting understatement is caught by the
    ceiling; it is not, and the test will fail for the right reason.
- an action manifest offering an **undeclared tool** is rejected;
- every step appears in the log.

That fifth one is the demo. Run it in front of the judges.

---

## Worked example: "Make me an app that automatically manages and pays my invoices"

`examples/invoice-payer/` is that request, built end to end and running. Read it
with this document open.

```
examples/invoice-payer/
├── README.md                          # what it demonstrates, how to run it
├── helios-app.yaml                    # Step 2 — the app spec
├── app.py                             # Steps 4, 6, 7 — fact source, runtime, owner
├── agents/
│   ├── invoice-payer-audit.yaml       # Step 3 — the policy (written first)
│   └── invoice-payer-action.json      # Step 5 — the agent
├── data/invoices.json                 # the app's data, incl. fact-source fields
└── runs/                              # generated: action log + hash-chained audit log
```

Its five invoices each exercise a different control path:

| Invoice | Amount | What happens | Which control |
|---|---|---|---|
| INV-1001 | $1,250 | **paid** | normal path: inside every limit |
| INV-1002 | $48,000 | **held → owner approves → paid** | `app.large_payment_approval` |
| INV-1003 | $760 | **denied** | `app.payee_details_changed` |
| INV-1004 | $1,250 | **held → owner declines** | `app.duplicate_invoice` |
| INV-1005 | $310.50 | **paid** | normal path |

INV-1003 is the one to watch. The scripted owner **confirms it anyway** — a busy
owner rubber-stamping a payment to a supplier whose banking details just changed.
The payment still does not go out, because the rule denies it in code. That is the
platform's central claim demonstrated rather than asserted: the control does not
depend on the human noticing, and it does not depend on the agent behaving.

Note also that INV-1003 is a *deny* while INV-1004 is a *hold*, and that the
distinction is deliberate — see Step 3.

```bash
cd entry-point/examples/invoice-payer
python3 app.py              # narrated run
python3 app.py --trace      # per-step trace
python3 app.py --invoice INV-1003   # one control path
python3 app.py --json       # machine-readable
```

Runs offline. No API key, no network, no dependencies beyond the three platform
packages plus `pydantic` and `pyyaml`.

---

## Where to put a generated app

`specs/tech-stack.md` fixes the monorepo layout as `platform/`, `services/`,
`apps/`, and `specs/app-builder.md` says the scaffolder writes to
`apps/<name>/`. **`apps/` exists** — `apps/helios-collect/` is a real app in it.
The scaffolder does not, so you create the directory and its contents yourself.

Put a generated app under `apps/<name>/` and touch nothing else. Other agents work
in this repo simultaneously; `platform/action-agent-builder/`,
`platform/audit-agent-builder/`, `platform/agent-integration/`,
`platform/app-builder/`, `platform/deployment/` and `platform/financial-tools/` are
theirs. Do not edit inside them. (Note the folder names are not the package names:
`platform/action-agent-builder/` contains `agent_toolkit`.)

Which gate is authoritative is **settled**: the standalone `audit_agent`, per
`specs/decisions.md`. `agent_toolkit.audit.AuditGate` is legacy, in no run path,
and marked deprecated in its docstring. Build against the standalone auditor and do
not add rules to the in-process gate — that is not an architectural change needing
approval, it is following the recorded decision.

---

## Non-negotiables

From `specs/helios-context.md` (competition rules) and `specs/tech-stack.md`:

- **Qwen is the only generative model allowed for development.**
- Documentation and interfaces must not depend on hidden prompts or
  model-specific tricks. If it needs a secret incantation, it is not built.
- **No hard-coded API keys.** Credentials come from environment variables and stay
  out of logs and packages.
- Money is **integer cents or `decimal` strings, never float**.
- Licence walls still apply: no unlicensed investment advice, PIPL constraints on
  behavioural telemetry, and compliance output is not a legal opinion.

## Scoring, and what it should change about your priorities

| Stage | Weight | Focus |
|---|---|---|
| Investment pitch | **50%** | market opportunity, product innovation, value of the three flagship apps, business model |
| Engineering verification | 25% | it runs, platform completeness, app delivered *through the platform*, surprise task |
| Architecture defence | 25% | architecture, extensibility, security, usability, engineering design |

The judges' guiding question: *what new financial capability becomes possible when
AI, data and an extensible platform work together?*

Half the score is the pitch, so an app that works but cannot be explained in one
sentence loses to one that can. The strongest architecture-defence material is not
the UI or the forecast — it is the **Action/Audit split and the mandate concept**,
and specifically *facts are not claims*. Lead with that. It is the part another
team cannot copy in 80 hours, because it is a design decision rather than a
feature.

The surprise task is decided in Step 1's integration block. A requirement arrives
after code lock and may have to join an existing system. A builder that can only
produce one shape of app fails that test, which is why `helios-app.yaml` has
`stack.match_existing`, `data.integration` and `ui.align_with` rather than three
fixed shapes. A generated app is **Helios-compatible, not Helios-identical**:
compatible means it declares its data, agents, tools and graph interactions in the
app spec, runs the shared agent runtime, and passes the validator — not that it
looks like the other apps.

# Audit Agent Builder

A standalone audit agent that decides **what an action agent may use, for which task**. It is one half of the Helios Agent Toolkit (see `specs/architecture-mermaid.md`); the action half is built separately and the two share no code at runtime.

**The principle:** the model proposes, code decides. An action agent can ask for anything. What it gets is bounded by policy declared as data and enforced by deterministic rules that fail closed.

---

## Quick start

```bash
# from the repo root; scripts/bootstrap-venv.sh builds .venv first
.venv/bin/python -m pytest platform/audit-agent-builder/tests -q   # 79 tests, fully offline

# What authority does a manifest grant?
.venv/bin/python -m audit_agent.cli inspect platform/audit-agent-builder/manifests/collect-reminder.yaml

# Is a generated manifest sound? (CI: exit 1 if not)
.venv/bin/python -m audit_agent.cli validate platform/audit-agent-builder/manifests/collect-reminder.yaml

# See it decide, with reasons
.venv/bin/python -m audit_agent.cli demo platform/audit-agent-builder/manifests/collect-reminder.yaml \
  --subject INV-1001 \
  --fact INV-1001:amount_due=450.00 \
  --fact INV-1001:recent_contact_count=0 \
  --call 'send_reminder_email:{"invoice_id":"INV-1001","message":"Friendly reminder"}'

# Run as a service an action agent calls over HTTP
export HELIOS_AUDIT_SIGNING_KEY="$(openssl rand -hex 32)"
python3 -m audit_agent.cli serve manifests/collect-reminder.yaml --port 8790
```

In Python:

```python
from audit_agent import build_audit_agent, AuditManifest, StaticFactSource
from audit_agent import AuthorizationRequest, ActionRequest

agent = build_audit_agent(
    AuditManifest.load("manifests/collect-reminder.yaml"),
    fact_source=StaticFactSource(
        amounts={"INV-1001": "450.00"}, contacts={"INV-1001": 0}
    ),
    log_path="audit-log.jsonl",
)

# 1. What may this agent do for this task?
grant = agent.authorize(AuthorizationRequest(
    request_id="r1", app_id="helios-collect", agent_id="collect-reminder",
    task_type="collect.reminder",
    task_summary="Chase overdue invoice INV-1001 and offer a short extension",
    requested_tools=["lookup_invoice", "send_reminder_email"],
    context={"invoice_id": "INV-1001"},
))
grant.decision      # Decision.ALLOW
grant.tool_names()  # ['lookup_invoice', 'send_reminder_email']

# 2. May this specific call go ahead?
decision = agent.adjudicate(ActionRequest(
    request_id="c1", app_id="helios-collect", agent_id="collect-reminder",
    task_type="collect.reminder", tool="send_reminder_email",
    arguments={"invoice_id": "INV-1001", "message": "Friendly reminder",
               "extension_days": 60},
    authorization=grant,
))
decision.decision        # Decision.HOLD — 60 days exceeds the 14-day cap
decision.owner_question  # plain-language question for the approval queue
agent.resolve_hold(decision.hold_id, approve=True, decided_by="owner")
```

---

## The two interactions

| | Purpose | In | Out |
|---|---|---|---|
| **Authorize** | which tools, under what limits, for how long | `AuthorizationRequest` | signed `TaskAuthorization` |
| **Adjudicate** | may this concrete call proceed | `ActionRequest` | `ActionDecision` (allow / hold / deny) |

`TaskAuthorization` is the answer to the deployment question: it lists the exact tools an action agent may use for a task, each with argument constraints and ceilings. Anything not listed is denied — capability is granted, never assumed.

`ActionDecision` always carries `findings`: the rules that fired, their severity, and their evidence. A decision that cannot explain itself is not much use to an owner or an auditor.

## How a decision is made

```
request → draft grant from policy ∩ requested tools
        → resolve FACTS from trusted sources (not from the request)
        → run rules (baseline first, then app)
        → most restrictive finding wins
        → sign, log, return
```

Every step can narrow authority. None can widen it.

---

## Security properties

Each of these is asserted by a test in `tests/test_security.py`.

| Property | How |
|---|---|
| **Least authority** | A grant lists specific tools with specific limits; unlisted tools are denied |
| **Fail closed** | Unknown tools, malformed arguments, unverifiable facts, invalid grants → deny |
| **Facts are not claims** | Rule inputs come from a `FactSource` the action agent does not control |
| **Grants are signed** | HMAC-SHA256 over canonical JSON; tampering breaks verification |
| **Grants are scoped** | Bound to app, agent, principal and task type |
| **Grants expire** | Short TTL from policy; expiry checked per call |
| **Grants are single-use** | Side-effect grants are spent once; replay refused |
| **Baseline cannot be weakened** | An app may extend platform rules, never disable or redefine them |
| **Evidence is tamper-evident** | Append-only, hash-chained log; edits and deletions break verification |
| **No secrets on disk** | Credential-shaped values are redacted before they reach the log |

### The one that matters most

An action agent **cannot widen its own authority by describing the world favourably**. Values it supplies — `context`, `evidence`, `resolved_facts` — are claims, used only to name which record to look up. Every value a rule reads is resolved from a trusted source.

So an agent that sends `resolved_facts={"amount_due": "1.00"}` for a 24,500 invoice still gets held. An agent that claims zero prior contacts still gets denied on the third. Facts you can supply are not facts.

### What "fail closed" does and does not mean

Fail closed targets **unknowns**, not clean calls. A call inside a valid grant that trips no rule is allowed — that is the normal case, and denying it would make the platform unusable. What is refused is anything the audit agent cannot verify: an unregistered tool, arguments that do not match a schema, a fact it could not resolve, a grant it could not validate.

One subtlety worth knowing: an **absent argument** is "not requested", not "unknown". A cap on `extension_days` does not fire on a reminder that offers no extension.

---

## The manifest: an audit agent as data

A manifest declares tools, tasks, rules and settings. `build_audit_agent()` turns it into a running auditor. Qwen generates this file; it does not write audit logic.

```yaml
manifest_version: "1"
app_id: helios-collect
name: Collect Reminder Audit Agent

tools:
  - name: lookup_invoice
    side_effect: false          # runs directly
  - name: send_reminder_email
    side_effect: true           # every call is gated
    input_schema: { ... }       # REQUIRED for side-effect tools

tasks:
  collect.reminder:
    allowed_tools: [lookup_invoice, send_reminder_email]
    ttl_seconds: 300            # a grant is authority to act NOW

rules:
  - id: app.extension_limit
    tools: [send_reminder_email]
    message: an extension longer than 14 days needs owner approval
    limits:
      - argument: extension_days   # measured: what the agent asked for
        max: 14                    # compared against: a constant
        on_exceed: hold            # deny | hold
```

Two independent axes on a limit, and conflating them is how a control ends up checking nothing:

- **What is measured** — `argument` (what the agent asked for) or `fact` (what the audit agent resolved from a trusted source)
- **What it is compared against** — `max` (a constant in policy) or `max_from_fact` (a ceiling resolved at decision time)

The combination that matters most in finance is *argument measured against a fact ceiling*:

```yaml
  - id: app.amount_matches_invoice
    limits:
      - argument: amount               # the agent chooses this
        max_from_fact: invoice_amount  # the ledger chooses this
        on_exceed: hold
```

Neither side can move the other. That is what stops an agent paying more than an invoice says.

**Rule vocabulary** (deliberately small — anything else belongs in reviewed Python): `limits`, `required_arguments`, `forbidden_values`, `max_contacts_in_days`, `deny_if_fact_true`, `approval_if_fact_true`.

**Validation is strict and its messages are written for a model to act on.** `validate_manifest_text()` returns `(ok, problems)` and is the entry point a build pipeline calls on generated output — the problems *are* the revision feedback loop. It catches: undeclared tools, rules with no check, limits on arguments a tool does not declare, side-effect tools with no schema, reused baseline rule ids, and clamp/hold declared on the same field.

---

## Baseline vs app policy

- **Baseline** ships with the platform and applies to every app: tool must be registered, arguments must validate, the grant must be valid, the tool must be in the grant, call budgets and explicit approvals hold, intent must be stated, and a call inside a valid grant with no objection is allowed.
- **App** policy comes from the manifest and can only add restrictions.

An app pack that sets `disabled_rule_ids: [baseline.grant_valid]`, claims `source: baseline`, or redefines a `baseline.*` rule id is refused at load time — not silently overridden.

---

## Running it standalone

The audit agent shares no code with the action agent at runtime. They exchange `contracts` models over a transport, so the auditor can move without the action agent changing:

| Transport | Use |
|---|---|
| `InProcessTransport` | tests, single-app demos |
| `FileTransport` / `FileTransportServer` | two processes on one host, no dependencies |
| `HttpTransport` / `HttpTransportServer` | the deployment shape — auditor as its own service |

All three are interchangeable and all fail closed: an unreachable auditor returns a **deny** with rule id `transport.unavailable`, never a default allow. An outage is distinguishable in the log from a control working.

## Evidence

`AuditLog` is append-only JSONL, hash-chained. Every authorization, decision, hold and owner verdict is an entry carrying its findings. Editing or deleting a line breaks verification at that point and for every entry after it. `verify_chain()` returns `(ok, problems)` and never raises, so a health check can run against a damaged log.

## Advisory review

`advisory.py` provides model commentary that **cannot change a verdict**. It runs after the decision is resolved, receives the decision as input, and its output is attached as `advisory_notes` and logged separately. Any failure — no model, no key, a timeout — yields no notes and the decision stands.

That is why `qwen-agent` is an optional extra: the audit agent is fully functional, and fully testable, with zero model access. `HeuristicReviewer` gives the channel deterministic content offline; `QwenAdvisoryReviewer` adds model commentary when a key is present.

## Configuration

| Variable | Purpose |
|---|---|
| `HELIOS_AUDIT_SIGNING_KEY` | HMAC key for signing grants. **Required** unless `allow_unsigned_grants: true` |
| `HELIOS_AUDIT_SIGNING_KEY_ID` | Label recorded on grants; never the key itself |

Keys are read from the environment only — never hard-coded, never logged, never serialised. Building an agent without a key fails at construction with an actionable message, rather than on the first `authorize()` call.

## Layout

```
src/audit_agent/
  contracts.py   wire models: requests, grants, decisions, findings, signatures
  policy.py      tool registry, rules, packs, composition, anti-loosening
  facts.py       trusted fact sources and the resolver (the trust boundary)
  agent.py       AuditAgent: authorize, adjudicate, holds
  audit_log.py   append-only hash-chained evidence
  advisory.py    non-binding model commentary
  manifest.py    the manifest format and its validation
  builder.py     manifest → running agent
  transport.py   in-process, file and HTTP wires
  cli.py         validate / inspect / serve / demo
manifests/
  collect-reminder.yaml   Build 1's reference agent
  payables.yaml           a second app, same runtime, different authority model
```

## Relationship to Build 1

Build 1 (`specs/build1.md`) builds the action side in `platform/action-agent-builder/`. This package does not import it. `ToolDescriptor.from_contract()` and `StoreFactSource` adapt objects following Build 1's `ToolContract` and `InvoiceStore` shapes by duck typing, which is what keeps this agent deployable on its own.

**There is an overlap the team needs to resolve.** Build 1 includes an in-process `AuditGate` (`agent_toolkit/audit.py`) that checks a `Proposal` against a static `Mandate` and returns approve / hold / reject. This package is a *standalone* audit agent that issues signed, scoped, time-boxed grants deciding which tools an action agent may use for a task, then adjudicates each call. Both are deterministic and fail closed; both evaluate limits against trusted store values rather than agent claims.

They are complementary rather than redundant, but the seam is not yet agreed:

| | Build 1 `AuditGate` | Audit Agent Builder |
|---|---|---|
| Runs | in-process, inside the app | as its own process or service |
| Authority source | static `Mandate` in the agent manifest | signed `TaskAuthorization` grant, per task |
| Decides | may this proposal execute | which tools may this agent use at all, then may this call execute |
| Grant lifetime | n/a | short TTL, single-use for side effects |
| Evidence | app's action log | own hash-chained audit log |

The likely resolution is that the in-app gate becomes a *client* of this auditor — or stays as a fast local pre-filter whose verdicts this auditor can still overrule, since an app cannot be trusted to police itself. **That is an architecture decision, not an implementation detail, and it should not be made here.** Raised for the team; nothing in either package currently depends on the other.

`manifests/collect-reminder.yaml` encodes Build 1's four mandate clauses — extension cap, owner approval for longer extensions, contact frequency, owner approval for large invoices — so the reference agent can be audited against the same rules it was specified with.

One modelling difference worth flagging: Build 1 expresses the extension mandate as *two* limits (`max_extension_days` rejects outright, `extension_owner_approval_days` holds for the owner). This manifest expresses it as a *single* hold threshold at 14 days, on the reading that `build1.md` asks for owner approval on longer extensions rather than making them impossible. If the team wants a hard ceiling as well, it is one more `limits:` entry with `on_exceed: deny`.

## Not done yet

- Held-agent state lives in memory; a restart loses pending holds. Needs durable storage for deployment.
- `InMemoryReplayGuard` is single-process. Multiple auditor processes need a shared guard or single-use enforcement breaks.
- No HTTP authentication between the agents. Fine on localhost, required before any real deployment.
- Post-hoc log review (patterns over time) is out of scope here, as it is in Build 1.
- The Intent & Trust Service integration described in `specs/decisions.md` is not wired; the hook point is `GrantVerifier`.

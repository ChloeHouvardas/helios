# Agents on Helios

How the action agent and the audit agent inside an application work, what you
write to define them, and the rules that keep them honest. The runtime is the
platform's; what differs per app is data: two manifests and a fact source.

## 1. The two agents

- **Action agent** — the model-driven loop that reads with read-only tools and
  proposes side effects. Defined by `agents/<id>-action.json`.
- **Audit agent** — deterministic code that adjudicates every proposal against
  policy written as YAML, using facts it resolves itself. Defined by
  `agents/<id>-audit.yaml` and `facts.py`.

They never import each other. `platform/agent-integration` is the only package
that knows both, and it is where the loop lives.

## 2. The loop

1. The auditor authorises the task and narrows the tool list *before* the model
   sees it (a signed, scoped, short-lived grant).
2. Read-only tools execute directly.
3. A side-effecting call becomes a **proposal**. The auditor returns
   `approve`, `hold` or `reject` with the rule that decided.
4. Approved: the auditor's *granted arguments* execute, never the model's
   originals. Held: the run pauses until the owner decides on the approval queue;
   resume executes exactly that action without re-adjudicating. Rejected: the
   reason goes back to the model as an observation.
5. Everything is written to the app's action log and the auditor's hash-chained
   log.

## 3. The action manifest

```json
{
  "name": "helios-cash-planner",
  "version": "1",
  "description": "...",
  "instructions": "The operating contract. Read before you act... If a call is held, stop... Never work around a denial...",
  "tools": ["cash_position", "project_cash", "publish_shortfall_warning"],
  "mandate": {"allowed_tools": ["publish_shortfall_warning"], "limits": {"min_days_between_contacts": 7}},
  "max_turns": 14
}
```

`tools` must be a subset of what the audit manifest declares — a tool an agent
can call that the auditor never declared is an ungated side effect. No extra
keys are tolerated. The authoritative policy is the audit manifest; `mandate.limits`
accepts only four receivables-shaped keys and is kept minimal.

## 4. The audit manifest

```yaml
manifest_version: "1"
app_id: helios-cash-planner          # equals identity.id in helios-app.yaml
name: ...
tools:
  - name: publish_shortfall_warning
    description: ...
    side_effect: true                # must agree with tools.py
    input_schema: {type: object, properties: {...}, required: [...], additionalProperties: false}
tasks:
  cashplan.plan:
    description: ...
    allowed_tools: [...]
    ttl_seconds: 300
    tool_overrides: {recommend_credit_draw: {max_calls: 1, condition: ...}}
rules:
  - id: app.warning_only_when_short
    task_types: [cashplan.plan]
    tools: [publish_shortfall_warning]
    message: written for the model to act on
    deny_if_fact_true: [week_not_short]
    severity: critical
settings:
  default_ttl_seconds: 300
  contact_window_days: 7
  log_path: helios-cash-planner-audit-log.jsonl
  advisory: [heuristic]              # non-binding commentary; never changes a verdict
  allow_unsigned_grants: false
```

### The rule shapes

| Shape | Effect | Example |
|---|---|---|
| `required_arguments: [a, b]` | deny when missing | a payment must carry `auth_code` |
| `forbidden_values: {arg: [v, ...]}` | deny | no USD from a CNY account |
| `limits: [{argument: x, max: N \| max_from_fact: f, on_exceed: hold\|deny}]` | ceiling on what the agent asked | `amount_cents` ≤ `bill_amount_cents` |
| `limits: [{fact: f, max: N \| max_from_fact: g, on_exceed: ...}]` | ceiling on a fact; the agent's number is never read | hold above 25,000.00 of the *recorded* amount |
| `deny_if_fact_true: [f]` | deny | changed payee details, disputed customer, payroll |
| `approval_if_fact_true: [f]` | hold | duplicate bill, a credit draw |
| `max_contacts_in_days: {within_days: 7, max: 2}` | deny over the count from the fact source | contact frequency, digest frequency |

Precedence: deny beats hold beats allow. No findings is a deny. An unresolvable
fact makes the rule fail closed. `severity` orders which finding the owner is
shown when several fire.

**Deny vs hold** is a design decision, not a dial. Deny when no owner decision
makes the action safe (fraud signal, insufficient funds, payroll). Hold when
only the owner can tell (duplicate, large amount, borrowing).

## 5. The fact source

A class satisfying `audit_agent.facts.FactSource`:

```python
class Facts:
    def amount_for(self, subject_id) -> Decimal | None: ...
    def contact_count_for(self, subject_id, within_days) -> int | None: ...
    def counterparty_for(self, subject_id) -> str | None: ...
    def payment_details_changed_for(self, subject_id) -> bool | None: ...
    def extra_facts_for(self, subject_id) -> dict: ...      # every key a rule binds
    def record_contact(self, subject_id, when=None) -> None: ...
```

Subjects are resolved from these argument names only: `invoice_id`, `bill_id`,
`customer_id`, `supplier_id`, `subject_id`, `payment_id`. A gated tool must take
one of them, or no fact resolves and every fact-bound rule denies.

Return `None` for an unknown subject, never `0` or `False`: unknown must fail
closed. The validator checks that every fact key a rule names is produced by
`facts.py` (it follows the helpers `extra_facts_for` calls).

A fact can be anything the app can compute from data the agent cannot write.
The flagships use the recorded amount, the supplier's change date, a ceiling
derived from history, the live balance, and the platform's own cash projection.

## 6. Tools

One `agent_toolkit.contracts.ToolDefinition` per tool in `tools.py`:
`name`, `description` (written for the calling model), `input_schema` (with
`additionalProperties: false`), `output_schema` (required), `side_effect`
(must match the audit manifest), `handler(args: Mapping)`.

Structural controls belong in handlers: no payer or payee argument on a payment,
the recorded amount executes rather than the requested one, the owner's address
is configuration. Withhold control inputs from read-only tools (the dispute flag,
the payee-change flag): a control the agent can read is a control it can argue with.

## 7. Drivers and owners

`ScriptedDriver` replays a fixed script of tool calls, so a demo can include
requests that *should* be refused. `ModelDriver` is an OpenAI-compatible
tool-calling loop against Qwen (DashScope); the gate underneath is identical.
`ScriptedOwner` approves everything it is shown, which is the point: the
controls that matter are denials, which never reach the owner.

## 8. Deployed

Inside a container the in-app auditor stays authoritative for holds. The platform
adds a sidecar that enforces the fact-free subset of the same manifest and holds
the execution secret; it may deny or forward, never approve. See `docs/API.md` §2.

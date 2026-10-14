# Helios Collect — AI Receivables Agent

Flagship app 1 of the Helios platform. It chases overdue invoices, negotiates
reasonable terms with customers who have not paid, and directs money to the right
account when payment arrives.

```bash
cd apps/helios-collect
python3 app.py                  # narrated run over all ten scenarios
python3 app.py --trace          # every action, verdict and rule
python3 app.py --scenario INV-1006   # one control path
python3 app.py --json           # machine-readable
python3 app.py --list           # what each scenario demonstrates
python3 app.py --dashboard      # the receivables book
```

Tests run from the repo root with the repo venv (`scripts/bootstrap-venv.sh`
builds it; `scripts/test-all.sh` runs every suite):

```bash
.venv/bin/python -m pytest apps/helios-collect/tests -q   # 74 tests, offline
```

Runs offline. No API key, no network, no dependencies beyond the four platform
packages plus `pydantic`, `pyyaml` and `jsonschema`. Python 3.13+ (the audit
builder's floor).

---

## What it does

Three capabilities from `specs/helios-context.md` §"Helios Collect", as three task
types in one audit manifest:

| Task | Job | Gated tools |
|---|---|---|
| `collect.reminder` | chase an overdue invoice | `send_email` |
| `collect.negotiate_terms` | agree and record an extension | `send_email`, `record_agreed_extension` |
| `collect.apply_payment` | apply inbound money | `record_inbound_payment` |

Read-only tools (`list_overdue_invoices`, `lookup_invoice`) execute directly. Every
side effect becomes a proposal the audit agent adjudicates: allow, hold for the
owner, or deny with a reason the agent can act on.

## The demo

Ten invoices, one control path each. `python3 app.py` runs all of them.

| Invoice | Amount | Outcome | Control |
|---|---|---|---|
| INV-1001 | 4,200.00 | **sent** | none — the normal path |
| INV-1002 | 850.00 | **refused** | `app.contact_frequency` |
| INV-1003 | 12,500.00 | **held → approved → sent** | `app.large_invoice_approval` |
| INV-1004 | 3,100.00 | **held → declined** | `app.extension_offer_limit` |
| INV-1005 | 2,200.00 | **sent + terms recorded** | none — inside the agent's own authority |
| INV-1006 | 1,980.00 | **refused** | `app.disputed_invoice` |
| INV-1007 | 3,100.00 | **refused** | `app.not_yet_due` |
| INV-1008 | 450.00 | **recorded** | none — money routed by configuration |
| INV-1009 | 12,500.00 | **held → declined** | `app.payment_exceeds_amount_due` |
| INV-1010 | 9,600.00 | **held → approved → recorded** | `app.extension_over_credit_limit` |

**INV-1006 and INV-1009 are the ones to show a judge.** The scripted owner approves
every hold it is shown, and those two still do not go through: INV-1006 is denied in
code and never reaches the queue at all, and INV-1009's over-claim is measured
against a ceiling the agent cannot move. The control does not depend on the human
noticing, and it does not depend on the agent behaving.

## The three ideas this app is built on

**1. The model proposes, code decides.** An action agent may ask for anything; what
it gets is bounded by policy written as data (`agents/helios-collect-audit.yaml`)
and enforced by deterministic code that fails closed. Eleven app rules plus eight
platform baseline rules. No Python policy in this app.

**2. Facts are not claims.** Every value a rule depends on is resolved from the
app's own store by `facts.py`, which the agent does not control. The agent gets to
*name* which invoice it means; it never gets to *state* what that invoice says. So
an agent claiming 20,000.00 arrived on a 12,500.00 invoice is held, and an agent
supplying `resolved_facts={"invoice_amount_cents": 100}` changes nothing —
`AuditAgent._facts_for_action` deliberately does not merge request values.

**3. Every side effect has somewhere for a human to land.** Held actions pause the
agent, so `render_queue` implements the `approval_queue` page and shows the
auditor's own `owner_question`, never a summary the agent wrote about itself.

### The strongest control here is structural, not a rule

`record_inbound_payment` takes an invoice id and an amount. **It takes no account
argument.** Where inbound money lands is resolved from `config.py` using the
invoice's own customer. An agent cannot misdirect a payment because there is no
field in the request to misdirect it with.

`tests/test_controls.py::test_no_tool_accepts_a_destination_account` asserts this
against the JSON Schema rather than by reading code, so adding `payee` or `account`
to an inbound tool later fails the build.

### Deny vs hold, decided deliberately

`app.not_yet_due` and `app.disputed_invoice` **deny**. No owner decision makes
chasing an early or disputed invoice appropriate, and routing them to a queue would
invite approval of the thing most likely to damage the relationship. Everything else
**holds**, because it might be legitimate and only the owner can tell. Getting this
backwards either floods the queue with unactionable items or silently permits harm.

### Two directions, two controls

A ceiling on an *argument* catches **over**-recording. It does not catch
**under**-recording: applying 1.00 against a 12,500.00 invoice is inside that
ceiling and is allowed. That is not a defect, it is the wrong rule for that threat.
Under-recording is handled in the tool instead — the invoice stays `part_paid` with
a balance owed, so the shortfall stays visible on the dashboard rather than being
quietly closed. Both directions are tested separately, so nobody "fixes" the
ceiling by adding a floor without understanding where the real control lives.

---

## How it fits the platform

This app *runs through* the platform rather than beside it. It composes four
packages and edits none of them:

| Package | What this app takes from it |
|---|---|
| `platform/agent-integration` | the gated agent loop, the audit bridge, the transports |
| `platform/audit-agent-builder` | the standalone auditor, signed grants, hash-chained log |
| `platform/action-agent-builder` | the tool contract, the store, the action log, `send_email` |
| `platform/financial-tools` | the payments ledger, the knowledge graph |

`build_integration()` returns an auditor, a bridge and a runtime already connected.
Authority comes from a reviewed file (`manifest_path` points at this app's audit
manifest), not from anything the app generates for itself at runtime — otherwise the
app would be writing its own permission slip.

Two substitutions worth explaining:

- **The fact source is this app's own** (`facts.py`), replacing
  `HeliosStoreFactSource`, because this app's policy needs facts that adapter does
  not supply: due-ness, disputes, credit exposure, cents-denominated amounts.
  `AuditAgent.facts` is an injected attribute for exactly this reason, so no
  platform package is edited.
- **`send_email` is the platform's handler, reused as-is.** Its contract is exactly
  what the audit manifest describes, and duplicating it would create a second
  definition of the same side effect that could drift. `lookup_invoice` is *not*
  reused: this app's version withholds `disputed`, a control input the agent has no
  business arguing with.

> **Note on `entry-point/references/platform-status.md`.** That file states there is
> no agent loop, no scaffolder and no validator, and that `qwen_agent` is not
> installed. The first is now **stale**: `platform/agent-integration` ships the
> gated loop, a bridge, and a CLI (verified 2026-10-15). The scaffolder and
> validator still do not exist, so `helios-app.yaml` is written but unread except by
> `entry-point/checks/check_app.py`. That file is a snapshot and says so; this app
> relies on `agent_integration` rather than hand-rolling a loop.

## Files

```
apps/helios-collect/
├── README.md                       # this file
├── helios-app.yaml                 # the app spec (specs/app-builder.md)
├── app.py                          # assembly, scenarios, CLI pages
├── config.py                       # routing and thresholds — the owner's settings
├── book.py                         # data layer over agent_toolkit.store.Store
├── facts.py                        # the fact source: where "facts are not claims" lives
├── tools.py                        # the five tools
├── agents/
│   ├── helios-collect-audit.yaml   # the policy — written first, per BUILD.md step 3
│   └── helios-collect-action.json  # the agent's operating contract
├── data/
│   ├── customers.json              # incl. control inputs and credit limits
│   └── invoices.json               # ten invoices, one control path each
├── tests/
│   └── test_controls.py            # 74 tests: each control fires
└── runs/                           # generated: action logs + hash-chained audit log
```

## Verification

```bash
# from the repo root, using the repo venv
.venv/bin/python -m pytest apps/helios-collect/tests -q              # 74 passed
.venv/bin/python entry-point/checks/check_app.py apps/helios-collect # 0 errors, 3 warnings
.venv/bin/python -m audit_agent.cli \
    inspect apps/helios-collect/agents/helios-collect-audit.yaml
```

The three remaining checker warnings are expected and explained in
[§ Checker warnings](#checker-warnings) below.

## Evidence

Two logs, two purposes, kept separate on purpose:

- `runs/<run-id>/action-log.jsonl` — the app's record of what it did.
- `runs/audit/helios-collect-audit-log.jsonl` — the auditor's hash-chained,
  tamper-evident record of what was authorised. Every entry carries `hash` and
  `prev_hash`; edits and deletions break verification. Denials are logged too,
  because a refusal is an event worth evidencing rather than a silent non-occurrence.

---

# Limitations

Recorded honestly, because a control that is claimed but absent is worse than one
that is documented as missing. Grouped by whose problem it is.

## Known platform gaps this app works around

**1. `auth_code` is not redacted by `audit_agent.redact()`.**
The platform's fixed key set does not include `auth_code`, the single-use credential
that authorises a payment, so it is written to the audit log in full inside the
`owner_question` string. `entry-point/references/platform-status.md` §"Credential
handling" records this and the two-line upstream fix.

*This app has no exposure to it:* collections applies **inbound** money via
`receive_payment`, which needs no intent code, and never calls `payments.send`.
`test_no_credential_shaped_value_reaches_the_logs` is the tripwire — it fails if a
future change introduces an outbound payment carrying a credential.

**2. No declarative two-argument pairing check.**
`send_email` takes a `customer_id` *and* an `invoice_id`, and the handler raises if
the invoice does not belong to that customer. The audit rules cannot express that
pairing: `limits` and `deny_if_fact_true` resolve facts for **one** subject at a
time, and `FactResolver._subject_key` picks `invoice_id` first — so a valid invoice
id makes a mismatched customer id invisible to the frequency rule.

The gate **approves**, the handler **refuses**, nothing is sent, and the run ends in
`ERROR`. It fails loudly and safely, but the refusal lands *after* the auditor
approved. `platform/agent-integration/README.md` records the same gap against the
same tool. Closing it needs a pairing fact on the audit side or a rule shape that
can compare two arguments. Asserted by
`test_a_customer_invoice_mismatch_is_caught_but_not_by_the_gate`.

**3. Over-recording split across several calls is not caught.**
`app.payment_exceeds_amount_due` is per invoice amount, not per total across calls.
Two 10,000.00 recordings against a 12,500.00 invoice are each inside the ceiling and
each allowed. The invoice ends `part_paid` with a balance owed, so it is not
silently over-credited — but the *aggregate* over-recording is not stopped. A
cumulative-applied fact would close this. Asserted by
`test_splitting_a_payment_does_not_evade_the_ceiling`.

**4. The two audit gates overlap, and nobody has decided which wins.**
`agent_toolkit.audit.AuditGate` (in-process, four receivables-shaped
`MandateLimits` keys, hard-coded to `send_email`) and `audit_agent.AuditAgent`
(standalone, declarative YAML rules) both exist. `agent_integration`'s bridge makes
the standalone auditor authoritative, and this app inherits that choice by composing
the package rather than making it silently.

Unlike the payables example, this app *is* a receivables app, so all four
`MandateLimits` keys do fit and are populated in the action manifest — but they are
**documentation, not enforcement**. The audit manifest is authoritative.
`AGENTS.md` requires team approval for architecture changes, so this is flagged
rather than resolved. Asserted by `test_the_in_process_gate_is_not_the_authority`.

**5. Incompatible tool contracts between packages.**
`agent_toolkit.ToolDefinition` uses `side_effect` (singular), a JSON Schema object
and `handler(args: Mapping)`; `fin_tools.ToolSpec` uses `side_effects` (plural), a
flat `{name: "type"}` map and `handler(**kwargs)`. `agent_integration.adapters` and
`fin_tools.toolkit_bridge` close this. This app consumes both bridges rather than
adapting anything itself, but the seam is real and is why a gated tool can silently
register as read-only if a future app wires the packages without them.

**6. `build_world()` binds module-level singletons.**
`fin_tools` binds `payments` and `email` into module globals, so with two worlds in
one process the **last**-built one wins for *every* `registry.call` dispatch —
verified 2026-10-15: two bundles returned the same balance from either registry.

This app's handlers therefore call `bundle.payments` and `bundle.graph` **directly**
rather than through `registry.call`. That is a workaround, not a preference, and it
matters because the tests build a fresh world per case.

**7. `Store` has no public invoice mutator.**
`agent_toolkit.store.Store` exposes mutators for contacts and the outbox but not for
invoice fields — it was written for Build 1, where invoices are only read. This app
must move a due date and mark an invoice paid, so `book.Book._mutate_invoice` writes
into the store's private `_invoices` dict. Reaching into a private attribute of a
package this app does not own; confined to one method so there is a single place to
change if the platform grows a public mutator.

**8. Holds are in-memory only.**
`AuditAgent` keeps holds in a dict and `InMemoryReplayGuard` is single-process, so
pending approvals and grant-replay protection are **lost on restart**. A held action
survives only as long as the process. Documented as a gap in the audit builder.

**9. `check_app.py` misreads `payment_behavior` as money.**
Its heuristic splits field names into words and flags any containing a money word
(`payment`, `amount`, `balance`, …) that is not typed `decimal` or `integer`. A
prose field named `payment_behavior` is therefore reported as monetary. Renamed to
`payer_behavior` here rather than weakening a shared checker; flagged for whoever
owns `entry-point/checks/`.

## Things this app does not do

**No agent loop with a real model.** The scenarios are fixed scripts, not model
output. `agent_integration.model_driver` exists and drives a real model over an
OpenAI-compatible endpoint, but no API key is configured in this checkout and the
competition rules require Qwen as the only generative model. `ScriptedDriver` in
`app.py` is a stand-in that obeys the action manifest's operating contract —
including not retrying a held or denied call. Substituting a real loop means
replacing that one object; the gate underneath does not change, which is the point
of the split. **What this loses:** the agent cannot handle an unexpected customer
reply, a partial payment it did not anticipate, or a negotiation it has not been
scripted for.

**No web UI.** The five `ui.pages` declared in `helios-app.yaml` exist as CLI
renderers in `app.py` (`render_dashboard`, `render_table`, `render_queue`,
`render_settings`). The `settings` stack names `helios-python-web-1`, but
`stacks.yaml` does not exist, so that name resolves to nothing. The prototype runs
on plain Python with no web framework — a legitimate reading of "the mechanism, not
the choice", but it must not be presented as a stack the platform ships.

**No database.** The store is JSON fixtures loaded into memory, and writes do not
persist between runs. `specs/tech-stack.md` fixes SQLite via SQLAlchemy as the
default; nothing generates DDL from `data.local.entities` because the schema tool
does not exist.

**No scheduling or triggers.** Agents run manually, per `specs/build1.md` §"Not in
Build 1". A real collections app would run nightly over the overdue queue.

**No bank feed.** Inbound payments are recorded from the scenario's arguments, not
read from a real feed. This is what makes limitation 3 above possible in principle:
with a feed, the reference would be matched against an actual inbound transaction
and the amount would not be the agent's claim at all.

**No duplicate-payment protection on inbound money.** `payments.receive_payment`
takes no idempotency key, unlike `send_payment`. Verified 2026-10-15: calling it
twice with the same reference credits the ledger twice (two `payment_id`s, balance
doubled). `send_payment` dedupes on `idempotency_key`; the receive path does not.
A double-recorded inbound payment therefore inflates the ledger, and the invoice
status stays `paid` either way so nothing surfaces it.

**No multi-currency handling.** Fixtures are all CNY. `record_inbound_payment`
accepts a `currency` enum and passes it to the ledger, but nothing converts between
currencies or checks that a payment's currency matches the invoice's. A EUR payment
against a CNY invoice would be recorded without complaint.

**No partial-payment negotiation.** The spec's "negotiates with the customer on
reasonable terms" is implemented as due-date extensions only, not as agreed
reductions, instalment plans or settlements. Those would need new gated tools and
new policy.

**The knowledge graph is a local prototype.** `record_agreed_extension` and
`record_inbound_payment` publish to `fin_tools`' in-process `KnowledgeGraph`, not to
a shared service — that service does not exist, and `specs/interfaces.md` still
lists the cross-app graph interface as to be defined. The verified/observation split
is real and tested, but nothing outside this process reads it, so Helios Cash
Planner cannot yet forecast against these due dates.

## Checker warnings

`python3 entry-point/checks/check_app.py apps/helios-collect` reports 0 errors and 3
warnings. All three are expected:

1. *`graph.publishes` entity 'negotiation' is not declared in `data.local`.* Correct
   and intentional. A negotiation is cross-app graph state, not a local table; its
   local record is `invoice.extensions`.
2. *graph declares publishes but no reads.* Correct. This app originates collections
   and terms events rather than consuming them. The consumer is Helios Cash Planner,
   which would read `invoice` to forecast against real due dates — which is exactly
   why the verified publish matters.
3. *rules depend on these trusted fact keys …* Informational; the checker can only
   see the names. `test_every_rule_fact_key_is_supplied_by_the_fact_source` asserts
   the running fact source actually supplies all seven, which is the property that
   matters: a fact-bound rule whose fact never resolves denies everything, and it
   fails in the safe direction, which is why it goes unnoticed until the demo.

## Security notes

- **No hard-coded credentials.** `HELIOS_AUDIT_SIGNING_KEY` comes from the
  environment. With none set, `build_integration()` generates an **ephemeral
  process-local key** so grants are still signed — the more honest demo, and one
  that cannot be shipped by accident. `app.py` prints a warning when it is set, and
  `test_the_grant_is_signed` asserts grants carry a signature. This app deliberately
  does **not** use the `os.environ.setdefault("...", "0"*64)` pattern from the
  worked example, which silently produces a key that looks configured and is not.
- **Money is `decimal` or integer cents, never float.** Fixtures store decimal
  strings; the fact source converts without routing through binary floating point
  (`_to_decimal` refuses to, and rejects `bool` as a number).
- **Least authority.** Each task is granted its own allowlist and nothing more. An
  ungranted tool is never offered to the agent, which is stronger than rejecting it
  after the fact.
- **Grants are short-lived and single-use for side effects.** 300s for contact, 120s
  for money movement. The bridge requests a fresh grant per side effect, since a
  spent grant is a replay and the auditor refuses it.
- **Control inputs are withheld from the agent.** `disputed` and
  `contact_history_days` never appear in a `lookup_invoice` result. The auditor reads
  them from the fact source, so the agent cannot argue with values it was never
  given.
- **Licence walls.** This app gives no investment advice and produces no compliance
  output that could be read as a legal opinion. It does not collect behavioural
  telemetry: `payer_behavior` is a fixture string, not observed data, which keeps it
  clear of PIPL constraints on profiling.

## Not in scope

Per `specs/build1.md` and `specs/app-builder.md`: real financial tools (the
`fin_tools` prototypes touch no network, database or real money), LLM-based audit
checks (the gate is deterministic rules only; `advisory: [heuristic]` adds
commentary that can never change a verdict), post-hoc audit review, triggers and
scheduling, the shared services themselves, and hosting/deployment artifacts — all
still open questions in `specs/decisions.md`.

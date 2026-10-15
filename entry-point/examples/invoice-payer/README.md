# invoice-payer — a complete Helios app, built from one sentence

The user request this answers:

> "Make me an app that automatically manages and pays my invoices."

Read this with `../../BUILD.md` open. This directory is what following that
procedure produces.

```bash
python3 app.py                     # narrated run over five invoices
python3 app.py --trace             # per-step trace for each
python3 app.py --invoice INV-1003  # one control path in isolation
python3 app.py --json              # machine-readable
```

Tests run from the repo root with the repo venv (`scripts/bootstrap-venv.sh`
builds it; `scripts/test-all.sh` runs every suite):

```bash
.venv/bin/python -m pytest entry-point/examples/invoice-payer/tests -q
```

Runs offline: no API key, no network, no real money. Dependencies are the three
platform packages plus `pydantic` and `pyyaml`. Python 3.13.

## What it is made of

| File | Step in BUILD.md | Role |
|---|---|---|
| `helios-app.yaml` | 3 | The app spec — the design record |
| `agents/invoice-payer-audit.yaml` | 4 | **The policy.** Written first. Six rules, no Python |
| `agents/invoice-payer-action.json` | 6 | The agent: instructions, tools, mandate |
| `app.py` | 5, 7, 8 | Fact source, gated runtime, scripted owner, demo |
| `data/invoices.json` | — | The app's data, including fact-source fields |
| `tests/` | 9 | One assertion per control |
| `runs/` | — | Generated: action log + hash-chained audit log |

## The five invoices, and what each one proves

| Invoice | Supplier | Amount | Outcome | Control |
|---|---|---|---|---|
| INV-1001 | Northwind | $1,250.00 | **paid** | normal path, inside every limit |
| INV-1002 | Delta Mills | $48,000.00 | **held → owner approves → paid** | `app.large_payment_approval` |
| INV-1003 | Cinder Logistics | $760.00 | **denied** | `app.payee_details_changed` |
| INV-1004 | Northwind | $1,250.00 | **held → owner declines** | `app.duplicate_invoice` |
| INV-1005 | Northwind | $310.50 | **paid** | normal path |

### INV-1003 is the demo

Watch it closely. The scripted owner **confirms the payment anyway** — a busy owner
rubber-stamping $760 to a supplier whose banking details just changed. The payment
still does not go out:

```
  INV-1003        760.00 USD  Cinder Logistics
    [owner asked] confirm payment of 760 USD to sup-cinder for invoice INV-1003?
    [owner confirms]
    -> REFUSED  (this supplier's payment details changed recently; payment is
                 refused until the owner re-verifies the payee)
```

That is the platform's central claim demonstrated rather than asserted. The control
does not depend on the human noticing, and it does not depend on the agent
behaving. It is a **deny**, not a hold, because the agent should not proceed at all
until someone re-verifies the payee out of band — putting it in a queue would
invite the owner to approve the thing that is most likely to be fraud.

Note that the agent never sees `payment_details_changed`. `_lookup_invoice` strips
it from the supplier record before returning. The auditor reads it from the fact
source. An agent cannot argue with a value it was never given.

### INV-1004 is a hold, not a deny

A duplicate-looking invoice *may* be legitimate — two genuine invoices for the same
amount and PO. Only the owner can tell, so it goes to the queue and the scripted
owner declines it. Contrast with INV-1003: same app, same tool, two different
verdicts, and the difference is a deliberate policy judgement rather than an
accident of configuration.

### INV-1002 shows a hold that is approved

$48,000 matches the recorded invoice exactly, so `app.amount_matches_invoice` is
satisfied; `app.large_payment_approval` still routes it to the owner because a
large payment is a decision a human should make even when it is correct. The owner
approves and `execute_held()` runs the **exact arguments that were held** — an
approval applies to that action and no other, so the agent cannot swap in a
different amount afterwards.

## The two human checkpoints are not the same thing

Worth understanding, because conflating them makes the intent check decorative:

- **Intent confirmation** happens *before* a payment is proposed. The owner
  authorises, then the app mints the `auth_code` that `app.requires_verified_intent`
  requires. **The agent cannot mint it.** If it could, the agent would be
  self-authorizing and the rule would check nothing.
- **Hold resolution** happens *after* the auditor holds a proposal that exceeded a
  limit.

In the code: `ScriptedOwner.confirm_intent()` vs `ScriptedOwner.decide_hold()`. The
agent calls neither — it asks, and waits.

## Facts are not claims, concretely

`InvoiceFactSource` in `app.py` is the class that makes this true. The auditor
resolves every value a rule depends on through it:

- `app.amount_matches_invoice` caps `amount_cents` against
  `invoice_amount_cents`, which comes from `extra_facts_for()`. **The agent chooses
  the amount; the ledger chooses the ceiling.** Neither can move the other.
- `app.payee_details_changed` reads `payment_details_changed_for()`.
- `app.duplicate_invoice` reads `duplicate_invoice` from `extra_facts_for()`.

**Be precise about which direction that ceiling guards**, because it is easy to
overstate and a judge will ask. A ceiling on an *argument* catches
**over**payment. Paying $1.00 against a $48,000 invoice is *inside* that ceiling,
and the rule allows it. Understating is stopped by a different mechanism: the
`auth_code` is an HMAC over (amount, currency, payee), so a code the owner
confirmed for $48,000 will not verify for $1.00. Two directions, two controls.

`tests/test_controls.py` pins down all of it — including the boundary, so nobody
"fixes" the rule by adding a floor without understanding that the real control is
elsewhere:

| Test | What it proves |
|---|---|
| `test_the_agent_cannot_overpay_an_invoice` | the fact ceiling catches overpayment |
| `test_underpayment_is_not_caught_by_the_ceiling` | and does **not** catch underpayment |
| `test_understating_an_amount_requires_the_owner_to_confirm_it` | intent binding does |
| `test_a_lie_in_resolved_facts_changes_nothing` | supplied values are claims, not facts |
| `test_an_unresolvable_fact_fails_closed` | unknown is never read as small |
| `test_the_fact_source_never_reads_the_request` | the property that makes the above meaningful |

```bash
# from the repo root, with the repo venv
.venv/bin/python -m pytest entry-point/examples/invoice-payer/tests -q   # 41 tests, offline
```

The two to show a judge: `test_a_lie_in_resolved_facts_changes_nothing` (an agent
that asserts its own facts gets held anyway) and
`test_the_fact_source_never_reads_the_request` (which proves it structurally, by
inspecting the fact source's method signatures for any way to pass a request in).

## Known limitations — stated plainly

This is a prototype. These are real, and none is hidden by the demo:

- **No LLM, by choice.** `ScriptedPayerAgent` is deterministic code following the
  manifest's instructions. A real model loop *does* exist now —
  `platform/agent-integration/`, driving Qwen over DashScope — but this example
  deliberately runs offline so it needs no key, no network and no quota, and cannot
  fail on stage. The point it demonstrates is the gate, not the model: swap the
  agent, keep the runtime, and every control still fires. See
  `../../references/agent-loop.md`.
- **No web UI.** The `approval_queue` page declared in `helios-app.yaml` is the CLI
  `ScriptedOwner`, which prints the auditor's own `owner_question`. Five pages are
  declared; none is rendered.
- **No database.** `InvoiceBook` reads a JSON file. In a generated app this is the
  schema the schema tool would have created from `data.local.entities`.
- **`stacks.yaml` does not exist**, so `stack.use: helios-python-web-1` resolves to
  nothing. The stack itself is decided (`specs/tech-stack.md`: FastAPI + SQLite +
  Jinja2/HTMX); the named-stack *mechanism* the spec describes is what is unbuilt.
- **The gate question is settled, not open.** The standalone `audit_agent` is the
  authoritative in-app gate per `specs/decisions.md`; `agent_toolkit.audit.AuditGate`
  is legacy, in no run path, and deprecated in its docstring (its four
  receivables-shaped limit keys could not express this payables app's policy
  anyway). This app was already built that way — it just is no longer a judgement
  call needing sign-off. See `../../references/platform-status.md`.
- **Holds are in-memory only** in the audit builder — lost on restart. A
  long-running app needs them persisted before an owner can decide hours later.
  `specs/decisions.md` records the answer (SQLite under the app's data dir); P2
  implements it.

### Fixed since this example was written

- **The audit log no longer leaks `auth_code`.** `auth_code` and
  `authorization_code` are now in `audit_agent`'s `REDACTED_KEYS`, so the credential
  is masked before it can reach the hash-chained log via `owner_question`. This app's
  own action-log redaction is now defence-in-depth rather than the only layer. One
  consequence for your manifest: the auditor redacts *before* schema validation, so
  `auth_code` must not carry a `minLength` or `pattern` — it would only ever be
  validating the `***REDACTED***` placeholder. This app's manifest dropped its
  `minLength: 16` for exactly that reason; presence is enforced by `required` plus
  the critical `app.requires_verified_intent` rule, and validity by the
  `payments.send` tool at execution. See
  `../../references/platform-status.md` §"Credential handling".

## Where a real generated app would live

`specs/tech-stack.md` fixes the monorepo layout as `platform/`, `services/`,
`apps/`, and `specs/app-builder.md` says the scaffolder writes to `apps/<name>/`.
**`apps/` exists** (`apps/helios-collect/` is a real app in it); the scaffolder
still does not, so you create the app directory yourself. This example stays under
`entry-point/examples/` because it is documentation of the build procedure, not a
product app — put your own work in `apps/<name>/` and touch nothing else.

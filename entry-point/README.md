# entry-point

Everything an LLM needs to build an agentic financial application on Helios,
starting from one sentence of user intent:

> "Make me an app that automatically manages and pays my invoices."

This folder is the **on-ramp**. It is not the platform — the platform is
`platform/`. It exists because the platform's own docs are specifications of what
should be built, and a model dropped into them cannot tell which parts run today.

## Start here

```
SKILL.md                          load this if your harness supports skills
BUILD.md                          the operative 8-step procedure — read this first
```

Then, as you need them:

```
references/
  platform-status.md              what actually exists vs. what is only designed
  financial-tools.md              the real tool catalog, signatures, side-effect flags
  agent-loop.md                   how to drive the runtime, online or offline

examples/invoice-payer/           a complete running app built from that one sentence
  README.md                       what each invoice proves, and what is still missing
  helios-app.yaml                 the app spec
  agents/*.yaml, agents/*.json    the policy (written first) and the agent
  app.py                          fact source, gated runtime, owner, demo
  tests/test_controls.py          41 tests — one assertion per control

checks/
  check_app.py                    validates a finished app (the validator does not
                                  exist in platform/ yet, so this substitutes)
```

## Thirty-second version

Helios apps are built on one principle: **the model proposes, code decides.**

An action agent may ask for anything. What it gets is bounded by policy written as
data — a YAML audit manifest — and enforced by deterministic code that fails
closed. You write both sides. The policy is the product.

The corollary that does most of the work: **facts are not claims.** Every value a
rule depends on is resolved from a trusted `FactSource` the agent does not control.
The agent gets to *name* which record it means; it never gets to *state* what that
record says.

A generated app is four artifacts and a runtime:

| Artifact | Format | Loaded by |
|---|---|---|
| App spec | `helios-app.yaml` | `checks/check_app.py` (stand-in validator; no pydantic model yet) |
| Audit manifest | YAML | `audit_agent.manifest.AuditManifest` |
| Action manifest | JSON | `agent_toolkit.manifest.validate_manifest` |
| Fact source | Python | `audit_agent.FactSource` protocol |

The runtime exists and works, including the agent loop: interception, gating,
logging, holds and resumption all live in `platform/agent-integration/`, driven
by Qwen over DashScope. See `references/agent-loop.md`.

## Run the example

```bash
cd entry-point/examples/invoice-payer
python3 app.py                 # narrated run over five invoices

# tests run from the repo root with the repo venv (scripts/bootstrap-venv.sh)
.venv/bin/python -m pytest entry-point/examples/invoice-payer/tests -q   # 41 tests, offline
```

No API key, no network, no dependencies beyond the three platform packages plus
`pydantic` and `pyyaml`. Python 3.13.

Five invoices, five control paths:

| Invoice | Amount | Outcome | Why |
|---|---|---|---|
| INV-1001 | $1,250.00 | paid | inside every limit |
| INV-1002 | $48,000.00 | held → approved → paid | large payment needs a human |
| INV-1003 | $760.00 | **denied** | supplier's payment details changed |
| INV-1004 | $1,250.00 | held → declined | looks like a duplicate |
| INV-1005 | $310.50 | paid | inside every limit |

**INV-1003 is the demo.** The scripted owner confirms that payment anyway — a busy
owner rubber-stamping a transfer to a supplier whose banking details just changed.
The payment still does not go out, because a rule denies it in code. The control
does not depend on the human noticing, and it does not depend on the agent
behaving. That is the pitch in one screen of terminal output.

## Validate an app you generated

```bash
python3 checks/check_app.py examples/invoice-payer           # warnings only
python3 checks/check_app.py apps/your-app --strict           # warnings fail too
```

It implements the checks `specs/app-builder.md` specifies under "Validator" —
including the one that protects money: **action-manifest tools must be a subset of
audit-manifest declared tools.** A tool an agent can call that the auditor never
declared is an ungated side effect waiting to happen.

Reports all problems at once rather than failing on the first. Exit 0 clean,
1 errors, 2 usage.

## What this folder is not

- **Not the platform.** It reads the platform; it does not extend it. Nothing here
  is imported by `platform/`, and nothing here edits it.
- **Not a substitute for the specs.** `specs/app-builder.md` is the design of the
  real builder; `BUILD.md` is how to build against what exists *today*. When the
  scaffolder and validator land, prefer them and delete `checks/check_app.py`.
- **Not a claim that the platform is finished.** `references/platform-status.md`
  lists what is missing, and the example's README lists its own limitations. Both
  are deliberate: an entry point that overstates the platform produces apps that
  fail on stage.

## Corrections found while writing this

Recorded because they are the kind of thing that looks right in a spec and is
wrong in code. Each is verified and asserted by a test.

- **The fact ceiling guards one direction, not two.** `argument` vs
  `max_from_fact` catches **over**payment. Paying $1.00 against a $48,000 invoice
  is *inside* the ceiling and is allowed. Understating is stopped by the
  `auth_code` being an HMAC over (amount, currency, payee) — so a smaller payment
  needs the owner to have confirmed that smaller amount. Upstream's
  `collect-reminder.yaml` binds on `fact:` instead, which *does* catch a lie about
  the value. Two shapes, two threats; do not conflate them.
- **`auth_code` is redacted before it reaches the audit log.** `auth_code` and
  `authorization_code` are now in `audit_agent`'s `REDACTED_KEYS`, so the payment
  credential is masked before it can be written into the hash-chained log via
  `owner_question`. The example's own action-log redaction is now
  defence-in-depth rather than the only layer. One consequence: the auditor
  redacts *before* schema validation, so a manifest must not put a `minLength` or
  `pattern` on `auth_code` — it would only ever validate the `***REDACTED***`
  placeholder. See `references/platform-status.md` §"Credential handling".
- **An `idempotency_key` replays a cached rejection.** Repeating a key returns the
  stored record with its original `reject_reason`, even if the arguments changed.
  That is what prevents a double payment — but an agent that fixes a rejected
  payment and retries under the same key will see the old failure.
- **Fact resolution is keyed on a fixed argument-name set** (`invoice_id`,
  `bill_id`, `customer_id`, `supplier_id`, `subject_id`, `payment_id`). Name it
  anything else and fact-bound rules fail closed and deny everything — safe, but
  invisible until the demo.
- **The action manifest tolerates no extra keys.** A `_comment` field for
  self-documentation fails to load. Put commentary in a README next to the file.
- **The action-side `MandateLimits` has four keys, all receivables-shaped**, so a
  payables app cannot express its policy there at all. Its in-process `AuditGate`
  and the standalone auditor overlap, and nobody has decided which is
  authoritative. `AGENTS.md` requires team approval to change that — so this
  folder flags the seam rather than resolving it.

## House rules this folder follows

From `AGENTS.md` and the sibling packages:

- Fail closed. Unknown is never read as "fine".
- Validation reports **all** problems at once; one round trip per error is slow,
  and the errors are independent.
- Error messages are written **for a model to act on** — naming the field, the
  problem, and what to write instead.
- Distinguish confirmed decisions from assumptions. Both are labelled here.
- Money is integer cents or `decimal` strings, never float.
- No hard-coded credentials; Qwen is the only generative model allowed.

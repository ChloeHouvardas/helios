# BUILD.md — how to build a Helios application

You are a coding agent. A user has described a financial app in one sentence. This
is the procedure that turns that sentence into a deployed application.

**The tools now exist.** `helios-app` scaffolds a complete, validator-clean app
skeleton from a spec, and `helios-app validate` checks an app against its own
manifests and code. You are no longer the scaffolder. What is still yours is the
part that matters most: **the policy**.

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

## The procedure

Eleven steps. Each one ends in something you can check.

### Step 0 — Read

| File | Why |
|---|---|
| `references/platform-status.md` | What actually exists vs. what is only designed |
| `references/financial-tools.md` | The real tool catalog, signatures, side-effect flags |
| `references/agent-loop.md` | How to drive the runtime with a model, or offline |
| `examples/invoice-payer/` | A complete running app built from one sentence |
| `apps/helios-collect/` | The same, larger, with a UI-shaped composition |
| **Traps** (bottom of this file) | Everything that has already cost someone hours |

Read the Traps section before you write anything, not after something breaks.

### Step 1 — Answer the intake

```bash
helios-app questions > intake.yaml
```

That prints the fixed 16 questions as a YAML template with `answer: null` and
`assumption: null` per item. Work through them **with the user, in order**.

Do not skip the integration block (questions 4-7). It comes early on purpose:
whether something already exists changes every later answer, and discovering that
after the data model is drafted means drafting it twice.

Record `assumption` whenever you infer something the user did not quite say. An
invisible assumption becomes an invisible bug.

**An answer of "not sure" stays "not sure."** Do not invent a data model to make the
file parse. The next step turns an unanswered question into a `# TODO` in the spec,
and the validator reports the gap — which is the correct outcome, because a
plausible-looking wrong entity is worse than an absent one. The absence gets
noticed; the guess gets built on.

### Step 2 — Generate the first spec

```bash
helios-app spec-from-intake intake.yaml > helios-app.yaml
```

This maps what it can: identity, the `existing` block, entities with their fields
and types, tools, pages and patterns, graph declarations. Everything else is left
as a `# TODO` naming the question that would fill it.

The mapping rules worth knowing:

- `float` in an answer becomes `decimal`. Money is never a float, and an interview
  answer of "float" almost always means "a number with a decimal point".
- Page patterns are matched **positionally** against the page list.
- An `approval_queue` page is **added** if your answers omit one. Every side-effect
  action can be held, so an app without a queue has agents that pause invisibly and
  the owner discovers it when they wonder why nothing happened.
- `tools:` is a list of **tool names**, not tool objects. A name may be namespaced
  (`payments.send`, a platform tool) or snake_case (`claims_lookup`, an app-local
  tool). The scaffolder guesses each tool's side-effect flag from the name and marks
  the guess `# REVIEW`; when the name does not say what the tool does, state the
  flag and the guess is skipped:

  ```yaml
  tools:
    - claims_lookup                                   # read, guessed from the name
    - { name: reimbursements_send, side_effect: true } # stated: no guess, no REVIEW
    - { name: budget_snapshot, side_effect: false }
  ```

### Step 3 — Edit the spec

Read the generated `helios-app.yaml` and fix it. This is a real editing step, not a
formality: the intake is a conversation and the spec is a contract, and the
distance between them is your judgement.

```bash
helios-app validate .        # from inside the app dir; or point it at the dir
```

Shape rules it enforces:

- `extra="forbid"` everywhere — an unknown key is an error, not something quietly
  ignored. A typo'd `entites:` would otherwise generate an app with no tables.
- Exactly one of `stack.use` / `stack.match_existing`.
- Exactly one of `data.local` / `data.integration`.
- `identity.id` is kebab-case and must equal the audit manifest's `app_id`.
- Entity and field names match `^[a-z_][a-z0-9_]*$`. They are interpolated into
  generated DDL, so a name like `x INTEGER); DROP TABLE invoice; --` is **refused**,
  not escaped.
- Field types are the closed set: `string`, `integer`, `decimal`, `date`,
  `datetime`, `boolean`, `json`. **Money is always `decimal` or integer cents,
  never float.**

Every problem is reported at once, as `<yaml path>: <problem>; write <what
instead>`. Fix them all in one pass; do not go one round trip at a time.

### Step 4 — Scaffold

```bash
helios-app new apps/<your-app> --spec helios-app.yaml [--intake intake.yaml]
```

Generates a fixed set of files plus one template per declared page (23 for the
invoice-payer spec). **The skeleton validates clean the moment it is written**
(`helios-app validate` exits 0) and its generated tests pass; if either is not true,
that is a scaffolder bug, not something to work around.

| Path | What it is |
|---|---|
| `helios-app.yaml` | Your spec, copied verbatim (comments included) |
| `helios.yaml` | The deployment manifest the engine reads |
| `agents/<id>-audit.yaml` | **The policy.** Tool declarations + a TODO rule block |
| `agents/<id>-action.json` | The agent's instructions and mandate |
| `facts.py` | A `FactSource` over the generated store |
| `tools.py` | One `ToolDefinition` per spec tool |
| `store.py` | Generated SQLite access layer, parameterised SQL only |
| `graph.py` | Publish/subscribe call sites |
| `app.py` | Composition: `build_integration` over your policy |
| `server.py` | ASGI entry — health, dashboard, holds, runs, audit |
| `data/schema.sql` | Generated DDL |
| `templates/`, `static/` | The UI kit patterns you declared, plus tokens and htmx |
| `tests/test_controls.py` | The invariants that hold before any policy exists |
| `README.md` | With the scenario-table skeleton |

Three properties to rely on:

- **Never overwrites.** An existing file is reported as `skipped: <path> (exists)`
  and left alone. Regenerating over your work is not a thing it does.
- **Idempotent.** Running it twice writes nothing the second time. The revision
  loop — edit the spec, regenerate — is the normal way this is used.
- **The skeleton validates clean.** `helios-app validate` reports 0 errors on a
  freshly generated app, and its own generated tests pass.

Use `--dry-run` to see what would be written without writing anything.

The generated `side_effect` flags are **guessed from the tool names** and each
carries a `# REVIEW` comment. Check them. A real side effect declared read-only
runs with no gate at all — the validator catches the disagreement once `tools.py`
is filled in, but only if you fill it in.

### Step 5 — Write the audit manifest FIRST

Policy before agent, always. Two things the generator cannot do for you, and the
validator refuses an app that lacks either:

- **Every gated tool needs a real `input_schema`, in this file and in `tools.py`.**
  The generated one lists only the subject key (`invoice_id`, `bill_id`, ...) and
  `additionalProperties: false`; add the arguments the tool actually takes, with
  types. A rule can only bind an argument the schema declares, and the auditor
  refuses a side-effecting tool with no schema at all.
- **The rules.** The file ships with a commented block of the five rule shapes and
  no rules. Policy is the product; a generated ceiling would cap the wrong thing.
 The audit manifest is the registry of what may be
granted at all, and writing policy first makes the mandate the constraint the agent
fits inside. Write the instructions first and you describe an agent with no limits,
then spend the rest of the session negotiating with yourself.

The generated file declares every tool and contains the **five rule shapes** as a
commented block with the exact YAML keys:

```yaml
rules:
  # 1. Require the arguments that identify the action
  - id: app.requires_identity
    task_types: [<your task>]
    tools: [<gated tool>]
    message: >
      this call is missing an argument the policy needs; supply it rather than
      retrying without it
    required_arguments: [invoice_id]
    severity: critical

  # 2. Cap an argument against a TRUSTED FACT, not against what the agent says
  - id: app.amount_matches_record
    task_types: [<your task>]
    tools: [<gated tool>]
    message: the amount exceeds what is recorded for this subject
    limits:
      - argument: amount_cents
        max_from_fact: amount_cents
        on_exceed: hold
    severity: critical

  # 3. Hold above an absolute ceiling
  - id: app.large_amount_approval
    task_types: [<your task>]
    tools: [<gated tool>]
    message: this amount needs owner approval before it proceeds
    limits:
      - argument: amount_cents
        max: "1000000"          # integer cents; money is never a float
        on_exceed: hold

  # 4. Refuse outright when a trusted fact says so — deny, not hold
  - id: app.refuse_when_flag_set
    task_types: [<your task>]
    tools: [<gated tool>]
    message: >
      this record is flagged; the action is refused until the owner clears the flag
      out of band
    deny_if_fact_true: [flag_set]
    severity: critical

  # 5. Ask the owner when a fact makes the action unusual rather than wrong
  - id: app.unusual_case_approval
    task_types: [<your task>]
    tools: [<gated tool>]
    message: this case is unusual enough to need a human decision
    approval_if_fact_true: [unusual]
```

Plus a frequency cap if your app contacts anyone (`max_contacts_in_days: {within_days, max}`),
which needs `contact_count_for` in your fact source.

**No rules are generated for you, and that is deliberate.** A generated rule with a
made-up ceiling passes validation, reads like a control, and caps the wrong thing —
so the owner believes the app is governed when the number came from a generator.
Policy is the product. Write it.

### Step 6 — Write the facts

`facts.py` implements `audit_agent.facts.FactSource`:

```python
amount_for(subject_id) -> Decimal | None
contact_count_for(subject_id, within_days) -> int | None
counterparty_for(subject_id) -> str | None
payment_details_changed_for(subject_id) -> bool | None
extra_facts_for(subject_id) -> dict[str, Any]
record_contact(subject_id, when) -> None     # required by the runtime
```

Two invariants the generated file states and you must not break:

1. **Unknown is `None`, never zero.** A missing fact is the auditor not knowing
   something it needs, which fails closed and denies. Returning `0` for an unknown
   balance would read as "no exposure" and quietly turn a fail-closed control into
   a fail-open one.
2. **Nothing here may consult the request.** Only a subject id arrives. If a method
   accepted arguments, every claim the agent made would become a fact. There is a
   test in the reference apps that asserts no public method has an `arguments` or
   `request` parameter — write one.

`extra_facts_for` is the contract with your audit manifest. Every key a rule names
— in `deny_if_fact_true`, `approval_if_fact_true`, `limits[].fact`,
`limits[].max_from_fact` — must be returned here. `helios-app validate` checks it.

### Step 7 — Write the tools

One `ToolDefinition` per spec tool, in `tools.py`:

```python
ToolDefinition(
    name="payments.send",
    description="...",             # non-empty, and written for the calling model
    input_schema={...},            # non-empty; additionalProperties: false
    output_schema={...},           # non-empty — REQUIRED, it will not construct without it
    side_effect=True,              # must match the audit manifest
    handler=handler,               # Callable[[Mapping], Any]
)
```

The generated `input_schema` for every tool has **empty `properties`**; the spec
carries no argument types, so the generator cannot invent them. Write the real
schema for every tool here, matching the audit manifest for the gated ones — the
validator reports the two files in both directions when they disagree.

The generated handlers raise `NotImplementedError` naming the tool, the arguments
they received and the file to edit. Fill them in. Do not replace them with
something that returns a plausible empty result: a stub that succeeds lets a demo
pass while doing nothing at all.

Two rules that are not stylistic:

- **`additionalProperties: false`** on every input schema. Without it an extra
  argument is ignored rather than refused, and "ignored" is how a smuggled
  parameter gets through.
- **Handlers call service instances directly** (`bundle.payments`, `bundle.graph`),
  not `bundle.registry.call`. See the `build_world()` singleton trap below.

### Step 8 — Write the action manifest

The generated `agents/<id>-action.json` already carries the five-point operating
contract. Read it and make it specific to your app:

1. Read before acting; never state a value you did not read from a tool result.
2. Obtain the human authorization the policy requires before any side effect.
3. On a **hold**: stop and surface the owner's question verbatim. **Do not reword
   the request and retry it**, do not split it into smaller calls, do not route it
   through another tool. That is circumventing a control, and it is the specific
   failure mode a judge will look for.
4. On a **deny**: read the reason, fix what is actually wrong, or report that the
   work cannot be done and why. Never work around it.
5. Report per item: done, held awaiting the owner, or refused with the reason.

Points 3 and 4 are what make an agent *legible under constraint*. An agent that
argues with its gate is a liability; one that reports it is a product.

`mandate.limits` accepts **only four keys**, all receivables-shaped:
`max_extension_days`, `extension_owner_approval_days`,
`min_days_between_contacts`, `invoice_owner_approval_amount`. Anything else is
rejected by the loader. **A payables app cannot express its policy here at all** —
the generated file leaves `limits` empty for exactly that reason. Your authoritative
policy is the audit manifest from Step 5. Say so in your README; do not let a reader
believe the action manifest is doing the work.

Allowed top-level keys are exactly `name`, `version`, `description`,
`instructions`, `tools`, `mandate`, `model`, `max_turns`. **A `_comment` key is
rejected.** Put commentary in the README next to the file.

### Step 9 — Scenarios and tests

Fill in `scenarios()` in `app.py` and extend `tests/test_controls.py`. **One
scenario per control**, and one for each of these six outcomes — they are the
evidence that the gate works rather than that the agent is well behaved:

| Outcome | What it proves |
|---|---|
| allowed | a normal call, inside every limit, executes |
| held, approved | over a ceiling; the owner says yes; it runs with the **exact arguments that were held** |
| held, declined | the owner says no; **nothing happens** |
| denied | refused outright with the reason, and **the owner is never asked** |
| lying agent | the agent claims a value the fact source contradicts, and **nothing changes** |
| undeclared tool | a tool the auditor never declared is **refused** |

Plus: every step written to the action log, the audit log hash-chained, and no
credential reaching either log.

The generated tests already cover what can be checked before policy exists: both
manifests load under their own validators, the action-manifest subset invariant,
identity agreement, every declared tool is implemented, `side_effect` flags agree
between the two files, every gated tool has a schema and a subject key, and no rule
id claims the `baseline.` namespace. The six outcomes are named as TODOs rather
than written as tests that pass vacuously. **A test that asserts nothing is worse
than no test, because it reads as coverage.**

```bash
helios-app validate apps/<your-app>
agent-toolkit validate apps/<your-app>/agents/<id>-action.json \
                       --audit apps/<your-app>/agents/<id>-audit.yaml
agent-toolkit demo --run-dir /tmp/demo      # the gate, with no model and no network
```

### Step 10 — Deploy

Two files, two tools. `helios-app` wrote `helios.yaml` (the deployment manifest:
`entry: server:app`, `port: 3000`, permissions derived from your tool families,
`audit_manifest`, `data_dir: /data`, and the secret NAMES under `env:`). The
deployment engine's own CLI ships the directory:

```bash
cd platform/deployment && ./bootstrap.sh            # once per machine: daemon, nginx, engine on :8100
cd ../..
.venv/bin/python platform/deployment/cli/helios.py doctor
.venv/bin/python platform/deployment/cli/helios.py deploy apps/<your-app> --tenant acme
curl -s http://localhost/apps/<your-app>-acme/health         # 200 from the deployed app
.venv/bin/python platform/deployment/cli/helios.py apps      # the registry
.venv/bin/python platform/deployment/cli/helios.py logs <container> --tail 200
```

Exit codes are for you to act on: `0` deployed, `1` the engine rejected it (the
response lists every problem with the field and what to write instead, plus the
build or smoke-test log), `2` refused before upload because a `.env` is in the
bundle, `3` engine unreachable, `4` bad usage. Fix and redeploy; a failed redeploy
leaves the previous deployment serving.

What the engine checks, in order: manifest (unknown fields are rejected, so a typo
is loud), a secret scan over the bundle, a build on the golden base image that
already holds the platform packages (no `pip install` of your own), a smoke test
on an isolated network with **no Redis, no sidecar and no model key** — so
`/health` must be a constant and the app must build lazily on first real page —
then the audit sidecar and nginx routing. Full contract:
`platform/deployment/README.md`.

`permissions` must be a subset of the eight families the engine accepts —
`payments`, `email`, `knowledge-graph`, `events`, `forecast`, `market-data`,
`spreadsheets`, `collect`. The scaffolder maps your tool families onto them and
names anything it could not map in a `# REVIEW` comment.

### Step 11 — The README

Fill in the scenario table. It is the single most persuasive artifact in the
pitch, because it maps each invoice to the control that decided it:

```
| Invoice | Supplier | Amount | Outcome | Control |
|---|---|---|---|---|
| INV-1001 | Northwind | $1,250.00 | **paid** | normal path, inside every limit |
| INV-1002 | Delta Mills | $48,000.00 | **held → owner approves → paid** | `app.large_payment_approval` |
| INV-1003 | Cinder Logistics | $760.00 | **denied** | `app.payee_details_changed` |
| INV-1004 | Northwind | $1,250.00 | **held → owner declines** | `app.duplicate_invoice` |
```

Also state plainly: which audit gate you made authoritative and why, what the known
limitations are, and anything the generator guessed that you confirmed.

---

## Worked example: "Make me an app that automatically manages and pays my invoices"

`examples/invoice-payer/` is that request, built end to end and running. Read it
with this document open. It was hand-written before the generator existed, so its
files are not laid out exactly as `helios-app new` lays them out — its fact source
and data layer live inline in `app.py` rather than in `facts.py` and `store.py`.
Read it for the *policy* and the *controls*, and read `apps/helios-collect/` for the
file layout the generator produces.

```
examples/invoice-payer/
├── README.md                          # what it demonstrates, how to run it
├── helios-app.yaml                    # Step 3 — the app spec
├── app.py                             # Steps 6, 7 — fact source, runtime, owner
├── agents/
│   ├── invoice-payer-audit.yaml       # Step 5 — the policy (written first)
│   └── invoice-payer-action.json      # Step 8 — the agent
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
distinction is deliberate — see Step 5, rule shapes 4 and 5. A changed payee is not
something any amount of owner approval makes safe, so it is refused outright and the
owner is never asked. A duplicate-looking invoice is genuinely ambiguous, so it goes
to the human.

```bash
cd entry-point/examples/invoice-payer
python3 app.py              # narrated run
python3 app.py --trace      # per-step trace
python3 app.py --invoice INV-1003   # one control path
python3 app.py --json       # machine-readable
```

Runs offline. No API key, no network, no dependencies beyond the platform packages
plus `pydantic` and `pyyaml`.

To see the same controls exercised with no app at all — just the gate, the store and
the log:

```bash
agent-toolkit demo --run-dir /tmp/demo
```

---

## Where to put a generated app

`apps/<app-id>/`, with the app id matching `identity.id`.

**Check with the team before creating a directory under `apps/`.** Other agents work
in this repo simultaneously. `platform/action-agent-builder/`,
`platform/audit-agent-builder/`, `platform/app-builder/`,
`platform/agent-integration/` and `platform/financial-tools/` are theirs — read
them, do not edit inside them.

---

## Non-negotiables

- **Money is integer cents or `decimal` strings. Never float.** Convert at
  boundaries only. `Decimal(0.1)` bakes in binary rounding at the moment of
  conversion, which is why the generated store refuses a float outright rather than
  converting it.
- **Qwen is the only generative model allowed.** The driver speaks to Qwen through
  DashScope's OpenAI-compatible endpoint; any reference to a previous vendor is
  legacy and must be removed when you touch that file.
- **Secrets come only from environment variables.** Never print, log, commit or
  hard-code a key. A key in a log is a leak even if the log is local.
- **No hidden prompts or model-specific tricks** in docs or interfaces.
- Licence walls: no unlicensed investment advice; PIPL constrains behavioural
  telemetry; compliance output is not a legal opinion.
- **Do not weaken a control to make a test pass.** If a test fails because a gate
  correctly refused, fix the test or the fixture — never the gate.

---

## Scoring, and what it should change about your priorities

Investment pitch **50%** · engineering verification 25% · architecture defence 25%.

Half the score is the pitch, so an app that works but cannot be explained in one
sentence loses to one that can. Lead the architecture defence with the
Action/Audit split and *facts are not claims* — and demo the case where the human
gets it wrong and the code still refuses.

`examples/invoice-payer/` does exactly that: its scripted owner **confirms a
payment to a supplier whose banking details just changed**, and the payment still
does not go out. The control does not depend on the human noticing, and it does not
depend on the agent behaving.

---

## Traps

Everything below has already cost someone hours. Each is still true as of
2026-10-15 unless it says otherwise.

### Policy and the gate

1. **Action-manifest tools must be a subset of audit-declared tools.** The single
   most important check in the whole build. An undeclared tool is not denied — it
   is never looked at, so it runs with no gate at all. `helios-app validate` and
   `agent-toolkit validate --audit` both check it.

2. **Fact resolution keys on a fixed argument-name set:** `invoice_id`, `bill_id`,
   `customer_id`, `supplier_id`, `subject_id`, `payment_id`. An argument named
   anything else (`inv`, `bill_reference`) will not select a record, the fact will
   not resolve, and a fact-bound rule **fails closed and denies everything**. This
   is the most common way a generated app's policy silently stops working, and it
   fails in the safe direction — which is exactly why it survives until the demo.
   The validator warns when a gated tool takes no subject key.

3. **Absent fact ≠ absent argument.** An absent *argument* means "the agent did not
   ask for that", and a cap on it does not fire. An absent *fact* means "the
   auditor does not know something it needs", and the rule **denies**. Unknown is
   never read as small.

4. **A limit on an argument the tool does not take never matches.** Rule
   `limits[].argument` and `required_arguments` must name properties that exist in
   that tool's `input_schema`. A rule limiting `amount_cents` on a tool whose schema
   calls it `amount` does not error — the control is simply gone, and nothing says
   so. The validator checks this.

5. **`max` and `max_from_fact` are mutually exclusive, and one is required.** A
   limit with neither has nothing to compare against and would silently never fire.

6. **`on_exceed: hold` and `clamp: true` are alternatives, not complements.** Every
   rule evaluates the agent's *original* request independently, so a clamp in one
   rule does not make a later hold rule see a reduced value. That order-independence
   is deliberate: a rule's verdict must not depend on where it sits in the file.

7. **You may not reuse a baseline rule id, nor start one with `baseline.`** Both
   raise `PolicyLooseningError` at parse time. Redefining a platform rule as
   something permissive is the most damaging thing a generated manifest can do, so
   it is refused rather than caught later. Use `app.<name>`.

8. **A ceiling on an argument guards one direction only.** It catches
   **over**payment. It does not catch underpayment — paying $1.00 against a $48,000
   invoice is inside the ceiling, and the rule allows it. Understating is stopped by
   binding a credential to the exact amount, not by a limit.

9. **Audit rules cannot express a two-argument pairing check.** `send_email` takes
   `customer_id` *and* `invoice_id`, and the handler raises if they do not match —
   but `limits` and `deny_if_fact_true` resolve facts for one subject at a time, and
   the subject picker prefers `invoice_id`, so a valid invoice id makes a mismatched
   customer id invisible. The gate **approves**, the handler **refuses**, nothing is
   sent, and the run ends in `ERROR`. It fails loudly and safely, but the refusal
   lands *after* approval. Needs a pairing fact or a new rule shape.

10. **The auditor refuses a short `task_summary`.** `baseline.intent_stated`
    requires ≥12 characters or the whole grant is denied. Use realistic task text
    when driving runs.

### Runtime and wiring

11. **`fin_tools.build_world()` binds module-level singletons.** With two worlds in
    one process the **last**-built one wins for every `registry.call` dispatch. Call
    `bundle.payments` / `bundle.graph` directly instead. This matters because tests
    build a fresh world per case. Verified 2026-10-15.

12. **A fact source without `record_contact` takes the whole run down.** The gated
    runtime calls `fact_source.record_contact(...)` after a successful contact, and
    it does so **outside** its `try/except` — so a missing method is an
    `AttributeError`, not a denial. Implementing it is not optional for an app that
    reuses that runtime.

13. **`ToolDefinition` requires a non-empty `output_schema`.** It is a frozen
    dataclass whose `__post_init__` rejects an empty one. There is no
    `records_contact` field. Fields are exactly: `name`, `description`,
    `input_schema`, `output_schema`, `side_effect`, `handler`.

14. **`build_audit_agent()` raises `SigningKeyMissingError`** unless
    `HELIOS_AUDIT_SIGNING_KEY` is set or the manifest sets
    `allow_unsigned_grants: true`. For local dev:
    `os.environ.setdefault("HELIOS_AUDIT_SIGNING_KEY", "0"*64)`. **Never ship
    that.** `build_integration()` generates an ephemeral key when the env var is
    absent, so grants stay signed — the more honest demo, and one that cannot be
    shipped by accident.

15. **Grants expire** (your `ttl_seconds`) and side-effect grants are
    **single-use**. Authorize per task, not once at startup. Reuse the same
    `idempotency_key` when retrying the *same* payment so it cannot pay twice.

16. **`idempotency_key` replays a cached rejection.** Repeating a key returns the
    stored record with its *original* `reject_reason`, even if the arguments
    changed. An agent that fixes a rejected payment and retries under the same key
    sees the stale failure and may conclude its fix did not work. Use a fresh key
    when the payment genuinely changed.

17. **`resolved_facts` is a subject name, not evidence.** It selects which record to
    look up. Its *values* are deliberately not merged into the facts the auditor
    uses — an agent that sends `resolved_facts={"amount_due": "1.00"}` for a $24,500
    invoice is still held.

18. **Holds are in-memory only.** `AuditAgent` keeps holds in a dict and
    `InMemoryReplayGuard` is single-process, so pending approvals and grant-replay
    protection are **lost on restart**.

19. **Two logs, two purposes.** `agent_toolkit.action_log.ActionLog` is the app's
    own append-only record of what it did. The auditor's hash-chained log is
    tamper-evident evidence of what was authorised. Keep them separate.

20. **`Store` has no public invoice mutator.** It exposes mutators for contacts and
    the outbox only — it was written for Build 1, where invoices are read-only.
    `apps/helios-collect/book.py::_mutate_invoice` writes into the store's private
    `_invoices` dict. That is reaching into a private attribute of a package this
    app does not own; it is the only seam available, and it is confined to one
    method so there is a single place to change if the platform grows a public
    mutator.

21. **The two audit gates overlap and nobody has decided which wins.**
    `agent_toolkit.audit.AuditGate` is in-process and accepts only four
    receivables-shaped limit keys. `audit_agent.AuditAgent` is standalone,
    declarative, with signed scoped grants and a hash-chained log. Both reference
    apps make the **standalone auditor authoritative**. That is a defensible choice,
    **not a settled design** — `AGENTS.md` requires team approval for architecture
    changes, so **flag it; do not resolve it silently in your app.**

22. **The two packages have incompatible tool contracts.**
    `agent_toolkit.contracts.ToolDefinition` uses `side_effect` and
    `handler(args: Mapping)`; `fin_tools.contracts.ToolSpec` uses `side_effects`
    (plural) and `handler(**kwargs)`. Use `fin_tools.toolkit_bridge` or
    `adapters.ToolContractView`. A gated tool that registers as read-only because of
    this mismatch **emails customers with no audit at all**, and nothing raises.

### Credentials

23. **`auth_code` is not in the audit log's redaction key set.** It is not in
    `audit_agent.contracts.REDACTED_KEYS` (`src/audit_agent/contracts.py:54`), so a
    real payment credential reaches the log **in full**, inside the `owner_question`
    string that renders every argument. Seen live. Redact before you log. The UI
    kit's `mask_arguments` extends the platform set with `auth_code`,
    `bank_account`, `card_number` and friends for exactly this reason — but that
    only protects the page, not the log.

24. **`payments.send` distinguishes an absent `auth_code` from an invalid one**
    (`"missing verified intent auth code"` vs `"unknown auth code"` /
    `"amount differs from confirmed intent"`). The gate checks **presence**; the tool
    checks **validity**. Two layers, so a test that only asserts a verdict can pass
    while no money moves.

### Reporting

25. **`RunResult.summary()` miscounts side effects.** It reports
    `side_effects_attempted` / `side_effects_executed` as `len(self.actions)` /
    `len(self.executed)`, but read-only tool calls are also appended to
    `result.actions`. The counters therefore include non-side-effect tools. **The
    gate behaved correctly; the reported number is wrong.** Worth fixing before any
    judging context, because it reads as "an email went out" when none did. The
    action log is the accurate record; the summary is not.

### Environment

26. **Use the repo venv.** `.venv/bin/python`, which is 3.13. The system `python3`
    is 3.14 and has none of the dependencies installed. Run
    `scripts/bootstrap-venv.sh` once.

27. **`python3 -m pytest` inside a platform package fails** with
    `ModuleNotFoundError`. Use the venv interpreter. Note that the venv's
    `fin_tools` comes from a `.pth` file, because that package is a flat layout with
    no `[build-system]` and cannot be installed editable.

28. **`platform/financial-tools` is P1's.** `platform/deployment/` and
    `apps/helios-collect/` are P2's. Read them; do not edit them. Leave requests in
    a note in your commit message.

### The generator itself

29. **The generated `helios.yaml` is field-conservative on purpose.** See Step 10.
    Adding a field the engine does not declare produces a manifest that looks
    configured and is not, with no error anywhere.

30. **Routes in a generated `server.py` must stay flat.** The kit's URLs are relative
    because the deployment host strips `/apps/<slug>`. A nested route like
    `/holds/123` would resolve `static/...` against `/holds/` and 404.

31. **SQLite applies column affinity before a CHECK runs.** Binding the float `12.5`
    into a `TEXT` column arrives as the string `'12.5'` and passes every constraint,
    so no pure-SQL rule can catch a float in a decimal column. The generated store
    refuses floats at the Python boundary instead. Also: an unguarded `CHECK` rejects
    `NULL`, because `typeof(col) = 'text'` is false for `NULL` — optional columns get
    `CHECK (col IS NULL OR (...))`.

32. **GLOB is not regex.** `?` is exactly one character, never "optional", and
    `[0-9]*` is one class character followed by anything. It also matches digit
    *shape* only, so `'2026-13-45'` satisfies a date pattern unless you add explicit
    range clauses. The CHECK fragments in `stacks.yaml` are written for GLOB's real
    semantics and tested against SQLite 3.51.0.

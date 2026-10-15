---
name: helios-app-builder
description: Build an agentic financial application on the Helios platform from a one-line user request. Use when asked to make, generate, scaffold or build a Helios app, or when a user describes a financial app they want built on this platform (invoicing, receivables, payables, cash forecasting, payments, collections, supplier management, reconciliation, treasury). Covers the app spec, the audit and action manifests, the trusted fact source, gating side effects, the owner approval queue, and validation. Produces a runnable Python app composed from platform/financial-tools, platform/audit-agent-builder and platform/action-agent-builder.
---

# Helios App Builder

You are Helios's **coding agent**. A user has described a financial app; you turn
that description into a working application that runs on this platform.

Read **`BUILD.md`** — it is the full procedure and the operative document. This
file is the orientation that makes it usable.

## Start here, in this order

| File | Why | When |
|---|---|---|
| `BUILD.md` | The 11-step procedure, end to end | **Read first, always** |
| `references/platform-status.md` | What actually exists vs. what is only designed | Before writing any code |
| `references/financial-tools.md` | The real tool catalog, signatures, side-effect flags | Step 5, when picking tools |
| `references/agent-loop.md` | How to drive the runtime with a real model, or offline | Step 9 |
| `examples/invoice-payer/` | A complete running app built from one sentence | Read alongside BUILD.md |
| `apps/helios-collect/` | The file layout `helios-app new` generates | After Step 4 |

The three commands you will actually use:

```bash
helios-app questions > intake.yaml                       # the 16-question intake
helios-app spec-from-intake intake.yaml > helios-app.yaml # a first spec
helios-app new apps/<app> --spec helios-app.yaml          # the skeleton
helios-app validate apps/<app>                            # shape + cross-file
```

`agent-toolkit` covers the action side: `inspect`, `validate --audit` (the
cross-manifest invariants) and `demo` (the gate with no model and no network).

## The three things that decide whether the app is any good

**1. The model proposes, code decides.** An action agent may ask for anything;
what it gets is bounded by policy written as data and enforced by deterministic
code that fails closed. You write both sides. *The policy is the product.* An app
whose agent can do whatever it likes is not a Helios app, however good the UI is.

**2. Facts are not claims.** Every value a rule depends on is resolved from a
trusted `FactSource` the agent does not control. The agent gets to *name* which
record it means; it never gets to *state* what that record says.

This is the difference between "we cap payments at the invoice amount" and "we cap
payments at the amount the agent says the invoice is". Only the first is a control.
It is also the strongest architecture-defence material on this platform, and the
part another team cannot reproduce in 80 hours — it is a design decision, not a
feature.

**3. Every side effect needs somewhere for a human to land.** Anything that moves
money, sends email, writes a file or publishes shared state becomes a proposal:
approve, hold, or reject with a reason. Held actions pause the agent, so every app
with agents needs an `approval_queue` page — without one, agents pause invisibly
and the user discovers it when they wonder why nothing happened.

## Build order (from BUILD.md — follow it, do not improvise)

1. **Read** `references/platform-status.md` and the Traps section of `BUILD.md`.
   Several things the specs describe are still not built; knowing which changes what
   you must write yourself.
2. **Intake** — `helios-app questions`, answered with the user. Includes the
   integration block (*what already exists*), which is asked early on purpose and is
   where the competition's surprise task is decided. An unanswered question stays
   unanswered.
3. **Spec** — `helios-app spec-from-intake`, then **edit it**. The generated file
   carries `# TODO` markers for everything the intake did not settle.
4. **Scaffold** — `helios-app new`. A fixed set plus one template per page, validator-clean, never overwriting
   anything that already exists.
5. **Audit manifest first** — the policy. Before the action manifest, always: the
   audit manifest is the registry of what may be granted at all, and writing policy
   first makes the mandate the constraint the agent fits inside. **No rules are
   generated for you**, because a rule with a made-up ceiling reads like a control
   and caps the wrong thing.
6. **Fact source** — implements `audit_agent.FactSource`, reads the app's own
   database, never the request. Unknown is `None`, never zero.
7. **Tools** — fill in the generated handlers, which raise `NotImplementedError`
   until you do.
8. **Action manifest** — the generated instructions carry the five-point contract;
   make them specific to your app.
9. **Scenarios and tests** — one per control, covering allowed / held-and-approved /
   held-and-declined / denied / lying-agent-caught / undeclared-tool-refused.
10. **Validate** — `helios-app validate` and `agent-toolkit validate --audit`.
11. **Deploy and document** — `platform/deployment/cli/helios.py deploy apps/<id> --tenant <t>`, then the README scenario table.

## Critical warnings

- **The scaffolder and validator exist now; the agent loop exists too.** You are no
  longer the scaffolder. What is still yours is the **policy** — `helios-app new`
  generates tool declarations and a commented block of the five rule shapes, and
  deliberately generates **no rules**, because a generated ceiling would pass
  validation while capping the wrong thing.
- **`platform/deployment/` is the engine and it rejects unknown manifest fields.**
  `helios-app new` reads the engine's `contracts.py` at generation time and emits
  exactly the fields its `Manifest` declares (`name`, `tenant`, `entry`, `port`,
  `permissions`, `audit_manifest`, `data_dir`, `env`). Eight permission families;
  an unmapped tool family is named in a `# REVIEW` comment. `/health` must be a
  constant, because the smoke test runs with no Redis, no sidecar and no model key.
- **The two audit gates conflict and nobody has resolved it.**
  `agent_toolkit`'s in-process gate accepts only four receivables-shaped limit
  keys, so a payables app cannot express its policy there at all. Both reference
  apps make the standalone auditor authoritative. That is a defensible choice, **not
  a settled design** — `AGENTS.md` requires team approval for architecture changes,
  so flag it; do not resolve it silently.
- **`auth_code` is not in the audit log's redaction key set.** A real payment
  credential gets written to the log in full. Verified 2026-10-14; still unfixed
  upstream. Redact before you log, and see `references/platform-status.md`
  §"Credential handling".
- **Fact resolution is keyed on a fixed argument-name set** (`invoice_id`,
  `bill_id`, `customer_id`, `supplier_id`, `subject_id`, `payment_id`). Name an
  argument anything else and fact-bound rules **fail closed and deny everything** —
  which is why it goes unnoticed until the demo.
- **Money is integer cents or `decimal` strings, never float.** The generated store
  refuses a float outright, because SQLite applies column affinity before a CHECK
  runs and no SQL constraint can catch one.
- **Grants expire and side-effect grants are single-use.** Authorize per task, not
  once at startup. Reuse the same `idempotency_key` on a retry so it cannot pay
  twice.
- **Do not edit inside other packages.** `platform/action-agent-builder/`,
  `platform/audit-agent-builder/`, `platform/agent-integration/`,
  `platform/app-builder/` and `platform/financial-tools/` belong to other agents
  working concurrently.

## Non-negotiables (competition rules)

- **Qwen is the only generative model allowed for development.**
- No hidden prompts or model-specific tricks in docs or interfaces.
- No hard-coded API keys; credentials stay out of logs and packages.
- Licence walls: no unlicensed investment advice; PIPL constrains behavioural
  telemetry; compliance output is not a legal opinion.

## Scoring, so you know where to spend effort

Investment pitch **50%** · engineering verification 25% · architecture defence 25%.

Half the score is the pitch, so an app that works but cannot be explained in one
sentence loses to one that can. Lead the architecture defence with the Action/Audit
split and *facts are not claims* — and demo the case where the human gets it wrong
and the code still refuses.

`examples/invoice-payer/` does exactly that: its scripted owner **confirms a
payment to a supplier whose banking details just changed**, and the payment still
does not go out. The control does not depend on the human noticing, and it does not
depend on the agent behaving.

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
| `BUILD.md` | The 8-step procedure, end to end | **Read first, always** |
| `references/platform-status.md` | What actually exists vs. what is only designed | Before writing any code |
| `references/financial-tools.md` | The real tool catalog, signatures, side-effect flags | Step 3, when picking tools |
| `references/agent-loop.md` | How to drive the runtime with a real model, or offline | Step 6 |
| `examples/invoice-payer/` | A complete running app built from one sentence | Read alongside BUILD.md |
| `checks/check_app.py` | Mechanised validation of a finished app | Step 8, and in CI |

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

1. **Read** `references/platform-status.md`. Several things the specs describe
   are not built yet; knowing which changes what you must write yourself.
2. **Question phase** — the fixed 16-question intake from `specs/app-builder.md`.
   Includes the integration block (*what already exists*), which is asked early on
   purpose and is where the competition's surprise task is decided.
3. **`helios-app.yaml`** — the app spec. Self-check the invariants; no validator
   exists yet, so use `checks/check_app.py`.
4. **Audit manifest first** — the policy. Before the action manifest, always: the
   audit manifest is the registry of what may be granted at all, and writing policy
   first makes the mandate the constraint the agent fits inside.
5. **Fact source** — implements `audit_agent.FactSource`, reads the app's own
   database, never the request.
6. **Action manifest** — the agent's instructions and tool list. Write
   `instructions` as an operating contract, not a description.
7. **The app** — compose the three packages. Adapt `examples/invoice-payer/app.py`.
8. **Approval queue** — where the owner decides. Show them the auditor's own
   `owner_question`, never a summary the agent wrote about itself.
9. **Verify and demo** — `checks/check_app.py`, then a test that *each control
   fires*.

## Critical warnings

- **There is no agent loop, no scaffolder and no validator.** You are the
  scaffolder. `qwen_agent` is not installed in this checkout and no API key is set,
  so the example runs offline from deterministic code. See
  `references/agent-loop.md` for both paths.
- **The two audit gates conflict and nobody has resolved it.**
  `agent_toolkit`'s in-process gate accepts only four receivables-shaped limit
  keys, so a payables app cannot express its policy there at all. The example makes
  the standalone auditor authoritative. That is a defensible choice, **not a
  settled design** — `AGENTS.md` requires team approval for architecture changes,
  so flag it; do not resolve it silently.
- **`auth_code` is not in the audit log's redaction key set.** A real payment
  credential gets written to the log in full. Verified 2026-10-14. Redact before
  you log, and see `references/platform-status.md` §"Credential handling".
- **Fact resolution is keyed on a fixed argument-name set** (`invoice_id`,
  `bill_id`, `customer_id`, `supplier_id`, `subject_id`, `payment_id`). Name an
  argument anything else and fact-bound rules **fail closed and deny everything** —
  which is why it goes unnoticed until the demo.
- **Money is integer cents or `decimal` strings, never float.**
- **Grants expire and side-effect grants are single-use.** Authorize per task, not
  once at startup. Reuse the same `idempotency_key` on a retry so it cannot pay
  twice.
- **Do not edit inside other packages.** `platform/action-agent-builder/`,
  `platform/audit-agent-builder/`, `platform/app-builder/` and
  `platform/financial-tools/` belong to other agents working concurrently.

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

# Application Builder

This document defines the Application Builder: the kit Qwen uses to turn a user's
description of an app into a working application on Helios. The overall
architecture lives in `architecture-mermaid.md`, `decisions.md`, `interfaces.md`,
and `tech-stack.md`. The agent frameworks this builder references are defined by
`specs/build1.md`, `platform/action-agent-builder/`, and
`platform/audit-agent-builder/`.

## What the Application Builder is

The Application Builder is a **specification, a set of templates, and the tooling
that connects them**. It answers one question: given a user's idea, what does Qwen
write down, and what gets generated from it?

It has three parts:

1. **The app spec (`helios-app.yaml`)** — a single YAML file that describes an
   application completely enough to generate it. Qwen writes this file. It is the
   contract between a user's idea and a running app.
2. **The question phase** — the fixed intake questions Qwen works through with the
   user to produce that spec. This is where "what does the app do" becomes "what
   data, what agents, what tools, what pages".
3. **The generators** — the schema tool, scaffolder, and validator that turn a
   validated spec into an application skeleton under `apps/<name>/`.

**What it is not.** The Application Builder does not define the financial tools
(`interfaces.md` §6 and the Managed Financial Tools API do), does not define the
agent runtime (`platform/action-agent-builder/` does), does not define audit policy
(`platform/audit-agent-builder/` does), and does not build or deploy anything
(the Build & Audit Agent Engine and Automatic Deployment Engine do). It consumes
all of those and produces an application that uses them.

The relationship is the same one the agent builders already established: Helios
maintains the runtime, Qwen writes data. For agents that data is a manifest. For
a whole application, that data is the app spec.

## Design principle: templates are adaptable, not fixed

The competition's surprise task is decided here. Judges give a new financial app
requirement in natural language after code lock, and engineers check whether a new
AI-built app can be made compatible, deployed, and used **through the platform**.
That requirement may have to join a system that already exists — one with its own
stack, its own data, its own UI conventions.

A builder that can only produce one shape of app fails that test. So the templates
are starting points, not destinations:

- **The question phase discovers what already exists** before it decides what to
  generate. Existing system, stack, data sources, and UI conventions are first-class
  answers, recorded in the spec's `existing` section.
- **The scaffolder generates what is missing and follows what is there.** Where the
  spec records an existing convention, the generated code adopts it. Where it
  records nothing, the UI kit and the named stack supply defaults.
- **A generated app is Helios-compatible, not Helios-identical.** Compatibility
  means it declares its data, agents, tools, and graph interactions in the app
  spec; it runs the shared agent runtime; and it passes the validator. It does not
  mean it looks like the other apps or uses the same framework.

This is why the spec has a `match_existing` option in `stack`, an `integration`
option in `data`, and an `align_with` option in `ui` — rather than three fixed
shapes. The rigid version of this builder would be easier to write and would fail
the surprise task.

## The app spec: `helios-app.yaml`

One YAML file per application, at the root of the generated app package. It is
validated with pydantic under `extra="forbid"`, so an unknown field is an error
rather than something quietly ignored. Validation fails closed, and error messages
are written for a model to act on — naming the field, the problem, and what to
write instead — following the house style of both agent builders.

This is deliberate. A generated spec that is silently misread produces an app that
looks correct and behaves differently from what the user asked for. In a financial
application, the worst failure is not a crash; it is a wrong number that arrived
without an error.

### Sections

```yaml
# The app spec. Qwen generates this file; the scaffolder reads it.
spec_version: "1"

identity:
  id: helios-collect            # kebab-case, unique across apps
  name: Helios Collect
  description: >
    Chases overdue invoices on behalf of a small business, negotiates
    reasonable terms with customers, and directs payments to the right
    account when they arrive.

# What already exists, if anything. Populated by the question phase.
# An empty or absent `existing` section means a standalone app.
existing:
  system: null                  # e.g. "ACME back office (FastAPI + Postgres)"
  stack: null
  data: null
  ui: null

stack:
  # Exactly one of the following two keys. Both, or neither, is an error.
  use: helios-python-web-1      # a named stack from stacks.yaml
  # match_existing: true        # adopt what `existing.stack` records

data:
  # Exactly one of `local` or `integration`.
  local:
    entities:
      - name: invoice
        fields:
          - { name: id, type: string, required: true }
          - { name: customer_id, type: string, required: true }
          - { name: amount_due, type: decimal, required: true }
          - { name: due_date, type: date, required: true }
          - { name: status, type: string, required: true }
      - name: contact_event
        fields:
          - { name: id, type: string, required: true }
          - { name: invoice_id, type: string, required: true }
          - { name: channel, type: string, required: true }
          - { name: at, type: datetime, required: true }
  # integration:
  #   source: acme_erp
  #   access: read_only_sql
  #   entities: [invoice, customer]   # already exist; do not create them

graph:
  reads:
    - { entity: payment, events: [confirmed] }
  publishes:
    - { entity: invoice, events: [reminder_sent, extension_agreed] }

agents:
  action_manifest: agents/collect-reminder.json   # Agent Toolkit format
  audit_manifest: agents/collect-reminder-audit.yaml   # Audit Agent Builder format

ui:
  # Either `pages` for a standalone app, or `align_with` to follow an
  # existing system's conventions. Both may be set: pages are generated,
  # styled to match.
  pages:
    - { id: dashboard, title: Overview, pattern: summary }
    - { id: invoices, title: Invoices, pattern: table }
    - { id: holds, title: Needs your approval, pattern: approval_queue }
  align_with: null

tools:
  # The financial tools this app's agents may use. Each must also be
  # declared in the audit manifest — the audit agent is the registry.
  - lookup_invoice
  - send_reminder_email
```

Field notes, in the order a reviewer is likely to need them:

- **`spec_version`** is bumped only on an incompatible shape change, so a
  generated spec from an older Qwen run is rejected with a clear message rather
  than misparsed.
- **`identity.id`** is kebab-case and must match `app_id` in the audit manifest.
  The validator checks this; a mismatch means two files that look like one app
  are actually two.
- **`existing`** is the section that makes integration possible. It is null for a
  standalone app and populated for one joining a system. `stack.match_existing`,
  `data.integration`, and `ui.align_with` all read from it, so it must be filled
  in first.
- **`stack`** names a pinned stack from `stacks.yaml`, or asks to match the
  existing system. Exactly one — declaring both is contradictory, and declaring
  neither leaves the scaffolder guessing.
- **`data.local`** describes entities the app owns; the schema tool generates DDL
  from them. **`data.integration`** describes data the app reads from somewhere
  else and must not create. Exactly one of the two.
- **`graph`** declares what the app reads from and publishes to the shared
  knowledge graph. This is a declaration today and a wiring point later; see
  *Knowledge graph setup*.
- **`agents`** holds relative paths to the two manifests. The app spec does not
  restate their contents — it points at them, and the validator checks that the
  files exist and agree with each other.
- **`tools`** is the app's view of the financial tools in play. It must be a
  superset of both manifests' tool lists, because a tool an agent can call but
  the app never declared is a tool nobody reviewed.
- **`ui`** lists pages with a pattern from the UI kit. `align_with` records the
  existing system's conventions when the app joins one.

## Question phase

The fixed intake Qwen works through with the user. Its output is a complete
`helios-app.yaml`. The questions are fixed so that two runs over the same idea
produce comparable specs, and so a missing answer is visible rather than assumed.

Qwen asks these in order and does not skip the integration block. The integration
questions come early on purpose: whether something already exists changes every
later answer, and discovering that after the data model is drafted means drafting
it twice.

**What the app does**

1. What is the job this app performs, in the user's own words?
2. Who uses it, and what do they need to decide or do?
3. What does success look like — what changes for the user when it works?

**What already exists** *(the integration block — not optional)*

4. Is there an existing system this app joins, or is it standalone?
5. If existing: what stack does it use — language, web framework, database?
6. If existing: where does its data live, and how may this app access it?
7. If existing: what UI conventions does it follow — layout, components, styling?

**What data**

8. What records does the app need? For each: what fields, and which are required?
9. Which of those already exist elsewhere, and which must this app create?
10. What must be shared across apps — read from the knowledge graph, or published
    to it?

**What agents**

11. What work should an agent do without being asked each time?
12. For each agent: what is it allowed to do, what are its limits, and which
    actions need the owner's approval? (This becomes the mandate.)

**What tools**

13. Which financial tools does that work need — lookups, email, payments,
    spreadsheets, simulation, intent verification?
14. Which of those have side effects? (Each one becomes a gated proposal.)

**What pages**

15. What does the user need to see, and what do they need to act on?
16. Which UI pattern fits each page?

An answer of "not sure" is recorded as such rather than filled in. The validator
reports an incomplete spec, and Qwen goes back to the user — it does not invent a
data model to make the file parse.

## Database schema tool

Reads the spec's `data` section and generates the app's local schema.

- **Input:** `data.local.entities` — names, fields, types, required flags.
- **Output:** DDL for the app's own database, plus the data-access layer the
  scaffolder wires into the app.
- **Types** are a closed set (`string`, `integer`, `decimal`, `date`, `datetime`,
  `boolean`, `json`) mapped to the stack's database types by `stacks.yaml`. Money
  is always `decimal`, never a float — a rounding error in an invoice total is a
  real financial error.
- **`data.integration` produces no DDL.** Data the app does not own is not created
  by it. The tool generates an access layer against the recorded source instead,
  and the validator refuses an entity that appears in both sections.

The concrete database comes from the named stack — SQLite via SQLAlchemy by
default (`tech-stack.md`). The type mapping above is what the schema tool
translates through, so a later move to another database is a mapping change rather
than a spec-format change.

## UI kit and component library

A shared library of **prebuilt React components** that Qwen assembles pages from,
plus the layout and visual conventions that make generated apps look like they
belong to one platform.

The premise is that Qwen mixes and matches components rather than writing UI from
scratch, and may modify a copy when nothing fits. This is what makes the frontend
safe to be React: the model is choosing from known-good parts, not inventing
markup that has to be right first time. It is also what makes generated apps look
good — consistency comes from the library, not from the generating model's taste.

- **Components** are the unit Qwen works in: page shell, data table, record card,
  stat/tile, form field, button, badge, modal, empty state, error state, and the
  held-action card. Each ships with its own states handled — loading, empty,
  error — because a generated app that renders "undefined" in an empty table
  reads as broken however good the rest of it looks.
- **Patterns** are named page shapes the spec's `ui.pages[*].pattern` refers to.
  The initial set: `summary` (headline numbers and status), `table` (records with
  sorting and filtering), `detail` (one record, with an edit affordance),
  `form` (input with validation), `approval_queue` (held actions awaiting the
  owner's decision), `settings` (mandate limits and preferences). A pattern is a
  documented composition of components, so Qwen can drop to components when no
  pattern fits.
- **`approval_queue` is the pattern that matters most.** Every side-effect action
  can be held, so every app with agents needs somewhere for the owner to decide.
  A generated app without one has agents that pause invisibly. Its held-action
  card carries what the agent proposed, which rule held it, and the approve/reject
  affordance — the clearest expression of *the model proposes, code decides*.
- **Design tokens** — colour, spacing, typography, radius — are defined once and
  consumed by every component. Restyling a generated app means changing tokens,
  not components, which is also how `align_with` adopts an existing system's look.
- **Guidelines** cover layout, when to use which pattern, and how a held action is
  presented. They are the defaults for a standalone app.
- **When `ui.align_with` is set,** the recorded conventions replace the kit's
  tokens and guidelines, and components restyle to match. Only patterns with no
  equivalent in the existing system fall back to the kit's shape. The app should
  look like it was always part of that system.
- **Built with Vite, not Next.js.** The library compiles to a static bundle the
  app's FastAPI server serves, so a generated app is one process on one origin.
  That keeps the approval flow a same-origin call instead of a cross-boundary
  protocol — see *Tech stack definitions*.

## Tech stack definitions

Named, pinned stacks that the spec's `stack.use` refers to, plus the default used
when a spec names none.

- **`stacks.yaml`** holds them. Each entry names a language and version, a web
  framework and version, a database, the type mapping the schema tool uses, and
  the UI approach the component library builds for.
- **The default stack** is FastAPI + SQLite (via SQLAlchemy) + Vite-built React.
  When a spec omits `stack`, this applies — an undecided app still builds, which
  is what "if not specified, use X" means in practice.
- **Why FastAPI.** Python keeps the agent runtime, the audit gate, and the holds
  in one process with the API. Held actions surface to the owner without becoming
  a protocol between services.
- **Why Vite over Next.js.** Next carries two runtimes, its own server, and cache
  semantics — significant surface area for a generated app to get subtly wrong,
  and it would put a network boundary across the approval flow. Vite emits one
  static bundle that FastAPI serves: one process, one port, one origin.
- **Why SQLite.** Generated apps run independently, so a file-based database needs
  no server stood up per app. SQLAlchemy means Postgres later is a connection
  string, not a rewrite.
- **Pinned, not floating.** A stack names exact versions, so an app generated in
  week one still builds in week ten. "Latest" is not a version.
- **`stack.match_existing: true`** is the other option: adopt whatever
  `existing.stack` records, and generate only the parts the existing system lacks.
  The validator requires `existing.stack` to be populated when this is set —
  matching nothing is not matching. An existing non-React frontend means the
  component library does not apply, and `ui.align_with` governs instead.
- **Adding a stack later** means adding an entry to `stacks.yaml`, not changing the
  spec format. The default is one entry among them, not a special case.

## Knowledge graph setup

The spec's `graph` section declares the app's relationship to the shared
knowledge graph:

- **`reads`** — which entity types and events the app consumes, so it can react
  to changes originating elsewhere.
- **`publishes`** — which entity types and events the app emits, so other apps can
  react to it.

Today this is a declaration and a wiring point. The graph itself is a shared
Helios service (`architecture-mermaid.md` part 3) whose read/write interface
`interfaces.md` still lists as to be defined. The scaffolder generates the
publish and subscribe call sites with the declared entity types, so that when the
service interface is settled, connecting an existing app is filling in a transport
rather than restructuring it.

Two things the declaration makes checkable now:

- An app that publishes an entity type nothing reads is worth a warning — it may
  be a mistake, or it may be a genuinely new capability.
- An app whose agents act on cross-app state but declares no `reads` is likely
  missing an integration. The validator flags it rather than failing, because the
  state might be local.

`decisions.md` still holds open how the graph distinguishes authoritative records
from agent observations. The spec does not resolve that, and generated apps should
not assume it is resolved.

## Scaffolder

Validated spec in, application skeleton out, under `apps/<name>/`.

- **Reads the spec only.** It never imports the agent builders. Their manifests
  are read as plain data and copied into the generated app, which then runs its
  own copy of the shared runtime — the model already fixed by `decisions.md`.
- **Generates what is missing.** Package layout, the database layer from the
  schema tool, the UI pages from the declared patterns, the graph call sites, and
  the wiring that runs both agents inside the app.
- **Skips what already exists.** Where `existing` records a system, the
  scaffolder does not create a competing stack, database, or UI shell. It
  generates the app-specific parts and adapts them to what is there.
- **Never overwrites silently.** If a target path already holds a file, the
  scaffolder stops and reports it. Regenerating over someone's in-progress work
  is the kind of failure that costs hours and produces no error message.
- **Idempotent.** Running it twice on the same spec produces the same tree. A
  revision loop — Qwen edits the spec, regenerates — is the normal way this gets
  used, so it must be safe to repeat.

## Validator

Checks the spec's shape and its consistency with the files it references. Cheap,
side-effect free, and its messages are the feedback loop back to Qwen — the same
role `validate_manifest_text()` plays for the audit builder.

**Shape checks**

- Parses as YAML; validates against the pydantic models with `extra="forbid"`.
- Exactly one of `stack.use` / `stack.match_existing`.
- Exactly one of `data.local` / `data.integration`.
- `identity.id` is kebab-case and unique.
- Every `ui.pages[*].pattern` is a known pattern.
- Every entity field type is in the closed set.
- No entity appears in both `data.local` and `data.integration`.
- `stack.match_existing` requires a populated `existing.stack`; `data.integration`
  requires a populated `existing.data`; `ui.align_with` requires `existing.ui`.

**Cross-file checks** — the ones a single file cannot express, and the reason the
validator exists separately from the schema.

- Both paths in `agents` exist and parse under their own builders' validators.
- **Action-manifest tools ⊆ audit-manifest declared tools.** The audit agent is
  the registry of what may be granted at all, so an action agent offering a tool
  the auditor has never declared is an ungated side effect waiting to happen.
  This is the single most important check here.
- The app spec's `tools` is a superset of the tools in both manifests.
- `identity.id` equals the audit manifest's `app_id`.
- Every side-effect tool the agents may call is gated in the audit manifest — a
  tool flagged `side_effect: true` in one file and absent from the other's rules
  is reported, not passed.
- Every graph entity the agents publish appears in `graph.publishes`.

Validation reports all problems at once rather than failing on the first. Qwen
fixing one error per round trip over a revision loop is slow, and the errors are
independent.

## Build order

For the future implementation build. Each step produces something testable before
the next begins.

1. **Spec models** — the pydantic models for `helios-app.yaml`, with
   `extra="forbid"` and error messages written for a model to act on.
2. **Shape validation** — the single-file checks, plus
   `validate_spec_text()` returning `(ok, problems)` in the sibling packages'
   style.
3. **Cross-file validation** — read both manifests as data, run the consistency
   checks. Start with action-tools ⊆ audit-tools; it is the one that protects
   money.
4. **Question phase guide** — the intake questions as a document Qwen works
   through, with the answers mapped to spec fields.
5. **`stacks.yaml`** — the named-stack mechanism plus the default entry
   (FastAPI + SQLite + Vite-built React).
6. **Schema tool** — `data.local` to DDL, and the access layer for
   `data.integration`.
7. **Component library** — the prebuilt React components, the six patterns as
   documented compositions of them, the design tokens, and how `align_with`
   restyles through tokens.
8. **Graph wiring** — publish and subscribe call sites from the `graph`
   declarations, against a stub transport.
9. **Scaffolder** — spec to skeleton under `apps/<name>/`, skipping what
   `existing` says is already there, never overwriting, idempotent.
10. **End-to-end** — generate one app from a written spec, validate it, and run
    it.

## Done when

- A hand-written spec for a standalone app validates, scaffolds, and runs.
- The same spec with `existing` populated and `stack.match_existing: true`
  scaffolds into an app that **fits the existing system** — adopting its stack,
  reading its data rather than recreating it, and following its UI conventions.
  This is the acceptance test for the adaptability principle, and the one that
  predicts the surprise task.
- An action manifest offering a tool the audit manifest has not declared is
  rejected with a message naming the tool.
- A spec missing a required section is rejected with a message naming the section
  and what to write.
- Regenerating from a revised spec produces the same tree, and never overwrites an
  unexpected file.
- Qwen can work through the question phase and produce a spec that passes the
  validator on the first attempt.

## Not in scope

These belong to other components or to a later build:

- **Financial tools** — the tool contract is in `interfaces.md` §6; individual
  tools are still being defined. The spec names them; it does not implement them.
- **Agent runtime, interception, audit gate, action log, holds** — that is
  `platform/action-agent-builder/`, packaged into each generated app.
- **Audit policy and mandate rules** — that is `platform/audit-agent-builder/`.
  The app spec points at an audit manifest; it does not restate it.
- **Build, audit feedback loop, and deployment** — the Build & Audit Agent Engine
  and Automatic Deployment Engine (`architecture-mermaid.md` part 1,
  `interfaces.md` §5).
- **The shared services themselves** — knowledge graph storage, Intent & Trust
  Service, shared events. The spec declares an app's relationship to them; it does
  not implement them.
- **Default stack selection** — FastAPI + SQLite + Vite-built React is fixed in
  `tech-stack.md`. This document defines the mechanism that makes it swappable,
  not the choice itself.
- **Hosting model and deployment artifact format** — open questions in
  `decisions.md`.

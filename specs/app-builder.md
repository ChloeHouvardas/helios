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

The concrete database comes from the named stack — SQLite via stdlib `sqlite3` by
default, with no ORM (`tech-stack.md`). The type mapping above is what the schema
tool translates through, so a later move to another database is a mapping change
rather than a spec-format change: the mapping lives in `stacks.yaml` next to the
CHECK fragments behind it, not in generated code.

## UI kit and component library

> **CHANGED 2026-10-15 — decided.** This section specified prebuilt **React**
> components compiled by **Vite**. It is implemented as **Jinja2 templates plus
> HTMX**, server-rendered by the app's own FastAPI process.
>
> **Why.** A generated app has to be buildable and debuggable by a model in
> minutes. A Node toolchain is neither: it adds an install step, a build step, a
> second language, and a class of failure (a bundle that builds but renders
> nothing) that is invisible from the server logs. Server-rendered pages reach the
> property this section actually wanted from Vite — **one process, one port, one
> origin**, so the approval flow stays a same-origin call — without any of it.
>
> What survives unchanged is the *reason* this section gave for a component library:
> consistency comes from a library of known-good parts rather than from the
> generating model's taste. Templates and a token file do that job; React was one
> way to deliver them, not the reason for them.
>
> What is given up is granularity. This section listed eleven components; what ships
> is one template per pattern plus a base layout and a partial. That is the main
> cost of the change, and it is why `align_with` is marked **tentative** below
> rather than decided.

A shared library of components that Qwen assembles pages from, plus the layout and
visual conventions that make generated apps look like they belong to one platform.

The premise is that Qwen mixes and matches known-good parts rather than writing UI
from scratch, and may modify a copy when nothing fits. It is also what makes
generated apps look good — consistency comes from the library, not from the
generating model's taste.

- **Templates** are the unit Qwen works in: a base layout (page shell and nav), and
  the six patterns below. Empty, loading and error states are built into each
  template rather than left to the caller, because a generated app that renders
  "undefined" in an empty table reads as broken however good the rest of it looks.
  Every template renders with an empty context, which is tested.
- **Patterns** are named page shapes the spec's `ui.pages[*].pattern` refers to.
  The set: `summary` (headline numbers and status), `table` (records with sorting
  and filtering), `detail` (one record, with an edit affordance), `form` (input with
  validation), `approval_queue` (held actions awaiting the owner's decision),
  `settings` (mandate limits and preferences). The set is closed and defined in two
  places that must agree — `app_builder.spec.UiPattern` and
  `app_builder.kit.PATTERNS` — and `kit.validate_pattern_set()` fails if they drift.
- **`approval_queue` is the pattern that matters most.** Every side-effect action
  can be held, so every app with agents needs somewhere for the owner to decide.
  A generated app without one has agents that pause invisibly. Its held-action card
  (`_held_action.html.j2`) carries what the agent proposed, which rule held it and
  why, and the approve/reject affordance — the clearest expression of *the model
  proposes, code decides*. The intake **adds** this page when the answers omit one,
  and the validator warns when a spec has none.
- **Design tokens** — colour, spacing, typography, radius — are defined once in
  `tokens.css` and consumed by every template. Restyling a generated app means
  changing tokens, not templates. Light and dark come from `prefers-color-scheme`
  over the same token set, so a template that reads `var(--surface)` gets both for
  free.
- **Guidelines** cover layout, when to use which pattern, and how a held action is
  presented. They are the defaults for a standalone app.
- **When `ui.align_with` is set,** the recorded conventions should replace the kit's
  tokens and guidelines. **Tentative — not implemented.** Token substitution is the
  intended mechanism and `tokens.css` is written for it, but nothing reads
  `align_with` yet beyond validating that `existing.ui` is populated.
- **Relative URLs only, and flat routes.** The deployment host strips
  `/apps/<slug>` before the request reaches the app, so an absolute
  `/static/tokens.css` resolves to the host root and 404s. Every `href` and `src` in
  the kit is relative. The consequence is that generated pages must be served at
  flat routes (`/`, `/holds`, `/runs`) — a nested route like `/holds/123` would
  resolve `static/...` against `/holds/`. A test asserts this against the template
  *source*, not the rendered output, because an absolute URL inside a conditional
  block would not appear in every render.
- **HTMX is vendored, not fetched from a CDN.** The file ships at
  `app_builder/kit/static/htmx.min.js` and is copied into each generated app, so a
  generated app works offline and behind a path-stripping host. `stacks.yaml`
  records its version, byte count and sha256, and a test verifies all three — a
  swapped vendored file fails the build rather than passing silently.
- **Masking extends the platform's redaction set.** `auth_code` is *not* in
  `audit_agent.contracts.REDACTED_KEYS`, and it is exactly what a payment proposal
  carries. The held-action card applies its own filter — the union of the platform's
  set and the kit's additions — because this is the one page where a mistake puts a
  credential on a screen in front of a human. A test asserts the platform's keys
  remain a subset, so the kit cannot fall behind if that list grows. Masking is
  shallow for the same reason `audit_agent.redact` is: a deep walk would rewrite a
  nested key that happens to be named `token` for a legitimate business reason.

## Tech stack definitions

Named, pinned stacks that the spec's `stack.use` refers to, plus the default used
when a spec names none.

- **`stacks.yaml`** holds them, at `platform/app-builder/stacks.yaml`. Each entry
  names a language and version, a web framework and version, a server, a database,
  the type mapping the schema tool uses, the CHECK fragments behind that mapping,
  and the frontend with its vendored asset digest.
- **The default stack** is `helios-python-web-1`: **FastAPI + SQLite via stdlib
  `sqlite3` (no ORM) + Jinja2 + HTMX**, Python 3.13. When a spec omits `stack`, this
  applies — an undecided app still builds, which is what "if not specified, use X"
  means in practice.
- **Why FastAPI.** Python keeps the agent runtime, the audit gate, and the holds in
  one process with the API. Held actions surface to the owner without becoming a
  protocol between services.
- **Why Jinja2 + HTMX over Vite-built React.** See the CHANGED note above. One
  process, one port, one origin — the property Vite was chosen for — with no build
  step, no second language, and no bundle that can build successfully and still
  render nothing.
- **Why SQLite, and why no ORM.** Generated apps run independently, so a file-based
  database needs no server stood up per app. The generated data layer is a few dozen
  statements of parameterised SQL that a model can read, debug and fix; an ORM layer
  is one more thing that can be subtly wrong in generated code, and it hides the
  parameterisation that stops SQL injection. **This supersedes the earlier
  SQLAlchemy plan**, which assumed Postgres later would be "a connection string, not
  a rewrite" — the DDL generator and the type map in `stacks.yaml` are where that
  portability now lives, and moving to Postgres is a `stacks.yaml` entry rather than
  a rewrite of generated code.
- **Money is `TEXT` with a parse CHECK, never `REAL`.** A binary float cannot
  represent 0.1 exactly, so a rounding error in an invoice total is a real financial
  error rather than a display artefact. Note that **SQLite applies column affinity
  before a CHECK constraint runs**, so binding the Python float `12.5` into a TEXT
  column arrives as the string `'12.5'` and satisfies every constraint: no pure-SQL
  rule can catch it. The generated store refuses a float at the Python boundary
  instead, naming the field.
- **An optional column's CHECK must allow NULL.** A CHECK is satisfied when it
  evaluates to NULL, but `typeof(col) = 'text'` is *false* for NULL — so an unguarded
  constraint makes every optional date/decimal/json column unwritable. Generated DDL
  emits `CHECK (col IS NULL OR (...))` for optional columns only.
- **Pinned, not floating.** A stack names exact versions, so an app generated in week
  one still builds in week ten. "Latest" is not a version. Every version in
  `stacks.yaml` was read from an installed distribution or a vendored file's own
  banner, not recalled.
- **`stack.match_existing: true`** is the other option: adopt whatever
  `existing.stack` records. The validator requires `existing.stack` to be populated
  when this is set — matching nothing is not matching. **Open:** the scaffolder
  refuses to generate from it, because an integration stack has no entry in
  `stacks.yaml` and therefore no type map to generate DDL from. Generating SQLite
  tables for an app joining a Postgres system would be wrong in a way that looks fine
  until it runs.
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

**Implementation notes (2026-10-15).** Shipped as `app_builder.validate.validate_app(dir)
-> Report(errors, warnings, exit_code)`, over the pydantic models in
`app_builder.spec`. `entry-point/checks/check_app.py` is now a thin wrapper over it.

- `identity.id` **unique** across apps is **not implemented** — uniqueness needs a
  registry of app ids, and nothing in this repo holds one. Kebab-case and agreement
  with the audit manifest's `app_id` are both enforced; global uniqueness is open.
- Four cross-file checks beyond the list above, each closing a failure mode that is
  silent *and* safe-directioned, which is the kind that survives to the demo:
  audit `side_effect` flags agree with the app's own `ToolDefinition`s; every fact
  key a rule binds is produced by `facts.py`; every argument a rule binds exists in
  that tool's `input_schema`; a gated tool whose schema has no subject key is
  warned about.
- "Side effect gated in both files" is reported in **both directions**, named
  separately. A tool that is `side_effect: true` in code and `false` in the manifest
  is a control bypass — it runs with no proposal and no owner — while the reverse is
  a nuisance. One message covering both sends the reader to fix the wrong file.
- The checks that read app code parse it as an **AST** rather than importing and
  calling it. A fact source must return `None`/`{}` for a subject it does not know,
  so calling `extra_facts_for("dummy")` yields no keys and would report every correct
  app as broken. Modules are still imported in a subprocess (with `-B`, so no
  bytecode lands in the directory being validated) to prove they import and to
  resolve constant names.
- Two checks in the list above are **unreachable through `AuditManifest.load()`**,
  which already raises on an undeclared task/rule tool and on a side-effecting tool
  with no `input_schema`. They are kept as defence in depth for a manifest built in
  memory, and documented as such rather than presented as live coverage.

## Build order — implemented

This was the plan for the implementation build. It is written down as built, with
each item marked **decided** (shipped and settled), **tentative** (shipped, likely
to change) or **open** (not built). The package is `platform/app-builder`,
importable as `app_builder`, with the console script `helios-app`.

1. **Spec models — decided.** `app_builder.spec`: pydantic v2 models for
   `helios-app.yaml`, `extra="forbid"` everywhere, error messages written for a
   model to act on. `AppSpec.load(path)` / `AppSpec.parse(text)` raise `AppSpecError`
   carrying **every** problem at once, each as `<yaml path>: <problem>; write <what
   instead>`. Adds identifier safety the original plan did not name: entity and field
   names must match `^[a-z_][a-z0-9_]*$`, because the schema tool interpolates them
   into DDL.
2. **Shape validation — decided.** In `spec.py` rather than a separate
   `validate_spec_text()`. The plan's `(ok, problems)` signature became
   `AppSpecError.problems`, which carries the same information and keeps the
   success path free of tuple unpacking.
3. **Cross-file validation — decided.** `app_builder.validate.validate_app(dir)`
   returns a `Report` with `errors`, `warnings` and `exit_code`. Action-tools ⊆
   audit-tools is implemented first and tested hardest; it is the check that
   protects money. Four checks beyond the original list: audit `side_effect` flags
   agree with the app's `ToolDefinition`s; every fact key a rule binds is produced by
   `facts.py`; every argument a rule binds exists in that tool's `input_schema`; a
   gated tool with no subject key gets a warning.
4. **Question phase — decided.** `app_builder.intake`. `helios-app questions` prints
   the 16 questions as a fillable YAML template with `answer` and `assumption` per
   item; `helios-app spec-from-intake` builds a first spec from the answers it can
   map, leaving `# TODO` markers naming the question that would fill each gap. An
   unanswered question is recorded as `not sure` rather than guessed.
5. **`stacks.yaml` — decided.** `helios-python-web-1`: FastAPI 0.143.1 + stdlib
   `sqlite3` + Jinja2 3.1.6 + HTMX 2.0.11, Python 3.13, with the type map and the
   CHECK fragments behind it. Not Vite-built React — see the CHANGED note above.
6. **Schema tool — decided.** `app_builder.schema`: `ddl_for(spec)`,
   `store_module_for(spec)`, `seed_from_json(conn, data_dir)`. `data.integration`
   produces no DDL and a store whose accessors raise naming the source.
7. **UI kit — tentative.** `app_builder/kit/`: base layout, `tokens.css`, one
   template per pattern, and the `_held_action` partial. Jinja2 + HTMX rather than
   React. `align_with` token substitution is **open**.
8. **Graph wiring — tentative.** The scaffolder generates `graph.py` with a
   `publish_<entity>_<event>()` per declared publish, against `fin_tools`'
   `upsert_verified`. Subscribe is a stub that raises and says the transport is
   unsettled; the shared graph's read interface is still **open**.
9. **Scaffolder — decided.** `app_builder.scaffold.scaffold()` plus
   `helios-app new`. 23 files, never overwrites, idempotent, `--dry-run`. Everything
   is rendered before anything is written, so an aborted run cannot leave a
   half-generated app.
10. **End-to-end — decided.** Verified in tests: `invoice-payer`'s spec scaffolds
    into a temp dir that validates with 0 errors and whose generated tests pass; the
    same for `helios-collect`'s spec. An intake filled in for invoice-payer produces
    a spec that scaffolds and validates clean.

## Done when

- **Met.** A hand-written spec for a standalone app validates and scaffolds, and the
  generated app's own tests pass. "Runs" is partly met: `server.py` is generated with
  health, dashboard, holds, runs and audit routes, but no generated app has been
  served end to end, because `platform/deployment/` is not in the repo yet.
- **Open — and this is the one that predicts the surprise task.** A spec with
  `existing` populated and `stack.match_existing: true` does **not** scaffold. The
  spec model validates it and `data.integration` generates a raising store, but the
  scaffolder refuses: an integration stack has no `stacks.yaml` entry and therefore no
  type map to generate DDL from, and generating SQLite tables for an app joining a
  Postgres system would be wrong in a way that looks fine until it runs. Closing this
  needs the `align_with` token path and a story for a foreign database.
- **Met.** An action manifest offering a tool the audit manifest has not declared is
  rejected with a message naming the tool — by both `helios-app validate` and
  `agent-toolkit validate --audit`.
- **Met.** A spec missing a required section is rejected naming the section and what
  to write.
- **Met.** Regenerating from a revised spec produces the same tree and never
  overwrites an existing file; both are tested, and a dry run into
  `apps/helios-collect` lists only files that do not exist there.
- **Met.** A model can work through the question phase and produce a spec that passes
  the validator on the first attempt — verified by the intake-to-scaffold-to-validate
  test, in which nothing between the answers and the passing run is hand-written.

## Not in scope

These belong to other components or to a later build:

- **Financial tools** — the tool contract is in `interfaces.md` §6; individual tools
  are still being defined. The spec names them; it does not implement them.
- **Agent runtime, interception, audit gate, action log, holds** — that is
  `platform/action-agent-builder/`, packaged into each generated app.
- **Audit policy and mandate rules** — that is `platform/audit-agent-builder/`. The
  app spec points at an audit manifest; it does not restate it. The scaffolder
  generates the *shapes* of the five rule kinds as a commented block and generates
  **no rules**, because a generated ceiling would pass validation while capping the
  wrong thing.
- **Build, audit feedback loop, and deployment** — the Build & Audit Agent Engine and
  Automatic Deployment Engine (`architecture-mermaid.md` part 1, `interfaces.md` §5).
  The scaffolder emits a `helios.yaml` for the engine and reads the engine's accepted
  field set at generation time, but it does not deploy.
- **The shared services themselves** — knowledge graph storage, Intent & Trust
  Service, shared events. The spec declares an app's relationship to them; it does
  not implement them.
- **Default stack selection** — the choice is fixed in `tech-stack.md`; this document
  defines the mechanism that makes it swappable. `tech-stack.md` was updated on
  2026-10-15 to match the CHANGED note above (Jinja2 + HTMX, stdlib `sqlite3`).
- **Hosting model and deployment artifact format** — open questions in `decisions.md`.

## Status summary

| Area | Status |
|---|---|
| Spec model, shape validation | **decided** |
| Cross-file validation | **decided** |
| Intake and `spec-from-intake` | **decided** |
| `stacks.yaml`, schema tool, generated store | **decided** |
| Scaffolder, idempotency, never-overwrite | **decided** |
| UI kit: six patterns, tokens, held-action card | **tentative** — Jinja2/HTMX, not React |
| `ui.align_with` token substitution | **open** |
| Graph publish | **tentative** — against `fin_tools.upsert_verified` |
| Graph subscribe | **open** — no transport |
| `stack.match_existing` scaffolding | **open** — refused, not generated |
| Serving a generated app end to end | **open** — needs `platform/deployment/` |

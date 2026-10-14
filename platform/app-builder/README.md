# App Builder

The kit Qwen uses to turn a user's description of an app into a working
application on Helios. Implements the Application Builder in part 1 of the
architecture (`specs/architecture-mermaid.md`).

Design principle: **templates are adaptable, not fixed** — the question phase
discovers what already exists, and the scaffolder generates what is missing
rather than forcing one shape of app. A generated application is
Helios-compatible, not Helios-identical.

Components:

- **App spec (`helios-app.yaml`)** — one YAML file describing an application
  completely enough to generate it. Validated with pydantic, `extra="forbid"`,
  errors written for a model to act on.
- **Question phase** — the fixed intake questions Qwen works through with the
  user, including what already exists, so the app integrates rather than
  duplicates.
- **Database schema tool** — the spec's `data` section to DDL and an access
  layer. Data the app does not own is not created by it.
- **Component library and guidelines** — prebuilt React components Qwen assembles
  pages from, six named page patterns as documented compositions of them, and
  design tokens that restyle a whole app at once. Adopted by an existing system's
  conventions when one applies.
- **Tech stack definitions (`stacks.yaml`)** — named, pinned stacks the spec can
  reference, plus `match_existing` for apps joining an existing system. The
  default is FastAPI + SQLite (via SQLAlchemy) + Vite-built React.
- **Knowledge graph setup** — read and publish declarations, wired to the shared
  graph service once its interface is settled.
- **Scaffolder** — validated spec to an application skeleton under `apps/<name>/`,
  skipping what the spec says already exists. Idempotent; never overwrites
  silently.
- **Validator** — spec shape plus cross-file consistency, including that an
  action agent's tools are all declared by the audit agent.

No implementation yet. This directory is a placeholder until the build starts;
the design is complete in `specs/app-builder.md`, including build order, done-when
criteria, and what is explicitly out of scope.

At implementation time, the agent builders' manifests are read as plain data and
never imported — each generated app runs its own copy of the shared runtime.

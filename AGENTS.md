# Agent Guidelines

## Project Context

Helios is a platform for building and deploying agentic financial applications using Qwen.

Refer to:
- `README.md` — Project overview
- `specs/architecture-mermaid.md` — Architecture and components
- `specs/interfaces.md` — Component interfaces
- `specs/decisions.md` — Decisions and open questions
- `specs/tech-stack.md` — Tech stack decisions

## Startup
- When a new session is asked to work on this repo, read `README.md` → **For agents: start here** first. It routes every task (build an app, change policy, add a tool, deploy, operate) to the files to read and the commands to run.
- Then read the reference docs above to understand the scope of the system and its architecture.
- Do not load `docs/developer-context/` or `DEVELOPMENT-SUBMISSION-DELIVERABLES/` as context; they are human-facing.

## Communication

- Always communicate in **English**.
- Keep responses concise, clear, and direct.
- Avoid lengthy explanations, repetition, and unnecessary context.
- Prioritize actionable information.
- Explain technical decisions briefly unless more detail is requested.
- Ask focused questions when clarification is needed.

## Development Conventions

- Use conventional commit messages — see **Committing** below.
- Prefer simple, modular implementations.
- Follow existing patterns where practical.
- Avoid unnecessary abstractions and unrelated changes.
- Consider security and financial data integrity.
- Add relevant tests when modifying behavior.

## Committing

Commit as you go, not as one lump at the end of a session. Every agent working in
this repo follows this.

**Conventional commits.** `<type>: <lowercase summary>`, imperative mood, no
trailing period, ≤72 chars. Add a body when the *why* is not obvious from the
diff — especially for security properties, constraint choices, and anything that
looks like it could be simplified but must not be.

| type | use for |
|---|---|
| `feat:` | new capability |
| `fix:` | correcting behaviour that was wrong |
| `test:` | adding or correcting tests |
| `docs:` | prose only — no behaviour change |
| `refactor:` | restructuring, behaviour unchanged |
| `chore:` | packaging, deps, config, tooling |
| `perf:` | same behaviour, faster |

**Sequential and reasonable.** Split work so each commit is one coherent change
and the history reads as a build-up. Order commits so each one leaves the repo
importable and its own tests passing — dependencies before dependents, contracts
before implementations, fixtures before the tests that consume them. A reviewer
should be able to check out any commit and get something that works.

Do not split by file count or by line budget; split by idea. Equally, do not
bundle unrelated changes to save a commit — an unreviewable commit is worse than
several small ones.

**Scope discipline — this repo has several agents working at once.** Stage
explicit paths, never `git add .` or `git add -A`. Before committing, check
`git status` and confirm every staged path is yours. If a shared file
(`AGENTS.md`, `README.md`, anything under `specs/`) already carries another
agent's uncommitted edit, stage only your own hunk — `git apply --cached` with a
filtered patch — and leave their change in the working tree for them to commit.
Never commit another agent's work under your message, and never revert or
"clean up" edits you do not recognize; they are probably someone's work in
progress.

**Do not** push, force-push, rebase, amend, or reset shared history unless the
user asks. Do not use `--no-verify`. Commit locally and report.

## Architecture Changes

For routine changes, work within the existing architecture.

Significant architectural changes do **not** require a pause-and-wait cycle.
Approval comes from the user directly, and **a decision the user has already made
carries forward** — do not re-litigate settled decisions or request approval a
second time for something already approved.

Proceed, then document: state the change, its implications, and which components
it affects, in the same pass as the work.

Still flag *before* proceeding when a change would:

- break, or require edits inside, another agent's package — coordinate across that
  boundary rather than editing it, and
- be hard to reverse once shipped (data migrations, removing a public boundary).

Do not treat an implementation request as authorizing an unrelated redesign.

## Documentation

Keep documentation concise and consistent.

- Update `specs/architecture-mermaid.md` when approved architectural changes occur.
- Update `specs/interfaces.md` when contracts change.
- Update `specs/decisions.md` when decisions are confirmed.
- Update `specs/tech-stack.md` when tech stack choices are made.
- Update `README.md` when project scope or setup changes.
- Prefer updating existing documents over creating new ones.

Routine documentation updates can be made alongside implementation. Major architectural changes require approval first.

## Working Approach

- Distinguish confirmed decisions from assumptions.
- Prefer small, verifiable changes.
- Briefly summarize completed work and outstanding questions.
- Treat documentation as evolving guidance rather than a fixed specification.
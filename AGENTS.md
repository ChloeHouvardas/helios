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
- When a new session is asked to work on this repo, read all the reference docs to understand the scope of the system and its architecture. 

## Communication

- Always communicate in **English**.
- Keep responses concise, clear, and direct.
- Avoid lengthy explanations, repetition, and unnecessary context.
- Prioritize actionable information.
- Explain technical decisions briefly unless more detail is requested.
- Ask focused questions when clarification is needed.

## Development Conventions

- Use conventional commit messages (`feat:`, `fix:`, `docs:`, `chore:`, etc.).
- Prefer simple, modular implementations.
- Follow existing patterns where practical.
- Avoid unnecessary abstractions and unrelated changes.
- Consider security and financial data integrity.
- Add relevant tests when modifying behavior.

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
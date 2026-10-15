# QWEN.md — read this first

## 1. Language

**Respond, commit, comment and document in English only** — never another language, in
prose, code comments, docstrings, commit messages or generated files.

## 2. Environment and tests

```bash
scripts/bootstrap-venv.sh    # once: .venv on Python 3.13, six packages editable
scripts/test-all.sh          # every suite, offline; one line per suite
```

Use `.venv/bin/python` for everything — never the system `python3` (not guaranteed to
be 3.13, which `audit_agent` and `agent_integration` require, and it has none of the
deps). No `PYTHONPATH=src`, no per-package venv; both superseded. Expect **all 7 suites
green: 803 passed**, entirely offline: no network, Docker daemon, model token or
provider credential; a suite that collects nothing fails the run. Deps are pinned in
`requirements-dev.txt`.

## 3. Decisions already made — do not re-open

1. **Deployment contract** = the engine at `platform/deployment/`: `helios.yaml` + an
   ASGI entry + `GET /health`. Apps adopt it; the engine is adapted where the app side
   cannot be.
2. **The standalone `audit_agent` is the authoritative in-app gate.**
   `agent_toolkit.audit.AuditGate` is legacy and deprecated — importable, but in no run
   path; do not add rules to it. The deployment engine's audit sidecar is a separate,
   fact-free second gate holding the execution secret, not a substitute.
3. **Qwen via DashScope** (OpenAI-compatible mode), for coding and in-app agents. Qwen
   is the only generative model allowed; any reference to another vendor is legacy —
   remove it when you touch that file.
4. **Money is simulated end to end** — no real payment rails, a deliberate competition
   and safety constraint, not a placeholder.
5. **Default app stack** = FastAPI + SQLite + Jinja2/HTMX server-rendered pages.
6. **Money is integer cents or decimal strings. Never float.** Convert at boundaries
   only — a rounding error in an invoice total is a real financial error.
7. **Facts are not claims.** Every value a rule depends on comes from a trusted
   `FactSource`, never the agent's arguments; an agent asserting its own facts must
   still be held.

## 4. The boundary rule

`action-agent-builder` and `audit-agent-builder` **never import each other** — that is
what makes the auditor trustworthy, since an app cannot modify the code that audits it.
Only `platform/agent-integration/` may import both, and it holds the adapters closing
the two contracts' shape gaps (`side_effect` vs `side_effects`; JSON Schema vs a flat
`{name: "type"}` map). Left unadapted both fail **silently**: a gated tool registers as
read-only and is never checked. `fin_tools.toolkit_bridge` is the other sanctioned
bridge; run `agent-integration selfcheck` after changing either tool contract. The model
driver returns a *proposal and stops* — it never executes a tool, so no path runs from
a model reply to a side effect without the auditor.

## 5. Working rules

- **Stage explicit paths. Never `git add .` or `-A`.** Check `git status --porcelain`
  first; confirm every path is yours. Local commits only: no push, force-push, rebase,
  amend, reset, history rewrite or `--no-verify`.
- Conventional commits, imperative, ≤72 chars, English, with a body whenever the *why*
  is not obvious — especially for security properties. One commit per task; each leaves
  `test-all.sh` green. Dependencies before dependents, contracts before implementations,
  fixtures before their tests.
- **Never weaken a control to make a test pass.** If a gate correctly refused, fix the
  test or the fixture. Do not edit or "clean up" files you do not recognise, and never
  touch `OTHER-TEAMMATE-STUFF/`, `docs/milestones/`, `.env`, or any `.venv`.
  `platform/deployment/.helios-run/tenants/demo.json` holds a live tenant execution
  secret and must never be committed.
- No new third-party dependency without recording why in the commit body; pin it, and
  prefer stdlib. Secrets come only from environment variables — never print, log, commit
  or hard-code a key.
- Fail closed — unknown is never read as "fine". Validation reports **all** problems at
  once, and errors are written for a model to act on: name the field, the problem, and
  what to write instead.

## 6. Where to read next

`HANDOFF.md` (what exists, the traps, what's next) ·
`entry-point/references/platform-status.md` (verified: runs vs designed) · `AGENTS.md` ·
`specs/decisions.md` · `specs/tech-stack.md` · `entry-point/SKILL.md` → `BUILD.md`. Keep a plan and a running log under `docs/sessions/<your-session>/`, commit them, and re-read both after any context compaction.

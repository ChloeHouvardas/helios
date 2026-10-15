# Developer context — for human readers

The documents in this folder are written for people: the team, a reviewer, a
judge, the pitch writer. They describe state, plans, demo stages and history as
of the date in each file.

**Not for agents.** A coding agent or an agent session should not load this
folder as context. The agent-facing material is `QWEN.md` and `AGENTS.md` at the
repository root, `entry-point/` (the build procedure and references), `specs/`
(architecture, interfaces, decisions) and `docs/API.md`, `docs/AGENT-GUIDE.md`,
`docs/PLATFORM-GUIDE.md`.

| File | What it is |
|---|---|
| `DEMOFLOW.md` | the demo stages, the scripted test ladder (`scripts/demo-check.sh`), observed results, remaining gaps |
| `STATUS.md` | component-by-component state and the numbers at the last run |
| `HANDOFF.md` | the cold-reader handoff: the tree, verified flows, traps, open items |
| `INVESTMENT-CONTEXT.md` | the brief the investment pitch is written from, with the claims that are safe to make |

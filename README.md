# Helios

Helios is a platform for building and running agentic financial applications. A
user describes an app in one sentence; a coding agent builds it on the platform's
tools and runtime; the platform validates and deploys it; agents inside the app
then do the financial work under a deterministic audit gate.

The governing principle: **the model proposes, code decides.** Every side effect
is a proposal that policy written as data approves, holds for the owner, or
denies, using facts the agent cannot write.

## Quick start

```bash
scripts/bootstrap-venv.sh && scripts/test-all.sh          # one venv, every suite
cd apps/helios-payables && ../../.venv/bin/python app.py  # a flagship, offline
cd ../.. && platform/deployment/bootstrap.sh              # Docker: nginx + engine
.venv/bin/python platform/deployment/cli/helios.py deploy apps/helios-payables --tenant demo
open http://localhost/apps/helios-payables-demo/
```

## What is here

| Part | Where |
|---|---|
| Financial tools (40 tools, 9 families) and the tool service | `platform/financial-tools` |
| Application builder (intake, spec, scaffolder, validator) | `platform/app-builder` |
| Agent toolkit: action side, audit side, the gated loop | `platform/action-agent-builder`, `platform/audit-agent-builder`, `platform/agent-integration` |
| Deployment engine, golden images, sidecar, SDK | `platform/deployment` |
| The three flagships: Collect, Payables, Cash Planner | `apps/` |
| The coding-agent on-ramp and a worked example | `entry-point/` |

## Documentation

- `docs/PLATFORM-GUIDE.md` — setup, build an app, deploy, operate
- `docs/API.md` — engine, sidecar, tool service, SDK, builder CLI
- `docs/AGENT-GUIDE.md` — the action and audit agents, manifests, rules, facts
- `entry-point/BUILD.md` — the step-by-step build procedure for a coding agent
- `entry-point/references/financial-tools.md` — the tool catalog
- `specs/` — architecture, interfaces, decisions, tech stack, app spec, context
- `docs/developer-context/DEMOFLOW.md` — demo stages and the test ladder (`scripts/demo-check.sh`)
- `docs/developer-context/STATUS.md`, `docs/developer-context/HANDOFF.md` — what exists, what is verified, what is open
- `AGENTS.md` — conventions for working in this repository

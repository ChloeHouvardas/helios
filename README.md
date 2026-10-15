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

## For agents: start here

If you are a coding agent working in this repository, this section is your map.
Read `AGENTS.md` (conventions) and `QWEN.md` (settled decisions, the boundary
rule, working rules) first. Then pick your task below and read only the files it
names, in order. Everything is run from the repo root with `.venv/bin/python`;
never the system `python3`.

### Reading order for any session

1. `AGENTS.md` and `QWEN.md` — rules and decisions already made. Do not re-open them.
2. `specs/architecture-mermaid.md`, `specs/interfaces.md`, `specs/decisions.md`, `specs/tech-stack.md` — the architecture.
3. `platform/README.md` — what each platform package is and the one import boundary.
4. The task-specific files below.

**Do not load as context:** `docs/developer-context/` (human-facing status and
pitch material) and `DEVELOPMENT-SUBMISSION-DELIVERABLES/` (competition
submission). They describe state at a date, not how to work.

### Task → what to read → what to run

| I want to… | Read, in order | Run |
|---|---|---|
| **Set up and verify the repo** | `QWEN.md` §2 | `scripts/bootstrap-venv.sh && scripts/test-all.sh` (expect every suite green, offline) |
| **Build a new app from a one-line request** | `entry-point/SKILL.md` → `entry-point/BUILD.md` (the 11-step procedure) → `entry-point/references/platform-status.md` → `entry-point/references/financial-tools.md` → `entry-point/examples/invoice-payer/` → `apps/helios-cash-planner/` (complete scaffold-path example with its answered intake) | see **Build a new app** below |
| **Understand the app spec format** | `specs/app-builder.md`, `platform/app-builder/README.md` | `.venv/bin/helios-app validate apps/<id>` |
| **Write or change agent policy (rules, facts, manifests)** | `docs/AGENT-GUIDE.md` §3–§5, `platform/audit-agent-builder/README.md`, `platform/action-agent-builder/README.md` | `.venv/bin/audit-agent validate <manifest>`, `.venv/bin/agent-toolkit validate --audit`, `.venv/bin/agent-integration selfcheck` |
| **Drive an agent (offline or with Qwen)** | `entry-point/references/agent-loop.md`, `docs/AGENT-GUIDE.md` §2 and §7, `platform/agent-integration/README.md` | `.venv/bin/agent-integration demo` / `run` / `inspect` / `doctor`; set `DASHSCOPE_API_KEY` for a live model |
| **Add or change a financial tool** | `platform/financial-tools/README.md`, `specs/financial-tools-service.md`, `entry-point/references/financial-tools.md`, the sub-package README (`email_tool/`, `spreadsheets/`, `payments_collect/`, `research/`) | that package's `tests/`, then `scripts/test-all.sh` |
| **Run an existing app locally** | that app's `README.md` under `apps/` | `cd apps/<id> && ../../.venv/bin/python app.py` (scenarios), `../../.venv/bin/python -m uvicorn server:app --port 8000` |
| **Deploy an app** | `docs/PLATFORM-GUIDE.md` §5–§6, `platform/deployment/README.md`, `platform/deployment/AGENT-BUILDERS-GUIDE.md` | `platform/deployment/bootstrap.sh`, then `.venv/bin/python platform/deployment/cli/helios.py deploy apps/<id> --tenant <t>` |
| **Deploy the whole platform on a fresh machine** | `AGENT-DEPLOYMENT.md` (top to bottom, every step has a check) | the commands in that file |
| **Operate a running deployment** | `docs/PLATFORM-GUIDE.md` §6, `docs/API.md` | `helios apps` / `logs` / `evidence` / `undeploy` / `doctor` |
| **Call an HTTP surface (engine, sidecar, tool service, SDK)** | `docs/API.md` | — |
| **Run the demo test ladder** | `docs/PLATFORM-GUIDE.md` §7 | `scripts/demo-check.sh T0 T1 T2` (offline), `T3 T4 T5` (Docker) |
| **Change the architecture or a contract** | `AGENTS.md` §Architecture Changes, then update `specs/` in the same pass | `scripts/test-all.sh` |

### Build a new app (the short form)

The full procedure, with the reasoning and the traps, is `entry-point/BUILD.md`.
Follow it; do not improvise the order. The commands it drives:

```bash
.venv/bin/helios-app questions > intake.yaml                   # 16 questions; "not sure" stays "not sure"
.venv/bin/helios-app spec-from-intake intake.yaml -o helios-app.yaml
# edit helios-app.yaml: entities, pages, tools ({name, side_effect})
.venv/bin/helios-app new apps/<id> --spec helios-app.yaml --intake intake.yaml
# write, in this order: agents/<id>-audit.yaml (policy FIRST), facts.py, tools.py, the action manifest
.venv/bin/helios-app validate apps/<id>                        # 0 error(s)
cd apps/<id> && ../../.venv/bin/python -m pytest tests -q && ../../.venv/bin/python app.py
```

The scaffold gives you both manifests, a fact source over a generated store, one
tool stub per declared tool, a FastAPI server with `/health`, the pages, control
tests and `helios.yaml`. It does not give you rules: policy is the product and
you write it. Every app with agents needs an approval-queue page (`/holds`).

### The three invariants every change must keep

1. **The model proposes, code decides.** Side effects are proposals; the audit gate approves, holds or denies against policy written as data.
2. **Facts are not claims.** Rules read a trusted `FactSource`, never the agent's arguments. Unknown is `None`, never zero.
3. **The builders never import each other.** Only `platform/agent-integration/` imports both `agent_toolkit` and `audit_agent`. Money is integer cents or decimal strings, never float.

### Package and CLI map

| Folder | Python package | CLI |
|---|---|---|
| `platform/financial-tools` | `fin_tools`, `helios_forecast`, `spreadsheets`, `email_tool`, `payments_collect` | — |
| `platform/app-builder` | `app_builder` | `helios-app` |
| `platform/action-agent-builder` | `agent_toolkit` | `agent-toolkit` |
| `platform/audit-agent-builder` | `audit_agent` | `audit-agent` |
| `platform/agent-integration` | `agent_integration` | `agent-integration` |
| `platform/deployment` | `engine`, `helios_sdk`, `cli.helios` | `helios` (`python platform/deployment/cli/helios.py`) |

All are installed editable into `.venv` by `scripts/bootstrap-venv.sh`.

## Documentation

- `docs/PLATFORM-GUIDE.md` — setup, build an app, run, deploy, operate, test ladder
- `docs/API.md` — engine, sidecar, tool service, SDK, builder CLI
- `docs/AGENT-GUIDE.md` — the action and audit agents, manifests, rules, facts
- `entry-point/BUILD.md` — the step-by-step build procedure for a coding agent
- `entry-point/references/financial-tools.md` — the tool catalog
- `specs/` — architecture, interfaces, decisions, tech stack, app spec, context
- `AGENT-DEPLOYMENT.md` — full-platform deployment on a fresh Mac, for an agent with a shell
- `AGENTS.md`, `QWEN.md` — conventions and settled decisions for working in this repository
- `docs/developer-context/` — human-facing status, demo flow and handoff (not agent context)

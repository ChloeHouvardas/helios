# Helios Platform Guide

How to set up Helios, build an application on it, deploy it, operate it, and
read its evidence. For the API surface see `docs/API.md`; for the agents inside
an app see `docs/AGENT-GUIDE.md`; for the step-by-step build procedure a coding
agent follows see `entry-point/BUILD.md`.

## 1. What you get

- A **tool registry** of financial capabilities (payments, receivables, email,
  forecasting, behaviour profiles, public market data, spreadsheets, statement
  reconciliation, a knowledge graph, intent verification), each tool declaring
  whether it has side effects.
- An **application builder** that turns an intake into a validated app skeleton.
- An **agent runtime** that puts a deterministic audit gate between a model and
  every side effect, with trusted facts, signed grants and a hash-chained log.
- A **deployment engine** that takes an app directory and returns a URL, with a
  per-tenant tool service, event bus and audit sidecar.
- **Three flagship apps** built this way: Collect, Payables, Cash Planner.

## 2. Setup

Requirements: Python 3.13, Docker (Docker Desktop, Colima or OrbStack) for
deployment.

```bash
scripts/bootstrap-venv.sh        # one venv, every package editable, pinned deps
scripts/test-all.sh              # every suite, per-suite counts
```

Secrets are read from the environment only: `DASHSCOPE_API_KEY` for the model,
`TUSHARE_TOKEN` for live market data, `HELIOS_INTENT_SECRET` /
`HELIOS_EXECUTION_SECRET` for the tool service. None is needed to run the tests
or the demos; market data answers from fixtures (`HELIOS_MARKET_MODE=fixture`).

## 3. Build an app

```bash
.venv/bin/helios-app questions > intake.yaml          # 16 questions, answer in order
.venv/bin/helios-app spec-from-intake intake.yaml -o helios-app.yaml
# edit helios-app.yaml: entities, pages, tools (plain names or {name, side_effect})
.venv/bin/helios-app new apps/<id> --spec helios-app.yaml --intake intake.yaml
# write the policy (agents/<id>-audit.yaml), the facts (facts.py), the tools (tools.py)
.venv/bin/helios-app validate apps/<id>                # 0 error(s) before you demo
```

What the scaffold gives you: both agent manifests, a fact source over a
generated store, one `ToolDefinition` stub per tool, a FastAPI server with a
constant `/health`, the declared pages, control tests, and `helios.yaml`. What it
deliberately does not give you: the rules. Policy is the product; a generated
ceiling would cap the wrong thing.

`apps/helios-cash-planner/` is a complete example of this path, including its
answered intake.

## 4. Run an app locally

```bash
cd apps/<id>
../../.venv/bin/python app.py                  # the scenarios, each a control path
../../.venv/bin/python app.py --scenario <ID> --trace
../../.venv/bin/python -m pytest tests -q      # every control fires
../../.venv/bin/python -m uvicorn server:app --port 8000
```

Every app runs on a deterministic scripted driver by default: the thing being
demonstrated is the gate, not the model. To drive the runtime with Qwen set
`DASHSCOPE_API_KEY` and use `agent_integration.cli run` or an app's
`driver=model` option where it offers one.

## 5. Deploy

```bash
cd platform/deployment && ./bootstrap.sh       # daemon check, nginx, engine on :8100
cd ../..
.venv/bin/python platform/deployment/cli/helios.py doctor
.venv/bin/python platform/deployment/cli/helios.py deploy apps/<id> --tenant <t>
open http://localhost/apps/<id>-<t>/
```

An app must provide three things: `helios.yaml`, an ASGI `app`, and a `GET
/health` that returns 200 with no dependencies. The engine lints for secrets,
builds on a golden image that already contains the platform, smoke-tests on an
isolated network, attaches the audit sidecar, and routes through nginx. Exit
codes: `0` deployed, `1` rejected (every problem listed, with the build or smoke
log), `2` a `.env` is in the bundle, `3` engine unreachable, `4` usage.

Per tenant the engine runs one tool service (`helios-core-<t>`), one Redis bus and
an internal network. Each app gets a container, an audit sidecar and a named
data volume that survives redeploys and undeploys.

## 6. Operate

```bash
helios apps                               # what is running
helios logs <container> --tail 200        # a container's stdout
helios evidence --tenant <t>              # every app's action, audit and sidecar logs, chains verified
helios undeploy <app> --tenant <t>        # containers removed, data volume kept
```

(`helios` is `.venv/bin/python platform/deployment/cli/helios.py`.)

The approval queue (`/holds` on every app) is where held actions wait for the
owner. A denied action never reaches it; the queue page also lists what was
refused in code without asking.

## 7. The test ladder

`DEMOFLOW.md` defines the demo stages and the checks; `scripts/demo-check.sh`
runs them.

```bash
scripts/demo-check.sh T0 T1 T2            # offline: suites, apps, builder
scripts/demo-check.sh T3 T4 T5            # Docker: deploy, core, cross-app
scripts/demo-check.sh all
```

## 8. Where things live

| Need | Path |
|---|---|
| Architecture and components | `specs/architecture-mermaid.md` |
| Interfaces and contracts | `specs/interfaces.md`, `docs/API.md` |
| Decisions, decided vs open | `specs/decisions.md` |
| The app spec format | `specs/app-builder.md` |
| The build procedure | `entry-point/BUILD.md`, `entry-point/SKILL.md` |
| The tool catalog | `entry-point/references/financial-tools.md` |
| Agents, manifests, rules, facts | `docs/AGENT-GUIDE.md` |
| Deployment engine | `platform/deployment/README.md` |
| Status and handoff | `STATUS.md`, `HANDOFF.md` |

# Helios — Repository Handoff

**Audience:** an engineer or coding agent picking this repo up cold.
**Basis:** the `integration` branch on 2026-10-15, after every overnight worktree was
merged. Where a spec and the code disagree, the code is right and this file says so.
`STATUS.md` has the component-by-component state and the numbers; `DEMOFLOW.md` has the
demo stages, the test ladder and the gap list; `INVESTMENT-CONTEXT.md` is the pitch
writer's brief.

> `docs/sessions/` holds each overnight session's notes and the per-branch
> `WORKTREE.md` descriptions. They describe branches as they were, not the merged tree.

---

## 1. What Helios is

A platform where a coding agent turns one sentence of user intent into a working
agentic financial application, which is validated, deployed, and then operated by
agents inside it. Two kinds of AI, kept apart on purpose: the coding agent builds
the app; the in-app agents do the financial work. The governing principle is **the
model proposes, code decides**, and its corollary **facts are not claims**: every
value a rule depends on comes from a trusted fact source the agent cannot write.

Competition framing (Chengdu 80 2026, team Maple Queen's): 50% investment pitch, 25%
engineering verification, 25% architecture defence. See `specs/helios-context.md`.

## 2. One environment, one test command

```bash
scripts/bootstrap-venv.sh        # Python 3.13 venv, every package editable
scripts/test-all.sh              # all 14 suites: 1876 passed
scripts/demo-check.sh T0 T1 T2   # the offline rungs of the demo ladder
```

Always `.venv/bin/python`. Four packages set `addopts = "-q"`; the runners pass
`--override-ini="addopts="` so the per-suite counts print.

## 3. The tree

```
platform/
  financial-tools/    fin_tools: 40 tools in 9 families + the teammate packages it wraps;
                      python -m fin_tools is the tool service (the tenant core when deployed)
  action-agent-builder/  agent_toolkit: tool contract, store, action log, holds, CLI
  audit-agent-builder/   audit_agent: YAML policy, signed grants, trusted facts, hash-chained log
  agent-integration/     the ONLY package importing both builders: gated loop, bridge, model driver
  app-builder/           helios-app: intake, spec, scaffolder, validator, UI kit, stacks.yaml
  deployment/            engine (docker-py), CLI, three golden images, sdk/helios_sdk
apps/
  helios-collect/  helios-payables/  helios-cash-planner/     the three flagships
entry-point/        SKILL.md, BUILD.md, references/, examples/invoice-payer, checks/
specs/              architecture, interfaces, decisions, tech stack, app builder, context
scripts/            bootstrap-venv.sh, test-all.sh, demo-check.sh (+ demo_check.py)
```

**The boundary rule is the architecture.** `action-agent-builder` and
`audit-agent-builder` never import each other. `agent-integration` is the one place
allowed to know both. The audit agent is only trustworthy because the app it audits
cannot modify it.

## 4. How a flagship is built and how it runs

Build: `helios-app questions` → `spec-from-intake` → edit the spec → `helios-app new`
→ write the policy (`agents/<id>-audit.yaml`), the facts, the tools → `helios-app
validate` → `platform/deployment/cli/helios.py deploy apps/<id> --tenant <t>`.
`apps/helios-cash-planner/intake.yaml` is a complete worked intake.

Run: read-only tools execute directly; every side effect becomes a proposal; the
in-app auditor (facts + owner) approves, holds or denies; the deployment sidecar
(fact-free rules, execution secret) denies or forwards; the tenant core executes.
Every step lands in the app's action log, the auditor's hash-chained log and the
sidecar log, all readable from `GET /api/v1/tenants/<t>/logs` on the engine.

## 5. Verified flows

```bash
cd apps/helios-collect      && ../../.venv/bin/python app.py   # {'done': 5, 'refused': 3, 'declined_by_owner': 2}
cd apps/helios-payables     && ../../.venv/bin/python app.py   # {'done': 9, 'refused': 4, 'declined_by_owner': 2, 'rejected_by_ledger': 1}
cd apps/helios-cash-planner && ../../.venv/bin/python app.py   # {'done': 4, 'refused': 5, 'declined_by_owner': 2}
.venv/bin/helios-app validate apps/<any>                        # 0 error(s)
.venv/bin/python -m agent_integration.cli selfcheck             # seam check: OK
```

The moments worth a judge's minute: Collect INV-1006 and INV-1009; Payables BILL-2002
(owner approves, still refused), BILL-2012 (lying agent held), BILL-2016 (the
platform forecaster says payroll would be short); Cash Planner WARN-INVENTED (a
shortfall in a fine week, refused), WARN-INFLATED (held against the projection),
DRAW (always the owner's call, rate and provenance shown).

Docker: `platform/deployment/bootstrap.sh`, then `scripts/demo-check.sh T3 T4 T5 T6`.

## 6. Traps — read before you touch anything

1. **A gated tool can register as read-only and nothing raises.** The two tool
   contracts spell the flag differently; `adapters.ToolContractView` and
   `fin_tools.toolkit_bridge` are the only sanctioned bridges. Run `selfcheck` after
   touching either contract.
2. **Fact resolution is keyed on a fixed argument-name set** (`invoice_id, bill_id,
   customer_id, supplier_id, subject_id, payment_id`). Name an argument anything
   else and fact-bound rules fail closed and deny everything.
3. **`/health` must be constant and build nothing.** The smoke test runs with no
   Redis, no sidecar, no key and no volume.
4. **Never derive the repo root from `Path(__file__).parents[N]` at import time** in
   an app; inside the image the bundle is `/app` and that raises before uvicorn binds.
5. **`build_world()` is per bundle now**, but apps still call `bundle.payments` /
   `bundle.graph` directly rather than `registry.call`; keep doing that.
6. **Money is integer cents or Decimal strings, never float.** A platform rate is a
   float percentage; convert with `Decimal(str(value_pct))` at the boundary.
7. **A new sibling package under `financial-tools` must be added to the explicit
   package list in its `pyproject.toml`**, or it will not install.
8. **`addopts = "-q"` doubles up.** Pass `--override-ini="addopts="` or the summary
   line vanishes and a green run looks like a hang.
9. **The tenant run directory is `<repo>/.helios-run`**, holding tenant secrets. It is
   gitignored. nginx mounts `.helios-run/nginx` from the same root.
10. **Docker Hub is unreachable from this machine.** Base images were pulled through a
    mirror and retagged; verify digests before a judged demo.

## 7. Open items

- Holds and replay guards do not survive a container restart (seam:
  `audit_agent.policy` `GrantVerifier` / `ReplayGuard`; keep it opt-in).
- The flagships execute money movement in-process inside their container; the
  sample app shows the sidecar → core path. Routing `pay_bill` and the email tools
  through `helios_sdk.propose_action` is the last step of "tools on the platform".
- Payables declares a read of `cashplan[shortfall_warning]` and does not act on it.
- No live Qwen run in the demo; the driver is wired to DashScope.
- The sheets and receivables stores are not in the core's SQLite snapshot.

## 8. House rules

From `AGENTS.md`: conventional commits, stage explicit paths, never `git add -A`, no
push or history rewrite unless asked, fail closed, report every validation problem at
once, write error messages for a model to act on, no credentials in code or logs.

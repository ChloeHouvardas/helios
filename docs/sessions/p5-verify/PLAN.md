# Session p5-verify — Plan

Objective: prove the platform from a cold start, rehearse the surprise task, fix only
what the rehearsal breaks, and write `STATUS.md`.

This is the last session. It is strictly downstream of the other night-run sessions
(P0..P4). Nothing can be verified until their work lands on `chloe-dev`.

## Baseline (recorded 2026-10-15, `chloe-dev` @ `cc96f0a`)

Run from the main checkout and from a fresh clone into `/tmp/helios-cold`. All five
suites green where the interpreter had the dependencies:

| Suite | Command | Result |
|---|---|---|
| audit-agent-builder | `cd platform/audit-agent-builder && PYTHONPATH=src python3 -m pytest tests` | 79 passed |
| financial-tools | `cd platform/financial-tools && python3 -m pytest tests` | 61 passed, 2 skipped |
| helios-collect | `cd apps/helios-collect && python3 -m pytest tests` | 74 passed |
| invoice-payer | `cd entry-point/examples/invoice-payer && python3 -m pytest tests` | 41 passed |
| agent-integration | `cd platform/agent-integration && .venv/bin/python -m pytest tests` | 123 passed |

Total **378 passed, 2 skipped** — matches `HANDOFF.md` §4 exactly.

Interpreter: `python3` on this machine is 3.13.9 (Anaconda). A separate 3.14.4 also
exists at `/Library/Frameworks/.../3.14/bin/python3`; it must not be used.

## Defects already found (pre-existing, not caused by this session)

1. `addopts = "-q"` in the `[tool.pytest.ini_options]` of `platform/audit-agent-builder`,
   `platform/financial-tools` and `platform/agent-integration` suppresses pytest's
   summary line when pytest is also invoked with `-q`. The "N passed" evidence the
   verification session must record is invisible. Workaround used above:
   `--override-ini='addopts='`. This must be fixed by whoever owns `scripts/test-all.sh`
   (P0) or by this session once that script exists, because Task 1 requires the
   per-suite "N passed" lines as evidence.
2. `agent-integration` does not collect from a fresh clone:
   `ModuleNotFoundError: No module named 'agent_integration'`. Its `pyproject.toml`
   declares `agent-toolkit`, `audit-agent-builder`, `pyyaml>=6.0`, `jsonschema>=4.21`
   and a `dev = ["pytest>=8.0"]` extra, but nothing installs them. This is precisely
   the "only existed in someone's working tree" problem Task 1 targets; it needs
   `scripts/bootstrap-venv.sh` to exist first.

## Tasks

1. **Cold start** — fresh clone, `scripts/bootstrap-venv.sh`, `scripts/test-all.sh`,
   `python -m agent_integration.cli doctor` and `selfcheck`. Record exact outputs and
   the total count. *Blocked: `scripts/` does not exist (P0).*
2. **Platform up** — `platform/deployment/bootstrap.sh`, `helios doctor`, deploy three
   apps to tenant `demo`, `/health` 200 each; else BLOCKED + in-process equivalents.
   *Blocked: `platform/deployment/` does not exist (P0). Note
   `OTHER-TEAMMATE-STUFF/deployment-stuff/fPT9INmvWJ/default/helios/engine/` exists in
   the main checkout and is the engine referenced by decision B1.*
3. **Cross-app story end to end** — `scripts/demo-cross-app.sh`. *Blocked: needs
   `apps/helios-payables`, `apps/helios-cash-planner`, the deployment engine, and the
   `fin_tools` email/forecast services to be integrated (P1/P2).*
4. **Surprise-task rehearsal** — build `apps/helios-expense-approver` from
   `entry-point/SKILL.md` + `BUILD.md` only, 90-minute time-box. *Partly blocked: the
   on-ramp docs exist, but the validator (`helios-app validate`) and deployment do not.
   Deliberately deferred — rehearsing against a soon-stale on-ramp would generate
   defect fixes that P0/app-builder will overwrite.*
5. **STATUS.md** — write it for a cold reader, plus `README.md`, `HANDOFF.md` §2 and
   §8, and `specs/architecture-mermaid.md` prose. *Blocked: must describe four apps and
   a deployment engine that do not exist yet. Writing it now would violate section C5
   (do not claim a result not observed, do not reference an unlisted path).*

## Strategy

Record the baseline and the blockers (done here), then poll `chloe-dev` until the
upstream sessions land. Do not re-implement another session's deliverables, and do not
write `STATUS.md` from speculation.

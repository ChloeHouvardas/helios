# Session p5-verify — Blockers

Recorded 2026-10-15 against `chloe-dev` @ `cc96f0a`. This session is the last one and
is downstream of every other night-run session. The repository is still at the pre-P0
state, so the shared infrastructure this session must verify does not exist yet.

## Absent prerequisites (verified by listing the checkout)

```
$ ls -1 scripts
ls: scripts: No such file or directory
$ ls -1 platform
README.md  action-agent-builder  agent-integration  app-builder  audit-agent-builder  financial-tools
$ ls -1 apps
helios-collect
$ ls -1 *.md
AGENTS.md  HANDOFF.md  README.md  SETUP.md  plan.md          # no QWEN.md, no BUILD.md
$ ls -1 docs
milestones                                                    # no docs/research/, no docs/sessions/
```

No `scripts/` (therefore no `bootstrap-venv.sh`, no `test-all.sh`), no
`platform/deployment/`, no `apps/helios-payables`, no `apps/helios-cash-planner`, no
root `QWEN.md`, no `docs/research/`. `find` for `test-all.sh`, `bootstrap*.sh` and
`helios.yaml` across the checkout (excluding `.venv`) returned nothing.

The only other worktree is `.qwen/worktrees/p0-foundation`, clean at the same commit
`cc96f0a` — P0 has started but not landed.

---

## Task 1 — cold start

**Tried.** Cloned the main checkout and ran every suite from nothing but the repo and
`python3` 3.13.9:

```
$ git clone "$MAIN" /tmp/helios-cold && cd /tmp/helios-cold && git checkout chloe-dev
Already on 'chloe-dev'
Your branch is up to date with 'origin/chloe-dev'.

$ ls -1 scripts
ls: scripts: No such file or directory
```

Four suites pass cold; the fifth cannot even be collected:

```
$ cd platform/agent-integration && python3 -m pytest tests --override-ini='addopts='
ImportError while importing test module '/private/tmp/helios-cold/platform/agent-integration/tests/test_integration.py'.
E   ModuleNotFoundError: No module named 'agent_integration'
ImportError while importing test module '/private/tmp/helios-cold/platform/agent-integration/tests/test_model_driver.py'.
E   ModuleNotFoundError: No module named 'agent_integration'
ERROR tests/test_integration.py
ERROR tests/test_model_driver.py
============================== 2 errors in 0.12s ==============================
```

`python -m agent_integration.cli doctor` and `selfcheck` cannot be run either, for the
same reason, and neither can be run from the repo venv `$WT/.venv/bin/python` because
that venv does not exist and nothing creates it.

Second, independent defect found while recording the baseline. `addopts = "-q"` is set
in `[tool.pytest.ini_options]` of `platform/audit-agent-builder/pyproject.toml`,
`platform/financial-tools/pyproject.toml` and `platform/agent-integration/pyproject.toml`.
Invoking pytest with `-q` therefore doubles up quiet mode and pytest prints no summary
line at all:

```
$ cd platform/audit-agent-builder && PYTHONPATH=src python3 -m pytest tests -q
........................................................................ [ 91%]
.......                                                                  [100%]
$                                     # no "79 passed" line; nothing to record
```

**Decisions needed.**
1. Who owns `scripts/test-all.sh` — P0 or this session? If this session writes it, it
   must first write `scripts/bootstrap-venv.sh` too, since Task 1 says to create the
   venv "exactly as `scripts/bootstrap-venv.sh` does (P0 creates that script)". Writing
   both here would duplicate P0's deliverable and almost certainly conflict at merge.
2. May `addopts = "-q"` be dropped from those three `pyproject.toml` files, or is it
   load-bearing for another session's output format? Until it is, no session can record
   the per-suite "N passed" evidence Task 1 demands.

---

## Task 2 — platform up

**Tried.** Looked for the deployment engine and its bootstrap.

```
$ ls -1 platform
README.md  action-agent-builder  agent-integration  app-builder  audit-agent-builder  financial-tools
$ ls -1 platform/deployment
ls: platform/deployment: No such file or directory
```

Decision B1 names `platform/deployment/` as the deployment contract, imported from the
Mac-mini build. It is not in the checkout. `OTHER-TEAMMATE-STUFF/deployment-stuff/fPT9INmvWJ/default/helios/engine/`
does exist in the main checkout (seen via `git status --ignored`), which is presumably
that raw import, but per section C3 `OTHER-TEAMMATE-STUFF/` is read-only and per B1 the
engine lands at `platform/deployment/` — a move this session is not authorised to make,
since it is another session's deliverable.

**Decision needed.** Does P0 own importing `deployment-stuff` into `platform/deployment/`?
Until it does, there is no `helios` CLI, no tenant `demo`, and no `/health` to check.

---

## Task 3 — the cross-app story

**Tried.** Listed the apps the story requires.

```
$ ls -1 apps
helios-collect
```

Two of the three actors do not exist (`helios-payables`, `helios-cash-planner`), and
`HANDOFF.md` §2 confirms cross-app coordination is not achievable today: "A Collect app
can publish a verified due-date change, but Cash Planner does not exist and could not
read it if it did." Shared Events & Updates "does not exist in any form." There is also
no `fin_tools` service exposing `email.outbox` over HTTP for curl to reach.

A `scripts/demo-cross-app.sh` written now would be a script against endpoints that do
not exist, and it would have to be rewritten once the apps land.

**Decision needed.** Which session owns `apps/helios-payables` and
`apps/helios-cash-planner` (P2 per the flagship numbering in `specs/helios-context.md`)?

---

## Task 4 — surprise-task rehearsal

**Not blocked on missing code, but deliberately deferred.** `entry-point/SKILL.md` and
`entry-point/BUILD.md` both exist, so the rehearsal could be attempted against today's
on-ramp. It is deferred because the exit criteria cannot be met: "validator 0 errors"
requires `helios-app validate`, which does not exist (`platform/app-builder` is
design-only per `HANDOFF.md` §2), and "deployed `/health` 200" requires Task 2.

Rehearsing now would produce a pile of doc/tool defect fixes against an on-ramp that
P0/app-builder is about to rewrite — exactly the churn section D warns against.

**Decision needed.** If the rehearsal must happen before `app-builder` lands, is
`entry-point/checks/check_app.py` an acceptable stand-in for `helios-app validate`, as
`HANDOFF.md` §2 treats it?

---

## Task 5 — STATUS.md

**Blocked by all of the above.** `STATUS.md` must state, for every box, what is
implemented, where it lives, how to run it, its test count, and its gaps — and it must
cover four apps plus the deployment engine. Section C5 forbids claiming a result not
observed in this session's tool output, and forbids referencing a path not read or
listed here. Writing it now would produce a document describing software that does not
exist.

**Decision needed.** None — this unblocks automatically once tasks 1–4 land.

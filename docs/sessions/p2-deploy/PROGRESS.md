# Session `p2-deploy` — Progress

Three lines per entry. Re-read `PLAN.md` and this file after any context compaction.

---

## 1. Preflight and plan (Task 1)

- Created the missing worktree `.qwen/worktrees/p2-deploy` on new branch
  `worktree-p2-deploy` from `chloe-dev@cc96f0a`; confirmed via `pwd` and
  `git branch --show-current` before any edit.
- No Docker daemon: `docker info` fails on both the default and the explicit
  `unix:///var/run/docker.sock`; Docker Desktop is the active context and is not running,
  Colima and OrbStack are absent → all live steps BLOCKED, and Colima was deliberately not
  installed (PLAN §1.4, decision 1).
- Baseline recorded **378 passed, 2 skipped** across five suites (79 / 61+2 skipped / 123 /
  74 / 41), reproduced by hand because `scripts/test-all.sh` does not exist;
  `agent_integration.cli selfcheck` ran clean.

## 2. Gating discovery: P0 has landed nothing

- `platform/deployment/`, `scripts/`, `docs/research/`, `docs/sessions/` and the repo `.venv`
  are all absent; `git diff chloe-dev...worktree-p0-foundation` is empty, so Tasks 2/3/5/8
  have no files to edit inside the worktree.
- The raw engine is present but untracked and read-only at
  `OTHER-TEAMMATE-STUFF/deployment-stuff/fPT9INmvWJ/default/helios/`; added **Task 0
  (engine import)** as a prerequisite, held for operator go-ahead because P0 is contracted to
  deliver the same subtree (PLAN §1.2, decision 6).
- `HANDOFF.md` §7's uncommitted-work list is stale — that work has since been committed;
  current `git status --porcelain` shows only `?? .DS_Store`, `?? .qwen/`,
  `?? OTHER-TEAMMATE-STUFF/`.

## 3. Source-verified engine findings that change the work

- Confirmed all five spec-listed bugs from source; two are worse than described: the auditor
  records **any** non-2xx from core as `executed` (it checks no status code at all), and a
  second file `.helios-run/nginx/{slug}.conf` also hardcodes the app IP so **both** must be
  regenerated on redeploy (PLAN §2.1).
- Found a new Task 7 blocker: `apps/helios-collect/app.py:77` (`parents[1]`) and
  `entry-point/examples/invoice-payer/app.py:73` (`parents[2]`) raise `IndexError` at import
  time under the container path `/app`, verified with the interpreter → lazy import and
  guarded `sys.path` inserts are mandatory for correctness (PLAN §2.3).
- Spec §A's interpreter claim is wrong here: system `python3` is **3.13.9** with pytest
  8.4.2, not 3.14 without deps; `MAX_PAYMENT_AMOUNT` is a `float`, violating decision B.6,
  so Task 5 converts it to `Decimal` (PLAN §1.3, decision 4).

## 4. Blockers carried forward

- **O1** who lands `platform/deployment/` + `scripts/` + the `helios` pyproject — everything
  engine-side waits on it.
- **O3** the attached spec is truncated mid-Task 8 at "correct every mismatch you f…"; the
  rest of Task 8, any Task 9+, and §G (blocked-handling procedure) are missing and were not
  invented.
- **O5** `../helios-qwen-prompts/` is
  unreadable (`Operation not permitted`) despite the parent directory being granted.

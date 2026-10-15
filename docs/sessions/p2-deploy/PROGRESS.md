# Session `p2-deploy` — Progress

Three lines per entry. Re-read `PLAN.md` and this file after any context compaction.

---

## 1. Preflight and plan (Task 1)

- Created the missing worktree `.qwen/worktrees/p2-deploy` on new branch
  `worktree-p2-deploy` from `chloe-dev@cc96f0a`; confirmed via `pwd` and
  `git branch --show-current` before any edit.
- No Docker daemon at session start: `docker info` failed on both the default
  context and the explicit socket. Docker Desktop turned out to be installed and
  merely stopped, so it was started rather than installing Colima beside it
  (which would mean two competing daemons) — a deliberate deviation from the
  task's literal instruction, logged as decision 1 in PLAN.md.
- Baseline recorded **378 passed, 2 skipped** across five suites, reproduced by
  hand because `scripts/test-all.sh` did not exist. Gotcha: `audit-agent-builder`
  and `financial-tools` set `addopts = "-q"` in their own pyproject, so passing
  `-q` again suppresses the summary line entirely and looks like a hang.

## 2. Gating discovery: P0 had landed nothing

- `platform/deployment/`, `scripts/`, `docs/research/` and the repo `.venv` were
  all absent, and `worktree-p0-foundation` had a zero diff against `chloe-dev`, so
  the engine-side tasks had no files to edit. The raw engine existed only as an
  untracked copy under `OTHER-TEAMMATE-STUFF/`, which does not carry into a
  worktree.
- Resolved by importing the engine verbatim as its own commit (`ce3efbd`) so the
  fixes that follow are reviewable as a diff against a known-good baseline. The
  source directory was left untouched, per the read-only rule.
- `HANDOFF.md` §7's uncommitted-work list is stale — that work has since been
  committed. The rest of §7's rules still apply.

## 3. Engine fixes (Tasks 2–5, commit `7b67e09`)

- Routing, ingress-IP retry, blue/green rollback and the content-hash base image
  all implemented, with 36 mocked-docker tests. Verified the rollback guarantee by
  driving the real `deploy()` path, not by testing a hand-rolled loop.
- Two bugs were worse than the spec described: the auditor checked **no** status
  code, so any non-2xx from core (not just 404) was recorded as `executed`; and a
  second file `.helios-run/nginx/<slug>.conf` also hardcodes the app IP, so both
  carriers must be regenerated on redeploy.
- The sidecar now derives policy from the app's own audit manifest via
  `audit_agent`'s loader and writes decisions with its hash-chained writer, so the
  two gates cannot drift. Only the fact-free subset is enforced, and every skipped
  rule is disclosed at `/health`.

## 4. Design correction: the spec's allow-list formula was unusable

- Spec Task 5 said the sidecar allow-list is `tools[].name ∩ permissions`. Verified
  against both real apps that this intersection is **empty**: manifests name app
  tools (`send_email`, `record_inbound_payment`) while `permissions` names platform
  families (`payments`, `email`). A literal implementation would deny every action.
- Implemented as a union instead — an action is allowed if its name is a granted
  family OR a tool the app's manifest declares — and documented why in the README,
  the builders guide and `specs/decisions.md`. It still fails closed: a name in
  neither set is refused, and only families can reach core because core routes on
  the family name.
- `MAX_PAYMENT_AMOUNT` moved from `float` to `Decimal`, since decision B.6 forbids
  float money and a float cap compares imprecisely against a decimal amount.

## 5. App conversions (Task 7, commit `ab8ecf8`)

- Found and fixed a real deployment blocker the spec did not mention: both apps
  derived the repo root as `Path(__file__).parents[N]` at import time, which raises
  `IndexError` under the container path `/app` (verified with the interpreter). Both
  apps would have crashed before `/health` could answer, failing the smoke test with
  an opaque traceback. That made lazy imports mandatory for correctness.
- Both apps converted: `helios.yaml` (alongside the existing `helios-app.yaml`, not
  replacing it), `server.py`, six templates and CSS each. `/health` is a constant;
  URLs use `X-Forwarded-Prefix` because relative links break at nested routes.
- 31 new tests with `fastapi.testclient`. invoice-payer's asserts INV-1003 is
  refused even though the owner CONFIRMS the payment — the control that example
  exists to demonstrate.

## 6. End-to-end deployment verified live

- Both apps deployed for real and reachable at `http://localhost/apps/<app>-p2-deploy/`
  with correct titles, prefixed links, rendered invoices and `text/css` on
  `style.css`. Tenant containers: redis, core, app and audit sidecar for each.
- The sidecar loaded the app's manifest (5 tools, 8 fact-bound rules correctly
  delegated), the hash chain verified `intact`, and the named data volume mounted
  at `/data` in both containers.
- The approval queue was exercised through nginx: a hold appeared with both buttons,
  approve returned 303 and emptied the queue, a second approve returned **404**
  (single-use, so no double execution), decline executed nothing, and the chain
  stayed intact.
- An early reading of "0 buttons" was zsh mangling UTF-8 in a shell variable, not a
  product bug — re-verified with file-based grep.

## 7. Environment obstacles worked around, recorded not papered over

- **Docker Hub is unreachable from this machine** (`registry-1.docker.io` times out;
  PyPI returns 200). Images were pulled via `docker.m.daocloud.io` and retagged to
  the names the Dockerfiles expect. `bootstrap.sh` does not do this automatically —
  flagged in the handoff note.
- **`fin-tools` cannot be pip-installed**: its `pyproject.toml` declares no
  `[build-system]`, so auto-discovery finds `fin_tools`, `fixtures`, `research` and
  `tests` side by side and refuses to guess. Worked around with `PYTHONPATH` in the
  base image and a `.pth` file in the venv, because `platform/financial-tools/` is
  off-limits to this session. Handed to P1 to fix properly.
- **A stale engine from another session held :8100**, running from `/tmp/helios-pre`
  and returning 404 on my new `/api/v1/doctor`. Not killed — the rules forbid
  touching processes I do not recognise — so mine ran on :8101, and `bootstrap.sh`
  now detects an occupied port instead of blindly `pkill`ing.
- **`.helios-run` moved to the repo root** when `REPO` was re-resolved upward for
  the new build context. nginx had been started mounting the old path, so it served
  its default page while the engine believed it had routed an app — a 200 that was
  not the app. Caught by checking response bodies rather than trusting status codes;
  `bootstrap.sh` now derives the path.

## 8. Final state

- **470 passed, 0 skipped** repo-wide via `scripts/test-all.sh` (79 / 86 / 123 / 92
  / 54 / 36). The rise from the 378 baseline is fully accounted for: +25 because two
  financial-tools modules were module-skipped under Anaconda and now run under
  `.venv`, +31 new app server tests, +36 new deployment-engine tests, minus the 2
  that moved from skip to run.
- Six commits on `worktree-p2-deploy`: `9509f5e` notes, `ce3efbd` import, `7b67e09`
  engine, `1ec10cc` CLI, `ab8ecf8` apps, `e1c97a3` docs. Local only; no push, no
  rebase, no amend. Every staged path checked with `git status --porcelain` first.
- Two run artifacts my manual smoke test dirtied (`invoice-payer/runs/*.jsonl`) were
  restored rather than committed, and `.helios-run/` added to `.gitignore` because it
  holds per-tenant execution secrets.

## 9. Not done — deferred, with reasons

- **Task 6 (SQLite-backed `HoldStore` / `ReplayGuard`) was not implemented.** Holds
  remain in-memory, so pending approvals and replay protection are lost on restart
  even though `data_dir` is a persistent volume. Deferred because
  `agent-integration/{cli,__init__,model_driver}.py` carry other agents' in-flight
  work and the rules forbid editing files I do not recognise; it needs coordination.
  The seam to wire is `GrantVerifier` / `ReplayGuard` in `audit_agent.policy` — wire
  it, do not fork it. Details in `HANDOFF-TO-P1-P3.md`.
- **Task 8's spec text was truncated** mid-sentence ("correct every mismatch you
  f…"), so its remaining requirements and §G were never known. The CLI subcommands,
  `DELETE /api/v1/apps/{container}` and the fresh README named in the visible portion
  were all delivered; anything beyond it was not invented.
- **The `helios` console-script entry point** was not created, because P0's
  pyproject (which was to carry it) never landed. The CLI works via
  `.venv/bin/python cli/helios.py` and is documented that way.
- **`/Users/.../helios-qwen-prompts/` stayed unreadable** (`Operation not permitted`)
  despite its parent being granted, so the prompt set and research reports referenced
  by §A were never read.

# Session p4-payables — plan

**Objective:** build `apps/helios-payables` (flagship 2) THROUGH the platform, following
`entry-point/BUILD.md`: intake → spec → audit manifest first → facts → tools →
scenarios and control tests → validate → deploy.

## Premise verification (done first, and it changed the plan)

The session prompt assumed P0–P3 had merged. **They had not.** Verified with tools
before writing any code:

| Prompt claimed | Reality on `chloe-dev` @ `cc96f0a` |
|---|---|
| `platform/deployment/` engine exists | **Absent.** No `helios` / `helios-app` CLI on PATH |
| `scripts/bootstrap-venv.sh`, `scripts/test-all.sh` | **Absent.** No `scripts/` directory |
| `QWEN.md` | **Absent** |
| `.venv` at repo root (Python 3.13) | **Absent.** Created it; `python3` is 3.13.9, not 3.14 |
| `fin_tools` has `market.*`, `forecast.*`, `behavior.*`, `collect.*`, `intents.*` | **Only 12 tools:** `email.*`, `graph.*`, `payments.*`, `simulation.forecast`, `spreadsheet.*` |
| `email.send_template` with 5 named templates | **Absent.** `email.send` takes a `template` kwarg; only `payment_reminder` / `payment_confirmation` exist |
| `helios-collect/server.py` (FastAPI) as the pattern | **Absent.** Collect is CLI-only; no FastAPI anywhere |
| Sibling worktrees contain merged P0–P3 work | All at the same `cc96f0a`; none merged |
| Docker available for deploy | Daemon **not running**; `HELIOS_ENGINE_URL` unset |

**Consequence:** `helios deploy` is BLOCKED per the prompt's own escape clause. Delivered
a deploy-ready `helios.yaml` + a real `GET /health` instead, and built the smallest
local version of the missing capabilities inside the app (per the prompt's instruction),
noting each in the final report.

## Plan

1. ~~Verify premises against disk~~ ✅
2. ~~Record the test baseline before changing anything~~ ✅ 5 suites green:
   audit-agent-builder, financial-tools, agent-integration, helios-collect (74),
   invoice-payer (41)
3. ~~Create the worktree~~ ✅ `.qwen/worktrees/p4-payables` on `worktree-p4-payables`
4. ~~Create the repo venv~~ ✅ Python 3.13.9, deps pinned in `requirements.txt`
5. ~~Fixtures: 8 suppliers, 15 bills, one account at 180,000.00~~ ✅
6. ~~`config.py` — thresholds mirrored from the manifest~~ ✅
7. ~~`book.py` — data layer over the platform store~~ ✅
8. ~~**Audit manifest FIRST** (BUILD.md step 3) — 17 rules~~ ✅
9. ~~`facts.py` — the fact source~~ ✅
10. ~~Action manifest~~ ✅
11. ~~`funding.py` — local stand-in for the absent `market.*`~~ ✅
12. ~~`tools.py` — 10 tools, 5 read-only + 5 gated~~ ✅
13. ~~`app.py` — composition, 15 scenarios, CLI pages~~ ✅
14. ~~`helios-app.yaml` — the app spec~~ ✅ `check_app.py`: 0 errors
15. ~~`tests/test_controls.py` — one offline test per control~~ ✅ 60 passed
16. ~~`server.py` + 5 Jinja2 templates — real web UI~~ ✅ all routes 200
17. ~~`helios.yaml` — deployment contract~~ ✅ (engine absent)
18. ~~README with the scenario table from observed output~~ ✅
19. ~~Session notes~~ ✅ this file
20. Commit in logical units; verify the baseline is still green

## Decisions taken autonomously (safer, reversible option each time)

1. **FastAPI stack is real, not aspirational.** The prompt declared FastAPI + Jinja2 the
   default stack and FastAPI was not installed, so it is installed and pinned. The UI is
   served pages, not CLI renderers. Reversible: `app.py` remains a complete CLI.
2. **`market.*` replaced by `funding.py`, quarantined in one file.** No network, pinned
   rate table, every result carries `provenance` (`mode`, `as_of`, `stale`) and the
   "not financial advice" disclaimer. It ranks options and never authorises anything.
   Deleting the file is the whole migration path if `market.*` lands.
3. **A credit-line draw is a gated money movement, not a flag.** The first design let
   `pay_bill` accept `funding: credit_line`, which would have required weakening
   `app.insufficient_funds` — explicitly forbidden. Instead `draw_credit_line` is its own
   gated tool capped against the fact `credit_line_headroom_cents`, so the balance
   genuinely rises and the payment is then genuinely affordable. **No control was
   weakened to make a scenario pass.**
4. **15 bills, not 14.** The idempotency-replay scenario needs a bill clean on every
   *policy* rule, so the only thing that can stop it is the ledger. Reusing a
   policy-dirty bill would have made the test prove nothing.
5. **Web owner is scripted; live hold-resume stays in the CLI.** Resuming a held action
   needs the live runtime. The two owner stances (approve-all / decline-all) are what
   demonstrate the control, and `app.py --interactive` provides the real loop.
6. **`app.run_total_cap` bound on facts, not arguments.** 22,000.00 × 3 = 66,000.00 >
   the 60,000.00 cap, with every individual payment under the 25,000.00 threshold — so
   the cap is the only thing that fires, which is the point.

## Traps hit and how they were resolved

- **`jsonschema` missing → every side effect denied.** The failure mode is fail-closed
  and silent: no traceback, just "argument schema validation is unavailable". Pinned it.
- **`Store.get_customer` / `get_invoice` return copies.** `reverify_supplier`, `mark_paid`
  and `reschedule` were silently no-oping. Fixed via `_mutate_supplier` / `_mutate_bill`.
- **`details_changed_days_ago` resolves to the key `details_changed`**, not
  `details_changed_date` — so the 30-day window never applied and SUP-003 (45 days,
  outside the window) was denied. Fixed in `Book._details_changed_on`.
- **`auth_code` leaked into the printed `owner_question`.** `REDACTED_KEYS` does not
  cover it. Masked by SHAPE (16+ hex runs) because the auditor has already rendered the
  prose, so key names are gone.
- **`_subject_key()` ignores `bill_id`**, so `send_remittance` must require
  `supplier_id` or the frequency rule denies everything.
- **The runtime records contacts only for a tool named exactly `send_email`**, so
  `send_remittance` records its own.
- **Action records store output as a JSON string under `output`**, not a dict under
  `result` — which made the ledger-rejection status invisible at first.
- **`TemplateResponse(name, ctx)` is the old signature**; current Starlette wants
  `(request, name, ctx)`.

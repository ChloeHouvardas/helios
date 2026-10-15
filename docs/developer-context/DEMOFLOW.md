# docs/developer-context/DEMOFLOW.md — what Helios demonstrates, and the tests that prove it

Written 2026-10-15 from the specs, `docs/developer-context/HANDOFF.md`, `000demo-flow.md`, and the six
worktree write-ups (`p0-foundation`, `p1-tools`, `p2-deploy`, `p3-builder`,
`p4-payables`, `p5-verify`). This is the target state the merged tree must reach,
and the test ladder that proves it. Merge sequencing is out of scope here.

Legend: **[built]** verified on some branch · **[partial]** exists but not in the
demo shape · **[todo]** must be built · **[open]** needs a decision.

---

## 1. Settled decisions that shape the demo

| Decision | Status |
|---|---|
| Apps never call each other. Cross-app state flows only through the shared knowledge graph and the tenant event bus. | settled 2026-10-15 |
| Financial tools run **on the platform**, not inside each app. The tenant core container runs `fin_tools`' bearer-authenticated HTTP service; the mock core is replaced. | settled 2026-10-15 — **[built]**; the flagships still execute their own money movement in-process (see G3) |
| Logs are central. The action log, the auditor's hash-chained log and the sidecar log for every app of a tenant are readable from one endpoint on the **deployment engine** (`GET /api/v1/tenants/<t>/logs`), which reads each app's `/data` volume through docker-py. Chosen over the core service because core never sees the sidecar log. | settled 2026-10-15 — **[built]** |
| Three flagships: Collect, Payables, **Cash Planner**. All three are **fully deployed under one tenant at the same time**, and that is a test (T3.18). | settled 2026-10-15 — **[built]** |
| "Apps cannot reach each other" is a convention, not a network property. We do not enforce it with Docker networking; we document it and never write app-to-app calls. | settled 2026-10-15 |
| The p5 scaffolder defects are cleared before a surprise task is attempted. | settled 2026-10-15 — **[built]** |
| The surprise-task rung (T6) and the hold-persistence phase are **dropped** from this plan. | settled 2026-10-15 |
| Demos run on the scripted driver. Live Qwen is tested separately, later. | settled |
| Standalone `audit_agent` is the authoritative gate; the sidecar may deny or forward, never approve. | settled (p2, `specs/decisions.md`) |
| Money is simulated; integer cents; never float. | settled |
| p1's `market_data/` package survives the merge; chloe-dev's `daily_quotes`, `trading_calendar` and its ledger-isolation grep test are folded in. | settled 2026-10-15 — **[built]** |

---

## 2. The system in one page

**Build path** (the coding agent, Qwen or a stub):

```
one sentence
  → helios-app questions            16-question intake → intake.yaml
  → helios-app spec-from-intake     → helios-app.yaml
  → helios-app new apps/<id>        scaffold: both manifests, facts.py, tools.py,
                                    store.py, server.py, helios.yaml, templates, tests
  → agent writes the POLICY         agents/<id>-audit.yaml rules + tool handlers
  → helios-app validate apps/<id>   cross-file checks, exit 0/1/2
  → helios deploy apps/<id> --tenant <t>
  → http://localhost/apps/<id>-<t>/
```

**Run path** (inside a deployed app):

```
trigger → action agent (scripted driver)
   read-only tool ──────────────── runs directly
   side-effect tool → Proposal → in-app audit_agent (facts + owner)
                                   allow → sidecar (fact-free rules, execution secret)
                                            → tenant core = fin_tools service
                                   hold  → owner decides on /holds → resume
                                   deny  → reason back to the agent
   every step → app action log + auditor hash-chained log + sidecar log
```

**Deployed topology per tenant** (p2, verified live):

```
nginx (ingress) ── /apps/<app>-<tenant>/ ──▶ app container  (HELIOS_ACTION_TOKEN only)
                                              │ propose
                                              ▼
                                         audit sidecar   (HELIOS_EXECUTION_SECRET)
                                              │ forward
                                              ▼
                   tenant_<t>_net (internal) helios-core-<t>  ◀── fin_tools service goes here
                                             helios-redis-<t>  (events, <t>:events channel)
         named volume helios-data-<t>-<app> mounted at /data in app + sidecar
```

---

## 3. The demo, stage by stage

### Stage 0 — cold start **[built]**

Nothing but the repo and Python 3.13. `scripts/bootstrap-venv.sh`, then
`scripts/test-all.sh` → every suite green with per-suite counts.

### Stage 1 — platform up **[built]**

`platform/deployment/bootstrap.sh` → nginx + engine on :8100.
`cli/helios.py doctor` → daemon, base image, nginx, deployed apps.

### Stage 2 — build an app from one sentence **[built]**

The surprise-task path. A stub agent (us, following only `entry-point/SKILL.md`
and `BUILD.md`) runs intake → spec → scaffold → policy → validate → deploy.
The rehearsal blockers are fixed: a fresh scaffold validates clean on arrival, its
generated tests fail rather than skip without a policy, and `BUILD.md` documents the
real deploy path. Cash Planner was built this way (`apps/helios-cash-planner/intake.yaml`).

### Stage 3 — deploy the three flagships **[built]**

`helios deploy` for Collect, Payables and Cash Planner under one tenant, **all three
running at once**. Each returns a URL; each `/health` is 200; each approval queue
renders; `helios apps` lists three app + audit pairs. Observed 2026-10-15 (T3.18):
all three running under tenant `demo`, every page 200, one core and one redis.

### Stage 4 — the control moments **[built]**

The four moments a judge should see, one screen each:

| App | Scenario | What it proves |
|---|---|---|
| Collect | INV-1006 disputed invoice → refused in code, never reaches the queue | fact from the app's own data, not the agent's argument |
| Collect | INV-1009 agent claims 20,000 arrived on a 12,500 invoice → held, declined | over-claim measured against a ceiling the agent cannot move |
| invoice-payer / Payables | INV-1003 / BILL-2002 owner **approves** a payment to a supplier whose bank details just changed → still refused | the control depends neither on the human noticing nor on the agent behaving |
| Payables | BILL-2012 agent lies about `amount_cents` to slip under the threshold → still held | rule binds on the fact `bill_amount_cents`, never the agent's number |
| Platform | duplicate payment proposed twice → second one `held_for_review` by the sidecar; both in `/audit-log` | the platform gate works without the app's cooperation |

### Stage 5 — the apps work together, after each works alone **[built]**

Each flagship demos on its own first (Stages 3 and 4). Working together is an
added feature, shown last, and nothing in it is an app calling another app.

1. **The forecaster inside the gate.** Payables' fact source asks the forecaster
   "if this bill is paid today, what is the projected minimum balance before the
   next payroll?" (`forecast.query(first_shortfall)` over the tenant's known
   inflows and outflows). A rule holds the payment when that projection goes
   below zero, so the owner sees "paying this now creates a shortfall on the
   25th" in the hold. The forecast is a fact the agent cannot state, inside the
   control rather than beside it. Works standalone with the app's own data.
2. **Collect publishes.** Collect agrees a due-date extension on INV-1005 and
   publishes `invoice.extension_agreed` as a **verified** graph node and a
   tenant event through `helios_sdk.publish_event`.
3. **Cash Planner re-forecasts.** It subscribes to the bus, re-runs
   `forecast.project` with the moved inflow, shows the shortfall shifting, and
   cites the LPR/SHIBOR rate with `provenance` and the disclaimer.
4. **Payables sees the shortfall.** Its next run reads the `cashplan` shortfall
   warning from the graph; the forecast fact in step 1 now reflects Collect's
   extension, and a non-urgent bill is held for rescheduling.

### Stage 6 — tools and logs are central **[built]**

- `GET /tools` on the tenant core lists the 38-tool catalog with side-effect flags;
  `/call` requires the bearer secret; `payments.send` without an `auth_code` is
  rejected even with a valid bearer token.
- One platform endpoint returns the action log, audit log and sidecar log of every
  app in the tenant; the hash chain verifies.

### Stage 7 — surprise task **[dropped]**

Not part of this plan. The path exists (Stage 2) and Cash Planner walked it.

---

## 4. The test ladder

Run in this order. A rung is green before the next is attempted. All commands run
from the repo root with `.venv/bin/python` unless stated. **Every rung is scripted**
as `scripts/demo-check.sh <rung>` (T0..T5, or `all`), including the Docker rungs,
so the ladder runs unattended and prints one line per check: id, PASS/FAIL, and the
observed value. "Expect" is what must be
observed, not inferred.

**Last full run, 2026-10-15, `integration` branch, Docker Desktop on this machine:**
T0 5/5 · T1 10/10 · T2 8/8 · T3 17/17 · T4 8/8 · T5 4/4. The Docker run built the
three golden images from this tree, deployed the sample app and all three flagships
under tenant `demo`, and left them running.

### T0 — offline, no Docker, no keys

| # | Command | Expect |
|---|---|---|
| T0.1 | `scripts/bootstrap-venv.sh` | ends with `ok agent_toolkit / audit_agent / agent_integration / fin_tools / engine / app_builder` |
| T0.2 | `scripts/test-all.sh` | `all N suites green`, per-suite counts printed, exit 0. No suite collects zero tests. |
| T0.3 | `.venv/bin/python -m agent_integration.cli selfcheck` | `seam check: OK` |
| T0.4 | `.venv/bin/python -c "from fin_tools import build_world; r=build_world().registry; print(len(r.list()))"` | 40 |
| T0.5 | `.venv/bin/python -m agent_integration.cli demo` | 5 scenarios, `chain verification: OK` |
| T0.6 | `grep -rniE 'glm|zhipu|bigmodel' --exclude-dir=.venv --exclude-dir=.qwen --exclude-dir=OTHER-TEAMMATE-STUFF .` | no hits outside session notes |

### T1 — each app runs its scenarios offline

| # | Command | Expect |
|---|---|---|
| T1.1 | `cd apps/helios-collect && ../../.venv/bin/python app.py` | `{'done': 5, 'refused': 3, 'declined_by_owner': 2}`, 4 emails |
| T1.2 | `cd apps/helios-collect && ../../.venv/bin/python app.py --scenario INV-1006 --trace` | `REFUSED`, rule `app.disputed_invoice`, never queued |
| T1.3 | `cd entry-point/examples/invoice-payer && ../../../.venv/bin/python app.py` | `{'paid': 3, 'refused': 1, 'declined_by_owner': 1}`; INV-1003 refused after owner confirms |
| T1.4 | `cd apps/helios-payables && ../../.venv/bin/python app.py` | `{'done': 9, 'refused': 4, 'declined_by_owner': 2, 'rejected_by_ledger': 1}`; BILL-2002 refused; BILL-2012 held; BILL-2016 held on the forecaster's payroll fact |
| T1.5 | `cd apps/helios-cash-planner && ../../.venv/bin/python app.py` | `{'done': 4, 'refused': 5, 'declined_by_owner': 2}`; WARN-INVENTED refused; WARN-INFLATED held; DRAW held then approved |
| T1.6 | `helios-app validate apps/<each>` | exit 0 for all three flagships and the example |
| T1.7 | `cd apps/<each> && ../../.venv/bin/python -m pytest tests -q` | each `test_controls.py` asserts every rule fires at least once; `test_server.py` asserts `/health` 200 with no Redis, sidecar, key or volume |

### T2 — the builder path with a stub agent (no model)

| # | Command | Expect |
|---|---|---|
| T2.1 | `helios-app questions > /tmp/intake.yaml` | 16 questions, `answer: null` / `assumption: null` each |
| T2.2 | fill intake for the rehearsal sentence; `helios-app spec-from-intake /tmp/intake.yaml > /tmp/helios-app.yaml` | spec parses; unanswered questions are `# TODO`, never a guess |
| T2.3 | `helios-app new /tmp/stub-app --spec /tmp/helios-app.yaml` | 23 files written, second run writes 0 |
| T2.4 | `helios-app validate /tmp/stub-app` **immediately after scaffold** | exit 0. Today it is exit 1 (`mandate.allowed_tools` empty) — p5 blocker 3 |
| T2.5 | scaffold a spec with a tool named `reimbursements_send` | audit manifest marks it `side_effect: true` — p5 defect 4 |
| T2.6 | `(cd /tmp/stub-app && PYTHONPATH=. ../.venv/bin/python -c "import server")` | imports without raising |
| T2.7 | write the policy by hand, re-validate, run `tests/test_controls.py` | `GATED_TOOLS` is populated from the spec; tests fail rather than skip when a control is missing — p5 defect 9 |
| T2.8 | `helios-app validate platform/app-builder/tests/fixtures/broken-app` | exit 1, exactly 8 errors, 8 warnings, each named |

### T3 — Docker on this machine

Preconditions observed 2026-10-15: Docker Desktop is the daemon and is up. Seven
containers from the p2 session are still running (`helios-nginx`,
`helios-core-p2-deploy`, `helios-redis-p2-deploy`, app + audit pairs for
`helios-collect` and `invoice-payer`). `registry-1.docker.io` is unreachable from
this machine; base images were pulled via a mirror and retagged. `.helios-run/`
at the repo root holds tenant secrets and is gitignored.

| # | Command | Expect |
|---|---|---|
| T3.1 | `docker info` | daemon up |
| T3.2 | `cd platform/deployment && ./bootstrap.sh` | idempotent; engine on :8100 (or `HELIOS_ENGINE_PORT`); does not kill a foreign :8100 |
| T3.3 | `curl -s 127.0.0.1:8100/health` | `{"status":"ok","service":"helios-deployment-engine"}` |
| T3.4 | `.venv/bin/python platform/deployment/cli/helios.py doctor` | daemon, platform hash, base image, nginx, apps listed; exit 0 |
| T3.5 | `helios deploy platform/deployment/examples/sample-app --tenant demo` | `{"status":"deployed", ...}`; `curl -i localhost/apps/sample-collect-demo/health` → 200 |
| T3.6 | `helios deploy apps/helios-collect --tenant demo` | 200 on `/`, `/holds`, `/runs`, `/audit`; `style.css` served as `text/css`; links carry the `/apps/<slug>` prefix |
| T3.7 | `helios deploy apps/helios-payables --tenant demo` | same checks; sidecar `/health` reports `manifest.loaded: true` and lists delegated fact-bound rules |
| T3.8 | `helios deploy apps/helios-cash-planner --tenant demo` | `/`, `/holds`, `/health` all 200 |
| T3.9 | redeploy T3.6 after editing a template | old container serves until the new pair passes health; zero 502s during the swap; nginx `default.conf` has exactly one block per slug |
| T3.10 | deploy a copy of Collect with a deliberately failing `/health` | deploy fails at `stage: smoke_test`, response carries `logs` with the traceback; the previous deployment keeps serving |
| T3.11 | deploy a bundle containing a `.env` | CLI refuses, exit 2, before upload |
| T3.12 | deploy a bundle with a hard-coded `sk-...` string in a `.py` | fails at `stage: static_audit`, `problems` names the file |
| T3.13 | `helios.yaml` with an unknown field or an unknown permission | fails at `stage: manifest`, every problem listed at once, each says what to write instead |
| T3.14 | hold → approve on the deployed Collect `/holds` | approve 303 and queue empties; second approve on the same hold → 404; decline executes nothing |
| T3.15 | `docker restart <tenant>-helios-collect-app` then reload `/runs` | logs survive (named volume) and the route survives (apps are routed by container name through Docker DNS, not by IP). Holds do not survive a restart; that phase was dropped |
| T3.16 | `helios undeploy helios-collect --tenant demo` | containers gone, volume `helios-data-demo-helios-collect` still present |
| T3.17 | `docker exec` into the app container: `env | grep -E 'SECRET|TOKEN'` | `HELIOS_ACTION_TOKEN` only; no `HELIOS_EXECUTION_SECRET`, no `TUSHARE_TOKEN` value unless declared under `env:` |
| T3.18 | **All three flagships deployed together**: deploy Collect, Payables, Cash Planner under tenant `demo`; `helios apps` | three app + audit pairs running; every `/`, `/holds`, `/health` returns 200; one `helios-core-demo` and one `helios-redis-demo` shared by all three; nginx `default.conf` has exactly three marker blocks |

### T4 — tools and logs central **[built]**

| # | Command | Expect |
|---|---|---|
| T4.1 | from the audit sidecar: `GET http://helios-core-demo:9000/health` | `{status, role, tool_count}` with no auth |
| T4.2 | `GET .../tools` without bearer | 401; with bearer → the full catalog with `side_effects` flags |
| T4.3 | `POST .../call {"tool":"payments.send", ...}` with bearer but no `auth_code` | the tool runs and the ledger refuses: `result.state == rejected`, `missing verified intent auth code`. A bearer token authenticates the caller; it does not authorise the payment. Through the sidecar the same call is recorded as a **denial**, never as executed |
| T4.4 | the same with a confirmed intent's `auth_code` and integer `amount_cents` | `settled`; ledger moves; `world_state.sqlite3` on the volume changes |
| T4.5 | restart `helios-core-demo` | ledger and verified graph nodes survive |
| T4.6 | `GET 127.0.0.1:8100/api/v1/tenants/demo/logs` | action, audit and sidecar logs for every app in the tenant, grouped by app; each hash chain verifies |
| T4.7 | app container `curl helios-core-demo:9000/tools` | refused or unreachable: only the sidecar may reach core |

### T5 — cross-app **[built]**

| # | Command | Expect |
|---|---|---|
| T5.0 | Payables alone: pay a bill whose projected minimum balance before payroll goes negative | held, owner question names the shortfall date; the same bill with a smaller amount pays |
| T5.1 | run Collect INV-1005 (extension agreed) on the deployed app | a **verified** `invoice` node in the tenant graph and an event on `demo:events` |
| T5.2 | `GET localhost/apps/helios-cash-planner-demo/` | forecast shifted by the new due date; the event is listed; rate shown with `provenance.mode` and `stale` |
| T5.3 | Payables run with the shortfall warning present | a reschedule proposal held for the owner, citing the warning |
| T5.4 | `grep -rn 'helios-collect\|helios-payables\|helios-cash-planner' apps/*/server.py apps/*/app.py apps/*/tools.py` | no app names another app's URL or container. Convention, not a network control: every cross-app read goes through the graph or the bus |

---

## 5. Gaps that block the flow, in dependency order

| # | Gap | Status |
|---|---|---|
| G1 | Merge conflicts across the six worktrees | **done** — `integration` branch, 14 suites green |
| G2 | Scaffolder defects from the rehearsal | **done** — stated side-effect flags, end-aware verb guess, populated mandate and gated tests, BUILD.md corrected, validator warns on the import-time `parents[N]` trap |
| G3 | Mock core replaced by the `fin_tools` service; sidecar forwards named tools to `/call` | **done** for the platform; the three flagships still execute `pay_bill` / `send_email` in-process inside their container. Routing those through `helios_sdk.propose_action` is the remaining step |
| G4 | Central evidence endpoint | **done** — `GET /api/v1/tenants/<t>/logs`, `helios evidence` |
| G5 | Cash Planner | **done** — built through the builder, 11 scenarios, 41 tests, deployed |
| G6 | Collect and Payables publish on the bus | **done** — `invoice.extension_agreed`, `invoice.payment_applied`, `bill.rescheduled`, `bill.paid`; the flagships' graph writes stay in-process (see G3) |
| G7 | Holds persisted across a container restart | **dropped** |
| G8 | `helios` console script; `fastapi`/`uvicorn` pinned | pins done; the CLI is still invoked as `python platform/deployment/cli/helios.py` |
| G9 | Base images pulled through a mirror | **open** — verify digests before a judged demo |
| G10 | Payables declares a read of `cashplan[shortfall_warning]` and does not act on it; its payroll fact comes from its own book | **open** |

---

## 6. The two market-data implementations

Both live at the import path `fin_tools.market_data`, so a textual merge cannot
keep both.

| | `chloe-dev` (`market_data.py`, one module) | `p1-tools` (`market_data/`, package) |
|---|---|---|
| Tools | `market.reference_rate`, `market.daily_quotes`, `market.trading_calendar`, `market.company_basic` | `market.rate`, `market.borrowing_cost_compare`, `market.company_lookup`, `market.collection_period` |
| Shape | thin, read-only Tushare reads; raw data back | app-facing answers: cost comparison with formula + `cheapest`, registry evidence, collection-period prior |
| Side effects | all `side_effects=False` | `market.rate` is **gated** because it publishes a verified graph node `market_rate:<series>@<date>` so an app can cite an attested rate |
| Sources | live only; no token → `ServiceUnavailable` | `HELIOS_MARKET_MODE=fixture\|live\|auto`; Tushare → core files → committed fixtures; 40/min token bucket; retries |
| Provenance | `{publisher, api_name, as_of, ...}` | adds `mode` and `stale`, plus a disclaimer on cost comparisons |
| Allow-list | 5 endpoints | 6 (adds `fina_indicator`, `index_member_all`, `stock_company`) |
| Tests | 43, injected transport | included in financial-tools 566 |
| Quote-is-not-money invariants | source-grep test for forbidden imports and `_cents` fields | same intent, not enforced by a grep test |

Recommendation: p1's package wins, because the demo is offline-first (fixtures) and
Cash Planner needs `borrowing_cost_compare` and `collection_period`. Fold in from
chloe-dev the two tools p1 lacks (`daily_quotes`, `trading_calendar`) and the
forbidden-import / `_cents` grep test. Payables' `funding.py` then migrates to
`market.rate` and `market.company_lookup`.

---

## 7. Open questions

None blocking. The sample app proposes named tools with integer cents: `email.send`
executes through core; `payments.send` without an intent code is recorded by the
sidecar as a denial carrying the ledger's reason.

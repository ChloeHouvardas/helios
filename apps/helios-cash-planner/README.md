# Helios Cash Planner

Flagship 3: the AI cash-flow agent on Helios. It forecasts the next thirteen
weeks of cash from how customers *actually* pay, warns the owner weeks ahead of
a shortfall, recommends one action per short week — chase a customer, move a
supplier bill, or draw on the credit line — with the public reference rate
cited, and answers what-if questions.

Built through the builder: `helios-app questions` → `spec-from-intake` →
`helios-app new` (the intake is in `intake.yaml`), then the policy, the facts,
the tools and the pages were written by hand. The generated SQLite store was
replaced by the JSON book the other flagships use.

## Run

```bash
cd apps/helios-cash-planner
../../.venv/bin/python app.py                 # the forecast, then eleven scenarios
../../.venv/bin/python app.py --forecast      # the projection and stop
../../.venv/bin/python app.py --scenario WARN-INVENTED --trace
../../.venv/bin/python -m pytest tests -q     # 41 tests, offline
../../.venv/bin/python -m uvicorn server:app --port 8000
```

Market data is pinned to fixture mode in the tests (`HELIOS_MARKET_MODE`);
with a `TUSHARE_TOKEN` the rate goes live and the pages say so.

## The idea: the forecast is a fact

The auditor never reads the agent's arguments for a value a rule depends on.
Here the values that matter are the projection: whether a week is short, by
how much, what the facility has left. `facts.py` hands those to the auditor
from `book.py`, which computes them with the platform's forecast engine. The
agent may **name** a week; it never gets to **state** what that week looks like.

| Scenario | Verdict | Rule | What it shows |
|---|---|---|---|
| WARN-SHORT | done | — | The projected short week is warned about, as a verified graph node citing the rate |
| WARN-INVENTED | **refused** | `app.warning_only_when_short` | **DEMO 1** — a shortfall claimed for a fine week. Refused in code; the owner is never asked |
| WARN-INFLATED | declined_by_owner | `app.warning_matches_projection` | **DEMO 2** — a real short week, the gap inflated 15×. Held against the projection |
| FOLLOW-UP | done | — | Chase the late payer whose timing causes the gap; an observation for Collect |
| FOLLOW-UP-DISPUTE | **refused** | `app.no_follow_up_in_dispute` | The owner paused collections; the agent never sees the flag |
| RESCHEDULE | done | — | A supplier bill may move; an observation for Payables |
| RESCHEDULE-PAYROLL | **refused** | `app.payroll_is_never_moved` | Payroll is never moved, whatever the reason says |
| DRAW | done | `app.credit_draw_needs_owner` | **DEMO 3** — borrowing is the owner's call, shown with the rate, date and provenance |
| DRAW-DECLINED | declined_by_owner | `app.credit_draw_needs_owner` | Same control, other direction |
| DRAW-OVER-FACILITY | **refused** | `app.credit_draw_within_facility` | Above the headroom there is nothing to approve |
| DIGEST-TWICE | **refused** | `app.one_digest_per_week` | The second digest in a week is refused; the first went out |

Expected totals: `{'done': 4, 'refused': 5, 'declined_by_owner': 2}`.

## Standalone first, together second

Everything above runs with the app's own fixtures. When Collect and Payables
share the tenant, their verified `invoice` / `bill` graph nodes and tenant
events (`invoice.extension_agreed`, `bill.rescheduled`, ...) replace the
fixture dates: `POST /events` accepts one, the bus delivers the same shape,
and the forecast re-projects. The app publishes `cashplan:<week>` as a
verified node and its recommendations as observations.

## Pages

`/` position and the thirteen weeks · `/forecast` the inflows timed by
behaviour and the outflows · `/scenarios` what-if · `/funding` options priced
at the public rate with provenance and the disclaimer · `/holds` the approval
queue, the decisions, and what was refused without asking · `/settings` the
thresholds and every rule.

`GET /health` is constant and builds nothing, which is what the deploy smoke
test needs. Deploy with
`.venv/bin/python platform/deployment/cli/helios.py deploy apps/helios-cash-planner --tenant <t>`.

## Known limitations

- Recommendations are recorded, not executed: Collect chases, Payables moves
  bills, the owner draws. By design.
- Customers with no payment history get the global prior; the forecast page
  says so (`evidence: default`).
- Holds are in-memory; a container restart loses pending approvals (platform
  gap, see `docs/developer-context/DEMOFLOW.md` G7).

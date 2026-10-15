# Helios Payables

The AI accounts-payable agent: flagship app 2, built **through** the Helios platform.

Collect supplier bills, make sure funds are available, pay on time — and hold
duplicates, changed payee details and unusual amounts for the owner. The agent
proposes; code decides.

```bash
cd apps/helios-payables

# The demo: 15 scenarios, one control path each, offline, no API key
../../.venv/bin/python app.py
../../.venv/bin/python app.py --scenario BILL-2002 --trace
../../.venv/bin/python app.py --list

# The web UI (five declared pages, Jinja2 over FastAPI)
../../.venv/bin/python -m uvicorn server:app --port 8000

# The controls, as tests
../../.venv/bin/python -m pytest tests -q          # 70 passed

# The app spec, validated
../../.venv/bin/python ../../entry-point/checks/check_app.py .   # 0 errors
```

---

## The two demos to show a judge

**1. Changed payee refused despite owner approval** — `--scenario BILL-2002`

Meridian Logistics changed bank details 5 days ago. The change request came from
`meridian-finance.net`, signed by Zhang Lu, naming account holder "Meridian Finance
Consulting". The registry on file says `meridian-logistics.cn`, legal representative
Li Wei. The scripted owner **approves everything it is shown** — and the payment still
does not happen, because `app.payee_details_changed` **denies**, and a denial never
becomes a queue item. The control does not depend on the human noticing, and it does
not depend on the agent behaving.

Then run `--scenario BILL-2003`: Kestrel changed details 45 days ago, outside the
30-day window, and pays normally. Without it the rule looks like a blanket refusal of
anyone who ever edited a bank account — which is a control nobody could live with, and
therefore one somebody would switch off.

**2. The lying agent** — `--scenario BILL-2012`

A bill recorded at 30,000.00. The agent claims `amount_cents: 900000` to slip under
the 25,000.00 approval threshold. It is **still held**, because the rule binds on the
FACT `bill_amount_cents` and never reads the agent's number. And the amount that
executes is the recorded 30,000.00, because `pay_bill` pays from the book. This is
"facts are not claims" made observable rather than asserted.

---

## Scenario table

Observed output of `app.py --json`, not the intended output. Every verdict matches its
expectation, and `tests/test_controls.py` asserts each one.

| Bill | Task | Verdict | Rule(s) that fired | What it demonstrates |
|---|---|---|---|---|
| BILL-2001 | pay_bills | **done** | — | The normal path: pays and settles with no owner involvement |
| BILL-2002 | pay_bills | **refused** | `app.payee_details_changed` | **DEMO 1** — denied despite an owner who approves everything |
| BILL-2003 | pay_bills | **done** | — | The 30-day window ends; a 45-day-old change is payable |
| BILL-2004 | pay_bills | **refused** | `app.supplier_contact_frequency` | Third email in 7 days, counted from trusted history |
| BILL-2006 | pay_bills | **declined_by_owner** | `app.duplicate_bill` | Held, not denied: only the owner can tell |
| BILL-2007 | pay_bills | **done** | `app.unusual_amount` | Held against a ceiling from the supplier's own history |
| BILL-2008 | pay_bills | **refused** | `app.insufficient_funds` | Payment denied; the reschedule that follows is allowed |
| BILL-2009 | pay_bills | **refused** | `app.currency_unsupported` | USD bill, CNY account, no FX desk |
| BILL-2010 | pay_bills | **done** | `app.large_payment_approval` | Held above 25,000.00, approved, exact amount paid |
| BILL-2011 | pay_bills | **declined_by_owner** | `app.large_payment_approval` | Same control, other direction: nothing is paid |
| BILL-2012 | pay_bills | **done** | `app.large_payment_approval` | **DEMO 2** — the lying agent is still held |
| BILL-2013 | pay_bills | **done** | — | Pays, then emails remittance advice (also gated) |
| BILL-2014 | pay_bills | **done** | `app.large_payment_approval` | Funding: draw under its own gate, then pay from real money |
| BILL-2015 | pay_bills | **rejected_by_ledger** | — | The gate allowed it; the ledger refused it. Two layers |
| BILL-2001 | pay_bills | **done** | `app.run_total_cap` | Three clean 22,000.00 bills total 66,000.00 > the 60,000.00 cap |

`rejected_by_ledger` is its own status on purpose. It means the **gate allowed the call
and the ledger refused it** — a credential problem, not a policy one. Conflating the
two would hide the fact that they check different things.

---

## The policy: 17 rules in one YAML file

`agents/helios-payables-audit.yaml` is the audit agent. There is no Python policy in
this app. Every control is data the auditor compiles.

| Rule | Verdict | Binds on | Why deny vs hold |
|---|---|---|---|
| `app.requires_verified_intent` | deny | argument | No owner-confirmed credential, no payment |
| `app.amount_matches_bill` | hold | argument vs **fact** ceiling | Overpayment against the recorded amount |
| `app.large_payment_approval` | hold | **fact** | A large payment is a human decision |
| `app.run_total_cap` | hold | **fact** vs **fact** | Stops splitting a bill to dodge the threshold |
| `app.payee_details_changed` | **deny** | **fact** | No approval makes fraud safe |
| `app.insufficient_funds` | **deny** | **fact** | No approval creates money |
| `app.currency_unsupported` | **deny** | **fact** | No FX desk; nothing to approve |
| `app.no_unlisted_currency` | **deny** | argument | Redundant on purpose — catches the explicit wrong currency |
| `app.duplicate_bill` | hold | **fact** | Two genuine identical bills do exist |
| `app.unusual_amount` | hold | argument vs **fact** ceiling | "Normal for them" is not the agent's call |
| `app.credit_line_draw_cap` | **deny** | argument vs **fact** headroom | Cannot borrow money that does not exist |
| `app.credit_line_draw_approval` | hold | argument | Borrowing is a financing decision |
| `app.credit_line_draw_reason` | deny | argument | Unexplained borrowing is not auditable |
| `app.reschedule_reason` | deny | argument | An unexplained date change hides a bill |
| `app.supplier_contact_frequency` | **deny** | trusted history | A queue item cannot make hounding appropriate |
| `app.remittance_is_identifiable` | deny | argument | A notification must say who and why |
| `app.escalation_is_identifiable` | deny | argument | An escalation must state the exception |

**Facts are not claims.** Every bolded **fact** is resolved by `facts.py` from the
app's own book. The agent gets to *name* a bill; it never gets to *state* what that
bill says. `resolved_facts` values are deliberately never merged into the facts the
auditor uses, so poisoning that dict changes nothing — `test_poisoned_resolved_facts_change_nothing`
proves it directly.

**Three controls are structural, not rules**, and they are the strongest ones:

- `pay_bill` has **no `payer` and no `payee` argument**. Money cannot be misdirected
  because there is nothing in the request to misdirect it with.
- The amount that **executes** is the recorded amount, never the requested one.
- `escalate_to_owner` has **no recipient argument**, so it cannot contact a supplier.
  That is also why it is exempt from the contact limit: an agent that cannot escalate
  is an agent that improvises around a refusal.

---

## Three human checkpoints, and they are not the same thing

1. **Intent confirmation** — *before* a payment is proposed. `mint_auth_code()` models
   the owner confirming out of band. The code is an HMAC bound to
   (amount, currency, payee) and is single-use. If the agent could mint its own code
   it would be self-authorising, so this belongs to the owner.
2. **Hold resolution** — *after* the auditor holds a proposal. The owner sees the
   auditor's own `owner_question`, never a summary the agent wrote about itself.
3. **Supplier re-verification** — on the settings page. This is the **only** thing that
   lifts a `app.payee_details_changed` denial. Not an owner approving a hold: that rule
   denies, and denials never reach the queue.

Re-verification does not delete the change request. The evidence stays on record and
still shows on the bill's detail page.

---

## The five pages

`server.py` serves them for real — Jinja2 over FastAPI, not CLI renderers.

| Page | Pattern | Route |
|---|---|---|
| dashboard | summary | `/` |
| bills | table | `/bills` |
| bill | detail | `/bills/{bill_id}` |
| **holds** | **approval_queue** | `/holds` |
| settings | settings | `/settings` |

The **holds** page has two sections and the second is the point: *Needs your approval*
and *Refused in code, without asking you*. The refused section is what shows controls
firing with no human in the loop at all.

`GET /health` is **constant**: it never builds the app. That is a deployment
requirement, not a stylistic one — the engine's smoke test runs the container on an
isolated network with no Redis, no audit sidecar, no model key and no data volume, and
an app built at import time or inside `/health` fails that check. The honest readiness
probe lives at `GET /diagnostics`, which does build the app and reports rules loaded,
tools declared, bills and suppliers.

The web owner is a `ScriptedOwner` whose stance you choose (approve everything / decline
everything), because resuming a held action needs the live runtime, which `app.py
--interactive` provides. The two stances are what demonstrate the control: approve
everything and BILL-2002 is still refused.

---

## What this app does NOT do, honestly

**`market.*` does not exist in this checkout.** The plan called for `market.rate`,
`market.borrowing_cost_compare` and `market.registry_match`. `fin_tools` registers
exactly twelve tools — `email.*`, `graph.*`, `payments.*`, `simulation.forecast`,
`spreadsheet.*` — and there is no `market` package anywhere in the repo. So
`funding.py` is a **local stand-in**, per the plan's own instruction to build the
smallest version needed inside the app:

- **No network.** Rates are a pinned table (`funding.RATES`, as of 2026-09-20). There
  is no `TUSHARE_TOKEN` in the environment and no fixture-backed core file to read, so
  pretending to fetch would be pretending.
- **Every result carries provenance** — `mode: offline-fixture`, the `as_of` date, and
  `stale: True` whenever that date is not today. A rate the app cannot date is worse
  than no rate.
- **It never decides anything.** Ranking an option does not authorise it, and every
  result carries the "not financial advice" disclaimer (which is also the licence
  position: no unlicensed investment advice).
- `funding.py` is quarantined so the swap is small, but it is **partial**, not a
  delete. `market_data.py` now exists on `chloe-dev` (`7ff4b74`) and its
  `RATE_SERIES` already pins `lpr_1y` and `shibor_3m`, so `funding.rate()` is
  replaceable by `market.reference_rate`. But there is no upstream
  `borrowing_cost_compare`, so the option ranking stays; and `market.company_basic`
  is a company *lookup*, not a change-request comparison, so `registry_evidence`
  stays too. Two traps for whoever does the swap: `reference_rate` returns
  `value_pct` as a **float** (convert with `Decimal(str(...))`, never
  `Decimal(float)`), and there is **no `stale` field** upstream — derive it from
  `as_of` vs `requested_as_of`. Full detail in `worktree.md` §4.

**Not deployed.** `helios deploy` was never run here: the engine did not exist at this
branch's base commit, the Docker daemon is not running and `HELIOS_ENGINE_URL` is
unset. The engine *does* now exist in the `p2-deploy` worktree, so the app was made
deploy-ready and verified against the engine's real contract rather than deployed.
`helios.yaml` passes the engine's `load_manifest()` and `lint()` with 0 problems, and
`GET /health` passes in ~1s under a simulated smoke-test environment with no Redis, no
audit sidecar, no model key and no data volume. Every dependency this app needs is
already in the golden base image, and the app layer is a single `COPY` with no pip
install. All of it is asserted by `tests/test_server.py`.

Three gaps remain before this deploys for real, in rough order of size: nothing reads
`HELIOS_DATA_DIR` yet (run logs land in `runs/`); holds are in-memory only, so pending
approvals do not survive a restart even though the named volume is mounted; and the app
calls `bundle.payments` / `.email` / `.graph` **in-process** rather than proposing through
`helios_sdk.propose_action`, so the sidecar that holds `HELIOS_EXECUTION_SECRET` is not
yet the second gate. See `worktree.md` §1.

**Money is simulated end to end.** No real payment rails. The `fin_tools` ledger would
happily drive a balance negative — it has no overdraft check — so
`app.insufficient_funds` is this app's policy, resolved from the live balance, and it is
the only thing standing between the agent and an overdraft.

**Holds are in-memory only.** `AuditAgent` keeps holds in a dict, so pending approvals
are lost on restart (HANDOFF trap 12).

**No scheduling.** Agents run manually. There is no morning trigger; `--scenario` and
the dashboard's run button are the triggers.

**`stacks.yaml` does not exist**, so `stack.use: helios-python-web-1` resolves to
nothing. That is "the mechanism, not the choice", but it must not be presented as a
stack the platform ships.

**Two private-attribute seams.** `Book._mutate_bill` and `_mutate_supplier` write into
the platform store's private dicts, because `Store` was built for receivables and
exposes no bill or supplier mutator. Each is confined to one method so there is a single
place to change if the platform grows a public mutator.

---

## Known platform gaps this app works around

| Gap | Where | What this app does |
|---|---|---|
| `auth_code` is not in `REDACTED_KEYS`, so a live credential reaches the owner prompt and both logs in full | `audit_agent/contracts.py:54` (HANDOFF trap 3) | `redact_text()` masks 16+ hex runs **by shape**, because the auditor has already rendered the prose and the key names are gone. Tested: no credential in stdout or in the action log |
| `_subject_key()` does not look at `bill_id` | `policy.py:1192` | `send_remittance` **requires** `supplier_id`, or the frequency rule finds no subject and denies everything |
| The runtime records a contact only for a tool named exactly `send_email` | `runtime.py:611` | `send_remittance` calls `book.record_supplier_contact()` itself |
| `build_world()` binds module-level singletons; the last world wins for every registry dispatch | `payments.py:158` | Handlers call `bundle.payments` / `bundle.email` / `bundle.graph` directly |
| Rules cannot express a two-argument pairing check | HANDOFF trap 7 | `send_remittance`'s handler refuses a supplier/bill mismatch. Fails loudly and safely; nothing is sent |
| `jsonschema` is optional at runtime, and its absence denies **every** side effect | `policy.py:416` | Pinned in `requirements.txt`. Worth knowing: the failure mode is fail-closed, so a missing dependency reads as "policy is strict" rather than "the app is broken" |

---

## Layout

```
apps/helios-payables/
├── README.md                          # this file
├── helios-app.yaml                    # the app-builder spec — check_app.py: 0 errors
├── helios.yaml                        # DEPLOYMENT manifest — passes the engine validator
├── config.py                          # thresholds mirrored from the manifest
├── book.py                            # the data layer over the platform store
├── facts.py                           # THE fact source — "facts are not claims"
├── funding.py                         # local stand-in for the missing market.* family
├── tools.py                           # the 10 tools, 5 read-only + 5 gated
├── app.py                             # composition, scenarios, CLI pages
├── server.py                          # FastAPI + Jinja2 web UI; lazy build, constant /health
├── agents/
│   ├── helios-payables-audit.yaml     # THE POLICY — 17 rules, written first
│   └── helios-payables-action.json    # the agent's operating contract
├── data/{suppliers,bills,accounts}.json
├── templates/*.html                   # the five declared pages
└── tests/
    ├── test_controls.py               # 60 tests, one per control
    └── test_server.py                 # 10 tests, the deployment contract
```

## Requirements

Pinned in `requirements.txt`. The venv at the repo root is `.venv` (Python 3.13.9).
Everything runs offline with **no API key**: `tests/conftest.py` blocks non-loopback
sockets and deletes `DASHSCOPE_API_KEY` / `HELIOS_LLM_API_KEY` for the whole suite, so
no test can depend on a vendor endpoint or on whose shell it ran in.

## Related

- `apps/helios-collect` — flagship 1, the composition pattern this app follows
- `entry-point/examples/invoice-payer` — the payables policy shape, proven as an example
- `entry-point/BUILD.md` — the procedure this app was built by
- `HANDOFF.md` — what exists, what does not, and the traps

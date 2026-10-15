# Worktree `p4-payables` — what this branch contains

**Branch:** `worktree-p4-payables` · **Base:** `cc96f0a` · **Commits:** local only, none pushed
**Working tree:** clean

**Paths touched:** `apps/helios-payables/**` and `docs/sessions/p4-payables/**` — nothing else.
Verified with `git diff --name-only cc96f0a..HEAD`: every added file is new, and **no existing
file was modified**. No platform package, no spec, no shared doc, no other app.

---

## 1. Deployment status: ready to deploy, not yet deployed

`helios deploy` was **not run in this worktree** — the deployment engine did not exist at
this branch's base commit, there is no Docker daemon running here, and `HELIOS_ENGINE_URL`
is unset. So the app was made deploy-ready and verified against the engine's real contract
by reading it, rather than deployed.

**What is in place for deployment:**

| Requirement | Status |
|---|---|
| `helios.yaml` at the bundle root | Present. **Passes the engine's real `load_manifest()` and `lint()`** (validated cross-worktree against `p2-deploy`, 0 problems) |
| `entry: server:app` → a FastAPI object | Present, `server.py` |
| `GET /health` returns 200 **without** Redis, audit sidecar, model key or data volume | **Verified.** Passes in 1s under a simulated smoke-test environment with all four absent |
| Golden-base-image dependencies | **All satisfied.** `fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `pydantic`, `pyyaml`, `jsonschema` are all pre-installed in `images/app-base/Dockerfile`. The app layer is a single `COPY` with **no pip install**, so this is what makes it deployable |
| Audit manifest copied into the sidecar | `audit_manifest: agents/helios-payables-audit.yaml` |
| `data_dir` absolute | `/data` |

**Two deployment blockers were found and fixed in this worktree** — both would have failed
the `smoke_test` stage:

1. **`server.py` built the app at import time**, and `/health` read that app. The engine's
   smoke test runs the container on an isolated network with no Redis, no sidecar, no key and
   no data volume (`engine/orchestrator.py`), so a `/health` that needs any of them makes the
   container never pass and the deploy fail. Fixed: the app is now built **lazily** on first
   use of a real page, and `/health` returns a constant.
2. **`helios.yaml` was written against a guessed schema** (`apiVersion`, `kind`, `metadata`,
   `spec`, `routes`, `healthCheck`). The engine **rejects unknown fields** — "a misspelled
   field is silently ignored by a lenient parser, so it is rejected here" — so that manifest
   would have failed at the `manifest` stage before any build. Rewritten to the real
   `Manifest` schema.

**Outstanding deployment work**, if another agent takes this app to production:

- **Nothing reads `HELIOS_DATA_DIR` yet.** The app writes run logs to
  `apps/helios-payables/runs/` and the auditor's hash-chained log alongside them. To survive a
  redeploy, `runs_dir` and `data_dir` in `build_app()` need to come from `HELIOS_DATA_DIR`.
- **Holds are in-memory only** (`AuditAgent` keeps them in a dict, `InMemoryReplayGuard` is
  single-process — HANDOFF trap 12), so pending approvals and grant-replay protection are lost
  on restart. The named volume is mounted but the app does not yet persist to it.
- **No `helios_sdk` usage.** The app calls `bundle.payments` / `.email` / `.graph` in-process
  rather than proposing through the sidecar. Under the deployment contract, side effects should
  route via `helios_sdk.propose_action(...)` so the sidecar — which holds
  `HELIOS_EXECUTION_SECRET` — is the second gate. This is the largest single gap between how
  the app runs standalone and how it would run deployed.
- **No `events` permission and no Redis publishing.** `helios-app.yaml` declares it reads
  `cashplan[shortfall_warning]` and publishes `bill[...]`, but nothing subscribes or publishes
  over the tenant Redis channel. Cross-app state currently goes through the in-process
  `KnowledgeGraph` prototype only.

`tests/test_server.py` guards the two fixed blockers with regression tests, including one
that validates the manifest against the **real engine** when the `p2-deploy` worktree is
present (skipped otherwise, so the suite still runs from a merged checkout).

---

## 2. What was built

`apps/helios-payables` — flagship app 2, the AI accounts-payable agent, built **through** the
platform by following `entry-point/BUILD.md`. It composes `agent_integration`, `audit_agent`,
`agent_toolkit` and `fin_tools`, and edits none of them.

| | |
|---|---|
| Audit rules | **17**, all declarative YAML, no Python policy |
| Tools | **10** — 5 read-only, 5 gated |
| Scenarios | **15**, one control path each, all verdicts match expectations |
| Tests | **70 passed** (60 controls + 10 deployment contract), offline, no API key, no network |
| Fixtures | 8 suppliers, 15 bills, one CNY account at 180,000.00 |
| Web UI | 5 pages, Jinja2 over FastAPI, plus `GET /health` and `GET /diagnostics` |
| `check_app.py` | **0 errors**, 1 warning |

### Layout

```
apps/helios-payables/
├── README.md                       # scenario table from observed output, gaps, workarounds
├── helios-app.yaml                 # the app-builder spec — passes check_app.py
├── helios.yaml                     # the DEPLOYMENT manifest — passes the engine validator
├── requirements.txt                # all deps pinned, with a note on why jsonschema matters
├── config.py                       # thresholds mirrored from the audit manifest
├── book.py                         # data layer over agent_toolkit.store.Store
├── facts.py                        # the fact source — "facts are not claims"
├── funding.py                      # local stand-in for the absent market.* family (§4)
├── tools.py                        # the 10 tools
├── app.py                          # composition, 15 scenarios, CLI pages
├── server.py                       # FastAPI app; lazy build; constant /health
├── agents/helios-payables-audit.yaml    # THE POLICY, written first
├── agents/helios-payables-action.json   # the agent's operating contract
├── data/{suppliers,bills,accounts}.json
├── templates/{base,dashboard,bills,bill,holds,settings}.html
└── tests/{conftest,test_controls,test_server}.py
```

Note both `helios-app.yaml` and `helios.yaml` exist and are **different schemas** — the first
describes the agents, the second is what the engine reads. The engine's own `contracts.py`
says so explicitly; neither replaces the other.

### Functionality

The agent lists bills due in the next 7 days, checks funds, pays what is safe, reschedules what
is not, emails remittance advice, and escalates exceptions. Controls: changed payee details
(deny), insufficient funds (deny), unsupported currency (deny), duplicate bill (hold), unusual
amount vs supplier history (hold), single payment over 25,000.00 (hold), run total over
60,000.00 (hold), contact frequency (deny), and three rules governing credit-line draws.

Three controls are **structural rather than rules**: `pay_bill` takes no `payer`/`payee`
argument, so money cannot be misdirected; the amount that executes is the *recorded* amount,
never the requested one; `escalate_to_owner` takes no recipient argument, so it cannot contact
a supplier.

### The two demos

- **`--scenario BILL-2002`** — Meridian changed bank details 5 days ago and the change request
  contradicts the registry on domain, signatory and account holder. The scripted owner
  **approves everything it is shown** and the payment still does not happen, because the rule
  *denies* and a denial never reaches the queue. Paired with `BILL-2003`, whose change is 45
  days old and therefore pays — which is what proves the 30-day window is real rather than a
  blanket refusal.
- **`--scenario BILL-2012`** — the agent claims `amount_cents: 900000` on a bill recorded at
  30,000.00 to slip under the threshold. It is **still held**, because the rule binds on the
  fact `bill_amount_cents` and never reads the agent's number.

---

## 3. Dependencies this branch adds

`requirements.txt` pins everything. **New third-party additions, with why:**

| Package | Version | Why | In the golden base image? |
|---|---|---|---|
| `fastapi` | 0.143.1 | Web UI | yes (`>=0.110`) |
| `uvicorn` | 0.40.0 | ASGI server | yes (`>=0.27`) |
| `jinja2` | 3.1.6 | Server-rendered templates | yes (`>=3.1`) |
| `python-multipart` | 0.0.20 | Form POSTs — the settings re-verification and run buttons | yes (`>=0.0.9`) |
| `httpx` | 0.28.1 | Test client | yes (`>=0.27`) |

**Every dependency this app needs is already in `images/app-base/Dockerfile`**, which matters
because the app image layer is a single `COPY` with no pip install. Nothing needs adding to the
base image to deploy this app.

Already required by the platform packages, pinned here for the local venv:
`jsonschema==4.25.1`, `pydantic==2.12.4`, `PyYAML==6.0.3`, `pytest==8.4.2`.

**`jsonschema` is the one that matters.** `audit_agent` imports it lazily inside a try/except
(`policy.py:416`) and, when absent, **denies every side effect** with "argument schema
validation is unavailable". Correct fail-closed behaviour, but it means a missing dependency
presents as "the policy is strict" rather than "the app is broken" — every payment refused, no
traceback. It is in the base image, so this cannot happen deployed; it bit locally.

### Environment

A venv was created at the **worktree root** (`.venv/`, Python 3.13.9). It is gitignored
(`.gitignore:4`) and **not committed**, so it will not travel with a merge. The app writes
`apps/helios-payables/runs/` at runtime, ignored by its own `.gitignore`.

README commands use `../../.venv/bin/python`. Against system `python3` (3.13.9, which has
pydantic/jinja2/httpx but **not** fastapi or jsonschema) the CLI and tests still run; only
`server.py` needs the extra packages.

---

## 4. `funding.py` — a local stand-in, and the migration path

At this branch's base commit `cc96f0a`, `fin_tools` registered exactly **12 tools**
(`email.*`, `graph.*`, `payments.*`, `simulation.forecast`, `spreadsheet.*`). There was no
`market.*` family, so `funding.py` was written as the smallest local version needed.

**That has since changed on `chloe-dev`.** Commit `7ff4b74` ("feat: live Tushare market data
behind an allow-list") adds `platform/financial-tools/fin_tools/market_data.py`. It is *not* in
this worktree. After a merge, this section becomes live.

What `market_data.py` actually registers — **four** tools:

| Real, on `chloe-dev` | This app's equivalent | Migration |
|---|---|---|
| `market.reference_rate` | `funding.rate()` | **Replaceable.** Its `RATE_SERIES` already contains `lpr_1y` and `shibor_3m` — the exact two series `funding.RATES` pins |
| `market.daily_quotes` | — | Not needed |
| `market.trading_calendar` | — | Not needed |
| `market.company_basic` | `funding.registry_evidence()` | **Not equivalent.** Company lookup ≠ comparing a change request's domain/signatory/account-holder against a registry record |
| *(absent)* | `funding.compare_options()` | **No upstream equivalent.** The borrowing-option ranking is app logic and must stay |

So the migration is **partial**, not "delete one file".

**Two traps for whoever does the swap:**

1. `reference_rate` returns **`value_pct` as a `float`**. This app's money path never touches a
   float, and `funding.interest_cost_cents()` takes a `Decimal`. Convert via
   `Decimal(str(value_pct))`, never `Decimal(value_pct)` — the latter bakes binary rounding into
   an interest calculation.
2. There is **no `stale` field** upstream. It returns `as_of` (when the rate was actually
   published) and `requested_as_of`. `funding.Rate.stale` is a computed property comparing
   `as_of` to today; a replacement must derive staleness from those two fields or the UI loses
   its "stale quote" warning. The upstream `provenance` dict is richer than this app's
   (`publisher`, `api_name`, `rows_returned`, `retrieved_at`, `source_url`, `mode: "live"`) and
   can be passed through unchanged.

**Import surface**, fully enumerated so a replacement can be checked against it:

| Consumer | Names used |
|---|---|
| `tools.py` | `compare_options`, `rate`, `interest_cost_cents`, `bps_to_percent` |
| `app.py` | `registry_evidence`, `rate`, `DISCLAIMER` |
| `tests/test_controls.py` | `registry_evidence`, `rate`, `AS_OF`, `interest_cost_cents` |
| `server.py` | none (reaches funding only through `app.py` and `tools.py`) |

`bps_to_percent` was made public deliberately: `tools.py` originally reached into
`funding._bps_to_percent`, and a private name at a cross-module call site is invisible to
whoever replaces this file.

**Behaviour a replacement must preserve:**

- Every result carries `provenance` with `mode`, `as_of` and `stale`, and every result carries
  `DISCLAIMER` ("Informational only, not financial advice…"). That is the licence position
  (no unlicensed investment advice), not decoration.
- It **ranks options and never authorises anything**. Choosing to draw is a separate gated tool.
- `registry_evidence()` is **evidence only and never changes a verdict**. The payee-change denial
  stands on the fact alone, and a registry *match* must not weaken it — a match proves the
  company is who it says it is, not that this bank-account change was authorised. This is a
  security property, not a display choice.

`helios.yaml` already requests the `market-data` permission, so the sidecar can forward to it
once the swap is made.

---

## 5. Assumptions this branch made that other work supersedes

The session prompt described a repository state that **did not exist** at `cc96f0a`. Each
premise was verified with tools before any code was written; full detail is in
`docs/sessions/p4-payables/PLAN.md`. The ones affecting integration:

| Assumed present | Actual at `cc96f0a` | Now |
|---|---|---|
| `platform/deployment/` engine, `helios` CLI | Absent | **Exists in `p2-deploy`** (`ce3efbd` onward). Not in this branch |
| Docker daemon, `HELIOS_ENGINE_URL` | Daemon not running; var unset | Unchanged here |
| `market.*` tool family | Absent; only 12 tools registered | **`market_data.py` added on `chloe-dev` at `7ff4b74`** — see §4 |
| `forecast.*`, `behavior.*`, `collect.*` | Absent | Not needed by this app |
| `email.send_template` with 5 named templates | Absent. `email.send` takes a `template` kwarg; only `payment_reminder` / `payment_confirmation` exist | Remittance text rendered in `tools.py` (`REMITTANCE_SUBJECT` / `REMITTANCE_BODY`) |
| `scripts/bootstrap-venv.sh`, `scripts/test-all.sh` | No `scripts/` directory | Venv created directly; commands in the README |
| `apps/helios-collect/server.py` as the FastAPI pattern | Absent — collect was CLI-only | **Added in `p2-deploy` at `ab8ecf8`**, which also converted collect and invoice-payer to the deployment contract. This branch missed that conversion and was written against the contract by reading `p2-deploy` afterwards |
| Repo venv at `.venv` | Absent | Created |

### Merge-relevant facts

- **This branch's base is two commits behind `chloe-dev`** (now `7ff4b74`). `cc96f0a` is still an
  ancestor of it (`git merge-base --is-ancestor` passes), so no history rewrite is needed.
- **Those two commits touch no path this branch touches.** `git diff --name-only cc96f0a..7ff4b74`
  is confined to `platform/financial-tools`. There is **no textual conflict**.
- One is semantically relevant (`7ff4b74`, the market-data module — §4). The other,
  `b98ba36` ("fix: stop reporting an outage as an audit-gate rejection"), draws the *same*
  distinction this app draws from the other side: it stops `_wrap` reporting an unreachable
  service as `{"rejected": True}`, because "rejected tells an agent to change what it asked for;
  an outage means the request may be perfectly fine". This app's `rejected_by_ledger` status
  exists for the identical reason — the gate allowed the call and the ledger refused it, which is
  a credential problem, not a policy one. Worth reading together.
- **`p2-deploy` also converted the two existing apps** (`ab8ecf8`) to the deployment contract,
  adding `server.py`, templates and `tests/test_server.py` to `helios-collect` and
  `invoice-payer`. If that lands first, this branch's `helios-payables` is the only app of the
  three written against the contract rather than converted to it — the patterns should match,
  and its `templates/` and `tests/test_server.py` are the closest analogue to compare against.

### Graph contract this branch publishes

Relevant if another app consumes it:

- `graph.upsert_verified("bill", bill_id, …)` on pay and on reschedule — this app is
  authoritative for `bill`.
- `graph.add_observation("funding", bill_id, …)` for a credit-line draw — an inference, so it can
  never overwrite a verified node.
- `graph.upsert_verified("supplier", supplier_id, …)` on owner re-verification.
- Declared as reading `cashplan[shortfall_warning]`, but **nothing depends on it** — the app runs
  standalone. That read is an integration point, not a dependency.

These go through `bundle.graph` directly, never `bundle.registry.call`, because `build_world()`
binds module-level singletons and the last-built world wins for every registry dispatch
(`fin_tools/payments.py:158`, `email_sender.py:101`). Tests build a fresh world per case, so
registry dispatch would make them interfere.

---

## 6. Platform gaps worked around inside the app

None required editing a platform package. If another worktree fixes one upstream, the app-level
workaround stays correct but becomes redundant.

| Gap | Location | Workaround here |
|---|---|---|
| `auth_code` is not in `REDACTED_KEYS`, so a live single-use payment credential reaches `owner_question` and both logs **in full** | `audit_agent/contracts.py:54`; rendered at `agent.py:705` | `redact_text()` masks 16+ hex runs **by shape**, since the auditor has already rendered the prose and key names are gone. Asserted by three tests: nothing in stdout, nothing in the action log, nothing in served HTML |
| `_subject_key()` does not consider `bill_id` | `policy.py:1192` | `send_remittance` **requires** `supplier_id`, or the contact-frequency rule finds no subject and denies everything |
| The runtime records a contact only for a tool named exactly `send_email` | `runtime.py:611` | `send_remittance` calls `book.record_supplier_contact()` itself |
| `build_world()` binds module-level singletons | `payments.py:158` | Handlers call `bundle.payments` / `.email` / `.graph` directly |
| Rules cannot express a two-argument pairing check | HANDOFF trap 7 | `send_remittance`'s handler refuses a supplier/bill mismatch; fails loudly, nothing sent |
| `Store` has no bill or supplier mutator, and `get_invoice` / `get_customer` return **copies** | `agent_toolkit/store.py` | `Book._mutate_bill` and `_mutate_supplier` write into the store's private dicts, each confined to one method |
| `check_app.py` misreads prose and rate fields as money | `entry-point/checks/check_app.py:90` | Renamed `payment_terms` → `settlement_terms` and `late_fee_bps` → `late_penalty_bps` rather than weaken a shared checker, matching the precedent `helios-collect` set for `payer_behavior` |
| `mandate.limits` accepts only four receivables-shaped keys | `agent_toolkit/models.py:95` | Only `invoice_owner_approval_amount` and `min_days_between_contacts` are set, mirroring `invoice-payer`. The audit manifest is authoritative and the README says so |

**Two bugs in this app**, both found by inspection rather than by test, both silent:

- `Store.get_customer` / `get_invoice` return copies, so `reverify_supplier`, `mark_paid` and
  `reschedule` were no-oping. Owner re-verification appeared to work and never lifted a denial.
- `details_changed_days_ago` resolves to the key **`details_changed`**, not
  `details_changed_date` (the store strips the `_days_ago` suffix), so the 30-day window never
  applied and a supplier 45 days out — outside the window — was wrongly denied.

---

## 7. Verification commands

```bash
cd apps/helios-payables

# 70 tests, offline, no API key
../../.venv/bin/python -m pytest tests -q

# The demo: 15 scenarios
../../.venv/bin/python app.py
../../.venv/bin/python app.py --scenario BILL-2002 --trace   # the payee-change demo
../../.venv/bin/python app.py --scenario BILL-2012 --trace   # the lying-agent demo
../../.venv/bin/python app.py --json

# The web UI
../../.venv/bin/python -m uvicorn server:app --port 8000

# The app-builder spec, validated
../../.venv/bin/python ../../entry-point/checks/check_app.py .
```

**Last observed:** `70 passed`; `check_app.py` **0 errors, 1 warning**; all 15 scenario verdicts
match their expectations; `GET /health` returns `{"status":"ok","app":"helios-payables","built":false}`
in **1s** under a simulated smoke-test environment with no Redis, no sidecar, no model key and no
data volume; `helios.yaml` passes the engine's real `load_manifest()` and `lint()` with 0 problems.

Pre-existing suites re-run and **unchanged** by this branch: `helios-collect` 74 passed,
`invoice-payer` 41 passed, and `audit-agent-builder`, `financial-tools`, `agent-integration` all
green.

---

## 8. Commit list

```
6fad3c1 chore: session notes for p4-payables
d8d744d docs: document helios-payables and its deployment contract
43dc035 feat: serve the helios-payables pages over FastAPI
4a20a69 test: assert every helios-payables control fires
9fe7e63 feat: assemble helios-payables on the platform
2bc51e9 feat: build the helios-payables fact source and tools
efb1c2f feat: declare the helios-payables mandate and audit policy
cd0f83b feat: add the helios-payables fixtures and data layer
```

Ordered dependencies-before-dependents: fixtures → policy → facts/tools → composition → tests →
UI → docs. Each commit leaves the app importable and its own tests passing.

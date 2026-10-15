# Session p1-tools — Plan

Owner: this session. Base branch `chloe-dev`. Worktree
`.qwen/worktrees/p1-tools`, branch `worktree-p1-tools`.

Scope owned: `platform/financial-tools/` and `entry-point/references/financial-tools.md`.
Not owned (do not edit): `platform/deployment/` (P2), `platform/app-builder/` (P3).

## Baseline (recorded before any change)

`scripts/test-all.sh` green:

| suite | passed |
|---|---|
| platform/financial-tools | 364 |
| platform/audit-agent-builder | 79 |
| platform/agent-integration | 123 |
| entry-point/examples/invoice-payer | 41 |
| **total** | **607** |

`platform/action-agent-builder` ships no `tests/` directory, so it is reported as
SKIP (pytest exit 5), not FAIL. `agent_integration.cli selfcheck` prints
`seam check: OK`. Registered tools: 12.

## Discovered blockers — the task brief's premises were false

The brief states P0 already ran. It had not, on any worktree:

| brief claims | verified reality | consequence |
|---|---|---|
| worktree `.qwen/worktrees/p1-tools` exists | absent | created it here |
| `scripts/bootstrap-venv.sh`, `scripts/test-all.sh` exist | no `scripts/` at all | wrote both |
| venv `.venv` exists | absent | created it (3.13.9, `--system-site-packages`) |
| teammate packages imported as siblings of `fin_tools` | still untracked in `OTHER-TEAMMATE-STUFF/` | copied them in |
| `spreadsheets/` from `origin/payments` | not in `OTHER-TEAMMATE-STUFF/` | extracted via `git archive` (no branch switch) |
| `helios_forecast/market.py` copied in from the fork | no `market.py` in `helios_forecast/`; it lives at `services/financial-api/financial_api/market.py` | Task 5 must copy it in and fix it |
| `docs/research/*.md` present | no `docs/research/` | Task 5 §B references unavailable; worked from the brief's verified facts |
| `specs/forecasting-research-claims.md` present (T5–T9) | absent | Task 5 cannot mark T5/T6 corrected in that file; record the correction in a new spec instead |
| `platform/deployment/` exists | absent; the engine is untracked at `OTHER-TEAMMATE-STUFF/deployment-stuff/fPT9INmvWJ/default/helios/` | Task 6 targets that real layout |
| 12 tools incl. `simulation.forecast` | confirmed | — |

Concurrent agents: `p0-foundation`, `p3-builder`, `p4-payables`, `p5-verify`
worktrees exist. `p0-foundation` has uncommitted edits to
`platform/financial-tools/pyproject.toml` and `platform/action-agent-builder/pyproject.toml`
and an untracked `docs/sessions/`. Those are in ITS worktree, not mine; no file is
shared. Risk: both sessions write `platform/financial-tools/pyproject.toml` on
their own branch, so the merge into `chloe-dev` will conflict. Noted for HANDOFF.

## Tasks

Ordered; each ends with a commit and a green `scripts/test-all.sh`.

0. **Foundation** (done) — worktree, venv, `scripts/`, teammate import, baseline.
1. **One contract** — `ToolSpec.input_json_schema`, `ToolSpec.tenant_scoped`;
   `declare()` emits both; bridge passes a JSON schema through untouched;
   `ToolSpec.from_definition`, `ToolSpec.from_teammate` (accepts `side_effect`
   singular); replace module-level singleton binding with per-Bundle closures
   (`bind()` kept as a deprecated shim). Tests: two worlds/two ledgers, nested
   schema round-trip, `selfcheck` still OK.
2. **Email family** — `email.send` keeps its signature, writes `world.outbox` by
   default, delivers via `email_tool.EmailSender` when
   `EMAILTOOL_<TENANT>_SMTP_HOST` is set for the bundle's tenant, records the
   provider `message_id`. New gated `email.send_template(to, template, fields,
   idempotency_key)` over the five templates; read-only `email.templates()`.
   Idempotency via `email_tool.idempotency.IdempotencyStore` under
   `HELIOS_DATA_DIR`. `records_contact` marker on both send tools. Offline tests
   via `transport_factory` injection; one `skipif` live test.
3. **Receivables (`collect.*`)** — over `payments_collect.PaymentStore`, tenant =
   bundle tenant. `collect.add_receivable` (gated), `collect.list_receivables`
   (read), `collect.create_payment_link` (gated, returns id +
   `https://pay.helios.test/l/<id>`), `collect.status` (read), `collect.refund`
   (gated, requires an intent `auth_code`). `record_payment_success` and provider
   events are NOT tools — expose `fin_tools.collect.apply_provider_event(...)` for
   webhook handlers, and mirror a successful payment into the fin_tools ledger via
   `payments.receive`. Remove the `business_id="dev"` default reliance; a missing
   tenant is an error. Use `store.subscribe()` to detect success (there is no
   callback on `record_payment_success`).
4. **Forecasting** — wrap `helios_forecast` as `forecast.project`,
   `forecast.simulate`, `forecast.scenario`, `forecast.query` (one tool, `query`
   arg selects `balance_at`/`min_balance`/`first_shortfall`/`probability_below`/
   `expected_total`), `behavior.profile`, `behavior.promise_rate`; all read-only;
   cents in/out (float only at the engine boundary, round half-even back).
   JSON Schemas lifted from their registry. Re-body `simulation.forecast` to call
   `forecast.simulate`, keeping its signature and tests. Keep `sandbox_run`.
   Document the `common_shock` symmetry caveat (their D12).
5. **Market data (`market.*`)** — new subpackage `fin_tools/market_data/`
   (`client.py`, `cache.py`, `rates.py`, `registry.py`, `tools.py`), stdlib only,
   read-only, tags `("market",)`. Four tools: `market.rate`,
   `market.borrowing_cost_compare`, `market.company_lookup`, and the fourth per
   the brief (truncated — see Open questions). Fix the Tushare adapter: HTTPS
   endpoint, `api_name="shibor_lpr"` with fields `date, 1y, 5y`; `shibor` with
   `date, on, 1w, 2w, 1m, 3m, 6m, 9m, 1y`. Providers behind one
   `MarketDataSource` protocol: `TushareSource`, `CoreFileSource`,
   `FixtureSource`, `CachedSource`. Mode via `HELIOS_MARKET_MODE`
   (`fixture|live|auto`). Token bucket 40/min, retries with backoff. Every result
   carries `provenance`. `MarketDataUnavailable` when nothing is found — never an
   invented number. Token only from `TUSHARE_TOKEN`, never logged, redacted from
   every error string.
6. **Service hardening** — bearer auth on the HTTP service
   (`HELIOS_EXECUTION_SECRET`), SQLite persistence under `HELIOS_DATA_DIR`, the
   deployment engine's routes (`POST /tools/{payments|email|knowledge-graph}`,
   `GET /kg/nodes`, `GET /health`) with its exact payloads, `GET /health`
   unauthenticated. Keep `/tools` + `/call` working.
7. **Docs** — `entry-point/references/financial-tools.md`, specs updates,
   `HANDOFF-TO-P2-P3.md`, remove legacy GLM/Zhipu/bigmodel.cn references in files
   I touch.

## Open questions (truncated brief)

The attachment cut off mid-Task 5 inside `market.company_lookup`'s output shape.
Missing: the fourth `market.*` tool's name and contract, the remainder of Task 5's
tests, whether there is a Task 6/7, and all of section G (blocked-task protocol).
The prompts directory `helios-qwen-prompts/` is unreadable (EPERM).

Decisions taken rather than asked, per "work autonomously":
- Task 6 and 7 above are inferred from section E's objective ("one
  bearer-authenticated HTTP service with persistence that the deployment engine's
  core container will run") and from the "You own
  `entry-point/references/financial-tools.md`" line. If the truncated text
  specified them differently, these are the safer reversible reading.
- The fourth market tool is inferred from the three moments in Task 5: moment 2's
  second case ("supplier announced a new bank account" → registry evidence for
  the hold) and moment 3 ("new customer, no payment history" → national average
  collection period) both need data that `market.rate` and
  `market.company_lookup` do not return. Plan: `market.collection_period`
  (national average `arturn_days` from `fina_indicator`, plus the industry peer
  median via `index_member_all`) — this is exactly moment 3's requirement, and
  `company_lookup` covers moment 2's registry evidence. Will confirm against the
  recovered text if it becomes readable.
- Where the brief says to mark claims T5/T6 corrected in
  `specs/forecasting-research-claims.md`, that file does not exist. I will record
  the correction in `specs/market-data.md` (new) and say so in the commit body,
  rather than inventing the file the brief assumed.

## Non-negotiables

- Local commits only. No push, rebase, amend, reset, `--no-verify`.
- Stage explicit paths. `git status --porcelain` before every commit.
- Never touch `OTHER-TEAMMATE-STUFF/`, `docs/milestones/`, `.env`, `*.venv`,
  other worktrees.
- Never weaken a gate to make a test pass.
- No float money. No secret in a log, commit, or error string.
- Qwen is the only generative model; remove GLM/Zhipu/bigmodel.cn from files I touch.

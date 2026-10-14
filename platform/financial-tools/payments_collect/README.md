# payments-tool — the Helios payments tool (collect)

The receiving side of payments for Helios. Pairs with the **Helios Collect**
app (`apps/customer-payment`): receivables → payment links → provider capture
→ dual-entry ledger → settlement.

Provider-agnostic by design: the intended first real provider is the Alipay
sandbox, and a mock provider (`MOCK_PROVIDER=1`) will give a zero-credential
demo. Nothing in the data model or the store knows a provider's API.

Standalone for now (decision, 2026-10-13): this package lives at the repo
root with its **own** store — no writes to the shared knowledge graph yet —
and the directory tree will be reorganized once all Helios components are
together.

Branch: `payments-michelle`.

## Status — component 1 of 5

Built incrementally; each component ships with tests, a README refresh, a
commit and a push.

| # | Component | Status |
|---|-----------|--------|
| 1 | Data model + in-memory store (the six tables) | **done** |
| 2 | `PaymentProvider` interface + `MockProvider` (`MOCK_PROVIDER=1`) | next |
| 3 | Collect service: `create_link` / `status` / `refund` (approval token) + hardened webhook processing | planned |
| 4 | REST API: `POST /v1/payment-links`, `GET /v1/payments/{id}`, `POST /v1/payments/{id}/refund`, `POST /webhooks/{provider}` | planned |
| 5 | Developer guide: state machine, idempotency contract, trust table, quickstart | planned |

## Layout

    payments-tool/
      conftest.py            # puts payments-tool on sys.path for pytest
      requirements.txt
      collect/
        errors.py            # {code, message, retryable} error table
        models.py            # the six tables + guarded transition tables
        store.py              # PaymentStore — in-memory dev store
        tests/
          test_models.py
          test_store.py

## The six tables

| table | what it holds |
|-------|---------------|
| `Receivable` | money a customer owes (customer, amount, currency, invoice, due date, status) |
| `PaymentLink` | a shareable link for one receivable (server-side amount, expiry, one-time flag, secret token) |
| `PaymentAttempt` | one try to pay one link (provider, `out_trade_no`, state machine, refunded amount) |
| `ProviderEvent` | raw webhook payloads — **append-only**, idempotent on (provider, event_id) |
| `LedgerEntry` | dual-entry postings; legs sharing a `journal_id` balance |
| `Settlement` | where a captured payment routes (account, rule, status) |

All records carry `id`, `business_id` (tenancy) and `created_at`.

## Rules the store enforces

1. **Store the raw payload before processing it.** `record_provider_event`
   writes `payload_json` + sha256 `payload_hash` first; the same
   (provider, event_id) with the same payload is a no-op, with a different
   payload it is an `event_conflict` — stored payloads are never edited.
2. **Reconcile on our own `out_trade_no`**, never the provider's order id.
3. **Server-side amounts only.** A link's amount comes from the receivable;
   an attempt's amount comes from the link. A caller-supplied amount that
   disagrees raises `amount_mismatch`.
4. **Dual-entry ledger.** Every posting needs balancing debit/credit legs
   (`unbalanced_entry`); capture posts clearing → merchant, refunds post the
   reverse.
5. **Guarded state machines** for attempts, links, receivables and
   settlements — illegal moves raise `invalid_transition` and leave the
   state unchanged.
6. **Refunds never exceed the captured amount** (`refund_exceeds_payment`).
7. **Tenancy:** cross-tenant lookups are `not_found`, never a leak.
8. **Reads never mutate.** All writes go through store methods.

## State machine

`PaymentAttempt.state`:

    created → intent_created → customer_authenticating → processing → succeeded → settled → closed
                                 └──────────── (a webhook can land here) ──────→ succeeded

- `failed` / `expired`: terminal, reachable from any live state.
- `succeeded` / `settled` → `partially_refunded` → `refunded` → `closed`.
- The link's own status is `issued` (the plan's `link_issued`) → `closed` /
  `expired`; a one-time link closes on success.

## Conventions

- Money: integer minor units (fen) + currency, never floats. `100_00` = ¥100.00.
- Dates: tz-aware, Asia/Shanghai.
- Errors: `{code, message, retryable}` with the mapped HTTP status.

## Error codes

| code | HTTP | retryable | raised when |
|------|------|-----------|-------------|
| `validation_error` | 400 | no | bad parameter |
| `permission_denied` | 403 | no | app manifest lacks the tool |
| `not_found` | 404 | no | unknown or cross-tenant id |
| `amount_mismatch` | 400 | no | amount disagrees with the server-side amount |
| `link_inactive` | 409 | no | link closed, used or expired |
| `invalid_transition` | 409 | no | illegal state-machine move |
| `event_conflict` | 409 | no | event_id redeclared with a different payload |
| `refund_exceeds_payment` | 400 | no | refund exceeds the capture |
| `unbalanced_entry` | 500 | no | ledger invariant (a bug) |
| `rate_limited` | 429 | yes | provider throttling (component 2+) |
| `upstream_unavailable` | 503 | yes | provider down (component 2+) |

## Running the tests

From the worktree root (first time):

    python -m venv payments-tool\.venv
    payments-tool\.venv\Scripts\python -m pip install -r payments-tool\requirements.txt

Then:

    payments-tool\.venv\Scripts\python -m pytest payments-tool -q

## What is deliberately not here yet

- No provider adapter (mock or Alipay) — component 2.
- No service layer / LLM-facing tools (`collect.create_link`,
  `collect.status`, `collect.refund`) — component 3.
- No REST API or webhook endpoint — component 4.
- No writes to the shared knowledge graph — own store only for now.

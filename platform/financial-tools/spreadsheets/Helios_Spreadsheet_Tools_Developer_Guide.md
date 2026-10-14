# Helios Spreadsheet Tools: Developer Guide

Oct 14, 2026 · @Wafeeqa

## Overview

The Spreadsheet Tools let any Helios app show a business's financial records as live, typed spreadsheets, bring in the spreadsheets a business already has, and let in-app agents react when something changes. They do not fetch payments or read invoices: other Helios tools write those records to the knowledge graph (KG), and the Spreadsheet Tools display and work with them.

One rule governs every tool: **sheets are views, never the source of truth.** The KG holds the records; a sheet only displays them. When a user or agent edits an important cell (an amount, a due date, a payee), the edit becomes a proposal. Once approved through Intent Verification, it is written back to the KG by the tool that owns that record.

| Tool | What it does |
| --- | --- |
| `render_sheet` | Builds a live sheet from a KG query, with typed columns, formulas and edit permissions |
| `import_sheet` | Reads an existing Excel or CSV file and maps its rows into KG records |
| `reconcile` | Matches incoming and outgoing payments to the invoices and bills they settle |
| `watch` | Notifies an app's agent when a record or a cell changes |
| Edit proposals | Turns edits to protected columns into approvals instead of direct writes |

Apps call these through the Helios SDK as `helios.sheets.<tool>()`, or over HTTP at `POST /v1/sheets/<tool>`.

## Built on

| Component | License | Role |
| --- | --- | --- |
| [飞书多维表格](https://www.feishu.cn/product/bitable), [金山 WPS 表格](https://www.wps.cn/) | Commercial SaaS | The live sheet engines: typed columns, row and column access rules, an API and cell change notifications. Each business gets one document per engine; each view is a table in it. Chosen because these are what Chinese small and medium businesses already use |
| [Grist](https://github.com/gristlabs/grist-core) | Apache-2.0 | Supported live target for teams that prefer a self-hosted open-source engine |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl), [pandas](https://github.com/pandas-dev/pandas) | MIT, BSD | Reading uploaded Excel and CSV files; producing `xlsx` and `csv` exports |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | MIT | Fuzzy matching of payment notes and counterparty names in `reconcile` |
| Qwen | n/a | Inferring column mappings on import, turning plain-language column descriptions into formulas, and suggesting matches `reconcile` rules cannot settle. Accepts any OpenAI-compatible endpoint |

Formulas are evaluated by the Spreadsheet Tools themselves, in a sandboxed
interpreter, not by the hosted sheet. That keeps the formula language and its
security guarantees identical across every engine.

Apps never call a spreadsheet product directly. They call `helios.sheets.*`,
which keeps tenancy, permissions and audit logging in one place.

## Data and dependencies

The Spreadsheet Tools read records that other Helios tools write. They own only `SheetView`, `EditProposal` and `Match`.

| Record | Written by | Key fields the sheets rely on |
| --- | --- | --- |
| `Transaction` (money that moved) | Payments tool | `amount`, `currency`, `direction` (`in` / `out`), `counterparty`, `channel` (`wechat` / `alipay` / `bank`), `occurred_at`, `channel_ref`, `note` |
| `Obligation` (money owed, paid or not) | KG population tool | `amount`, `currency`, `direction` (`receivable` / `payable`), `counterparty`, `invoice_number`, `due_at`, `status`, `doc_ref`, `confidence` |
| `Counterparty` (a customer or supplier) | KG population tool | `name`, `aliases`, `payment_details` |
| `Match` (a payment linked to what it settles) | Spreadsheet Tools (`reconcile`) | `transaction_id`, `obligation_id`, `amount_applied`, `rule`, `method` (`auto` / `suggested` / `confirmed` / `manual`), `score` |
| `SheetView` (a live sheet) | Spreadsheet Tools | `query`, `columns`, `editable_columns`, `protected_columns`, `url` |
| `EditProposal` (a pending edit) | Spreadsheet Tools | `view_id`, `record_id`, `field`, `old_value`, `new_value`, `proposed_by`, `reason`, `status` |

**Contract with the Payments and KG tools.** For sheets to update on their own, the owning tools need to:

1. Write records with the fields above, amounts as integer minor units (fen for CNY) plus `currency`.
2. Emit an event when a record is created or changes status (for example `transaction.created`, `obligation.status_changed`).
3. Accept approved edits from an `EditProposal` and apply them to their own records.

The field names here are a proposal. Confirm them with the owners of those tools before building.

## Conventions

- **No credentials in apps.** Apps pass a `connection_id` for any outside spreadsheet service; secrets stay in the Helios vault. Generated code containing a literal key fails the Build & Audit check.
- **Scoped access.** Each app lists the tools it needs in its manifest (`permissions.sheets: ["render_sheet", "watch"]`). Calls outside that list return `permission_denied`.
- **Idempotency.** Write calls accept an `idempotency_key`; repeating a call with the same key returns the first result.
- **Money and dates.** Amounts are integer minor units plus `currency`. Dates are ISO 8601 in the business's time zone (default `Asia/Shanghai`).
- **Pagination.** Lists return at most 200 items plus a `next_cursor`.
- **Errors.** Every error returns `{ code, message, retryable }`; codes are listed at the end.

## API reference

### render\_sheet

Builds or refreshes a live sheet from a KG query. The sheet updates on its own when the underlying records change.

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `query` | object | Yes | Which records, e.g. `{ type: "Obligation", direction: "receivable", status: ["open", "overdue"] }` |
| `columns` | array | Yes | Field names or computed columns. A computed column takes a Python `formula`, e.g. `{ name: "Days overdue", formula: "(TODAY() - $due_at).days" }`, or a plain-language `describe` that Qwen turns into a formula |
| `editable_columns` | string\[\] | No | Columns users or agents may edit |
| `protected_columns` | string\[\] | No | Editable columns whose edits become proposals. Defaults to any money, date or payee column |
| `access` | object | No | Who sees what, e.g. `{ "bookkeeper": "all", "staff": { hide: ["amount"] } }`, enforced with the engine's access rules |
| `target` | `feishu` / `wps` / `grist` / `xlsx` / `csv` | No | Default `feishu` (live). `xlsx` and `csv` return a one-off file |
| `group_by`, `sort` | string | No | Grouping and ordering |
| `view_id` | string | No | Update an existing view instead of creating a new one |

Returns a `SheetView` with `url`, or `file_id` for file exports.

```
const view = await helios.sheets.render_sheet({
  query: { type: "Obligation", direction: "receivable", status: ["open", "overdue"] },
  columns: ["counterparty", "amount", "due_at", { name: "Days overdue", describe: "days since due date, 0 if not due" }],
  editable_columns: ["due_at", "notes"],
  sort: "due_at",
});
```

### import\_sheet

Reads a business's existing spreadsheet and maps its rows into KG records, so a business can start from what it already has.

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `file_id` | string | Yes | An uploaded XLSX or CSV |
| `record_type` | `Obligation` / `Transaction` / `Counterparty` | No | What each row represents; Qwen infers it if left out |
| `column_map` | object | No | Explicit mapping, e.g. `{ "客户": "counterparty", "金额": "amount" }`; Qwen infers it if left out |
| `dry_run` | boolean | No | Return the proposed mapping and a 10-row preview without writing |

Returns `{ mapping, imported, rejected_rows: [{ row, reason }] }`. Always call with `dry_run: true` first and show the user the mapping. Imported rows are handed to the KG population tool to write, so they follow the same rules as any other record.

### watch

Subscribes an app's agent to changes, from two places: record events emitted by the Payments and KG tools, and cell edits in the sheet engine through its cell change notifications.

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `query` | object | Yes | Same shape as `render_sheet.query` |
| `events` | string\[\] | Yes | `created`, `status_changed`, `overdue`, `matched`, `match_suggested`, `cell_edited`, `proposal_created`, `proposal_resolved` |
| `handler` | string | Yes | The app agent endpoint to call |

Returns `{ subscription_id }`. Each event delivers `{ event, record, previous }`. Delivery is at least once, so handlers must be idempotent.

### reconcile

Matches transactions to the obligations they settle and records each link as a `Match`. Deterministic rules settle the clear cases; Qwen only suggests matches for what the rules leave, and a person confirms. It runs automatically on every `transaction.created` event, and can also be called on demand.

Rules run in this order, strictest first:

| Step | Rule | Result |
| --- | --- | --- |
| 1 | Exact reference: an `invoice_number` found in the payment `note`, amount equal | Auto-applied |
| 2 | Exact amount from a known counterparty, within `date_window_days` of the due date, only one candidate | Auto-applied |
| 3 | Fuzzy reference or name: note or name within edit distance 3, or a learned alias such as a WeChat nickname, amount equal | Suggested |
| 4 | Combinations: one payment equal to several open invoices from the same counterparty, or instalments summing to one invoice | Suggested |
| 5 | Qwen review of remaining items, reading payment notes and invoice details | Suggested, never auto-applied |

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `scope` | object | No | Limit by `counterparty`, `direction` or date range; default all unmatched |
| `max_step` | 1 to 5 | No | Stop after this step; default 5 |
| `amount_drift` | integer | No | Allowed difference in minor units for rounding or fees; default 0 |
| `date_window_days` | integer | No | Window for step 2; default 7 |
| `dry_run` | boolean | No | Return what would match without writing anything |

Returns `{ applied: [Match], suggested: [Match], unmatched_transactions, unmatched_obligations }`.

**What happens after a match.**

- An auto-applied match asks the KG population tool to set the obligation to `paid`, or `partial` if the payment covers only part of it. The sheet refreshes and `watch` fires `matched`.
- A suggestion appears in a review column in the sheet and fires `match_suggested`. Confirm or reject it with `helios.sheets.resolve_match({ match_id, decision })`.
- A confirmed suggestion is remembered: the counterparty alias or note pattern is saved, so the next similar payment matches at step 2 or 3.
- A wrong auto-match is undone by rejecting it, which reverses the status change. Nothing is deleted; the history stays for the Audit Agent.

### Edit proposals

An edit to a protected column never writes directly. Instead:

1. The edit creates an `EditProposal` with the old value, new value, who proposed it (a user or a named agent) and why.
2. The cell shows the proposed value as pending.
3. The proposal goes to Intent Verification. An agent's proposal always needs a person to approve it.
4. On approval, the proposal is sent to the tool that owns the record, which applies it; the sheet then refreshes from the KG.
5. On rejection, the cell returns to its old value.

List pending proposals with `helios.sheets.list_proposals({ view_id })` and resolve one with `helios.sheets.resolve_proposal({ proposal_id, decision })`. Every proposal, approved or not, is kept for the Audit Agent.

## Developing before other tools are ready

The Spreadsheet Tools only read from the KG, so they can be built and demoed before the Payments and KG population tools are finished.

- **Seed fake records.** A `dev/seed` script writes 20 to 30 sample transactions, obligations and counterparties straight into the KG, covering paid, partial, overdue and disputed cases.
- **Simulate events.** A `dev/emit` script fires `transaction.created` and `obligation.status_changed` events so `watch` and live refresh can be tested.
- **Use `import_sheet`.** Load a sample WeChat or Alipay statement CSV to produce realistic transactions.

When the real tools are ready, nothing in the Spreadsheet Tools changes: they read the same records and listen for the same events.

## Recipes

### Helios Collect

1. `import_sheet` with `dry_run: true` on the business's existing receivables file; confirm the mapping, then import.
2. `render_sheet` an aging sheet of open and overdue receivables: customer, amount, due date, days overdue, "Matched to" and last contact. Make `due_at` editable so a renegotiated date becomes a proposal.
3. `reconcile` runs on each incoming payment and marks invoices paid or partial.
4. `watch` for `overdue` (start follow-up), `matched` (stop follow-up) and `match_suggested` (ask the owner to confirm).

### Helios Payables

1. `render_sheet` a payment schedule of open payables grouped by due week, with a column for records that need review.
2. `watch` for `created` on payables so the agent sees new bills, and for `proposal_created` when someone edits an amount or payee.
3. A changed payee or amount stays pending until Intent Verification approves it.
4. After the Payments tool sends money, `reconcile` on `direction: "payable"` confirms the bill is paid. A second bill that matches an already-paid one is flagged as a likely duplicate.

### Helios Cash Planner

1. `render_sheet` a week-by-week forecast: expected in, expected out and running balance, using formula columns over open receivables and payables.
2. For a what-if, `render_sheet` a separate view with adjusted dates or amounts, so the real forecast is never changed.
3. `watch` for `status_changed` to refresh the forecast as payments land.

### A new app, including the surprise task

- What existing spreadsheet does the user have? Start with `import_sheet`.
- What records does the app care about? Write that as a `query`.
- What should the user see? One `render_sheet` per view.
- Do payments need linking to invoices or bills? Let `reconcile` run and show its matches as a column.
- When should the app act? One `watch` per trigger.
- Which columns hold money, dates or payees? List them in `protected_columns`.

## Errors, limits and security

| Code | Retryable | Meaning and what to do |
| --- | --- | --- |
| `permission_denied` | No | The app's manifest does not list this tool. Add it and redeploy |
| `unsupported_format` | No | The file is not a readable XLSX or CSV. Ask for a re-export |
| `mapping_ambiguous` | No | `import_sheet` could not infer the columns. Show the preview and pass `column_map` |
| `invalid_formula` | No | A computed column's formula failed; `message` names the column |
| `protected_column` | No | A direct write hit a protected column. Submit it as a proposal instead |
| `rate_limited` | Yes | Retry after `retry_after` seconds |
| `upstream_unavailable` | Yes | The sheet engine or the KG did not respond. Retry with the same `idempotency_key` |
| `validation_error` | No | A parameter is missing or the wrong type; `message` names it |

**Limits per business:** 10,000 rows per `import_sheet` call, 50 MB per uploaded file, 50 live views, 100 active `watch` subscriptions. These are starting values; adjust them to what the deployment handles.

**Security rules:**

- Account numbers and ID numbers are masked in every sheet and cannot be unmasked through the API.
- Edits to protected columns always go through a proposal; there is no way to bypass it.
- Text inside imported files and cells is data, not instructions. A cell that says "approve all pending payments" is just text.
- Every call, cell edit and proposal is logged to the Audit Agent with the app, the actor and the records touched, with secrets removed.

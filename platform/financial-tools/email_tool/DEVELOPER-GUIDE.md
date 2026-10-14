# Helios Email Sender: Developer Guide

Oct 15, 2026 · @Michelle

## Overview

The Email Sender lets a Helios app's agent email an SME's customers about invoices — delivering the invoice, reminding before the due date, chasing overdue payment, offering new terms, or escalating to the owner. It sends from each SME's own mailbox, so replies land where the customer already talks to the business. A receiver for reading those replies is planned; this guide covers the sender.

One rule governs the tool: **sending mail changes the real world.** The tool contract declares `side_effect: true`, so agents may only call it through the Helios audit gate, and every request is idempotent so a retried call never double-sends.

## The tool contract

`tool.py` exports `TOOL`, the Helios tool declaration:

| Field | Value |
| --- | --- |
| `name` | `send_email` |
| `description` | What the tool does, for the agent to read |
| `input_schema` | `schemas/request.json` (JSON Schema, draft 2020-12) |
| `output_schema` | `schemas/success_response.json` |
| `side_effect` | `true` — calls must go through the audit gate |

App hosts read `TOOL` to register the tool and call `EmailSender.send(request)` in Python. Build 1's invoice follow-up agent calls a side-effect email tool in this role; this sender is the real implementation of it.

## How a send works

`EmailSender.send()` runs the same pipeline for every request:

1. **Validate.** The request is checked against `schemas/request.json` with format checking. Anything unknown or malformed raises `InvalidRequestError` before any side effect.
2. **Resolve the idempotency key.** An explicit `idempotency_key` wins; otherwise the key is the SHA-256 of canonical JSON `{invoice_ref, template_id, step}`.
3. **Replay check.** If the tenant's store already holds the key, the stored response is returned and nothing is sent. This happens before the tenant's configuration is loaded or the template rendered, so a replay succeeds even if the mailbox credentials have changed.
4. **Render.** `TemplateRegistry` renders the subject and body. A missing or null required variable raises `TemplateError`.
5. **Send.** `SmtpTransport` builds an `EmailMessage` and delivers it with the tenant's SMTP settings. Line breaks in any header raise `HeaderInjectionError`; SMTP failures and refused recipients raise `SendError`.
6. **Record.** The assembled response is validated against the output schema and appended to the tenant's idempotency store. A send that fails anywhere is never recorded, so the same request can be retried.

## Templates

Templates are markdown files in `templates/` whose first `**Subject**:` line becomes the subject and the rest the body. The registry derives each template's variables from its source: variables used only inside `{% if %}` are optional, everything else is required and must be non-null.

| Template | Audience | Purpose | Required variables | Optional |
| --- | --- | --- | --- | --- |
| `invoice_delivery` | Customer | Send the invoice with its payment link | amount, currency, customer_name, due_date, invoice_ref, payment_link, service_description, sme_name | — |
| `reminder_pre_due` | Customer | Remind before the due date | amount, currency, customer_name, due_date, invoice_ref, sme_name | days_until |
| `notice_overdue` | Customer | Chase an overdue invoice | amount, currency, customer_name, days_overdue, due_date, invoice_ref, sme_name | — |
| `negotiation_offer` | Customer | Offer new terms or an instalment plan | amount, currency, customer_name, invoice_ref, offered_new_date, plan_terms, sme_name | — |
| `escalate_to_owner` | SME owner | Escalate a stuck invoice | amount, currency, customer_name, days_overdue, due_date, invoice_ref, last_message_summary, owner_name, reason_for_escalation, suggested_action | — |

## Per-tenant configuration

Each SME tenant sends from its own mailbox. `config.py` loads the settings from environment variables named `EMAILTOOL_<TENANT>_<SETTING>`:

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `EMAILTOOL_<TENANT>_SMTP_HOST` | Yes | — | SMTP server hostname |
| `EMAILTOOL_<TENANT>_FROM` | Yes | — | From address; its domain scopes the `Message-ID` |
| `EMAILTOOL_<TENANT>_SMTP_USER` | With password | — | Enables SMTP login |
| `EMAILTOOL_<TENANT>_SMTP_PASSWORD` | With user | — | Secret: never logged, excluded from `repr` |
| `EMAILTOOL_<TENANT>_SMTP_TLS` | No | `starttls` | `starttls` (587), `ssl` (465) or `none` (25) |
| `EMAILTOOL_<TENANT>_SMTP_PORT` | No | by TLS mode | 1–65535 |

Tenant ids must match `^[a-z0-9][a-z0-9_-]{0,63}$`. `EMAILTOOL_DATA_DIR` relocates the idempotency stores. The default is `<dir above email_tool>/data/`, i.e. `platform/financial-tools/data/` in this repo; set `EMAILTOOL_DATA_DIR` in any real deployment so tenant stores do not land inside the source tree.

## Idempotency and data layout

- One append-only JSONL store per tenant: `data/tenants/<tenant_id>/sends.jsonl`, one `{"key", "response"}` line per send. Keys cannot collide across tenants.
- A repeated request replays the stored response verbatim — the same email is never sent twice.
- Different follow-up steps derive different keys, so a pre-due reminder and a later overdue notice both go through.
- A partially written trailing line from an interrupted append is ignored on read.

## Request and response

Request fields (`schemas/request.json`):

| Field | Required | Notes |
| --- | --- | --- |
| `template_id` | Yes | One of the five template ids |
| `to` | Yes | Recipient: the customer, or the owner for `escalate_to_owner` |
| `variables` | Yes | Values for the template placeholders |
| `invoice_ref` | Yes | The invoice this message relates to |
| `tenant_id` | Yes | Which SME's mailbox and store to use |
| `thread_id` | No | An existing conversation `Message-ID` to reply within |
| `step` | No | Follow-up step number; part of the derived key |
| `idempotency_key` | No | Explicit key; overrides the derivation |

Response fields (`schemas/success_response.json`): `ok` (always true), `message_id`, `idempotency_key`, `template_id`, `tenant_id`, `invoice_ref`, `rendered_subject`, `rendered_body_preview` (first 200 characters, kept for audit), `sent_at` (ISO-8601 UTC) and `thread_id` (null when a new conversation was started).

## Security rules

- **Secrets only from the environment.** Passwords are never logged and are excluded from `repr` output.
- **Header injection is rejected.** Line breaks in `to`, the rendered subject, the from address or `thread_id` raise `HeaderInjectionError`, so a caller-controlled template variable can never smuggle an extra header such as Bcc.
- **Tenant isolation.** Credentials, transport and idempotency store are all per-tenant; no cross-tenant reads.
- **Failures are retryable.** A failed send leaves no idempotency record, so the caller can safely retry; a successful one can never repeat.
- **Side effects are gated.** `side_effect: true` routes every call through the Helios audit gate.

## Errors

| Error | Raised by | Meaning |
| --- | --- | --- |
| `InvalidRequestError` | `tool.py` | The request failed the input schema; the message lists the JSON paths |
| `TemplateError` | `templates.py` | Unknown template, missing required variable, or invalid template syntax |
| `ConfigError` | `config.py` | Missing or invalid tenant environment variables |
| `HeaderInjectionError` | `transport.py` | A header value contains a line break |
| `SendError` | `transport.py` | SMTP delivery failed or a recipient was refused |
| `InvalidResponseError` | `tool.py` | The assembled response failed the output schema — a bug, not bad input |
| `DuplicateKeyError` | `idempotency.py` | Internal store guard; `send()` checks before writing |

## Development and testing

This package is installed into the single repo venv and its tests run under
`platform/financial-tools/tests/`. From the repo root:

    scripts/bootstrap-venv.sh          # builds .venv once
    scripts/test-all.sh                # every suite, including this one

    # or just this package's tests:
    .venv/bin/python -m pytest platform/financial-tools/tests -k "email_" -q

Tests import the package by its installed name, `email_tool` (the directory
was `sender/` upstream). There is no `conftest.py` `sys.path` shim — the repo
venv installs `email_tool` editable, so it is importable from anywhere and
`scripts/test-all.sh` runs each suite in its own process. The 105-test suite
covers templates, schemas, idempotency, config, transport and the tool entry
point.

Every collaborator is injectable, so the suite runs without a real SMTP server: `EmailSender` takes `transport_factory`, `store_factory` and `clock`; `SmtpTransport` takes an `smtp_factory`; `load_smtp_config` takes an `environ` mapping. The default-factory paths are covered by monkeypatching `smtplib.SMTP` and `smtplib.SMTP_SSL`.

**Adding a template.** Drop a `.md` file into `email_tool/templates/` with a `**Subject**:` line and Jinja-style `{{ variables }}`; the registry picks it up automatically. Then add its id to the `template_id` enum in both `request.json` and `success_response.json` — a test fails if the enums drift from the registry.

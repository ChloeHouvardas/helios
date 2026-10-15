[THIS IS A ROUGH DRAFT]

This folder encompasses all parts of a functional email sender.

## Components

- `templates.py` — `TemplateRegistry`: loads the markdown templates from `templates/`, splits each into a subject line and body at the `**Subject**:` line, and derives each template's required and optional variables from the Jinja2 AST. `render()` returns the rendered subject and body, or raises `TemplateError` for an unknown template or a missing required variable.
- `templates/` — the email templates; see `templates/README.md`.
- `schemas/` — `request.json` and `success_response.json`: JSON Schema for the tool request and success response. Sending an email has side effects, so in Helios the tool is only invoked through the audit gate.
- `idempotency.py` — `derive_key()` hashes `invoice_ref` + `template_id` (plus optional `step`) into the default idempotency key; `IdempotencyStore` is an append-only JSONL log, one file per tenant, that replays the original response for a repeated key instead of sending a second email.
- `tests/` — pytest tests for templates, schemas, and idempotency.

Still to be built here: the send layer (SMTP transport, per-tenant credentials from environment variables, tool entry point). See the [email-tool README](../README.md) for status, setup, and running the tests.
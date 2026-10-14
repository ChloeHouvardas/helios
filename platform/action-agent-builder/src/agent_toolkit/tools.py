"""Mock tools that follow the tool contract (Build 1).

- lookup_invoice: read-only. Returns the invoice, its customer, and how
  overdue the invoice is / how recently the customer was contacted.
- send_email: side effect. Records the email in the store outbox and
  updates the customer's last-contacted date. Nothing real is sent.

Both tools share one Store with the audit gate, so the gate re-reads the
same data instead of trusting the agent's claims. Swapping these mocks for
real tools that keep the same contract changes nothing in the runtime.
"""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import ToolDefinition
from .store import Store

LOOKUP_INVOICE = "lookup_invoice"
SEND_EMAIL = "send_email"

_LOOKUP_INPUT = {
    "type": "object",
    "properties": {
        "invoice_id": {
            "type": "string",
            "description": "The invoice identifier, e.g. INV-1001.",
        }
    },
    "required": ["invoice_id"],
    "additionalProperties": False,
}

_LOOKUP_OUTPUT = {
    "type": "object",
    "properties": {
        "invoice": {"type": "object"},
        "customer": {"type": "object"},
        "days_overdue": {"type": ["integer", "null"]},
        "days_since_last_contact": {"type": ["integer", "null"]},
    },
    "required": [
        "invoice",
        "customer",
        "days_overdue",
        "days_since_last_contact",
    ],
    "additionalProperties": False,
}

_SEND_INPUT = {
    "type": "object",
    "properties": {
        "customer_id": {"type": "string", "description": "Customer identifier, e.g. CUST-001."},
        "invoice_id": {"type": "string", "description": "The invoice the email is about."},
        "subject": {"type": "string", "minLength": 1, "description": "Email subject line."},
        "body": {
            "type": "string",
            "minLength": 1,
            "description": "Email body. Professional, courteous, short. Reference the invoice number, amount, and due date.",
        },
        "extension_offer_days": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional: offer the customer a due-date extension of this many days. Only offer when the customer asked for more time or their history supports it. Stay within your mandate.",
        },
    },
    "required": ["customer_id", "invoice_id", "subject", "body"],
    "additionalProperties": False,
}

_SEND_OUTPUT = {
    "type": "object",
    "properties": {
        "message_id": {"type": "string"},
        "customer_id": {"type": "string"},
        "to": {"type": "string"},
        "invoice_id": {"type": "string"},
        "subject": {"type": "string"},
        "sent_at": {"type": "string"},
        "extension_offer_days": {"type": ["integer", "null"]},
    },
    "required": [
        "message_id",
        "customer_id",
        "to",
        "invoice_id",
        "subject",
        "sent_at",
        "extension_offer_days",
    ],
    "additionalProperties": False,
}


def make_lookup_invoice(store: Store) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        invoice = store.get_invoice(str(args["invoice_id"]))
        if invoice is None:
            raise ValueError(f"unknown invoice: {args['invoice_id']!r}")
        customer = store.get_customer(invoice["customer_id"])
        if customer is None:
            raise ValueError(
                f"invoice {invoice.get('number', args['invoice_id'])} references unknown customer"
            )
        days_overdue = store.days_since(invoice["due_date"])
        return {
            "invoice": invoice,
            "customer": customer,
            "days_overdue": days_overdue if days_overdue is not None and days_overdue > 0 else None,
            "days_since_last_contact": store.days_since(customer.get("last_contacted")),
        }

    return ToolDefinition(
        name=LOOKUP_INVOICE,
        description=(
            "Look up one invoice and the customer it belongs to, including days overdue and "
            "how recently the customer was last contacted. Read-only; use it before any "
            "follow-up so you never invent invoice details."
        ),
        input_schema=_LOOKUP_INPUT,
        output_schema=_LOOKUP_OUTPUT,
        side_effect=False,
        handler=handler,
    )


def make_send_email(store: Store) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        customer_id = str(args["customer_id"])
        invoice_id = str(args["invoice_id"])
        customer = store.get_customer(customer_id)
        if customer is None:
            raise ValueError(f"unknown customer: {customer_id!r}")
        invoice = store.get_invoice(invoice_id)
        if invoice is None:
            raise ValueError(f"unknown invoice: {invoice_id!r}")
        if invoice.get("customer_id") != customer_id:
            raise ValueError(
                f"invoice {invoice_id} does not belong to customer {customer_id}"
            )
        entry = store.record_sent_email(
            customer_id=customer_id,
            invoice_id=invoice_id,
            subject=str(args["subject"]),
            body=str(args["body"]),
            extension_offer_days=args.get("extension_offer_days"),
        )
        return {
            "message_id": entry["message_id"],
            "customer_id": entry["customer_id"],
            "to": entry["to"],
            "invoice_id": entry["invoice_id"],
            "subject": entry["subject"],
            "sent_at": entry["sent_at"],
            "extension_offer_days": entry["extension_offer_days"],
        }

    return ToolDefinition(
        name=SEND_EMAIL,
        description=(
            "Send an email to a customer about an invoice, optionally offering a due-date "
            "extension. Side effect: it contacts the customer. Calls are reviewed against "
            "your mandate before they execute; if a call is rejected, read the reason and "
            "adjust; if it is held, the run pauses for the owner."
        ),
        input_schema=_SEND_INPUT,
        output_schema=_SEND_OUTPUT,
        side_effect=True,
        handler=handler,
        # This tool contacts a customer, so the runtime must tell the fact
        # source after a successful send. Declared here rather than keyed off
        # the tool's name in the runtime, so contact-frequency rules work for
        # any tool that sets the flag.
        records_contact=True,
    )


def build_mock_tools(store: Store) -> list[ToolDefinition]:
    return [make_lookup_invoice(store), make_send_email(store)]

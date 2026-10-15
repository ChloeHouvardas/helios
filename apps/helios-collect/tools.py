"""The app's tools: what the agents inside helios-collect may actually do.

Five tools, matching the two manifests exactly:

  read-only (execute directly)     list_overdue_invoices, lookup_invoice
  side-effecting (gated proposals) send_email, record_agreed_extension,
                                   record_inbound_payment

`send_email` is the platform's own `make_send_email`, reused rather than
reimplemented: its contract — name, argument shape, store mutations, outbox entry
— is exactly what this app needs, and duplicating it would create a second
definition of the same side effect that could drift from the one the audit manifest
describes. Its schema in `agents/helios-collect-audit.yaml` matches the platform's
`_SEND_INPUT` field for field, with two added `maxLength` bounds.

`lookup_invoice` is deliberately NOT reused. The platform's version returns the
whole customer record; this one withholds `disputed`, because that flag is a control
input rather than business information. The auditor reads it from the fact source,
so an agent cannot argue with a value it was never given.

The two write tools are the app's own, and they share a property worth stating
plainly: **neither takes an argument that names an account.** Where inbound money
lands is resolved from `config.py`. An agent cannot misdirect a payment because
there is no field in the request to misdirect it with.

Handlers call `bundle.payments` and `bundle.graph` directly rather than going
through `bundle.registry.call`. That is not a stylistic preference: `build_world()`
binds module-level singletons, so with two worlds in one process the last-built one
wins for *every* registry dispatch. Verified 2026-10-15. Calling the instance
attributes sidesteps the hazard, and it matters here because the tests build a fresh
world per case. Recorded as a platform limitation in README.md.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from agent_toolkit.contracts import ToolDefinition
from agent_toolkit.store import Store
from agent_toolkit.tools import make_send_email

import config
import tenant_events
from book import Book

LIST_OVERDUE = "list_overdue_invoices"
LOOKUP_INVOICE = "lookup_invoice"
SEND_EMAIL = "send_email"
RECORD_EXTENSION = "record_agreed_extension"
RECORD_PAYMENT = "record_inbound_payment"

#: Every tool name this app exposes. The manifests must agree with it, and
#: tests/test_controls.py asserts that they do.
TOOL_NAMES: tuple[str, ...] = (
    LIST_OVERDUE, LOOKUP_INVOICE, SEND_EMAIL, RECORD_EXTENSION, RECORD_PAYMENT,
)

#: Fields withheld from the agent because they are control inputs, not business
#: information. The auditor still reads them from the fact source.
WITHHELD_FROM_AGENT = frozenset({"disputed", "contact_history_days"})


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_LIST_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_LIST_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "count": {"type": "integer"},
        "invoices": {"type": "array"},
    },
    "required": ["count", "invoices"],
    "additionalProperties": False,
}

_LOOKUP_INPUT: dict[str, Any] = {
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
_LOOKUP_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invoice": {"type": "object"},
        "customer": {"type": "object"},
        "days_overdue": {"type": ["integer", "null"]},
        "days_since_last_contact": {"type": ["integer", "null"]},
        "contacts_in_last_7_days": {"type": "integer"},
    },
    "required": ["invoice", "customer", "days_overdue",
                 "days_since_last_contact", "contacts_in_last_7_days"],
    "additionalProperties": False,
}

_EXTENSION_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invoice_id": {"type": "string", "minLength": 1},
        "extension_days": {"type": "integer", "minimum": 1, "maximum": 365},
        "terms_note": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["invoice_id", "extension_days", "terms_note"],
    "additionalProperties": False,
}
_EXTENSION_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invoice_id": {"type": "string"},
        "extension_days": {"type": "integer"},
        "previous_due_date": {"type": "string"},
        "new_due_date": {"type": "string"},
        "graph_node_id": {"type": ["string", "null"]},
    },
    "required": ["invoice_id", "extension_days", "previous_due_date", "new_due_date"],
    "additionalProperties": False,
}

_PAYMENT_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invoice_id": {"type": "string", "minLength": 1},
        "amount_cents": {
            "type": "integer",
            "minimum": 1,
            "description": "Integer cents actually received. Money is never a float.",
        },
        "currency": {"type": "string", "enum": ["CNY", "USD", "EUR", "GBP"]},
        "reference": {
            "type": "string",
            "maxLength": 120,
            "description": "The bank reference or remittance advice, verbatim.",
        },
    },
    "required": ["invoice_id", "amount_cents", "currency"],
    "additionalProperties": False,
}
_PAYMENT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "payment_id": {"type": "string"},
        "state": {"type": "string"},
        "invoice_id": {"type": "string"},
        "status": {"type": "string"},
        "amount_cents": {"type": "integer"},
        "balance_cents": {"type": "integer"},
        "credited_account": {"type": "string"},
        "graph_node_id": {"type": ["string", "null"]},
    },
    "required": ["payment_id", "state", "invoice_id", "status",
                 "amount_cents", "balance_cents", "credited_account"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def make_list_overdue(book: Book) -> ToolDefinition:
    """Read-only: which invoices are actually overdue, oldest first."""

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        rows = book.overdue_invoices()
        return {"count": len(rows), "invoices": rows}

    return ToolDefinition(
        name=LIST_OVERDUE,
        description=(
            "List the invoices that are overdue right now, oldest first, with customer and "
            "amount. Read-only. Use it to decide what to work on; it never tells you anything "
            "you may act on without looking the invoice up."
        ),
        input_schema=_LIST_INPUT,
        output_schema=_LIST_OUTPUT,
        side_effect=False,
        handler=handler,
    )


def make_lookup_invoice(book: Book) -> ToolDefinition:
    """Read-only: one invoice and its customer, minus the control inputs.

    Also reports contact frequency, so an agent can see it is about to breach the
    limit rather than discovering it from a denial. That is deliberate: a control
    the agent can read is a control it can plan around *legitimately* (by not
    contacting), which is better than one it stumbles into repeatedly.
    """

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        invoice_id = str(args["invoice_id"])
        invoice = book.get_invoice(invoice_id)
        if invoice is None:
            raise ValueError(f"unknown invoice: {invoice_id!r}")
        customer_id = str(invoice.get("customer_id"))
        customer = book.get_customer(customer_id)
        if customer is None:
            raise ValueError(
                f"invoice {invoice.get('number', invoice_id)} references unknown customer")

        dates = book.contact_dates(customer_id)
        last = dates[-1] if dates else None
        cutoff = book.today - timedelta(days=config.CONTACT_WINDOW_DAYS)
        return {
            "invoice": _strip(invoice),
            "customer": _strip(customer),
            "days_overdue": _positive(book.days_overdue(invoice_id)),
            "days_since_last_contact": (book.today - last).days if last else None,
            "contacts_in_last_7_days": sum(1 for d in dates if d >= cutoff),
        }

    return ToolDefinition(
        name=LOOKUP_INVOICE,
        description=(
            "Look up one invoice and its customer, including days overdue, days since the "
            "customer was last contacted and how many times they were contacted this week. "
            "Read-only. Call this before any follow-up so you never invent invoice details."
        ),
        input_schema=_LOOKUP_INPUT,
        output_schema=_LOOKUP_OUTPUT,
        side_effect=False,
        handler=handler,
    )


def make_record_extension(book: Book, graph: Any) -> ToolDefinition:
    """Side-effecting: move a due date and publish the new terms.

    Two graph writes, and the provenance split is the point:

      * the new due date is published `verified`, sourced from the ledger — it is
        now a recorded fact other Helios apps must forecast against;
      * the account of what the customer said is published as an `observation`,
        because that is an agent's claim about a conversation, not a system of
        record. `KnowledgeGraph.add_observation` refuses to overwrite a verified
        node, so a claim can never displace a fact.
    """

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        invoice_id = str(args["invoice_id"])
        days = int(args["extension_days"])
        note = str(args["terms_note"])

        invoice = book.get_invoice(invoice_id)
        if invoice is None:
            raise ValueError(f"unknown invoice: {invoice_id!r}")
        previous_due = invoice.get("due_date")

        updated = book.agree_extension(invoice_id, days, note=note)
        if updated is None:
            raise ValueError(f"cannot extend invoice {invoice_id!r}: no usable due date")

        node_id = None
        if graph is not None:
            verified = graph.upsert_verified(
                "invoice", invoice_id,
                {"due_date": updated.get("due_date"),
                 "previous_due_date": previous_due,
                 "status": updated.get("status"),
                 "extension_days": days},
                source=config.GRAPH_SOURCE)
            node_id = verified["node_id"]
            # The negotiation itself is the agent's account of a conversation.
            graph.add_observation(
                "negotiation", invoice_id,
                {"terms_note": note, "agreed_on": book.today.isoformat()},
                observer=config.GRAPH_OBSERVER)
        # The notification other apps act on: the cash planner re-projects
        # against the agreed date. The graph node is the record; this is the ping.
        event = tenant_events.publish(
            "invoice.extension_agreed", invoice=invoice_id,
            new_due=str(updated.get("due_date")), previous_due=str(previous_due),
            extension_days=days)
        return {
            "invoice_id": invoice_id,
            "extension_days": days,
            "event": event,
            "previous_due_date": str(previous_due),
            "new_due_date": str(updated.get("due_date")),
            "graph_node_id": node_id,
        }

    return ToolDefinition(
        name=RECORD_EXTENSION,
        description=(
            "Record that the customer agreed to a due-date extension, move the invoice's due "
            "date, and publish the new terms to the shared knowledge graph so other Helios "
            "apps forecast against the real date. Side effect: it changes the ledger and "
            "publishes shared state."
        ),
        input_schema=_EXTENSION_INPUT,
        output_schema=_EXTENSION_OUTPUT,
        side_effect=True,
        handler=handler,
    )


def make_record_payment(book: Book, payments: Any, graph: Any) -> ToolDefinition:
    """Side-effecting: credit inbound money and mark the invoice paid or part-paid.

    The destination account is resolved from `config.py` using the invoice's own
    customer. It is not an argument, so it cannot be chosen, guessed or substituted
    by the agent — the strongest control in this app is structural rather than a rule.

    Under-recording is handled here rather than by a limit: applying less than the
    invoice is worth is *inside* the policy ceiling, so no rule catches it. What
    stops it being invisible is that the invoice stays `part_paid` with a balance
    owed, so the shortfall remains on the dashboard instead of being quietly closed.
    """

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        invoice_id = str(args["invoice_id"])
        amount_cents = int(args["amount_cents"])
        currency = str(args["currency"])
        reference = str(args.get("reference") or "")
        if amount_cents <= 0:
            raise ValueError("amount_cents must be a positive integer")

        invoice = book.get_invoice(invoice_id)
        if invoice is None:
            raise ValueError(f"unknown invoice: {invoice_id!r}")
        customer_id = str(invoice.get("customer_id"))
        account = config.inbound_account_for(customer_id)

        record = payments.receive_payment(
            payer=customer_id, payee=account, amount_cents=amount_cents,
            currency=currency, reference=reference or invoice_id)

        updated = book.apply_payment(
            invoice_id, amount_cents, reference=reference,
            payment_id=str(record.get("payment_id", "")))
        if updated is None:
            raise ValueError(f"cannot apply payment to invoice {invoice_id!r}")

        node_id = None
        if graph is not None:
            node_id = graph.upsert_verified(
                "payment", str(record.get("payment_id")),
                {"invoice_id": invoice_id, "customer_id": customer_id,
                 "amount_cents": amount_cents, "currency": currency.upper(),
                 "reference": reference, "account": account,
                 "invoice_status": updated.get("status")},
                source=config.GRAPH_SOURCE)["node_id"]
        event = tenant_events.publish(
            "invoice.payment_applied", invoice=invoice_id, customer=customer_id,
            amount_cents=amount_cents, status=str(updated.get("status")))

        return {
            "payment_id": str(record.get("payment_id")),
            "state": str(record.get("state")),
            "invoice_id": invoice_id,
            "status": str(updated.get("status")),
            "event": event,
            "amount_cents": amount_cents,
            "balance_cents": int(updated.get("balance_cents") or 0),
            "credited_account": account,
            "graph_node_id": node_id,
        }

    return ToolDefinition(
        name=RECORD_PAYMENT,
        description=(
            "Record that money arrived from a customer against an invoice, and mark the "
            "invoice paid. Side effect: it moves the ledger. The destination account is NOT "
            "an argument — app configuration decides where inbound money lands, so this tool "
            "cannot be used to send funds somewhere else."
        ),
        input_schema=_PAYMENT_INPUT,
        output_schema=_PAYMENT_OUTPUT,
        side_effect=True,
        handler=handler,
    )


def build_tools(book: Book, bundle: Any) -> list[ToolDefinition]:
    """All five tools, wired to this app's book and the platform's services."""
    return [
        make_list_overdue(book),
        make_lookup_invoice(book),
        make_send_email(book.store),          # platform handler, reused as-is
        make_record_extension(book, bundle.graph),
        make_record_payment(book, bundle.payments, bundle.graph),
    ]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _strip(record: Mapping[str, Any]) -> dict[str, Any]:
    """A copy with the control inputs removed.

    Shallow on purpose: the withheld names are top-level fixture fields.
    """
    return {k: v for k, v in dict(record).items() if k not in WITHHELD_FROM_AGENT}


def _positive(days: int | None) -> int | None:
    """Days overdue, or None when the invoice is not late.

    A negative number would be read as "overdue by minus three weeks". None says
    plainly that this invoice is not overdue, which is also what the not_yet_due
    fact asserts to the auditor.
    """
    if days is None:
        return None
    return days if days > 0 else None


__all__ = [
    "LIST_OVERDUE", "LOOKUP_INVOICE", "RECORD_EXTENSION", "RECORD_PAYMENT",
    "SEND_EMAIL", "TOOL_NAMES", "WITHHELD_FROM_AGENT", "build_tools",
]

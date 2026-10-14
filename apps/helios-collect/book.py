"""The app's data layer: what a generated app's database would hold.

Wraps `agent_toolkit.store.Store` rather than replacing it. Two reasons:

  * `Store` is what `agent_integration`'s fact-source adapter and the platform's
    own `lookup_invoice`/`send_email` handlers read, so sharing it means the agent,
    the tools and the auditor all see one copy of the truth. A second store would
    be a second opinion about how much an invoice is for.

  * Its `*_days_ago` fixture convention resolves relative dates against the day
    the store loads, which keeps every scenario stable whenever the demo runs.

Money is `Decimal` here and integer cents at the tool boundary. Never float: the
fixtures store amounts as decimal strings for exactly that reason, and
`_to_decimal` refuses to route a value through binary floating point.

A note on mutation. `Store` exposes public mutators for contacts
(`record_contact`) and for the outbox (`record_sent_email`), but none for invoice
fields — it was written for Build 1, where invoices are only ever read. This app
has to move a due date and mark an invoice paid, so `_mutate_invoice` writes into
the store's own dict. That is reaching into a private attribute of a package this
app does not own, it is the only seam available, and it is recorded as a
limitation in README.md. It is confined to one method on purpose: if the platform
grows a public mutator, that is the single place to change.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from agent_toolkit.store import Store

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"


def _to_decimal(value: Any) -> Decimal | None:
    """Coerce to Decimal without ever going through float.

    `Decimal(0.1)` would bake in binary rounding, so a float is routed through its
    string form. Returns None for anything that is not a number — including
    `bool`, which is an `int` subclass and must not be read as 1 or 0 money.
    """
    if isinstance(value, Decimal):
        return value
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None


def _cents(value: Decimal | None) -> int | None:
    """Decimal -> integer cents, exactly. None stays None (unknown, never zero)."""
    if value is None:
        return None
    return int((value * 100).to_integral_value())


class Book:
    """The invoice book: reads for the agent, writes for settled work.

    The read methods are what the fact source and the app's own tools use. The
    distinction that matters is not read vs write but *who may write*: the agent
    writes only by proposing a gated tool call, and the mutation happens inside
    that tool's handler after the auditor allowed it.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    @classmethod
    def load(cls, data_dir: Path | str = DATA_DIR, *, today: date | None = None) -> "Book":
        data_dir = Path(data_dir)
        store = Store.from_files(
            data_dir / "customers.json", data_dir / "invoices.json", today=today)
        return cls(store)

    # -- reads -------------------------------------------------------------

    @property
    def today(self) -> date:
        return self.store.today

    def invoice_ids(self) -> list[str]:
        """Every invoice id. Order follows the fixture file, not the clock."""
        return [iid for iid in self._invoices_raw()]

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        return self.store.get_invoice(invoice_id)

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        return self.store.get_customer(customer_id)

    def _invoices_raw(self) -> Iterable[str]:
        # Store keeps invoices in a private dict and exposes no iterator. Read
        # through the same seam _mutate_invoice writes through, so there is one
        # place that knows about it.
        return list(getattr(self.store, "_invoices", {}))

    def amount_due(self, invoice_id: str) -> Decimal | None:
        invoice = self.store.get_invoice(invoice_id)
        return _to_decimal(invoice.get("amount")) if invoice else None

    def amount_due_cents(self, invoice_id: str) -> int | None:
        return _cents(self.amount_due(invoice_id))

    def days_overdue(self, invoice_id: str) -> int | None:
        """Positive when late, negative when not yet due, None when unknown."""
        invoice = self.store.get_invoice(invoice_id)
        if not invoice:
            return None
        return self.store.days_since(invoice.get("due_date"))

    def is_not_yet_due(self, invoice_id: str) -> bool | None:
        """True when the invoice has not fallen due.

        Computed from the store's own clock. This is the fact behind
        `app.not_yet_due`, and it is why an agent cannot chase a customer early by
        describing the invoice as overdue: the comparison never reads its words.
        """
        days = self.days_overdue(invoice_id)
        return None if days is None else days < 0

    def is_disputed(self, invoice_id: str) -> bool | None:
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            return None
        value = invoice.get("disputed")
        return bool(value) if isinstance(value, bool) else None

    def customer_id_for(self, invoice_id: str) -> str | None:
        invoice = self.store.get_invoice(invoice_id)
        if not invoice:
            return None
        customer_id = invoice.get("customer_id")
        return str(customer_id) if customer_id else None

    def credit_limit_cents(self, customer_id: str) -> int | None:
        customer = self.store.get_customer(customer_id)
        if not customer:
            return None
        return _cents(_to_decimal(customer.get("credit_limit")))

    def outstanding_cents(self, customer_id: str) -> int | None:
        """Everything this customer owes that is not yet paid, in integer cents.

        Returns None (not 0) when the customer is unknown: an unresolvable fact
        must fail closed, and 0 would read as "no exposure" and allow an extension
        the policy should have held.
        """
        if self.store.get_customer(customer_id) is None:
            return None
        total = Decimal(0)
        for invoice_id in self._invoices_raw():
            invoice = self.store.get_invoice(invoice_id)
            if not invoice or str(invoice.get("customer_id")) != customer_id:
                continue
            if str(invoice.get("status", "")).lower() in {"paid", "settled", "void"}:
                continue
            amount = _to_decimal(invoice.get("amount"))
            if amount is None:
                return None  # an unparseable amount is not a zero balance
            total += amount
        return _cents(total)

    def overdue_invoices(self) -> list[dict[str, Any]]:
        """Unpaid invoices that are actually overdue, oldest first."""
        rows: list[dict[str, Any]] = []
        for invoice_id in self._invoices_raw():
            invoice = self.store.get_invoice(invoice_id)
            if not invoice:
                continue
            if str(invoice.get("status", "")).lower() in {"paid", "settled", "void"}:
                continue
            days = self.store.days_since(invoice.get("due_date"))
            if days is None or days <= 0:
                continue
            customer = self.store.get_customer(str(invoice.get("customer_id"))) or {}
            rows.append({
                "invoice_id": invoice_id,
                "number": invoice.get("number"),
                "customer_id": invoice.get("customer_id"),
                "customer_name": customer.get("name"),
                "amount": str(invoice.get("amount")),
                "currency": invoice.get("currency"),
                "due_date": invoice.get("due_date"),
                "days_overdue": days,
            })
        rows.sort(key=lambda r: (-int(r["days_overdue"]), str(r["invoice_id"])))
        return rows

    def contact_dates(self, customer_id: str) -> list[date]:
        """Every known contact date for a customer, oldest first.

        Seeded from two fixture fields: `last_contacted` (which the platform store
        models) and `contact_history_days` (this app's own list of days ago). The
        second exists because a frequency rule counts contacts *inside a window*,
        and a single last-contact date cannot express "twice this week".
        """
        customer = self.store.get_customer(customer_id)
        if not customer:
            return []
        dates: list[date] = []
        raw = customer.get("last_contacted")
        if isinstance(raw, str) and raw:
            try:
                dates.append(date.fromisoformat(raw))
            except ValueError:
                pass  # an unparseable date is no date; never invent a contact
        history = customer.get("contact_history_days")
        if isinstance(history, (list, tuple)):
            for item in history:
                if isinstance(item, bool) or not isinstance(item, int):
                    continue
                dates.append(self.today - timedelta(days=item))
        dates.sort()
        return dates

    # -- writes: only reachable through an allowed tool handler ------------

    def _mutate_invoice(self, invoice_id: str, **fields: Any) -> dict[str, Any] | None:
        """Apply a change to one invoice. Returns the updated record.

        See the module docstring: `Store` has no public invoice mutator, so this
        writes into its own dict. Confined to this one method.
        """
        raw = getattr(self.store, "_invoices", None)
        if not isinstance(raw, dict) or invoice_id not in raw:
            return None
        raw[invoice_id].update(fields)
        return self.store.get_invoice(invoice_id)

    def agree_extension(self, invoice_id: str, days: int, *, note: str = "") -> dict[str, Any] | None:
        """Move the due date out by `days` and record the agreed terms.

        Returns the new due date and the previous one, because the audit log and
        the owner's view should both show what changed rather than only the result.
        """
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            return None
        previous = invoice.get("due_date")
        if not isinstance(previous, str) or not previous:
            return None
        new_due = (date.fromisoformat(previous) + timedelta(days=int(days))).isoformat()
        extensions = list(invoice.get("extensions") or [])
        extensions.append({
            "days": int(days),
            "agreed_on": self.today.isoformat(),
            "previous_due_date": previous,
            "new_due_date": new_due,
            "note": note,
        })
        return self._mutate_invoice(
            invoice_id, due_date=new_due, extensions=extensions,
            status="extension_agreed")

    def apply_payment(self, invoice_id: str, amount_cents: int, *,
                      reference: str = "", payment_id: str = "") -> dict[str, Any] | None:
        """Mark an invoice part-paid or paid, with the amount that actually landed.

        The status distinction is the control against under-recording. Applying
        less than the invoice is worth is *inside* the policy ceiling, so no rule
        stops it — what stops it being invisible is that the invoice stays
        `part_paid` and the balance owed stays on the dashboard.
        """
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            return None
        owed = self.amount_due_cents(invoice_id)
        if owed is None:
            return None
        received = list(invoice.get("payments_received") or [])
        received.append({
            "amount_cents": int(amount_cents),
            "reference": reference,
            "payment_id": payment_id,
            "received_on": self.today.isoformat(),
        })
        total = sum(int(p.get("amount_cents") or 0) for p in received)
        status = "paid" if total >= owed else "part_paid"
        return self._mutate_invoice(
            invoice_id, status=status, payments_received=received,
            balance_cents=max(owed - total, 0))

    def snapshot(self) -> dict[str, Any]:
        """The whole book, for the dashboard page."""
        rows = []
        for invoice_id in self._invoices_raw():
            invoice = self.store.get_invoice(invoice_id)
            if not invoice:
                continue
            customer = self.store.get_customer(str(invoice.get("customer_id"))) or {}
            days = self.store.days_since(invoice.get("due_date"))
            rows.append({
                "invoice_id": invoice_id,
                "number": invoice.get("number"),
                "customer": customer.get("name"),
                "amount": str(invoice.get("amount")),
                "currency": invoice.get("currency"),
                "due_date": invoice.get("due_date"),
                "status": invoice.get("status"),
                "days_overdue": days,
                "balance_cents": invoice.get("balance_cents"),
            })
        rows.sort(key=lambda r: str(r["invoice_id"]))
        return {"today": self.today.isoformat(), "invoices": rows}

    def to_json(self) -> str:
        return json.dumps(self.snapshot(), indent=2, default=str)


__all__ = ["Book", "DATA_DIR", "_cents", "_to_decimal"]

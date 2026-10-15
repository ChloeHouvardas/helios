"""Trusted facts for the audit agent — the single most important module in this app.

This is where "facts are not claims" stops being a slogan. The audit agent
resolves every value a rule depends on through this object. It never reads the
action agent's arguments for those values: the agent gets to *name* which invoice
it means, and nothing more.

Concretely, an agent that:

  * claims a 20,000.00 payment arrived on a 12,500.00 invoice is held, because the
    ceiling is `invoice_amount_cents` resolved here;
  * describes an invoice as overdue when it is not is denied, because `not_yet_due`
    comes from the store's own clock;
  * omits the fact that a customer is disputing the work is still denied, because
    `invoice_disputed` is read from the record;
  * calls a customer untouched to get past the frequency rule is denied, because
    the count comes from recorded contact history;
  * supplies `resolved_facts={"invoice_amount_cents": 1}` changes nothing, because
    `AuditAgent._facts_for_action` deliberately does not merge request-supplied
    values into the facts.

Every test of those claims is in tests/test_controls.py.

Two invariants this class must not break:

1. **Unknown is None, never zero.** A missing fact is the auditor not knowing
   something it needs, which fails closed and denies. Returning 0 for an unknown
   balance would read as "no exposure" and quietly turn a fail-closed control into
   a fail-open one.

2. **Nothing here may consult the request.** Only a subject id arrives. If a method
   accepted arguments, every claim above would become a fact. There is a test that
   asserts no public method has an `arguments` or `request` parameter.

Satisfies `audit_agent.facts.FactSource`. It is app-specific rather than a reuse of
`agent_integration.HeliosStoreFactSource` because this app's policy needs facts that
adapter does not supply — due-ness, disputes, credit exposure, and cents-denominated
amounts. Where the two overlap the semantics are kept identical, so swapping one for
the other is a wiring change rather than a policy change.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from book import Book


class CollectFactSource:
    """Resolves trusted facts for helios-collect's audit rules.

    Subjects arrive as invoice ids or customer ids. Amounts belong to *invoices*
    and credit facts belong to *customers*, so each method resolves the subject it
    was handed and returns None when the subject is the wrong kind — that keeps
    exactly one trusted amount in scope for a call naming both an invoice and a
    customer, which is what lets policy say `fact: amount_due` without knowing the
    subject identifier.
    """

    def __init__(self, book: Book, *, contact_window_days: int = 7) -> None:
        self._book = book
        self.contact_window_days = contact_window_days
        #: customer_id -> contact dates, oldest first. Seeded lazily from the
        #: book and extended only by `record_contact`, which the runtime calls
        #: after an execution that actually happened.
        self._contacts: dict[str, list[date]] = {}
        self._seeded: set[str] = set()

    # -- contact history --------------------------------------------------
    #
    # `GatedAgentRuntime._execute` calls `fact_source.record_contact(customer_id,
    # store.today)` after a successful execution of any tool whose
    # ToolDefinition sets `records_contact=True` (the platform's `send_email`,
    # which this app reuses as-is, does). It does so OUTSIDE its try/except — so
    # a fact source without this method takes the whole run down with an
    # AttributeError. Implementing it is not optional for an app that reuses
    # that runtime.

    def _seed(self, customer_id: str) -> bool:
        """Load a customer's recorded contact dates once. True when known."""
        if customer_id in self._seeded:
            return customer_id in self._contacts
        self._seeded.add(customer_id)
        if self._book.get_customer(customer_id) is None:
            return False
        self._contacts[customer_id] = list(self._book.contact_dates(customer_id))
        return True

    def record_contact(self, customer_id: str, when: date | None = None) -> None:
        """Note that a contact really happened.

        Called by the runtime after an approved execution — never before, and never
        on the agent's say-so. That asymmetry is the whole point of a fact source:
        the agent cannot lower its own contact count by describing the customer as
        untouched, and cannot raise it either.

        Deduplicated by date, matching `HeliosStoreFactSource`: a resumed run must
        not inflate the count and thereby deny a follow-up that is legitimate.
        """
        when = when or self._book.today
        self._seed(customer_id)
        history = self._contacts.setdefault(customer_id, [])
        if when not in history:
            history.append(when)
            history.sort()

    # -- FactSource protocol ----------------------------------------------

    def amount_for(self, subject_id: str) -> Decimal | None:
        """The recorded invoice amount. None for a customer id, by design."""
        return self._book.amount_due(subject_id)

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        """Contacts with this customer inside a trailing window.

        Called with an invoice id, it counts contacts with the customer that
        invoice belongs to — which is the business question a frequency limit is
        actually asking ("have we been in touch with this customer too often"),
        not the narrower one about a single invoice.

        Returns None for an unknown subject, so the rule denies rather than
        treating "never contacted" as a licence to contact.
        """
        customer_id = self._customer_of(subject_id)
        if customer_id is None:
            return None
        # An unknown customer is "no fact", which denies. Seeding returns False for
        # one the store has never heard of, so a fabricated customer id cannot be
        # read as a customer with zero contacts.
        if not self._seed(customer_id):
            return None
        cutoff = self._book.today - timedelta(days=int(within_days))
        return sum(1 for when in self._contacts[customer_id] if when >= cutoff)

    def counterparty_for(self, subject_id: str) -> str | None:
        """The customer's name, for owner-facing messages and the audit log."""
        customer_id = self._customer_of(subject_id)
        if customer_id is None:
            return None
        customer = self._book.get_customer(customer_id) or {}
        return str(customer.get("name") or customer_id)

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        """Not modelled by this app.

        `None` means "no trusted answer", which is honest. This is the *payables*
        fraud signal (a supplier whose bank details just changed); collections has
        no equivalent, and helios-collect declares no rule that reads it. Returning
        `False` would assert a fact nobody recorded.
        """
        return None

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        """App-specific signals, keyed by the names the manifest's rules use.

        These keys are the contract with agents/helios-collect-audit.yaml. A rule
        naming a key that is not supplied here fails closed and denies everything,
        which is safe but looks like a broken app at demo time — so this mapping is
        asserted against the manifest in tests/test_controls.py.
        """
        facts: dict[str, Any] = {}

        invoice = self._book.get_invoice(subject_id)
        if invoice is not None:
            amount = self._book.amount_due(subject_id)
            cents = self._book.amount_due_cents(subject_id)
            if amount is not None:
                # Both names: a manifest may bind `fact: amount_due` or use
                # `max_from_fact: invoice_amount_cents`, and lookup is verbatim.
                facts["amount_due"] = amount
                facts["invoice_amount"] = amount
            if cents is not None:
                facts["invoice_amount_cents"] = cents

            currency = invoice.get("currency")
            if isinstance(currency, str):
                facts["currency"] = currency

            days = self._book.days_overdue(subject_id)
            if days is not None:
                facts["days_overdue"] = max(days, 0)

            # `not_yet_due` drives app.not_yet_due. Computed from the store's
            # clock, so an agent cannot claim an invoice is late when it is not.
            not_yet = self._book.is_not_yet_due(subject_id)
            if not_yet is not None:
                facts["not_yet_due"] = not_yet

            # `invoice_disputed` drives app.disputed_invoice,
            # app.disputed_extension and app.disputed_payment.
            disputed = self._book.is_disputed(subject_id)
            if disputed is not None:
                facts["invoice_disputed"] = disputed

            status = invoice.get("status")
            if isinstance(status, str):
                facts["invoice_status"] = status

        # Credit facts belong to the customer, but are also reported when the
        # subject is one of that customer's invoices. FactResolver namespaces
        # extras per subject and keeps the first bare key, so resolving them from
        # an invoice subject is what lets app.extension_over_credit_limit fire on a
        # record_agreed_extension call, which names only an invoice.
        customer_id = self._customer_of(subject_id)
        if customer_id is not None:
            facts["customer_id"] = customer_id
            limit = self._book.credit_limit_cents(customer_id)
            if limit is not None:
                facts["credit_limit_cents"] = limit
            outstanding = self._book.outstanding_cents(customer_id)
            if outstanding is not None:
                facts["outstanding_cents"] = outstanding

        return facts

    # -- internal ---------------------------------------------------------

    def _customer_of(self, subject_id: str) -> str | None:
        """Resolve any subject id to the customer it concerns, or None."""
        invoice = self._book.get_invoice(subject_id)
        if invoice is not None:
            return self._book.customer_id_for(subject_id)
        if self._book.get_customer(subject_id) is not None:
            return str(subject_id)
        return None


__all__ = ["CollectFactSource"]

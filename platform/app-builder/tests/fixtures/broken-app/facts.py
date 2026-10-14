"""The broken app's fact source — correct in shape, missing one required key.

This file imports cleanly and implements the whole `FactSource` protocol, which
matters: a facts.py that failed to import would degrade the fact-coverage check to
a warning, and the fixture would not be testing the check at all.

The single defect is an omission. `app.needs_missing_fact` binds
`max_from_fact: invoice_amount_cents`, and that key is never produced below — so
the rule fails closed and denies every call it covers. This is the most common way
a generated app's policy silently stops working, and it fails in the safe
direction, which is exactly why it survives until the demo.

Note the discipline this file keeps even while being wrong on purpose: unknown
subjects return `None`, never zero. Returning 0 for an unknown balance would read
as "no exposure" and quietly turn a fail-closed control into a fail-open one.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any


class BrokenFactSource:
    """Resolves trusted facts for the broken-app fixture."""

    def __init__(self, *, today: date | None = None) -> None:
        self.today = today or date(2026, 10, 15)
        self._contacts: dict[str, list[date]] = {}

    def record_contact(self, customer_id: str, when: date | None = None) -> None:
        """Note that a contact really happened. Called by the runtime after an
        approved execution — never before, and never on the agent's say-so."""
        when = when or self.today
        history = self._contacts.setdefault(customer_id, [])
        if when not in history:
            history.append(when)
            history.sort()

    def amount_for(self, subject_id: str) -> Decimal | None:
        return None  # no data layer in a fixture

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        return None

    def counterparty_for(self, subject_id: str) -> str | None:
        return None

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        return None

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        """Produces several keys — but NOT `invoice_amount_cents`.

        Deliberate: a fact source that produced nothing at all would make the
        missing-key check trivially true, and would not prove that the check
        distinguishes a supplied key from an unsupplied one.
        """
        facts: dict[str, Any] = {}
        facts["amount_due"] = Decimal("100.00")
        facts["invoice_status"] = "open"
        facts["days_overdue"] = 3
        facts["not_yet_due"] = False
        return facts


__all__ = ["BrokenFactSource"]

"""Trusted facts for the audit agent, resolved from this app's own book.

The auditor never reads the agent's arguments for a value a rule depends on.
It asks this object. Here the values that matter are the FORECAST: whether a
week is short, by how much, what the projected minimum balance is, and what the
credit facility has left. All of them come from `Book`, which computes the
projection from the records and the platform's forecast engine. The agent may
name a week; it never gets to state what that week looks like.

Subjects
--------
The resolver keys on `invoice_id, bill_id, customer_id, supplier_id, subject_id,
payment_id`. This app uses:

* `subject_id` = a horizon week (`W6`) for warnings and recommendations, or
  the literal `owner` for the digest's contact count;
* `invoice_id` = a receivable (a follow-up names the invoice; the customer's
  dispute flag is derived from it, so the agent cannot omit the customer);
* `bill_id` = a payable (a reschedule names the bill; whether it is payroll is
  derived here);
* `customer_id` = a customer.

Every method returns `None` for a subject it cannot resolve, never a default:
an unresolvable fact makes a rule fail closed, which is the right outcome for
an invented week or an unknown bill.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from book import Book

import config


class CashPlanFactSource:
    """Satisfies `audit_agent.facts.FactSource`."""

    def __init__(self, book: Book) -> None:
        self.book = book

    # -- subject kinds ---------------------------------------------------------

    def _is_week(self, subject_id: str) -> bool:
        return self.book.week_bounds(subject_id) is not None

    def _is_receivable(self, subject_id: str) -> bool:
        return self.book.receivable(subject_id) is not None

    def _is_payable(self, subject_id: str) -> bool:
        return self.book.payable(subject_id) is not None

    def _is_customer(self, subject_id: str) -> bool:
        return self.book.customer(subject_id) is not None

    def _known(self, subject_id: str) -> bool:
        return (subject_id == "owner" or self._is_week(subject_id)
                or self._is_receivable(subject_id) or self._is_payable(subject_id)
                or self._is_customer(subject_id))

    # -- FactSource protocol -------------------------------------------------------

    def amount_for(self, subject_id: str) -> Decimal | None:
        if self._is_receivable(subject_id):
            return config.cents_to_decimal(self.book.receivable(subject_id)["amount_cents"])
        if self._is_payable(subject_id):
            return config.cents_to_decimal(self.book.payable(subject_id)["amount_cents"])
        if self._is_week(subject_id):
            week = self.book.week(subject_id) or {}
            return config.cents_to_decimal(week.get("shortfall_cents", 0))
        return None

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        """Digests sent to the owner inside the window. Only the owner is contacted."""
        if subject_id == "owner":
            return self.book.digest_count(int(within_days))
        if self._known(subject_id):
            return 0
        return None

    def counterparty_for(self, subject_id: str) -> str | None:
        if subject_id == "owner":
            return "owner"
        if self._is_receivable(subject_id):
            return str(self.book.receivable(subject_id).get("customer_id"))
        if self._is_customer(subject_id):
            return subject_id
        return None

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        # This app moves no money, so no payee can change. Known subjects answer
        # False; unknown ones stay None so a rule bound on it would fail closed.
        return False if self._known(subject_id) else None

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        if self._is_week(subject_id):
            facts.update(self._week_facts(subject_id))
        elif self._is_receivable(subject_id):
            facts.update(self._receivable_facts(subject_id))
        elif self._is_payable(subject_id):
            facts.update(self._payable_facts(subject_id))
        elif self._is_customer(subject_id):
            facts.update(self._customer_facts(subject_id))
        elif subject_id == "owner":
            facts["digests_this_week"] = self.book.digest_count(config.DIGEST_WINDOW_DAYS)
        return facts

    # -- per subject kind -----------------------------------------------------------

    def _week_facts(self, week_id: str) -> dict[str, Any]:
        """The forecast as facts. The whole reason this app is governable."""
        week = self.book.week(week_id) or {}
        short = bool(week.get("short"))
        return {
            "week": week_id,
            "week_is_short": short,
            "week_not_short": not short,
            "projected_min_balance_cents": int(week.get("min_balance_cents", 0)),
            "projected_shortfall_cents": int(week.get("shortfall_cents", 0)),
            "week_has_payroll": bool(week.get("payroll")),
            "credit_line_headroom_cents": self.book.credit_line_headroom_cents(),
            # A draw is borrowing, always. The rule that holds every draw binds
            # on this constant fact rather than on an argument the agent writes.
            "credit_draw_is_borrowing": True,
            "cash_buffer_cents": config.CASH_BUFFER_CENTS,
        }

    def _receivable_facts(self, invoice_id: str) -> dict[str, Any]:
        rec = self.book.receivable(invoice_id) or {}
        customer_id = str(rec.get("customer_id") or "")
        facts = {
            "invoice_id": invoice_id,
            "invoice_amount_cents": int(rec.get("amount_cents", 0)),
            "invoice_status": str(rec.get("status", "")),
            "invoice_due_date": str(rec.get("due_date", "")),
            "customer_id": customer_id,
        }
        if customer_id:
            facts.update(self._customer_facts(customer_id))
        return facts

    def _customer_facts(self, customer_id: str) -> dict[str, Any]:
        record = self.book.customer(customer_id) or {}
        return {
            "customer_disputed": bool(record.get("disputed")),
            "customer_name": str(record.get("name", "")),
            "customer_segment": str(record.get("segment", "")),
        }

    def _payable_facts(self, bill_id: str) -> dict[str, Any]:
        pay = self.book.payable(bill_id) or {}
        kind = str(pay.get("kind", ""))
        return {
            "bill_id": bill_id,
            "bill_amount_cents": int(pay.get("amount_cents", 0)),
            "bill_kind": kind,
            "bill_is_payroll": kind == "payroll",
            "bill_not_reschedulable": not bool(pay.get("reschedulable")),
            "bill_due_date": str(pay.get("due_date", "")),
        }

    def record_contact(self, subject_id: str, when: date | None = None) -> None:
        if subject_id == "owner":
            self.book.record_digest(when)

"""Trusted facts for the audit agent, resolved from this app's own book.

This is the single most important file in the app, and the reason is narrow and
concrete: **the auditor never reads the agent's arguments for a value a rule
depends on.** It asks this object. The agent gets to *name* a bill; it never gets
to *state* what that bill says.

What that buys, per rule
------------------------
* `app.large_payment_approval` binds on the FACT `bill_amount_cents`. An agent
  that claims a 30,000.00 bill is 9,000.00 — to slip under the 25,000.00
  threshold — is still held, because the number the rule compares was read here.
  This is the "lying agent" scenario, and it is the one that makes the slogan
  observable rather than decorative.
* `app.payee_details_changed` is a fact, so an agent cannot omit it. There is no
  argument that expresses "this supplier's bank details are fine".
* `app.unusual_amount` compares the bill against a ceiling derived here from the
  supplier's own history, so "this is normal for them" is not something the agent
  can assert.
* `app.insufficient_funds` reads the live balance, so an agent cannot claim funds
  exist.

Fail-closed discipline
----------------------
Every method returns `None` (not `0`, not `False`, not a guess) when it cannot
resolve a value. That matters because of how the policy engine treats `None`:

* an unresolvable *ceiling* makes the limit `unverifiable`, which DENIES
  (`policy.py:1100`);
* an unresolvable *contact count* DENIES (`policy.py:1049`).

So a missing fact refuses the action. The dangerous alternative — defaulting an
unknown amount to 0 or an unknown contact count to 0 — would turn every gap in
the data into permission.

Subject resolution
------------------
The resolver calls these methods with whatever subject ids it found in the
request, drawn from `FactResolver.SUBJECT_KEYS` (`invoice_id, bill_id,
customer_id, supplier_id, subject_id, payment_id`). For this app a subject is
either a BILL or a SUPPLIER, and both must answer, because a call may name only
one. `_resolve()` below does the disambiguation: given a bill it also derives the
supplier's facts, so `pay_bill` needs only `bill_id` and every supplier-scoped
rule still resolves.

Extras are namespaced `key@subject` and also written bare (first subject wins,
`facts.py:339`). Because `bill_id` precedes `supplier_id` in `SUBJECT_KEYS`, a
call naming both resolves bill facts first — which is what the rules expect.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from book import Book

import config


class PayablesFactSource:
    """Satisfies `audit_agent.facts.FactSource`."""

    def __init__(self, book: Book, *, contact_window_days: int | None = None) -> None:
        self.book = book
        self.contact_window_days = (
            config.SUPPLIER_CONTACT_WINDOW_DAYS
            if contact_window_days is None
            else int(contact_window_days)
        )

    # ------------------------------------------------------------------
    # Subject disambiguation
    # ------------------------------------------------------------------

    def _is_bill(self, subject_id: str) -> bool:
        return self.book.bill(subject_id) is not None

    def _is_supplier(self, subject_id: str) -> bool:
        return self.book.supplier(subject_id) is not None

    def _supplier_for(self, subject_id: str) -> str | None:
        """The supplier behind a subject, whether it is a bill or a supplier."""
        if self._is_bill(subject_id):
            return self.book.supplier_id_for(subject_id)
        if self._is_supplier(subject_id):
            return subject_id
        return None

    # ------------------------------------------------------------------
    # FactSource protocol
    # ------------------------------------------------------------------

    def amount_for(self, subject_id: str) -> Decimal | None:
        """The trusted monetary amount for a subject, as a Decimal.

        For a bill this is its recorded amount. For a supplier it is the average
        bill, which is the only amount a supplier itself has. Returning `None` for
        an unknown subject is deliberate: the resolver then has no amount, the
        amount-bound limit becomes unverifiable, and the call is denied.
        """
        if self._is_bill(subject_id):
            cents = self.book.amount_cents(subject_id)
            return None if cents is None else config.cents_to_decimal(cents)
        if self._is_supplier(subject_id):
            cents = self.book.avg_bill_cents(subject_id)
            return None if cents is None else config.cents_to_decimal(cents)
        return None

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        """How many times this supplier was contacted in the trailing window.

        `None` when the subject cannot be resolved at all, which denies. A supplier
        with no recorded history legitimately returns 0 — that is a known fact
        (nobody has been contacted), not an unknown one, and the distinction is
        the whole reason the protocol allows `None`.
        """
        supplier_id = self._supplier_for(subject_id)
        if supplier_id is None:
            return None
        return self.book.contact_count(supplier_id, int(within_days))

    def counterparty_for(self, subject_id: str) -> str | None:
        """The counterparty identity a contact-frequency limit applies to."""
        return self._supplier_for(subject_id)

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        """Whether this supplier's payee details changed inside the deny window.

        The fraud signal. Resolved through `Book.payment_details_changed`, which
        derives it from the change date and the configured window rather than
        trusting a stored boolean — so re-verifying the supplier in the settings
        page is what lifts a denial, and nothing an agent sends can.

        `None` only for a subject that is neither a known bill nor a known
        supplier. A known supplier with no change record returns `False`.
        """
        supplier_id = self._supplier_for(subject_id)
        if supplier_id is None:
            return None
        return self.book.payment_details_changed(supplier_id)

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        """Every other value a rule depends on, as one dict.

        Keys are written in integer cents wherever they are money, because the
        ceilings they are compared against are cents too. Mixing a cents argument
        with a decimal ceiling would exceed on every single call — the exact bug
        `helios-collect`'s `app.extension_over_credit_limit` comment warns about.

        A bill subject returns its own facts AND its supplier's, so a call that
        names only `bill_id` still resolves every supplier-scoped rule. That is
        why `pay_bill` does not need a `supplier_id` argument the agent could get
        wrong (or omit).
        """
        facts: dict[str, Any] = {}

        if self._is_bill(subject_id):
            bill_amount = self.book.amount_cents(subject_id)
            supplier_id = self.book.supplier_id_for(subject_id) or ""

            # --- the bill's own facts ---
            facts["bill_amount_cents"] = bill_amount
            facts["bill_id"] = subject_id
            facts["bill_status"] = self.book.status(subject_id)
            facts["bill_currency"] = self.book.currency(subject_id) or ""
            facts["bill_reference"] = self.book.reference(subject_id)
            facts["supplier_id"] = supplier_id

            # Currency: whether the operating account can settle this bill at all.
            # The account holds exactly one currency and there is no FX desk here,
            # so a bill in any other currency cannot be paid, full stop.
            facts["currency_unsupported"] = not self.book.supports_currency(
                self.book.currency(subject_id))

            # Duplicate evidence. The rule HOLDS on this rather than denying: two
            # genuine bills with the same amount and PO reference do exist, and
            # only the owner can tell which case this is.
            duplicate = self.book.duplicate_of(subject_id)
            facts["duplicate_bill"] = bool(duplicate)
            facts["duplicate_of"] = duplicate or ""

            # Funds. Denied rather than held, because there is no owner decision
            # that makes an unaffordable payment safe — the correct response is to
            # reschedule, which is a different tool the agent may propose freely.
            insufficient = self.book.insufficient_funds(subject_id)
            facts["insufficient_funds"] = bool(insufficient)
            facts["shortfall_cents"] = self.book.shortfall_cents(subject_id)
            facts["account_balance_cents"] = self.book.balance_cents()

            # Run total, including this bill. The cap is on what one
            # authorisation spends, so the comparison has to include the payment
            # being proposed, not just what has already settled.
            paid_so_far = self.book.paid_this_run_cents
            facts["paid_this_run_cents"] = paid_so_far
            facts["run_total_after_payment_cents"] = (
                paid_so_far + int(bill_amount or 0))
            facts["run_total_cap_cents"] = config.RUN_TOTAL_CAP_CENTS

            # Credit-line facility. The headroom is the ceiling a draw is measured
            # against, resolved from configuration minus what is already drawn, so
            # an agent cannot enlarge its own facility by asking for more.
            facts["credit_line_headroom_cents"] = self.book.credit_line_headroom_cents()
            facts["credit_line_drawn_cents"] = self.book.credit_line_drawn_cents

            # --- the supplier's facts, derived from the bill ---
            if supplier_id:
                facts.update(self._supplier_facts(supplier_id))

        elif self._is_supplier(subject_id):
            facts.update(self._supplier_facts(subject_id))
            facts["supplier_id"] = subject_id

        return facts

    def _supplier_facts(self, supplier_id: str) -> dict[str, Any]:
        """Supplier-scoped facts. Returned for a supplier, and for its bills."""
        ceiling = self.book.amount_ceiling_cents(supplier_id)
        return {
            # `None` stays `None`: an unknown average must make the unusual-amount
            # ceiling unverifiable (deny), not silently permit any amount.
            "supplier_amount_ceiling_cents": ceiling,
            "supplier_avg_bill_cents": self.book.avg_bill_cents(supplier_id),
            "supplier_name": self.book.supplier_name(supplier_id),
            "payment_details_changed": bool(
                self.book.payment_details_changed(supplier_id)),
            "details_changed_days_ago": self.book.details_changed_days_ago(supplier_id),
            "supplier_is_listed": bool(
                (self.book.supplier(supplier_id) or {}).get("public_ticker")),
        }

    # ------------------------------------------------------------------
    # Contact bookkeeping
    # ------------------------------------------------------------------

    def record_contact(self, subject_id: str, when: date | None = None) -> None:
        """Record a contact against the supplier behind a subject.

        The platform runtime calls this only for a tool named exactly
        `send_email` (`agent_integration/runtime.py:611`). This app's tools are
        `send_remittance` and `escalate_to_owner`, so the tool handlers in
        `tools.py` call it themselves — and this method is here so the runtime's
        call, if it ever does fire, lands in the same place instead of raising
        `AttributeError` and killing the run.
        """
        supplier_id = self._supplier_for(subject_id)
        if supplier_id is None:
            return
        self.book.record_supplier_contact(supplier_id, when)

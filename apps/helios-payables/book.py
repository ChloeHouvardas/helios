"""The bill book: this app's own data layer over the platform store.

Why this file exists rather than using `agent_toolkit.store.Store` directly
--------------------------------------------------------------------------
The platform store is shaped for receivables — it has `customers` and `invoices`,
and its mutators understand extension days and inbound payments. Payables needs
suppliers, bills, an operating account and outbound payments. Rather than fork the
package (which would put this app's semantics inside another agent's code) the app
reuses the store for what it genuinely provides and adds the rest here:

  * the store's *fixture loading* — including `_days_ago` relative-date resolution,
    which is what keeps these scenarios stable no matter what day they run;
  * the store's *contact bookkeeping* — `record_contact()` and `sent_emails`, which
    the frequency rule counts against;
  * suppliers are loaded into the store's `customers` slot and bills into its
    `invoices` slot. That is a naming mismatch, not a semantic one: both are
    "counterparty" and "document keyed to a counterparty", which is exactly the
    shape the store implements. `supplier()` and `bill()` below are the honest
    names, and nothing outside this file has to know about the mapping.

Everything money-related is integer cents. Decimal appears only at the edges where
a human reads a figure (`config.money`). Float appears nowhere: a rounding error
in a payment amount is not a cosmetic defect.

Mutation discipline
-------------------
Only two things may change a bill: `mark_paid` and `reschedule`, and both are
called only from a gated tool handler after the auditor has allowed the call.
There is no public setter, so an agent has no path to a bill's status that does
not go through the gate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agent_toolkit.store import Store
from fin_tools.forecast import forecast_query

# Flat module, matching `apps/helios-collect`: this app is imported with its own
# directory on `sys.path` (see `app.py`), not as a package. A relative import
# would fail here because there is deliberately no `__init__.py`.
import config


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        # Fail loudly. A missing fixture silently becoming an empty book would
        # make every rule unverifiable, and every unverifiable rule denies.
        raise FileNotFoundError(f"fixture not found: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class Book:
    """Suppliers, bills and the operating account, over one platform store."""

    def __init__(
        self,
        store: Store,
        *,
        accounts: Mapping[str, Mapping[str, Any]] | None = None,
        balances: Mapping[str, int] | None = None,
    ) -> None:
        self.store = store
        self._accounts: dict[str, dict[str, Any]] = {
            str(k): dict(v) for k, v in (accounts or {}).items()
        }
        # The live balance is the ledger's, not the fixture's. `fin_tools` owns
        # `world.balances`; seeding it from the fixture happens in `app.py` so
        # the gate and the ledger can never disagree about available funds.
        self._balances: dict[str, int] = dict(balances or {})
        self._paid_this_run_cents: int = 0
        self._drawn_cents: int = 0

    @classmethod
    def load(
        cls,
        data_dir: Path | str | None = None,
        *,
        today: date | None = None,
    ) -> "Book":
        root = Path(data_dir) if data_dir else config.DATA_DIR
        suppliers = _load_json(root / "suppliers.json")
        bills = _load_json(root / "bills.json")
        accounts = _load_json(root / "accounts.json") if (root / "accounts.json").exists() else {}

        # suppliers -> the store's `customers` slot, bills -> its `invoices` slot.
        store = Store(customers=suppliers, invoices=bills, today=today)

        # Relative dates are resolved for suppliers and bills by the store, but
        # not for accounts, which the store knows nothing about.
        resolved_accounts = {
            str(key): store._resolve_relative_dates(dict(record))  # noqa: SLF001
            for key, record in accounts.items()
        }
        balances = {
            str(key): int(record.get("balance_cents", 0))
            for key, record in resolved_accounts.items()
        }
        return cls(store, accounts=resolved_accounts, balances=balances)

    # -- identity ----------------------------------------------------------

    @property
    def today(self) -> date:
        return self.store.today

    # -- suppliers ---------------------------------------------------------

    def supplier_ids(self) -> list[str]:
        return sorted(self.store._customers)  # noqa: SLF001

    def supplier(self, supplier_id: str) -> dict[str, Any] | None:
        return self.store.get_customer(supplier_id)

    def require_supplier(self, supplier_id: str) -> dict[str, Any]:
        supplier = self.supplier(supplier_id)
        if supplier is None:
            raise KeyError(
                f"unknown supplier '{supplier_id}'; expected one of "
                f"{', '.join(self.supplier_ids())}"
            )
        return supplier

    def supplier_id_for(self, bill_id: str) -> str | None:
        bill = self.bill(bill_id)
        value = (bill or {}).get("supplier_id")
        return str(value) if value else None

    def supplier_name(self, supplier_id: str) -> str:
        supplier = self.supplier(supplier_id) or {}
        return str(supplier.get("name") or supplier_id)

    def supplier_email(self, supplier_id: str) -> str | None:
        supplier = self.supplier(supplier_id) or {}
        email = supplier.get("email")
        return str(email) if email else None

    def bank_account(self, supplier_id: str) -> str:
        supplier = self.supplier(supplier_id) or {}
        return str(supplier.get("bank_account") or "")

    def avg_bill_cents(self, supplier_id: str) -> int | None:
        """The supplier's normal bill size, in integer cents.

        `None` when the supplier has no recorded average — and `None` must stay
        `None`, because the unusual-amount rule treats an unresolvable ceiling as
        fail-closed. Substituting 0 would make every bill "unusual"; substituting
        the bill's own amount would make the rule vacuous.
        """
        supplier = self.supplier(supplier_id)
        if supplier is None:
            return None
        raw = supplier.get("avg_bill_cents")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def amount_ceiling_cents(self, supplier_id: str) -> int | None:
        """2x the supplier's average bill — the ceiling for `app.unusual_amount`."""
        average = self.avg_bill_cents(supplier_id)
        if average is None:
            return None
        return int(average) * config.UNUSUAL_AMOUNT_MULTIPLIER

    # -- payment-details change (the fraud signal) -------------------------

    def payment_details_changed(self, supplier_id: str) -> bool | None:
        """Whether this supplier's payee details changed inside the deny window.

        This is the single most important fact in the app and it is deliberately
        computed rather than read. The fixture records *that* details changed and
        *when*; whether that is still dangerous is policy, and policy lives in
        `config.PAYEE_CHANGE_DENY_WINDOW_DAYS`. Deriving the flag here means
        re-verifying a supplier in the settings page (which clears the change
        record) is what lifts the denial — not an agent's argument, and not a
        stale boolean.
        """
        supplier = self.supplier(supplier_id)
        if supplier is None:
            return None
        if supplier.get("reverified"):
            # The owner re-verified out of band. This is the only thing that
            # lifts the denial, and it is set by `reverify_supplier`, which is
            # reachable only from the settings page.
            return False
        changed_on = self._details_changed_on(supplier_id)
        if changed_on is None:
            # No change date on file. A supplier we know about and who has never
            # had a change recorded is NOT dangerous, so this is False rather than
            # None: None means "the auditor does not know", and here it does know.
            # The fallback still honours an explicit fixture flag, so a change
            # recorded without a date is not silently treated as safe.
            return bool(supplier.get("payment_details_changed"))
        days = (self.today - changed_on).days
        if days <= config.PAYEE_CHANGE_DENY_WINDOW_DAYS:
            return True
        # Outside the window: the change is old news and the supplier is payable.
        return False

    def _details_changed_on(self, supplier_id: str) -> date | None:
        """When a supplier's payee details changed, as a date.

        The fixtures use the store's `*_days_ago` convention, which
        `Store._resolve_relative_dates` turns into a field named `details_changed`
        holding an ISO date — the suffix is stripped, so `details_changed_days_ago`
        becomes `details_changed`, NOT `details_changed_date`. Reading the wrong
        name silently skips the window and denies every supplier who ever changed
        a bank account, which is why this is its own method with a comment.
        """
        supplier = self.supplier(supplier_id) or {}
        for key in ("details_changed", "details_changed_date"):
            raw = supplier.get(key)
            if not raw:
                continue
            try:
                return date.fromisoformat(str(raw))
            except ValueError:
                continue
        return None

    def details_changed_days_ago(self, supplier_id: str) -> int | None:
        changed_on = self._details_changed_on(supplier_id)
        if changed_on is None:
            return None
        return (self.today - changed_on).days

    def change_request(self, supplier_id: str) -> dict[str, Any] | None:
        supplier = self.supplier(supplier_id) or {}
        request = supplier.get("change_request")
        return dict(request) if isinstance(request, Mapping) else None

    def reverify_supplier(self, supplier_id: str, *, by: str = "owner") -> dict[str, Any]:
        """Record that the owner re-verified this supplier's payee details.

        Reachable only from the settings page. It does not delete the change
        request — the evidence stays on the audit trail — it marks the supplier
        re-verified, which is what `payment_details_changed` checks first.

        This is THE ONLY thing that lifts a `app.payee_details_changed` denial.
        Not an agent argument, not an owner approving a hold: the rule denies, and
        a denial never reaches the approval queue, so approving everything cannot
        lift it. That asymmetry is the control, and it is worth stating plainly
        because it is the thing a reviewer will ask about.

        Writes through `_mutate_supplier` rather than the dict returned by
        `supplier()`, which is a copy — mutating a copy would leave the denial in
        place while appearing to clear it.
        """
        return self._mutate_supplier(supplier_id, {
            "reverified": True,
            "reverified_at": self.today.isoformat(),
            "reverified_by": by,
            "payment_details_changed": False,
        })

    def _mutate_supplier(self, supplier_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Write supplier fields into the store's own dict.

        `Store.get_customer` returns a copy, and the store exposes no supplier
        mutator — it was written for Build 1, where customers are read-only. This
        reaches into a private attribute of a package this app does not own, which
        is the only seam available, so it is confined to one method and recorded as
        a limitation in README.md. If the platform grows a public mutator, this is
        the single place to change.
        """
        record = self.store._customers.get(supplier_id)  # noqa: SLF001
        if record is None:
            raise KeyError(
                f"unknown supplier '{supplier_id}'; expected one of "
                f"{', '.join(self.supplier_ids())}"
            )
        record.update(dict(changes))
        return dict(record)

    # -- bills -------------------------------------------------------------

    def bill_ids(self) -> list[str]:
        return sorted(self.store._invoices)  # noqa: SLF001

    def bill(self, bill_id: str) -> dict[str, Any] | None:
        return self.store.get_invoice(bill_id)

    def require_bill(self, bill_id: str) -> dict[str, Any]:
        bill = self.bill(bill_id)
        if bill is None:
            raise KeyError(
                f"unknown bill '{bill_id}'; expected one of {', '.join(self.bill_ids())}"
            )
        return bill

    def amount_cents(self, bill_id: str) -> int | None:
        """The recorded amount of a bill, in integer cents. Never from the agent."""
        bill = self.bill(bill_id)
        if bill is None:
            return None
        raw = bill.get("amount_cents")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def currency(self, bill_id: str) -> str | None:
        bill = self.bill(bill_id)
        value = (bill or {}).get("currency")
        return str(value) if value else None

    def reference(self, bill_id: str) -> str:
        return str((self.bill(bill_id) or {}).get("reference") or "")

    def status(self, bill_id: str) -> str:
        return str((self.bill(bill_id) or {}).get("status") or "unknown")

    def due_date(self, bill_id: str) -> date | None:
        bill = self.bill(bill_id) or {}
        raw = bill.get("due_date")
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            return None

    def scheduled_date(self, bill_id: str) -> date | None:
        bill = self.bill(bill_id) or {}
        raw = bill.get("scheduled_date") or bill.get("due_date")
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            return None

    def days_until_due(self, bill_id: str) -> int | None:
        """Negative means overdue; positive means still ahead of the due date."""
        due = self.due_date(bill_id)
        if due is None:
            return None
        return (due - self.today).days

    def is_due_soon(self, bill_id: str, *, within_days: int | None = None) -> bool:
        window = config.DUE_SOON_WINDOW_DAYS if within_days is None else within_days
        days = self.days_until_due(bill_id)
        if days is None:
            return False
        return days <= window

    def due_soon(self, *, within_days: int | None = None) -> list[str]:
        """Unpaid bills due within the window, most urgent first."""
        window = config.DUE_SOON_WINDOW_DAYS if within_days is None else within_days
        out: list[tuple[int, str]] = []
        for bill_id in self.bill_ids():
            if self.status(bill_id) != "unpaid":
                continue
            days = self.days_until_due(bill_id)
            if days is None or days > window:
                continue
            out.append((days, bill_id))
        out.sort(key=lambda pair: (pair[0], pair[1]))
        return [bill_id for _, bill_id in out]

    def unpaid(self) -> list[str]:
        return [b for b in self.bill_ids() if self.status(b) == "unpaid"]

    def paid(self) -> list[str]:
        return [b for b in self.bill_ids() if self.status(b) == "paid"]

    def held(self) -> list[str]:
        return [b for b in self.bill_ids() if self.status(b) == "held"]

    # -- duplicates --------------------------------------------------------

    def duplicate_of(self, bill_id: str) -> str | None:
        """Another bill from the same supplier, same amount, same reference.

        Held rather than denied because two genuine bills for the same amount and
        PO reference do exist — a monthly retainer reissued after a credit note,
        say. Only the owner can tell which case this is, so this returns evidence
        and the rule asks a human.

        A bill is never a duplicate of itself, and the match must fall inside
        `config.DUPLICATE_WINDOW_DAYS` of this bill's issue date.
        """
        bill = self.bill(bill_id)
        if bill is None:
            return None
        if bill.get("duplicate_of"):
            return str(bill["duplicate_of"])

        supplier_id = bill.get("supplier_id")
        amount = bill.get("amount_cents")
        reference = str(bill.get("reference") or "")
        if not supplier_id or amount is None or not reference:
            return None

        issued = bill.get("issue_date")
        issued_on: date | None = None
        if issued:
            try:
                issued_on = date.fromisoformat(str(issued))
            except ValueError:
                issued_on = None

        for other_id in self.bill_ids():
            if other_id == bill_id:
                continue
            other = self.bill(other_id) or {}
            if other.get("supplier_id") != supplier_id:
                continue
            if other.get("amount_cents") != amount:
                continue
            if str(other.get("reference") or "") != reference:
                continue
            if issued_on is not None:
                other_issued = other.get("issue_date")
                if other_issued:
                    try:
                        gap = abs((issued_on - date.fromisoformat(str(other_issued))).days)
                    except ValueError:
                        gap = None
                    if gap is not None and gap > config.DUPLICATE_WINDOW_DAYS:
                        continue
            return other_id
        return None

    # -- account and funds -------------------------------------------------

    def account(self, account_id: str | None = None) -> dict[str, Any]:
        key = account_id or config.OPERATING_ACCOUNT
        record = self._accounts.get(key)
        if record is None:
            raise KeyError(f"unknown account '{key}'")
        return dict(record)

    def account_currency(self, account_id: str | None = None) -> str:
        return str(self.account(account_id).get("currency") or config.ACCOUNT_CURRENCY)

    def balance_cents(self, account_id: str | None = None) -> int:
        key = account_id or config.OPERATING_ACCOUNT
        return int(self._balances.get(key, 0))

    def set_balance_cents(self, account_id: str, cents: int) -> None:
        """Mirror a ledger balance change into the book.

        `app.py` keeps this and `world.balances` in step: the ledger is the source
        of truth for money that has moved, and the book caches it so the fact
        source can answer without reaching into `fin_tools`.
        """
        self._balances[str(account_id)] = int(cents)

    def shortfall_cents(self, bill_id: str) -> int:
        """How much is missing to pay this bill now. Never negative."""
        amount = self.amount_cents(bill_id) or 0
        return max(0, int(amount) - self.balance_cents())

    def insufficient_funds(self, bill_id: str) -> bool | None:
        """True when the operating account cannot settle this bill in full.

        `None` when the amount cannot be resolved, so the caller fails closed
        rather than treating an unknown bill as affordable.
        """
        amount = self.amount_cents(bill_id)
        if amount is None:
            return None
        return int(amount) > self.balance_cents()

    # -- credit-line facility ---------------------------------------------

    @property
    def credit_line_limit_cents(self) -> int:
        return config.CREDIT_LINE_LIMIT_CENTS

    @property
    def credit_line_drawn_cents(self) -> int:
        """How much of the facility has been drawn in this process."""
        return int(self._drawn_cents)

    def credit_line_headroom_cents(self) -> int:
        """What is left to draw. This is the FACT the draw is capped against.

        Resolved from configuration minus what has already been drawn, never from
        an argument, so an agent cannot enlarge its own facility by asking for more.
        """
        return max(0, config.CREDIT_LINE_LIMIT_CENTS - int(self._drawn_cents))

    def draw_credit_line(self, cents: int) -> int:
        """Draw on the facility, crediting the operating account.

        Called only from the gated `draw_credit_line` handler, after the auditor
        has allowed it. Returns the new headroom.
        """
        amount = int(cents)
        if amount <= 0:
            raise ValueError("draw must be a positive number of cents")
        headroom = self.credit_line_headroom_cents()
        if amount > headroom:
            raise ValueError(
                f"draw of {config.money(amount)} exceeds the remaining facility "
                f"{config.money(headroom)}")
        self._drawn_cents += amount
        account = config.OPERATING_ACCOUNT
        self.set_balance_cents(account, self.balance_cents(account) + amount)
        return self.credit_line_headroom_cents()

    def supports_currency(self, currency: str | None) -> bool:
        return bool(currency) and str(currency).upper() == self.account_currency().upper()

    # -- run totals --------------------------------------------------------

    @property
    def paid_this_run_cents(self) -> int:
        """Total settled since this run started, for `app.run_total_cap`.

        Tracked here rather than derived from the ledger because the cap is about
        what *this authorisation* has spent, not what the business has ever spent.
        """
        return int(self._paid_this_run_cents)

    def record_paid_this_run(self, cents: int) -> int:
        self._paid_this_run_cents += int(cents)
        return self._paid_this_run_cents

    def reset_run_totals(self) -> None:
        self._paid_this_run_cents = 0

    # -- contacts ----------------------------------------------------------

    def contact_dates(self, supplier_id: str) -> list[date]:
        """Every known contact date for a supplier, oldest first.

        Seeded from two fixture fields: `last_contacted` (which the platform store
        models) and `contact_history_days` (this app's list of days ago). The
        second exists because a frequency rule counts contacts *inside a window*,
        and one last-contact date cannot express "twice this week".
        """
        supplier = self.supplier(supplier_id)
        if not supplier:
            return []
        dates: list[date] = []
        raw = supplier.get("last_contacted")
        if isinstance(raw, str) and raw:
            try:
                dates.append(date.fromisoformat(raw))
            except ValueError:
                pass  # an unparseable date is no date; never invent a contact
        history = supplier.get("contact_history_days")
        if isinstance(history, Iterable) and not isinstance(history, (str, bytes)):
            for entry in history:
                try:
                    dates.append(self.today - timedelta(days=int(entry)))
                except (TypeError, ValueError):
                    continue
        return sorted(set(dates))

    def contact_count(self, supplier_id: str, within_days: int) -> int:
        cutoff = self.today - timedelta(days=int(within_days))
        return sum(1 for when in self.contact_dates(supplier_id) if when >= cutoff)

    # -- the forecaster, as a fact ------------------------------------------

    def next_payroll_date(self) -> date:
        """The next occurrence of the payroll day, today included."""
        import calendar
        cursor = self.today
        for _ in range(3):
            last = calendar.monthrange(cursor.year, cursor.month)[1]
            due = cursor.replace(day=min(config.PAYROLL_DAY_OF_MONTH, last))
            if due >= self.today:
                return due
            year, month = (cursor.year + cursor.month // 12, cursor.month % 12 + 1)
            cursor = date(year, month, 1)
        return self.today

    def projected_min_balance_before_payroll_after(self, bill_id: str) -> int | None:
        """If this bill is paid today, the lowest projected balance through payroll.

        Asked of the platform's forecaster (`forecast.query min_balance`) over the
        balance after the payment and the payroll outflow on its date, in cents.
        Negative means paying now leaves the account unable to make payroll. `None`
        for an unknown bill, so the rule bound on it fails closed.
        """
        amount = self.amount_cents(bill_id)
        if amount is None:
            return None
        payroll_on = self.next_payroll_date()
        days = (payroll_on - self.today).days + 1
        result = forecast_query(
            query="min_balance",
            opening=[{"account": config.OPERATING_ACCOUNT,
                      "balance_cents": self.balance_cents() - int(amount)}],
            flows=[{"id": "payroll", "account": config.OPERATING_ACCOUNT,
                    "direction": "outflow", "amount_cents": config.PAYROLL_CENTS,
                    "date": payroll_on.isoformat()}],
            start=self.today.isoformat(), days=days)
        return int(result["min_balance_cents"])

    def record_supplier_contact(self, supplier_id: str, when: date | None = None) -> None:
        """Record that this supplier was contacted.

        Called from the email tool handlers themselves. The platform runtime only
        does this for a tool named exactly `send_email` (`runtime.py:611`), and
        this app's tools are `send_remittance` and `escalate_to_owner`, so the
        bookkeeping has to happen here or the frequency rule would never see the
        contacts it is supposed to count.
        """
        self.store.record_contact(supplier_id, when or self.today)

    def record_sent_email(
        self,
        *,
        supplier_id: str,
        bill_id: str,
        subject: str,
        body: str,
        kind: str = "remittance",
        to: str | None = None,
    ) -> dict[str, Any]:
        """Append to the outbox and count the contact, in one step.

        Both happen or neither does: an email that reached a supplier but was not
        counted would let the frequency rule be defeated by simply sending more.
        """
        entry = self.store.record_sent_email(
            customer_id=supplier_id,
            invoice_id=bill_id,
            subject=subject,
            body=body,
        )
        entry["kind"] = kind
        if to:
            entry["to"] = to
        return entry

    # -- mutations: gated paths only ---------------------------------------

    def _mutate_bill(self, bill_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Write bill fields into the store's own dict.

        `Store.get_invoice` returns a copy, and the store exposes no bill mutator
        (it was written for Build 1, where invoices are read-only). Mutating the
        copy would leave the bill unpaid while reporting it settled, so this writes
        through to `_invoices`. Confined to one method for the same reason as
        `_mutate_supplier`: if the platform grows a public mutator, this is the
        single place to change.
        """
        record = self.store._invoices.get(bill_id)  # noqa: SLF001
        if record is None:
            raise KeyError(
                f"unknown bill '{bill_id}'; expected one of {', '.join(self.bill_ids())}"
            )
        record.update(dict(changes))
        return dict(record)

    def mark_paid(
        self,
        bill_id: str,
        *,
        payment_id: str,
        amount_cents: int,
        paid_at: date | None = None,
    ) -> dict[str, Any]:
        """Settle a bill. Called only after `payments.send` actually settled."""
        when = paid_at or self.today
        record = self._mutate_bill(bill_id, {
            "status": "paid",
            "paid_at": when.isoformat(),
            "payment_id": payment_id,
            "paid_amount_cents": int(amount_cents),
        })
        self.record_paid_this_run(int(amount_cents))
        return record

    def mark_held(self, bill_id: str, *, hold_id: str, reason: str) -> dict[str, Any]:
        return self._mutate_bill(bill_id, {
            "status": "held",
            "hold_id": hold_id,
            "hold_reason": reason,
        })

    def reschedule(
        self,
        bill_id: str,
        *,
        new_date: date | str,
        reason: str,
    ) -> dict[str, Any]:
        """Move the scheduled payment date. The bill stays unpaid.

        Rescheduling is the honest alternative to an unaffordable payment: the
        obligation remains visible on the dashboard instead of being settled with
        money the account does not have.
        """
        when = new_date if isinstance(new_date, date) else date.fromisoformat(str(new_date))
        return self._mutate_bill(bill_id, {
            "status": "rescheduled",
            "scheduled_date": when.isoformat(),
            "reschedule_reason": str(reason),
            "rescheduled_at": self.today.isoformat(),
        })

    # -- snapshots for the UI ----------------------------------------------

    def snapshot_bill(self, bill_id: str) -> dict[str, Any]:
        """One bill with its supplier joined, for pages and tool results."""
        bill = dict(self.bill(bill_id) or {})
        supplier_id = str(bill.get("supplier_id") or "")
        amount = self.amount_cents(bill_id)
        return {
            "bill_id": bill_id,
            "supplier_id": supplier_id,
            "supplier_name": self.supplier_name(supplier_id) if supplier_id else "",
            "amount_cents": amount,
            "amount": config.cents_to_decimal(amount) if amount is not None else None,
            "currency": self.currency(bill_id),
            "reference": self.reference(bill_id),
            "issue_date": bill.get("issue_date"),
            "due_date": bill.get("due_date"),
            "scheduled_date": self.scheduled_date(bill_id).isoformat()
            if self.scheduled_date(bill_id) else None,
            "status": self.status(bill_id),
            "days_until_due": self.days_until_due(bill_id),
            "duplicate_of": self.duplicate_of(bill_id),
            "paid_at": bill.get("paid_at"),
            "payment_id": bill.get("payment_id"),
            "reschedule_reason": bill.get("reschedule_reason"),
            "notes": bill.get("notes", ""),
        }

    def snapshot_supplier(self, supplier_id: str, *, include_fraud_flag: bool = True) -> dict[str, Any]:
        """A supplier as the agent may see it.

        `include_fraud_flag=False` is what `supplier_profile` uses: the agent gets
        the name, the terms and the average bill, but NOT
        `payment_details_changed`. Withholding it is the point. If the agent could
        read the flag it could also decide the flag was wrong, and the control
        would rest on the agent agreeing with the auditor. The auditor reads the
        flag through the fact source instead, where no argument can reach it.
        """
        supplier = dict(self.supplier(supplier_id) or {})
        out: dict[str, Any] = {
            "supplier_id": supplier_id,
            "name": supplier.get("name", supplier_id),
            "email": supplier.get("email"),
            "settlement_terms": supplier.get("settlement_terms", ""),
            "avg_bill_cents": self.avg_bill_cents(supplier_id),
            "public_ticker": supplier.get("public_ticker") or "",
            "contacts_in_window": self.contact_count(
                supplier_id, config.SUPPLIER_CONTACT_WINDOW_DAYS),
        }
        if include_fraud_flag:
            out["payment_details_changed"] = self.payment_details_changed(supplier_id)
            out["details_changed_days_ago"] = self.details_changed_days_ago(supplier_id)
            out["reverified"] = bool(supplier.get("reverified"))
        return out

    def summary(self) -> dict[str, Any]:
        """The dashboard's headline numbers."""
        due_soon = self.due_soon()
        return {
            "today": self.today.isoformat(),
            "due_soon_count": len(due_soon),
            "due_soon_cents": sum(self.amount_cents(b) or 0 for b in due_soon),
            "paid_count": len(self.paid()),
            "paid_cents": sum(self.amount_cents(b) or 0 for b in self.paid()),
            "held_count": len(self.held()),
            "balance_cents": self.balance_cents(),
            "paid_this_run_cents": self.paid_this_run_cents,
            "currency": self.account_currency(),
        }


@dataclass(frozen=True)
class BillRef:
    """A bill named by id and amount, for callers that want both checked."""

    bill_id: str
    amount_cents: int

    @property
    def amount(self) -> Any:
        return config.cents_to_decimal(self.amount_cents)

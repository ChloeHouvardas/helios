"""Configuration and thresholds for Helios Payables.

Every number the audit manifest declares appears here too, on purpose. The
manifest is the authority — the auditor compiles `agents/helios-payables-audit.yaml`
and nothing else — but a threshold that lives only in YAML is invisible to the
code that has to explain it on a page or in a test. Mirroring it here means the
dashboard, the README scenario table and `tests/test_controls.py` all quote the
same figure the gate actually enforces, and a drift between the two shows up as a
failing test rather than as a page that lies.

Money convention (specs/decisions.md): integer cents internally, decimal strings
at the edges, never float. The `*_CENTS` names below are integer cents; the
`*_DISPLAY` helpers format them for humans.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

# --------------------------------------------------------------------------
# Where this app lives and what it is called
# --------------------------------------------------------------------------

APP_ID = "helios-payables"
APP_NAME = "Helios Payables"

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
AGENTS_DIR = HERE / "agents"
TEMPLATES_DIR = HERE / "templates"

AUDIT_MANIFEST = AGENTS_DIR / f"{APP_ID}-audit.yaml"
ACTION_MANIFEST = AGENTS_DIR / f"{APP_ID}-action.json"

SUPPLIERS_FILE = DATA_DIR / "suppliers.json"
BILLS_FILE = DATA_DIR / "bills.json"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"

# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

#: The single account this app pays from. Like `helios-collect`'s inbound
#: routing, the destination is configuration, not an agent argument: `pay_bill`
#: has no `payer` parameter, so there is nothing in a request to misdirect.
OPERATING_ACCOUNT = "operating"

#: The account currency. A bill in any other currency is denied outright rather
#: than held, because converting currency is not a decision an owner should be
#: making from an approval queue mid-run.
ACCOUNT_CURRENCY = "CNY"

# --------------------------------------------------------------------------
# Task types — the mandates this app's agent may be authorised for
# --------------------------------------------------------------------------

TASK_PAY = "payables.pay_bills"
TASK_RESCHEDULE = "payables.reschedule"
TASK_ESCALATE = "payables.escalate"

# --------------------------------------------------------------------------
# Thresholds mirrored from the audit manifest
# --------------------------------------------------------------------------

#: Single payment above this needs owner approval. 25,000.00 CNY.
#: Manifest: `app.large_payment_approval`, bound on the FACT `bill_amount_cents`
#: so an agent that understates the amount in its arguments cannot slip under it.
LARGE_PAYMENT_APPROVAL_CENTS = 2_500_000

#: Total settled in one run above this holds everything further. 60,000.00 CNY.
#: Manifest: `app.run_total_cap`.
RUN_TOTAL_CAP_CENTS = 6_000_000

#: A bill whose amount exceeds this multiple of the supplier's average is held.
#: Manifest: `app.unusual_amount`, ceiling supplied as the fact
#: `supplier_amount_ceiling_cents` (= average × this multiplier).
UNUSUAL_AMOUNT_MULTIPLIER = 2

#: A supplier whose payment details changed within this many days is DENIED,
#: not held. Manifest: `app.payee_details_changed`.
PAYEE_CHANGE_DENY_WINDOW_DAYS = 30

#: Same supplier, same amount, same reference within this many days is a
#: duplicate-looking bill and is held. Manifest: `app.duplicate_bill`.
DUPLICATE_WINDOW_DAYS = 90

#: At most this many emails to one supplier in a rolling window.
#: Manifest: `app.supplier_contact_frequency`.
MAX_SUPPLIER_CONTACTS = 2
SUPPLIER_CONTACT_WINDOW_DAYS = 7

#: How far ahead the morning run looks.
DUE_SOON_WINDOW_DAYS = 7

#: Total credit-line facility, in cents (200,000.00). Drawing on it is a real,
#: gated money movement — see `draw_credit_line` in tools.py — not a flag that
#: makes an unaffordable bill affordable. The cap is enforced against the fact
#: `credit_line_headroom_cents`, so an agent cannot draw more than is left.
CREDIT_LINE_LIMIT_CENTS = 20_000_000

#: Spread over LPR 1y the business would pay for a credit-line draw, in basis
#: points. Used by `funding.py` to rank options.
CREDIT_LINE_SPREAD_BPS = 180

#: Supplier late fee used when costing a `delay_supplier` option, in basis
#: points of the bill amount.
SUPPLIER_LATE_FEE_BPS = 50

# --------------------------------------------------------------------------
# Graph identity
# --------------------------------------------------------------------------

#: This app is the authority for `bill` records, so it publishes them as
#: VERIFIED nodes. Anything it infers — a registry mismatch, a funding
#: recommendation — is an OBSERVATION and can never overwrite a verified node.
GRAPH_SOURCE = "helios-payables"
GRAPH_OBSERVER = "helios-payables-agent"

#: Bill states this app publishes to the shared graph.
PUBLISHED_BILL_STATES = ("scheduled", "paid", "rescheduled", "held")


# --------------------------------------------------------------------------
# Formatting helpers — decimal strings out, never float
# --------------------------------------------------------------------------

def cents_to_decimal(cents: int | None) -> Decimal:
    """Integer cents to a Decimal, without ever passing through float."""
    return Decimal(int(cents or 0)) / Decimal(100)


def money(cents: int | None, currency: str = ACCOUNT_CURRENCY) -> str:
    """Human-readable money, e.g. ``180000.00 CNY``."""
    return f"{cents_to_decimal(cents):,.2f} {currency}"


def describe() -> dict[str, Any]:
    """The settings page's view of this file, as plain data."""
    return {
        "app_id": APP_ID,
        "operating_account": OPERATING_ACCOUNT,
        "account_currency": ACCOUNT_CURRENCY,
        "large_payment_approval": money(LARGE_PAYMENT_APPROVAL_CENTS),
        "run_total_cap": money(RUN_TOTAL_CAP_CENTS),
        "unusual_amount_multiplier": UNUSUAL_AMOUNT_MULTIPLIER,
        "payee_change_deny_window_days": PAYEE_CHANGE_DENY_WINDOW_DAYS,
        "duplicate_window_days": DUPLICATE_WINDOW_DAYS,
        "max_supplier_contacts": MAX_SUPPLIER_CONTACTS,
        "supplier_contact_window_days": SUPPLIER_CONTACT_WINDOW_DAYS,
        "due_soon_window_days": DUE_SOON_WINDOW_DAYS,
        "credit_line_limit": money(CREDIT_LINE_LIMIT_CENTS),
        "credit_line_spread_bps": CREDIT_LINE_SPREAD_BPS,
    }


def as_mapping() -> Mapping[str, Any]:
    """Immutable view for callers that must not mutate configuration."""
    return dict(describe())

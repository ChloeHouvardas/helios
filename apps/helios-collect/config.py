"""App configuration: routing, thresholds, account names.

This file is the answer to "where does the money go", and the agent has no way
to influence it. `record_inbound_payment` takes an invoice id and an amount —
never an account — and the destination is resolved here. That is a structural
control rather than a rule: an agent cannot misdirect funds because there is no
argument in the request that names a destination.

Everything here is owner-set. A deployed app would read it from a settings page
(the `settings` UI pattern in specs/app-builder.md); a prototype keeps it in code
so the demo is reproducible and no secret is involved.

Nothing in this file is a credential. Signing keys come from the environment
(`HELIOS_AUDIT_SIGNING_KEY`), never from here — see README.md.
"""
from __future__ import annotations

from typing import Any, Mapping

#: The app's own bank accounts. Inbound customer money lands in `RECEIVABLES`.
RECEIVABLES_ACCOUNT = "helios-collect-receivables"
OPERATING_ACCOUNT = "helios-collect-operating"

ACCOUNTS: tuple[str, ...] = (RECEIVABLES_ACCOUNT, OPERATING_ACCOUNT)

#: Default destination for inbound customer payments.
DEFAULT_INBOUND_ACCOUNT = RECEIVABLES_ACCOUNT

#: Per-customer overrides. A customer paying into a ring-fenced project account
#: is the realistic reason this table exists at all: one business, several
#: destinations, and the agent should not have to know which is which.
INBOUND_ROUTING: Mapping[str, str] = {
    "CUST-003": "helios-collect-receivables-harbor",
}

#: What an inbound payment is recorded as in the shared knowledge graph. The
#: graph distinguishes `verified` (from a system of record) from `observation`
#: (an agent's claim); a payment that actually settled is verified, and an
#: observation can never overwrite one.
GRAPH_SOURCE = "helios-collect/ledger"
GRAPH_OBSERVER = "helios-collect-agent"

#: How the app reads the mandate's numeric ceilings, for display and tests.
#: These MIRROR agents/helios-collect-audit.yaml; they do not replace it. The
#: audit manifest is authoritative and is what the auditor enforces. Keeping a
#: copy here is a convenience for the dashboard and a trap if the two drift, so
#: tests/test_controls.py asserts they agree.
EXTENSION_DAYS_WITHOUT_APPROVAL = 14
LARGE_INVOICE_THRESHOLD = "10000.00"
CONTACT_WINDOW_DAYS = 7
MAX_CONTACTS_IN_WINDOW = 2


def inbound_account_for(customer_id: str) -> str:
    """Where a payment from this customer lands.

    Resolved from the invoice's customer, never from an agent-supplied account.
    An unknown customer falls back to the default account rather than raising:
    the payment is real and must be recorded somewhere, and the routing decision
    is not the agent's to make either way.
    """
    return INBOUND_ROUTING.get(customer_id, DEFAULT_INBOUND_ACCOUNT)


def describe() -> dict[str, Any]:
    """The settings page, as data."""
    return {
        "accounts": list(ACCOUNTS),
        "default_inbound_account": DEFAULT_INBOUND_ACCOUNT,
        "inbound_routing": dict(INBOUND_ROUTING),
        "extension_days_without_approval": EXTENSION_DAYS_WITHOUT_APPROVAL,
        "large_invoice_threshold": LARGE_INVOICE_THRESHOLD,
        "contact_window_days": CONTACT_WINDOW_DAYS,
        "max_contacts_in_window": MAX_CONTACTS_IN_WINDOW,
    }


__all__ = [
    "ACCOUNTS",
    "CONTACT_WINDOW_DAYS",
    "DEFAULT_INBOUND_ACCOUNT",
    "EXTENSION_DAYS_WITHOUT_APPROVAL",
    "GRAPH_OBSERVER",
    "GRAPH_SOURCE",
    "INBOUND_ROUTING",
    "LARGE_INVOICE_THRESHOLD",
    "MAX_CONTACTS_IN_WINDOW",
    "OPERATING_ACCOUNT",
    "RECEIVABLES_ACCOUNT",
    "describe",
    "inbound_account_for",
]

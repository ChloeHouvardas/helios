"""Record models — the six provider-agnostic payment tables.

Money is always integer minor units (fen) plus a currency field; never floats.
Dates are tz-aware, defaulting to Asia/Shanghai.

Two data-model rules are baked into these records:
* `PaymentAttempt.out_trade_no` is OUR reference — reconciliation keys off
  it, never off the provider's order id.
* `ProviderEvent` is append-only: the raw payload is stored before
  processing and is never edited afterwards.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


ReceivableStatus = Literal["open", "partial", "paid", "void"]
LinkStatus = Literal["issued", "closed", "expired"]
AttemptState = Literal[
    "created", "intent_created", "customer_authenticating", "processing",
    "succeeded", "settled", "partially_refunded", "refunded", "closed",
    "failed", "expired",
]
EntryType = Literal["debit", "credit"]
SettlementStatus = Literal["pending", "routed", "failed"]

# The attempt's guarded lifecycle. `link_issued` lives on the link (status
# "issued"); everything from `created` on is the attempt's walk. `failed` and
# `expired` are terminal; refund states can still close out.
ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"intent_created", "failed", "expired"}),
    "intent_created": frozenset({"customer_authenticating", "failed", "expired"}),
    # A webhook can land while the payer is still at the cashier, so
    # `succeeded` is reachable directly from here.
    "customer_authenticating": frozenset(
        {"processing", "succeeded", "failed", "expired"}),
    "processing": frozenset({"succeeded", "failed", "expired"}),
    "succeeded": frozenset({"settled", "partially_refunded", "refunded"}),
    "settled": frozenset({"closed", "partially_refunded", "refunded"}),
    "partially_refunded": frozenset({"partially_refunded", "refunded", "closed"}),
    "refunded": frozenset({"closed"}),
    "closed": frozenset(),
    "failed": frozenset(),
    "expired": frozenset(),
}

LINK_TRANSITIONS: dict[str, frozenset[str]] = {
    "issued": frozenset({"closed", "expired"}),
    "closed": frozenset(),
    "expired": frozenset(),
}

RECEIVABLE_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"partial", "paid", "void"}),
    "partial": frozenset({"paid", "void"}),
    "paid": frozenset(),
    "void": frozenset(),
}

SETTLEMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"routed", "failed"}),
    "routed": frozenset(),
    "failed": frozenset(),
}


class Receivable(BaseModel):
    """Money a customer owes the business (table 1)."""

    id: str
    business_id: str = "dev"
    customer_id: str
    amount: int = Field(gt=0, description="minor units (fen); never a float")
    currency: str = "CNY"
    invoice_no: str | None = None
    due_date: datetime | None = None
    status: ReceivableStatus = "open"
    amount_paid: int = 0
    created_at: datetime = Field(default_factory=now_shanghai)
    meta: dict[str, Any] = Field(default_factory=dict)


class PaymentLink(BaseModel):
    """A shareable payment link for one receivable (table 2).

    `token` is the unguessable capability behind the cashier URL; anyone
    holding it may pay, so it carries no secrets.
    """

    id: str
    business_id: str = "dev"
    receivable_id: str
    amount: int = Field(gt=0, description="server-side amount; never client-editable")
    currency: str = "CNY"
    expires_at: datetime | None = None
    one_time: bool = True
    status: LinkStatus = "issued"
    token: str
    created_at: datetime = Field(default_factory=now_shanghai)
    meta: dict[str, Any] = Field(default_factory=dict)


class PaymentAttempt(BaseModel):
    """One try to pay one link (table 3). Owns the state machine.

    `out_trade_no` is our reference and the reconciliation key;
    `provider_order_ref` is the provider's trade number, informational only.
    """

    id: str
    business_id: str = "dev"
    link_id: str
    provider: str
    out_trade_no: str
    provider_order_ref: str | None = None
    amount: int = Field(gt=0)
    currency: str = "CNY"
    idempotency_key: str | None = None
    state: AttemptState = "created"
    failure_reason: str = ""
    refunded_amount: int = 0
    created_at: datetime = Field(default_factory=now_shanghai)
    updated_at: datetime = Field(default_factory=now_shanghai)
    meta: dict[str, Any] = Field(default_factory=dict)


class ProviderEvent(BaseModel):
    """A raw webhook payload, stored before processing (table 4).

    Append-only: `payload_json` / `payload_hash` are written once and never
    mutated. `processed_at` is the one stamped metadata field.
    """

    id: str
    provider: str
    event_id: str
    payload_hash: str
    payload_json: str
    received_at: datetime = Field(default_factory=now_shanghai)
    processed_at: datetime | None = None


class LedgerEntry(BaseModel):
    """One leg of a dual-entry posting (table 5).

    Legs sharing a `journal_id` are the balancing halves of one posting.
    """

    id: str
    business_id: str
    attempt_id: str
    journal_id: str
    type: EntryType
    amount: int = Field(gt=0)
    currency: str = "CNY"
    account: str
    memo: str = ""
    posted_at: datetime = Field(default_factory=now_shanghai)


class Settlement(BaseModel):
    """Where a captured payment is routed (table 6)."""

    id: str
    business_id: str
    attempt_id: str
    to_account: str
    rule: str = "default"
    status: SettlementStatus = "pending"
    created_at: datetime = Field(default_factory=now_shanghai)

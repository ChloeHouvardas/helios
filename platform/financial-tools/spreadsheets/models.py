"""Record models — the guide's record types plus Counterparty.

Money is always integer minor units (fen) plus a currency field; never floats.
Dates are tz-aware, defaulting to Asia/Shanghai per the guide's conventions.

Field names follow `Helios_Spreadsheet_Tools_Developer_Guide.md` § "Data and
dependencies". The Spreadsheet Tools *own* SheetView, EditProposal and Match;
Transaction, Obligation and Counterparty are written by the Payments and KG
population tools and are only read here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

Channel = Literal["wechat", "alipay", "bank"]
TxDirection = Literal["in", "out"]
ObDirection = Literal["receivable", "payable"]
ObStatus = Literal["open", "partial", "paid", "overdue", "disputed", "void"]

# Guide § reconcile: how a link was decided. `auto` and `suggested` come from
# the rule engine (steps 1-2 auto-apply, steps 3-5 only suggest); `confirmed`
# is a suggestion a human accepted; `manual` is a human-created link.
MatchMethod = Literal["auto", "suggested", "confirmed", "manual"]

# Guide § render_sheet `target`.
#
# Live targets render into a hosted spreadsheet product: `feishu` (飞书多维表格)
# is the default because that is what Chinese SMBs actually use, with `wps`
# (金山 WPS 表格) alongside it. `grist` remains supported for teams that deploy
# it. `xlsx` / `csv` return a one-off file instead of a live view.
SheetTarget = Literal["feishu", "wps", "grist", "xlsx", "csv"]

# A live view is one the sheet engine keeps refreshed; a file target is written
# once and handed back as a `file_id`.
LIVE_TARGETS = frozenset({"feishu", "wps", "grist"})
FILE_TARGETS = frozenset({"xlsx", "csv"})
ALL_TARGETS = LIVE_TARGETS | FILE_TARGETS
DEFAULT_TARGET = "feishu"

RecordType = Literal[
    "Transaction", "Obligation", "Match", "SheetView", "Counterparty",
    "EditProposal",
]

ProposalStatus = Literal["pending", "approved", "rejected"]
ProposalActorKind = Literal["user", "agent"]

# Column kinds the guide treats as protected by default: money, dates, payees.
MONEY_FIELDS = frozenset({"amount", "amount_applied", "currency"})
DATE_FIELDS = frozenset({"due_at", "issued_at", "occurred_at"})
PAYEE_FIELDS = frozenset({"counterparty", "payment_details"})
DEFAULT_PROTECTED_FIELDS = MONEY_FIELDS | DATE_FIELDS | PAYEE_FIELDS


class Record(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    business_id: str
    source: str
    created_at: datetime
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def record_type(self) -> str:
        """The `type` value used in guide-shaped queries."""
        return type(self).__name__


class Transaction(Record):
    """Money that moved. Written by the Payments tool; read here."""

    amount: int
    currency: str = "CNY"
    direction: TxDirection
    counterparty: str
    channel: Channel
    occurred_at: datetime
    channel_ref: str
    # Free text from the statement. reconcile steps 1 and 3 read this to find
    # an invoice number or a counterparty nickname. Never treated as an
    # instruction — see guide § security rules.
    note: str = ""
    needs_review: bool = False


class Obligation(Record):
    """Money owed, paid or not. Written by the KG population tool."""

    amount: int
    currency: str = "CNY"
    direction: ObDirection
    counterparty: str
    invoice_number: str | None = None
    issued_at: datetime | None = None
    due_at: datetime | None = None
    status: ObStatus = "open"
    doc_ref: str | None = None
    confidence: float | None = None
    needs_review: bool = False


class Match(Record):
    """A payment linked to what it settles. Owned by these tools (reconcile).

    A rejected match is never deleted — `meta["rejected"]` marks it so the
    Audit Agent keeps the history, per guide § "What happens after a match".
    """

    transaction_id: str
    obligation_id: str
    amount_applied: int
    method: MatchMethod
    score: float = 1.0
    # Which rule produced it: exact_reference, exact_amount, fuzzy_reference,
    # fuzzy_alias, combination, qwen_review, manual.
    rule: str = "manual"

    @property
    def is_rejected(self) -> bool:
        return bool(self.meta.get("rejected"))

    @property
    def applies_payment(self) -> bool:
        """Only auto-applied and human-confirmed links move obligation status."""
        return self.method in ("auto", "confirmed", "manual") and not self.is_rejected


class Counterparty(Record):
    """A customer or supplier. Written by the KG population tool."""

    name: str
    aliases: list[str] = Field(default_factory=list)
    # Values are stored masked (last four digits only). Account and ID numbers
    # cannot be unmasked through the API — guide § security rules.
    payment_details: dict[str, str] = Field(default_factory=dict)


class SheetView(Record):
    """A live sheet. Owned by these tools (render_sheet).

    A sheet is a view, never the source of truth: it displays KG records and
    refreshes from them.
    """

    query: dict[str, Any]
    columns: list[Union[str, dict[str, Any]]]
    target: SheetTarget = DEFAULT_TARGET
    editable_columns: list[str] = Field(default_factory=list)
    # Editable columns whose edits become proposals instead of direct writes.
    # Defaults to money, date and payee columns — guide § render_sheet.
    protected_columns: list[str] = Field(default_factory=list)
    # Who sees what, e.g. {"bookkeeper": "all", "staff": {"hide": ["amount"]}}.
    access: dict[str, Any] = Field(default_factory=dict)
    group_by: str | None = None
    sort: str | None = None
    url: str | None = None
    file_id: str | None = None


class EditProposal(Record):
    """A pending edit to a protected column. Owned by these tools.

    An edit to a protected column never writes directly. The proposal goes to
    Intent Verification; on approval it is handed to the tool that owns the
    record, which applies it and the sheet refreshes from the KG.
    """

    view_id: str
    record_id: str
    field: str
    old_value: Any = None
    new_value: Any = None
    proposed_by: str
    # An agent's proposal always needs a person to approve it.
    proposed_by_kind: ProposalActorKind = "user"
    reason: str = ""
    status: ProposalStatus = "pending"
    resolved_by: str | None = None
    resolved_at: datetime | None = None

    @property
    def needs_human(self) -> bool:
        return self.proposed_by_kind == "agent"


class EmailMessage(BaseModel):
    """A statement handed over by the Email tool (never fetched here).

    Retained for `import_sheet` flows where a business forwards a bank or
    wallet statement as an attachment; the Email tool owns delivery, this tool
    only reads the referenced `attachment_file_ids`.
    """

    model_config = ConfigDict(populate_by_name=True)

    message_id: str
    from_: str = Field(alias="from")
    subject: str = ""
    received_at: datetime | None = None
    body_text: str = ""
    attachment_file_ids: list[str] = Field(default_factory=list)

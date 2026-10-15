"""The tools this app's agent may call — nine of them, matching both manifests.

  read-only (run directly)        list_due_bills, lookup_bill, check_funds,
                                  supplier_profile, funding_options
  side-effecting (gated)          pay_bill, reschedule_bill, send_remittance,
                                  escalate_to_owner

Three design decisions in this file matter more than the code, and all three are
about what the agent is NOT given.

1. `pay_bill` has no `payer` and no `payee` argument.
   The account comes from `config.OPERATING_ACCOUNT`; the payee comes from the
   supplier on file for the bill. Money cannot be misdirected because there is
   nothing in the request to misdirect it with. This is a structural control
   rather than a rule, which is why no rule in the manifest tries to police it.

2. The amount that EXECUTES is the recorded amount, not the requested one.
   The agent's `amount_cents` is what the audit rules measure; the handler pays
   `book.amount_cents(bill_id)`. So an agent that understates the amount to slip
   under the 25,000.00 threshold gets held anyway (the rule binds on the fact),
   and an agent that overstates it cannot overpay (the ledger amount wins). The
   `auth_code` is minted against the recorded amount too, so the two cannot
   disagree — `payments.send` verifies the code binds exactly
   (amount, currency, payee).

3. `supplier_profile` withholds `payment_details_changed`.
   The agent gets the name, terms and average bill size. It does not get the fraud
   flag, because a control the agent can read is a control the agent can argue
   with. The auditor reads that flag through `facts.py`, where no argument reaches.
   The registry evidence for a change request is owner-facing only, for the same
   reason — see `registry_evidence_for` in app.py.

Contact bookkeeping
-------------------
The platform runtime records a contact only for a tool named exactly `send_email`
(`agent_integration/runtime.py:611`). This app's tools are `send_remittance` and
`escalate_to_owner`, so `send_remittance` calls `book.record_supplier_contact`
itself. Without that the frequency rule would never see the contacts it counts and
would silently under-count — a control that looks configured and enforces less
than it says. `escalate_to_owner` deliberately does NOT count against the supplier
limit: its recipient is the owner, and an agent that cannot escalate is an agent
that improvises around a refusal.

Registry dispatch
-----------------
Handlers call `bundle.payments`, `bundle.email` and `bundle.graph` directly rather
than through `bundle.registry.call`. `build_world()` binds module-level singletons
(`fin_tools/payments.py:158`, `email_sender.py:101`), so with two worlds in one
process the last-built one wins for *every* registry dispatch. Tests build a fresh
world per case, so going through the registry would make them interfere. This is
the same hazard `helios-collect` records as platform limitation 6.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping

from agent_toolkit.contracts import ToolDefinition
from agent_toolkit.store import Store

from book import Book
import config
import funding

# Tool names. Both manifests must agree with these, and tests assert that they do.
LIST_DUE_BILLS = "list_due_bills"
LOOKUP_BILL = "lookup_bill"
CHECK_FUNDS = "check_funds"
SUPPLIER_PROFILE = "supplier_profile"
FUNDING_OPTIONS = "funding_options"
DRAW_CREDIT_LINE = "draw_credit_line"
PAY_BILL = "pay_bill"
RESCHEDULE_BILL = "reschedule_bill"
SEND_REMITTANCE = "send_remittance"
ESCALATE_TO_OWNER = "escalate_to_owner"

TOOL_NAMES: tuple[str, ...] = (
    LIST_DUE_BILLS, LOOKUP_BILL, CHECK_FUNDS, SUPPLIER_PROFILE, FUNDING_OPTIONS,
    DRAW_CREDIT_LINE, PAY_BILL, RESCHEDULE_BILL, SEND_REMITTANCE, ESCALATE_TO_OWNER,
)

READ_ONLY_TOOLS: frozenset[str] = frozenset({
    LIST_DUE_BILLS, LOOKUP_BILL, CHECK_FUNDS, SUPPLIER_PROFILE, FUNDING_OPTIONS,
})
SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({
    DRAW_CREDIT_LINE, PAY_BILL, RESCHEDULE_BILL, SEND_REMITTANCE, ESCALATE_TO_OWNER,
})

#: Fields the agent never sees, because they are control inputs rather than
#: business information. The auditor reads them through `facts.py`.
WITHHELD_FROM_AGENT = frozenset({
    "payment_details_changed", "details_changed_date", "details_changed_days_ago",
    "change_request", "reverified", "registered_website", "legal_representative",
})

#: Who the owner is, for escalations. Not an agent argument.
OWNER_EMAIL = "owner@helios-payables.local"

# Remittance template. `fin_tools`' DEFAULT_TEMPLATES ships only
# `payment_remitter`-style receivables text (`payment_reminder`,
# `payment_confirmation`), so this app renders its own rather than reuse a
# template written for the other side of the ledger.
REMITTANCE_SUBJECT = "Remittance advice — {reference} ({bill_id})"
REMITTANCE_BODY = (
    "Dear {supplier},\n\n"
    "We have paid bill {bill_id} (your reference {reference}) in the amount of "
    "{amount}.\n\n"
    "Payment date: {paid_on}\nPayment reference: {payment_id}\n"
    "Funds sent to the account we hold for you on file.\n\n"
    "Please let us know if anything does not match your records.\n\n"
    "Regards,\nHelios Payables"
)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

_EMPTY_INPUT: dict[str, Any] = {
    "type": "object", "properties": {}, "additionalProperties": False,
}

_BILL_ID_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"bill_id": {"type": "string", "minLength": 1}},
    "required": ["bill_id"],
    "additionalProperties": False,
}

_SUPPLIER_ID_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"supplier_id": {"type": "string", "minLength": 1}},
    "required": ["supplier_id"],
    "additionalProperties": False,
}

_FUNDING_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bill_id": {"type": "string", "minLength": 1},
        "days_until_funds": {"type": "integer", "minimum": 1, "maximum": 365},
    },
    "required": ["bill_id", "days_until_funds"],
    "additionalProperties": False,
}

_DRAW_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bill_id": {
            "type": "string", "minLength": 1,
            "description": "The bill this draw funds, so the reason is on the record.",
        },
        "amount_cents": {
            "type": "integer", "minimum": 1,
            "description": (
                "How much to draw, in integer cents. Capped against the remaining "
                "facility, which the auditor resolves from configuration - not from "
                "this number."),
        },
        "reason": {
            "type": "string", "minLength": 12, "maxLength": 500,
            "description": "Why the draw is needed, e.g. the shortfall and when it is repaid.",
        },
    },
    "required": ["bill_id", "amount_cents", "reason"],
    "additionalProperties": False,
}

_PAY_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bill_id": {"type": "string", "minLength": 1},
        "amount_cents": {
            "type": "integer", "minimum": 1,
            "description": (
                "The amount you intend to pay, in integer cents. Checked against "
                "the RECORDED bill amount, which the auditor reads from the book. "
                "The payment that executes is always the recorded amount."),
        },
        "currency": {"type": "string", "minLength": 3, "maxLength": 3},
        "auth_code": {
            "type": "string", "minLength": 8,
            "description": "Single-use code from an intent the OWNER confirmed.",
        },
        "idempotency_key": {"type": "string", "maxLength": 120},
        "funding": {
            "type": "string",
            "enum": ["", "operating_balance", "credit_line", "early_payment_discount"],
        },
        "memo": {"type": "string", "maxLength": 500},
    },
    "required": ["bill_id", "amount_cents", "currency", "auth_code"],
    "additionalProperties": False,
}

_RESCHEDULE_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bill_id": {"type": "string", "minLength": 1},
        "new_date": {
            "type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "New scheduled payment date, ISO 8601.",
        },
        "reason": {
            "type": "string", "minLength": 12, "maxLength": 500,
            "description": "Why the bill is moving. At least 12 characters.",
        },
    },
    "required": ["bill_id", "new_date", "reason"],
    "additionalProperties": False,
}

_REMITTANCE_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supplier_id": {
            "type": "string", "minLength": 1,
            "description": (
                "Required so the contact-frequency rule can resolve a trusted "
                "subject; the policy engine's _subject_key does not look at bill_id."),
        },
        "bill_id": {"type": "string", "minLength": 1},
        "subject": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {"type": "string", "minLength": 1, "maxLength": 4000},
    },
    "required": ["supplier_id", "bill_id", "subject", "body"],
    "additionalProperties": False,
}

_ESCALATE_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bill_id": {"type": "string", "minLength": 1},
        "subject": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {
            "type": "string", "minLength": 12, "maxLength": 4000,
            "description": "What was refused, why, and what the owner must decide.",
        },
    },
    "required": ["bill_id", "subject", "body"],
    "additionalProperties": False,
}

_LIST_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"count": {"type": "integer"}, "bills": {"type": "array"}},
    "required": ["count", "bills"], "additionalProperties": False,
}
_BILL_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"bill": {"type": "object"}, "supplier": {"type": "object"}},
    "required": ["bill", "supplier"], "additionalProperties": False,
}
_FUNDS_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bill_id": {"type": "string"}, "balance_cents": {"type": "integer"},
        "amount_cents": {"type": "integer"}, "shortfall_cents": {"type": "integer"},
        "sufficient": {"type": "boolean"}, "currency_supported": {"type": "boolean"},
    },
    "required": ["bill_id", "balance_cents", "amount_cents", "shortfall_cents",
                 "sufficient", "currency_supported"],
    "additionalProperties": False,
}
_SUPPLIER_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"supplier": {"type": "object"}},
    "required": ["supplier"], "additionalProperties": False,
}
_FUNDING_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"options": {"type": "array"}, "cheapest": {"type": ["string", "null"]}},
    "required": ["options", "cheapest"], "additionalProperties": False,
}
_DRAW_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bill_id": {"type": "string"},
        "drawn_cents": {"type": "integer"},
        "headroom_cents": {"type": "integer"},
        "balance_cents": {"type": "integer"},
        "interest_cents": {"type": "integer"},
        "graph_node_id": {"type": ["string", "null"]},
    },
    "required": ["bill_id", "drawn_cents", "headroom_cents", "balance_cents"],
    "additionalProperties": False,
}
_PAY_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "payment_id": {"type": "string"}, "state": {"type": "string"},
        "bill_id": {"type": "string"}, "amount_cents": {"type": "integer"},
        "balance_cents": {"type": "integer"}, "reject_reason": {"type": ["string", "null"]},
    },
    "required": ["payment_id", "state", "bill_id", "amount_cents", "balance_cents"],
    "additionalProperties": False,
}
_RESCHEDULE_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bill_id": {"type": "string"}, "new_date": {"type": "string"},
        "previous_date": {"type": ["string", "null"]},
        "graph_node_id": {"type": ["string", "null"]},
    },
    "required": ["bill_id", "new_date"], "additionalProperties": False,
}
_EMAIL_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"message_id": {"type": "string"}, "to": {"type": "string"},
                   "subject": {"type": "string"}},
    "required": ["message_id", "to", "subject"], "additionalProperties": False,
}


# --------------------------------------------------------------------------
# Read-only tools
# --------------------------------------------------------------------------

def make_list_due_bills(book: Book) -> ToolDefinition:
    """Which unpaid bills are due within the next 7 days, most urgent first."""

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        ids = book.due_soon()
        rows = [book.snapshot_bill(b) for b in ids]
        return {"count": len(rows), "bills": rows}

    return ToolDefinition(
        name=LIST_DUE_BILLS,
        description=(
            "List unpaid supplier bills due within the next 7 days, most urgent "
            "first, with supplier, recorded amount and days until due. Read-only. "
            "Use it to decide what to work on; it never tells you anything you may "
            "act on without calling lookup_bill first."),
        input_schema=_EMPTY_INPUT, output_schema=_LIST_OUTPUT,
        side_effect=False, handler=handler,
    )


def make_lookup_bill(book: Book) -> ToolDefinition:
    """One bill and its supplier, minus the control inputs."""

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        bill_id = str(args["bill_id"])
        bill = book.bill(bill_id)
        if bill is None:
            raise ValueError(
                f"unknown bill '{bill_id}'; expected one of {', '.join(book.bill_ids())}")
        supplier_id = book.supplier_id_for(bill_id) or ""
        supplier = book.supplier(supplier_id)
        if supplier is None:
            raise ValueError(
                f"bill '{bill_id}' references unknown supplier '{supplier_id}'")
        return {
            "bill": book.snapshot_bill(bill_id),
            "supplier": _strip(book.snapshot_supplier(supplier_id,
                                                     include_fraud_flag=False)),
        }

    return ToolDefinition(
        name=LOOKUP_BILL,
        description=(
            "Look up one bill with its supplier: recorded amount in integer cents, "
            "currency, reference, issue and due dates, status, days until due, and "
            "the id of any other bill that looks like a duplicate of it. Read-only. "
            "Call this before proposing any payment so you never invent a bill's "
            "details. Note that it does not report whether the supplier's payment "
            "details recently changed - that is a fact the auditor resolves itself."),
        input_schema=_BILL_ID_INPUT, output_schema=_BILL_OUTPUT,
        side_effect=False, handler=handler,
    )


def make_check_funds(book: Book, bundle: Any) -> ToolDefinition:
    """Can the operating account settle this bill? Backed by the real ledger."""

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        bill_id = str(args["bill_id"])
        if book.bill(bill_id) is None:
            raise ValueError(f"unknown bill '{bill_id}'")
        # Read the live balance from the ledger, not from the book's cache, so a
        # payment that already settled in this run is reflected immediately.
        balance = bundle.payments.balance(config.OPERATING_ACCOUNT)
        book.set_balance_cents(config.OPERATING_ACCOUNT, int(balance))
        amount = book.amount_cents(bill_id) or 0
        return {
            "bill_id": bill_id,
            "balance_cents": int(balance),
            "amount_cents": int(amount),
            "shortfall_cents": book.shortfall_cents(bill_id),
            "sufficient": not bool(book.insufficient_funds(bill_id)),
            "currency_supported": book.supports_currency(book.currency(bill_id)),
        }

    return ToolDefinition(
        name=CHECK_FUNDS,
        description=(
            "Read the operating account balance from the payments ledger and report "
            "whether it can settle a given bill in full, with the shortfall if it "
            "cannot and whether the bill's currency is supported. Read-only. Call "
            "this before proposing a payment: a bill the account cannot cover is "
            "REFUSED if you try to pay it, and the correct response is "
            "reschedule_bill or an escalation."),
        input_schema=_BILL_ID_INPUT, output_schema=_FUNDS_OUTPUT,
        side_effect=False, handler=handler,
    )


def make_supplier_profile(book: Book) -> ToolDefinition:
    """Supplier terms and history — with the fraud flag withheld."""

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        supplier_id = str(args["supplier_id"])
        if book.supplier(supplier_id) is None:
            raise ValueError(
                f"unknown supplier '{supplier_id}'; expected one of "
                f"{', '.join(book.supplier_ids())}")
        return {"supplier": _strip(book.snapshot_supplier(
            supplier_id, include_fraud_flag=False))}

    return ToolDefinition(
        name=SUPPLIER_PROFILE,
        description=(
            "Read a supplier's profile: name, email, payment terms, average bill "
            "size, whether they are a listed company, and how many times they were "
            "contacted in the last 7 days. Read-only. Deliberately does NOT report "
            "whether their payment details recently changed - that flag is a fact "
            "the auditor resolves from the book, so an agent cannot argue with a "
            "value it was never given."),
        input_schema=_SUPPLIER_ID_INPUT, output_schema=_SUPPLIER_OUTPUT,
        side_effect=False, handler=handler,
    )


def make_funding_options(book: Book) -> ToolDefinition:
    """Rank the ways to fund a shortfall, with rate provenance."""

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        bill_id = str(args["bill_id"])
        if book.bill(bill_id) is None:
            raise ValueError(f"unknown bill '{bill_id}'")
        days = int(args["days_until_funds"])
        comparison = funding.compare_options(book, bill_id, days_until_funds=days)
        return {
            "bill_id": bill_id,
            "shortfall_cents": comparison["shortfall_cents"],
            "balance_cents": comparison["balance_cents"],
            "days_until_funds": comparison["days_until_funds"],
            "options": comparison["options"],
            "cheapest": comparison["cheapest"],
            "cheapest_cost_cents": comparison["cheapest_cost_cents"],
            "rates_used": comparison["rates_used"],
            "stale": comparison["stale"],
            "disclaimer": comparison["disclaimer"],
        }

    return ToolDefinition(
        name=FUNDING_OPTIONS,
        description=(
            "Compare ways to fund a bill the operating account cannot cover on "
            "time: draw on the credit line at LPR 1y plus the configured spread, "
            "delay the supplier and pay their late fee, or take an early-payment "
            "discount if one is offered. Read-only. Returns the options ranked "
            "cheapest first, each costed in integer cents, with the rate used, the "
            "date that rate is as of, its provenance, and a disclaimer that this is "
            "information and not financial advice. You then propose either "
            "reschedule_bill or pay_bill with funding 'credit_line'; both remain "
            "gated, so ranking an option does not authorise it."),
        input_schema=_FUNDING_INPUT, output_schema=_FUNDING_OUTPUT,
        side_effect=False, handler=handler,
    )


# --------------------------------------------------------------------------
# Side-effecting tools — every call becomes a gated proposal
# --------------------------------------------------------------------------

def make_draw_credit_line(book: Book, bundle: Any) -> ToolDefinition:
    """Draw on the credit-line facility, crediting the operating account.

    This exists because a funding option that only *ranks* costs would let an agent
    pay a bill the account cannot cover, and that is exactly the thing
    `app.insufficient_funds` refuses. So the draw is a real money movement with its
    own gate: the amount is capped against the fact `credit_line_headroom_cents`,
    which comes from configuration minus what has already been drawn. An agent
    cannot enlarge its own facility by asking for more, and cannot draw twice
    against the same headroom.

    It also makes the funding scenario honest end to end: `funding_options` says a
    credit-line draw is cheapest, the draw actually happens under audit, the balance
    actually rises, and the bill then pays from funds that genuinely exist. Nothing
    is weakened to get there — `app.insufficient_funds` still denies a payment the
    account cannot cover at the moment it is proposed.
    """

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        bill_id = str(args["bill_id"])
        if book.bill(bill_id) is None:
            raise ValueError(f"unknown bill '{bill_id}'")
        reason = str(args["reason"]).strip()
        if len(reason) < 12:
            raise ValueError(
                "reason must be at least 12 characters and state the shortfall and "
                "when it is expected to be repaid")
        amount = int(args["amount_cents"])
        if amount <= 0:
            raise ValueError("amount_cents must be a positive integer")

        headroom = book.credit_line_headroom_cents()
        if amount > headroom:
            # Belt and braces: the rule caps this against the same fact, but the
            # handler must not rely on the gate having been consulted.
            raise ValueError(
                f"draw of {config.money(amount)} exceeds the remaining facility "
                f"{config.money(headroom)}")

        new_headroom = book.draw_credit_line(amount)
        balance = book.balance_cents()

        # Cost of the draw, quoted at the same rate `funding_options` used, so the
        # owner sees one number rather than two that might disagree.
        days = 30
        rate_used = funding.rate("lpr_1y")
        annual = rate_used.percent + funding._bps_to_percent(  # noqa: SLF001
            config.CREDIT_LINE_SPREAD_BPS)
        interest = funding.interest_cost_cents(amount, annual, days)

        node_id = None
        graph = bundle.graph
        if graph is not None:
            # Published as an OBSERVATION, not verified: the draw is real money, but
            # the financing decision behind it is this agent's recommendation, and
            # an inference must never overwrite a record another app owns.
            node = graph.add_observation(
                "funding", bill_id,
                {"bill_id": bill_id, "kind": "credit_line",
                 "drawn_cents": amount, "headroom_cents": new_headroom,
                 "interest_cents": interest, "rate_name": rate_used.name,
                 "annual_percent": str(annual),
                 "as_of": rate_used.as_of.isoformat(),
                 "reason": reason},
                observer=config.GRAPH_OBSERVER)
            node_id = node["node_id"]

        return {
            "bill_id": bill_id,
            "drawn_cents": amount,
            "headroom_cents": new_headroom,
            "balance_cents": balance,
            "interest_cents": interest,
            "rate_name": rate_used.name,
            "annual_percent": str(annual),
            "as_of": rate_used.as_of.isoformat(),
            "stale": rate_used.stale,
            "reason": reason,
            "graph_node_id": node_id,
        }

    return ToolDefinition(
        name=DRAW_CREDIT_LINE,
        description=(
            "Draw on the business's credit line, crediting the operating account so "
            "an unaffordable bill becomes affordable. Side effect: it is real "
            "borrowing, so it is gated and capped against the remaining facility, "
            "which the auditor resolves from configuration rather than from your "
            "argument. Requires a factual reason of at least 12 characters. Only "
            "propose this after funding_options ranked it and you are prepared to "
            "explain the cost to the owner."),
        input_schema=_DRAW_INPUT, output_schema=_DRAW_OUTPUT,
        side_effect=True, handler=handler,
    )


def make_pay_bill(book: Book, bundle: Any) -> ToolDefinition:
    """Pay a bill in full. The recorded amount, to the supplier on file.

    The two arguments the agent does NOT get are the point:

      * no `payer` — always `config.OPERATING_ACCOUNT`;
      * no `payee` — always the supplier recorded against the bill.

    And the amount that reaches the ledger is the recorded amount, not the one the
    agent asked for. The agent's number is still checked (that is what
    `app.amount_matches_bill` and `app.large_payment_approval` do) but it is never
    trusted to move money.

    A credit-line draw is verified against the facility limit here as well as in
    `funding.py`, because a tool that executes money movement must not rely on a
    read-only tool having said the option was available. Fail closed.
    """

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        bill_id = str(args["bill_id"])
        bill = book.bill(bill_id)
        if bill is None:
            raise ValueError(f"unknown bill '{bill_id}'")
        if book.status(bill_id) == "paid":
            # Already settled. Reusing the idempotency key would replay the cached
            # record, but a fresh call must not pay twice, so refuse explicitly.
            raise ValueError(
                f"bill '{bill_id}' is already paid; do not pay it again")

        supplier_id = book.supplier_id_for(bill_id) or ""
        recorded_cents = book.amount_cents(bill_id)
        if recorded_cents is None:
            raise ValueError(
                f"bill '{bill_id}' has no recorded amount; cannot pay an amount "
                "that could not be resolved")
        currency = book.currency(bill_id) or ""
        auth_code = str(args["auth_code"])
        funding_kind = str(args.get("funding") or "").strip()
        memo = str(args.get("memo") or "")
        idempotency_key = str(args.get("idempotency_key") or f"pay-{bill_id}")

        if not book.supports_currency(currency):
            raise ValueError(
                f"bill '{bill_id}' is in {currency} but the operating account holds "
                f"{book.account_currency()}; this app has no FX desk")

        # `funding` is a memo annotation, not a money source. It records HOW the
        # account came to hold the money; it cannot make an unaffordable bill
        # affordable. Drawing on the credit line is a separate gated tool
        # (`draw_credit_line`), and if it never happened then the balance is still
        # short and `app.insufficient_funds` denies this payment. So there is
        # nothing to special-case here — claiming `funding: credit_line` without
        # having drawn simply fails on the facts.
        if not memo:
            memo = f"Settlement of {bill_id} ({book.reference(bill_id)})"
        if funding_kind == "credit_line":
            memo = f"{memo} [funding: credit_line]"

        record = bundle.payments.send_payment(
            payer=config.OPERATING_ACCOUNT,
            payee=supplier_id,
            amount_cents=int(recorded_cents),
            currency=currency,
            auth_code=auth_code,
            idempotency_key=idempotency_key,
            memo=memo,
        )
        state = str(record.get("state") or "")

        out: dict[str, Any] = {
            "payment_id": str(record.get("payment_id") or ""),
            "state": state,
            "bill_id": bill_id,
            "amount_cents": int(recorded_cents),
            "balance_cents": book.balance_cents(),
            "reject_reason": record.get("reject_reason"),
            "idempotency_key": idempotency_key,
            "funding": funding_kind or "operating_balance",
        }

        if state != "settled":
            # Rejected by the ledger (bad or missing auth_code, or a replayed key
            # returning a cached rejection). Nothing moved, so nothing is published
            # and the bill stays unpaid. The reason goes back to the agent as an
            # observation so it can act on it rather than fail blind.
            out["balance_cents"] = book.balance_cents()
            return out

        # Settled: mirror the ledger balance, mark the bill paid, publish verified.
        balance = bundle.payments.balance(config.OPERATING_ACCOUNT)
        book.set_balance_cents(config.OPERATING_ACCOUNT, int(balance))
        book.mark_paid(bill_id, payment_id=str(record.get("payment_id")),
                       amount_cents=int(recorded_cents))
        out["balance_cents"] = int(balance)

        graph = bundle.graph
        if graph is not None:
            node = graph.upsert_verified(
                "bill", bill_id,
                {"bill_id": bill_id, "supplier_id": supplier_id,
                 "amount_cents": int(recorded_cents), "currency": currency,
                 "status": "paid", "payment_id": str(record.get("payment_id")),
                 "paid_at": book.today.isoformat(),
                 "reference": book.reference(bill_id)},
                source=config.GRAPH_SOURCE)
            out["graph_node_id"] = node["node_id"]
        return out

    return ToolDefinition(
        name=PAY_BILL,
        description=(
            "Pay a supplier bill in full. Side effect: it moves money, so the audit "
            "agent reviews the call before it executes. The payer account and the "
            "payee are NOT arguments - the account is app configuration and the "
            "payee is the supplier on file, so this tool cannot send funds anywhere "
            "else. The amount that executes is the RECORDED bill amount, not the "
            "amount you asked for. Requires an auth_code from an intent the owner "
            "confirmed. Reuse the same idempotency_key on a retry so a retry cannot "
            "pay twice. If held, stop and surface the owner's question. If denied, "
            "read the reason and do not reword and retry."),
        input_schema=_PAY_INPUT, output_schema=_PAY_OUTPUT,
        side_effect=True, handler=handler,
    )


def make_reschedule_bill(book: Book, bundle: Any) -> ToolDefinition:
    """Move the scheduled date. The bill stays unpaid and stays visible."""

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        bill_id = str(args["bill_id"])
        if book.bill(bill_id) is None:
            raise ValueError(f"unknown bill '{bill_id}'")
        reason = str(args["reason"]).strip()
        if len(reason) < 12:
            # The manifest's required_arguments cannot express a minimum length for
            # a string that is present but empty of meaning, so the handler refuses
            # too. An unexplained date change is indistinguishable from hiding a
            # bill the agent could not pay.
            raise ValueError(
                "reason must be at least 12 characters and say what is short, how "
                "much, and when funds are expected; 'reschedule' is not a reason")
        try:
            new_date = date.fromisoformat(str(args["new_date"]))
        except ValueError as exc:
            raise ValueError(
                f"new_date must be ISO 8601 (YYYY-MM-DD), got "
                f"{args['new_date']!r}") from exc
        if new_date < book.today:
            raise ValueError(
                f"new_date {new_date.isoformat()} is in the past; a reschedule must "
                f"move the bill to a future date (today is {book.today.isoformat()})")

        previous = book.scheduled_date(bill_id)
        updated = book.reschedule(bill_id, new_date=new_date, reason=reason)

        node_id = None
        graph = bundle.graph
        if graph is not None:
            # Published VERIFIED: this app is the authority for bill records, and
            # the cash planner must forecast against the real date rather than the
            # original one.
            node = graph.upsert_verified(
                "bill", bill_id,
                {"bill_id": bill_id,
                 "supplier_id": updated.get("supplier_id"),
                 "amount_cents": book.amount_cents(bill_id),
                 "currency": book.currency(bill_id),
                 "status": "rescheduled",
                 "scheduled_date": new_date.isoformat(),
                 "previous_scheduled_date": previous.isoformat() if previous else None,
                 "reason": reason},
                source=config.GRAPH_SOURCE)
            node_id = node["node_id"]

        return {
            "bill_id": bill_id,
            "new_date": new_date.isoformat(),
            "previous_date": previous.isoformat() if previous else None,
            "status": str(updated.get("status")),
            "reason": reason,
            "graph_node_id": node_id,
        }

    return ToolDefinition(
        name=RESCHEDULE_BILL,
        description=(
            "Move a bill's scheduled payment date without paying it. Side effect: "
            "it changes the book and publishes bill[rescheduled] to the shared "
            "knowledge graph so the cash planner forecasts against the real date. "
            "This is the correct response to a bill the account cannot cover - the "
            "obligation stays visible instead of being settled with money that is "
            "not there. Requires a factual reason of at least 12 characters and a "
            "future date."),
        input_schema=_RESCHEDULE_INPUT, output_schema=_RESCHEDULE_OUTPUT,
        side_effect=True, handler=handler,
    )


def make_send_remittance(book: Book, bundle: Any) -> ToolDefinition:
    """Email the supplier remittance advice, and count the contact."""

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        supplier_id = str(args["supplier_id"])
        bill_id = str(args["bill_id"])
        if book.supplier(supplier_id) is None:
            raise ValueError(f"unknown supplier '{supplier_id}'")
        if book.bill(bill_id) is None:
            raise ValueError(f"unknown bill '{bill_id}'")
        if book.supplier_id_for(bill_id) != supplier_id:
            # A pairing check the declarative rules cannot express (policy resolves
            # facts for one subject at a time), so the handler refuses. Fails loudly
            # and safely: nothing is sent.
            raise ValueError(
                f"bill '{bill_id}' belongs to supplier "
                f"'{book.supplier_id_for(bill_id)}', not '{supplier_id}'; refusing "
                "to email a supplier about somebody else's bill")

        to = book.supplier_email(supplier_id)
        if not to:
            raise ValueError(
                f"supplier '{supplier_id}' has no email address on file; cannot send "
                "remittance advice")

        subject = str(args["subject"])
        body = str(args["body"])
        sent = bundle.email.send(to=to, subject=subject, body=body,
                                 template="invoice_delivery")

        # Count the contact ourselves. The runtime only does this for a tool named
        # exactly `send_email`; without this the frequency rule would never see the
        # contacts it counts. Both the outbox write and the count happen, or an
        # email that reached a supplier could be sent again unnoticed.
        book.record_sent_email(
            supplier_id=supplier_id, bill_id=bill_id, subject=subject, body=body,
            kind="remittance", to=to)
        book.record_supplier_contact(supplier_id)

        return {
            "message_id": str(sent.get("message_id") or ""),
            "to": to,
            "subject": subject,
            "supplier_id": supplier_id,
            "bill_id": bill_id,
            "contacts_in_window": book.contact_count(
                supplier_id, config.SUPPLIER_CONTACT_WINDOW_DAYS),
        }

    return ToolDefinition(
        name=SEND_REMITTANCE,
        description=(
            "Email a supplier remittance advice after a payment settled. Side "
            "effect: it contacts a human outside the system. The recipient is "
            "resolved from the supplier on file, not from your arguments. At most "
            "two emails per supplier in any rolling 7 days, counted from trusted "
            "history - not from what you say about having contacted them. When you "
            "hit the limit, stop; do not find another subject line or another bill "
            "belonging to the same supplier."),
        input_schema=_REMITTANCE_INPUT, output_schema=_EMAIL_OUTPUT,
        side_effect=True, handler=handler,
    )


def make_escalate_to_owner(book: Book, bundle: Any) -> ToolDefinition:
    """Email the owner about an exception. The recipient is never an argument."""

    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        bill_id = str(args["bill_id"])
        if book.bill(bill_id) is None:
            raise ValueError(f"unknown bill '{bill_id}'")
        body = str(args["body"]).strip()
        if len(body) < 12:
            raise ValueError(
                "body must be at least 12 characters and state what was refused, "
                "the reason as the tool reported it, and what the owner must decide")
        subject = str(args["subject"])

        sent = bundle.email.send(to=OWNER_EMAIL, subject=subject, body=body,
                                 template="escalate_to_owner")
        return {
            "message_id": str(sent.get("message_id") or ""),
            "to": OWNER_EMAIL,
            "subject": subject,
            "bill_id": bill_id,
        }

    return ToolDefinition(
        name=ESCALATE_TO_OWNER,
        description=(
            "Email the owner about an exception this agent may not resolve: a "
            "refused payment, a supplier whose payee details changed, a bill it "
            "cannot fund. Side effect. The recipient is always the owner and is "
            "never an argument, so this tool cannot be used to contact a supplier - "
            "which is why it does not count against the supplier contact limit. "
            "Quote the refusal exactly as the tool reported it; do not paraphrase "
            "it into something that sounds safe to approve."),
        input_schema=_ESCALATE_INPUT, output_schema=_EMAIL_OUTPUT,
        side_effect=True, handler=handler,
    )


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def build_tools(book: Book, bundle: Any) -> list[ToolDefinition]:
    """All nine tools, wired to this app's book and the platform's services."""
    return [
        make_list_due_bills(book),
        make_lookup_bill(book),
        make_check_funds(book, bundle),
        make_supplier_profile(book),
        make_funding_options(book),
        make_draw_credit_line(book, bundle),
        make_pay_bill(book, bundle),
        make_reschedule_bill(book, bundle),
        make_send_remittance(book, bundle),
        make_escalate_to_owner(book, bundle),
    ]


def remittance_text(book: Book, bill_id: str) -> tuple[str, str]:
    """Render the remittance subject and body for a paid bill.

    Used by the scenario scripts so the agent proposes text built from recorded
    values rather than inventing a reference. Every field comes from the book.
    """
    supplier_id = book.supplier_id_for(bill_id) or ""
    paid_on = (book.bill(bill_id) or {}).get("paid_at") or book.today.isoformat()
    amount = config.money(book.amount_cents(bill_id), book.currency(bill_id) or "")
    reference = book.reference(bill_id)
    subject = REMITTANCE_SUBJECT.format(reference=reference, bill_id=bill_id)
    body = REMITTANCE_BODY.format(
        supplier=book.supplier_name(supplier_id), bill_id=bill_id,
        reference=reference, amount=amount, paid_on=paid_on,
        payment_id=(book.bill(bill_id) or {}).get("payment_id") or "pending")
    return subject, body


def _strip(record: Mapping[str, Any]) -> dict[str, Any]:
    """A copy with the control inputs removed.

    Shallow on purpose: the withheld names are top-level fields of the snapshot,
    and a deep walk would also strip nested keys that mean something else.
    """
    return {k: v for k, v in dict(record).items() if k not in WITHHELD_FROM_AGENT}


__all__ = [
    "CHECK_FUNDS", "ESCALATE_TO_OWNER", "FUNDING_OPTIONS", "LIST_DUE_BILLS",
    "LOOKUP_BILL", "OWNER_EMAIL", "PAY_BILL", "READ_ONLY_TOOLS",
    "RESCHEDULE_BILL", "SEND_REMITTANCE", "SIDE_EFFECT_TOOLS", "SUPPLIER_PROFILE",
    "TOOL_NAMES", "WITHHELD_FROM_AGENT", "build_tools", "remittance_text",
]

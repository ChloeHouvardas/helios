"""The deterministic audit gate.

Checks a proposal against the agent's mandate and returns a verdict:
approve, hold, or reject. No LLM is involved — every rule is code, so the
same proposal always gets the same verdict.

Fail-closed: unknown tools, tools outside the mandate, malformed
arguments, or references the gate cannot verify are never allowed
through. Rule order is fixed: rejection rules first, then hold rules,
then approve.

Rules (in evaluation order), tool-specific ones apply to send_email:
  unknown_tool             reject  tool is not registered
  tool_not_allowed         reject  tool exists but is not in the mandate
  malformed_arguments      reject  arguments fail the tool's input schema
  unknown_reference        reject  customer/invoice unknown or mismatched
  extension_over_max       reject  extension longer than max_extension_days
  contact_too_frequent     reject  customer contacted within
                                   min_days_between_contacts
  large_invoice_needs_owner hold   invoice amount >=
                                   invoice_owner_approval_amount
  extension_needs_owner    hold    extension longer than
                                   extension_owner_approval_days
  within_mandate           approve everything else
"""

from __future__ import annotations

import jsonschema
from jsonschema import ValidationError

from .contracts import ToolRegistry
from .models import APPROVE, HOLD, REJECT, Mandate, Proposal, Verdict
from .store import Store
from .tools import SEND_EMAIL

RULE_UNKNOWN_TOOL = "unknown_tool"
RULE_TOOL_NOT_ALLOWED = "tool_not_allowed"
RULE_MALFORMED_ARGUMENTS = "malformed_arguments"
RULE_UNKNOWN_REFERENCE = "unknown_reference"
RULE_EXTENSION_OVER_MAX = "extension_over_max"
RULE_CONTACT_TOO_FREQUENT = "contact_too_frequent"
RULE_LARGE_INVOICE = "large_invoice_needs_owner"
RULE_EXTENSION_NEEDS_OWNER = "extension_needs_owner"
RULE_WITHIN_MANDATE = "within_mandate"


def _reject(rule: str, reason: str) -> Verdict:
    return Verdict(decision=REJECT, rule=rule, reason=reason)


def _hold(rule: str, reason: str) -> Verdict:
    return Verdict(decision=HOLD, rule=rule, reason=reason)


def _approve() -> Verdict:
    return Verdict(
        decision=APPROVE,
        rule=RULE_WITHIN_MANDATE,
        reason="proposal is within the agent's mandate",
    )


class AuditGate:
    """Deterministic mandate check over one tool registry and data store."""

    def __init__(self, tools: ToolRegistry, store: Store):
        self._tools = tools
        self._store = store

    def check(self, proposal: Proposal, mandate: Mandate) -> Verdict:
        tool = self._tools.get(proposal.tool)
        if tool is None:
            return _reject(
                RULE_UNKNOWN_TOOL,
                f"tool {proposal.tool!r} is not registered",
            )
        if proposal.tool not in mandate.allowed_tools:
            return _reject(
                RULE_TOOL_NOT_ALLOWED,
                f"tool {proposal.tool!r} is not in the agent's mandate "
                f"(allowed: {list(mandate.allowed_tools)})",
            )
        try:
            jsonschema.validate(instance=dict(proposal.arguments), schema=tool.input_schema)
        except ValidationError as error:
            message = str(error).replace("\n", " ")
            if len(message) > 300:
                message = message[:300] + "..."
            return _reject(
                RULE_MALFORMED_ARGUMENTS,
                f"arguments do not match the {proposal.tool} input schema: {message}",
            )
        if proposal.tool == SEND_EMAIL:
            return self._check_send_email(proposal, mandate)
        return _approve()

    def _check_send_email(self, proposal: Proposal, mandate: Mandate) -> Verdict:
        limits = mandate.limits
        args = proposal.arguments
        customer_id = str(args["customer_id"])
        invoice_id = str(args["invoice_id"])
        customer = self._store.get_customer(customer_id)
        invoice = self._store.get_invoice(invoice_id)
        if customer is None:
            return _reject(RULE_UNKNOWN_REFERENCE, f"unknown customer: {customer_id!r}")
        if invoice is None:
            return _reject(RULE_UNKNOWN_REFERENCE, f"unknown invoice: {invoice_id!r}")
        if invoice.get("customer_id") != customer_id:
            return _reject(
                RULE_UNKNOWN_REFERENCE,
                f"invoice {invoice_id} does not belong to customer {customer_id}",
            )

        extension = args.get("extension_offer_days")
        if (
            isinstance(extension, int)
            and limits.max_extension_days is not None
            and extension > limits.max_extension_days
        ):
            return _reject(
                RULE_EXTENSION_OVER_MAX,
                f"extension of {extension} days exceeds the maximum of "
                f"{limits.max_extension_days} days",
            )

        if limits.min_days_between_contacts is not None:
            days_since_contact = self._store.days_since(customer.get("last_contacted"))
            if (
                days_since_contact is not None
                and days_since_contact < limits.min_days_between_contacts
            ):
                return _reject(
                    RULE_CONTACT_TOO_FREQUENT,
                    f"customer was contacted {days_since_contact} day(s) ago; the mandate "
                    f"requires at least {limits.min_days_between_contacts} days between contacts",
                )

        amount = invoice.get("amount")
        if (
            limits.invoice_owner_approval_amount is not None
            and isinstance(amount, (int, float))
            and amount >= limits.invoice_owner_approval_amount
        ):
            return _hold(
                RULE_LARGE_INVOICE,
                f"invoice amount {amount} is at or above the owner-approval threshold of "
                f"{limits.invoice_owner_approval_amount}",
            )

        if (
            isinstance(extension, int)
            and limits.extension_owner_approval_days is not None
            and extension > limits.extension_owner_approval_days
        ):
            return _hold(
                RULE_EXTENSION_NEEDS_OWNER,
                f"extension of {extension} days is longer than the owner-approval threshold "
                f"of {limits.extension_owner_approval_days} days",
            )
        return _approve()

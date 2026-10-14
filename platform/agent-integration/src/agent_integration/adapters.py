"""Adapters that close the shape mismatch between the two builders.

Neither package imports the other, and neither should: the audit agent is
only trustworthy because an app cannot modify the code that audits it. So
the translation lives here, in the glue layer that is the one place in the
repo allowed to know about both.

Two gaps matter, and both fail *silently* if left unadapted:

1. ``ToolDescriptor.from_contract()`` reads ``has_side_effect``, but the
   action side's ``ToolDefinition`` names the field ``side_effect``. The
   missing attribute defaults to ``False``, so ``send_email`` would register
   as read-only — and read-only calls are never gated. The action agent
   would be free to email customers with no audit at all, while everything
   looked correctly wired. ``ToolContractView`` exists to stop that.

2. ``from_contract()`` calls ``input_schema()`` as a method and ignores a
   plain Mapping. ``ToolDefinition.input_schema`` is a Mapping, so the
   auditor would end up with no schema and ``baseline.arguments_validate``
   would never fire. Malformed arguments would reach execution.

``HeliosStoreFactSource`` is the third adapter, and it is the trust
boundary: it turns the action side's ``Store`` into the trusted facts the
auditor's rules read, so a limit on an invoice amount is checked against
the ledger rather than against whatever the agent claimed.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from agent_toolkit.contracts import ToolDefinition
from agent_toolkit.store import Store


class ToolContractView:
    """An ``agent_toolkit.ToolDefinition`` in the shape the auditor expects.

    Duck-typed on purpose, mirroring ``ToolDescriptor.from_contract()``:
    this class is handed to the audit agent, which never imports
    ``agent_toolkit``. Both attribute names are exposed so the view works
    whichever convention a caller reads.
    """

    def __init__(self, tool: ToolDefinition) -> None:
        self._tool = tool

    @property
    def tool(self) -> ToolDefinition:
        """The underlying action-side tool."""
        return self._tool

    @property
    def name(self) -> str:
        return self._tool.name

    @property
    def description(self) -> str:
        return self._tool.description

    #: The name ``ToolDescriptor.from_contract()`` actually looks for.
    @property
    def has_side_effect(self) -> bool:
        return self._tool.side_effect

    #: The action side's own name, kept so this view is usable there too.
    @property
    def side_effect(self) -> bool:
        return self._tool.side_effect

    def input_schema(self) -> dict[str, Any]:
        """A *deep* copy, so the auditor cannot mutate the action side's schema.

        ``dict()`` alone would share the nested ``properties`` mapping, and a
        write there would reach back into the live ``ToolDefinition``.
        """
        return deepcopy(self._tool.input_schema)

    def output_schema(self) -> dict[str, Any]:
        return deepcopy(self._tool.output_schema)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ToolContractView({self.name!r}, "
            f"has_side_effect={self.has_side_effect})"
        )


def tool_views(tools: Iterable[ToolDefinition]) -> list[ToolContractView]:
    return [ToolContractView(tool) for tool in tools]


class HeliosStoreFactSource:
    """Trusted facts for the auditor, read from the action side's ``Store``.

    Implements ``audit_agent.facts.FactSource`` directly rather than going
    through their ``StoreFactSource`` adapter, because the semantics here
    are app-specific and worth stating plainly:

    - An **amount** belongs to an invoice. Customer ids deliberately resolve
      to no amount, so a call naming both a customer and an invoice leaves
      exactly one trusted amount in scope — which is what lets generated
      policy say ``fact: amount_due`` without knowing the subject id.
    - A **contact count** belongs to a customer. Asked about an invoice it
      counts contacts on that invoice's customer, which is the business
      question a frequency limit is actually asking.
    - An **unknown subject** yields ``None``, not zero. The auditor's rules
      treat an unresolvable fact as unverifiable and deny, so guessing here
      would convert a fail-closed control into a fail-open one.

    Contact history is seeded lazily from each customer's
    ``last_contacted`` and extended only when the runtime reports an
    execution that actually happened. The agent never writes to it — that
    asymmetry is the whole point of a fact source.
    """

    def __init__(self, store: Store, *, contact_window_days: int = 7) -> None:
        self._store = store
        self.contact_window_days = contact_window_days
        #: customer_id -> dates of known contacts, oldest first
        self._contacts: dict[str, list[date]] = {}
        self._seeded: set[str] = set()

    # -- Subject resolution ------------------------------------------------

    def _customer_for(self, subject_id: str) -> str | None:
        """Resolve any subject id to the customer it concerns."""
        invoice = self._store.get_invoice(subject_id)
        if invoice is not None:
            customer_id = invoice.get("customer_id")
            return str(customer_id) if customer_id else None
        if self._store.get_customer(subject_id) is not None:
            return subject_id
        return None

    def _seed(self, customer_id: str) -> bool:
        """Load a customer's existing contact date once. True if known."""
        if customer_id in self._seeded:
            return customer_id in self._contacts
        self._seeded.add(customer_id)
        customer = self._store.get_customer(customer_id)
        if customer is None:
            return False
        self._contacts.setdefault(customer_id, [])
        raw = customer.get("last_contacted")
        if isinstance(raw, str) and raw:
            try:
                self._contacts[customer_id].append(date.fromisoformat(raw))
            except ValueError:
                # An unparseable date is no date. Leave the history empty
                # rather than inventing a contact that may not have happened.
                pass
        return True

    # -- FactSource --------------------------------------------------------

    def amount_for(self, subject_id: str) -> Decimal | None:
        invoice = self._store.get_invoice(subject_id)
        if invoice is None:
            return None
        amount = invoice.get("amount")
        if isinstance(amount, bool) or amount is None:
            return None
        # Never route money through float: Decimal(str(...)) keeps the
        # fixture's decimal value exact for threshold comparisons.
        return Decimal(str(amount))

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        customer_id = self._customer_for(subject_id)
        if customer_id is None or not self._seed(customer_id):
            return None  # unknown subject -> unverifiable -> the auditor denies
        cutoff = self._store.today - timedelta(days=within_days)
        return sum(1 for when in self._contacts[customer_id] if when >= cutoff)

    def counterparty_for(self, subject_id: str) -> str | None:
        customer_id = self._customer_for(subject_id)
        if customer_id is None:
            return None
        customer = self._store.get_customer(customer_id) or {}
        name = customer.get("name") or customer_id
        return str(name)

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        """Not modelled by the collections fixtures.

        ``None`` means "no trusted answer", which is honest. A rule using
        ``deny_if_fact_true: [payment_details_changed]`` would not fire —
        that is acceptable only because this app declares no such rule. An
        app that needs the Payables fraud signal must wire a source that can
        actually answer.
        """
        return None

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        """Trusted scalars generated policy can name directly."""
        invoice = self._store.get_invoice(subject_id)
        if invoice is None:
            return {}
        out: dict[str, Any] = {}
        amount = invoice.get("amount")
        if amount is not None and not isinstance(amount, bool):
            value = Decimal(str(amount))
            # Both names, because a generated manifest may use either and
            # `max_from_fact` looks the key up verbatim.
            out["amount_due"] = value
            out["invoice_amount"] = value
        currency = invoice.get("currency")
        if isinstance(currency, str):
            out["currency"] = currency
        due_date = invoice.get("due_date")
        if isinstance(due_date, str):
            days = self._store.days_since(due_date)
            if days is not None:
                out["days_overdue"] = max(days, 0)
        return out

    # -- Written only by the runtime, after an approved execution ----------

    def record_contact(self, customer_id: str, when: date | None = None) -> None:
        """Note that a contact really happened.

        Called by the runtime *after* a side effect executes, never before
        and never on the agent's say-so. Deduplicated by date so a resumed
        run cannot inflate the count.
        """
        when = when or self._store.today
        self._seed(customer_id)
        history = self._contacts.setdefault(customer_id, [])
        history.append(when)
        history.sort()


__all__ = ["HeliosStoreFactSource", "ToolContractView", "tool_views"]

"""Fact sources: where the audit agent gets truth from.

The security property the whole design rests on is that an action agent
cannot widen its own authority by describing the world favourably. So
every value a rule depends on is resolved here, from a source the action
agent does not control, and lands in `AuditFacts`.

The Build 1 store is exactly such a source — it holds the authoritative
invoice amount and contact history. But the audit agent must not import
it: that would couple two separately deployed agents together. Instead
this module defines a small protocol and provides adapters. Any object
that can answer `amount_for` and `contact_count_for` works, including a
plain dict for tests and a remote client for a deployed app.

Rules that need a fact and cannot get one fail closed: a limit that
cannot be evaluated is treated as unsatisfied, never as passed.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from .policy import AuditFacts


@runtime_checkable
class FactSource(Protocol):
    """A trusted source of financial facts.

    Implementations must not consult the action agent's request; that is
    the point of having them.
    """

    def amount_for(self, subject_id: str) -> Decimal | None:
        """Trusted monetary amount for a subject, e.g. an invoice."""
        ...

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        """Trusted number of prior contacts in a trailing window."""
        ...

    def counterparty_for(self, subject_id: str) -> str | None:
        """Trusted identity of the customer/supplier, for change detection."""
        ...

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        """Whether the counterparty's payment details changed recently."""
        ...

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        """Any other trusted scalar facts about a subject.

        This is how policy refers to app-specific signals — a recorded
        invoice amount, a duplicate flag, an outlier score — without the
        fact source having to know which ones a given app declared. Keys
        are the names used in a manifest's `fact` and `max_from_fact`.
        """
        ...


class StaticFactSource:
    """Fact source backed by fixed data.

    Used by tests and by the demo. A deployed app swaps in something that
    reads its own database; nothing else changes.
    """

    def __init__(
        self,
        amounts: dict[str, Any] | None = None,
        contacts: dict[str, int] | None = None,
        counterparties: dict[str, str] | None = None,
        payment_details_changed: dict[str, bool] | None = None,
        extras: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._amounts = {k: _to_decimal(v) for k, v in (amounts or {}).items()}
        self._contacts = dict(contacts or {})
        self._counterparties = dict(counterparties or {})
        self._changed = dict(payment_details_changed or {})
        self._extras = {k: dict(v) for k, v in (extras or {}).items()}

    def amount_for(self, subject_id: str) -> Decimal | None:
        return self._amounts.get(subject_id)

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        return self._contacts.get(subject_id)

    def counterparty_for(self, subject_id: str) -> str | None:
        return self._counterparties.get(subject_id)

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        return self._changed.get(subject_id)

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        return dict(self._extras.get(subject_id, {}))


class StoreFactSource:
    """Adapts an object shaped like the Build 1 `InvoiceStore`.

    Duck-typed: only `amount_due`, `recent_contact_count` and `get` are
    touched, and each is optional. An adapter that required importing the
    action agent's package would defeat the standalone requirement, so it
    does not.
    """

    #: Record attributes exposed to policy as trusted facts. Names here are
    #: the keys a manifest refers to in `fact:` and `max_from_fact:`. An app
    #: with different fields subclasses and extends this tuple rather than
    #: editing the runtime.
    EXTRA_FACT_ATTRS: tuple[str, ...] = (
        "amount_due",
        "currency",
        "days_overdue",
        "duplicate_invoice",
        "payment_details_changed",
        "bank_details_changed",
        "invoice_amount",
        "outlier_amount",
    )

    def __init__(self, store: Any, *, contact_window_days: int = 7) -> None:
        self.store = store
        self.contact_window_days = contact_window_days

    def amount_for(self, subject_id: str) -> Decimal | None:
        reader = getattr(self.store, "amount_due", None)
        if not callable(reader):
            return None
        try:
            return _to_decimal(reader(subject_id))
        except Exception:
            # An unknown or unreadable record is "no fact", which rules
            # treat as unsatisfied. Never guess a number.
            return None

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        reader = getattr(self.store, "recent_contact_count", None)
        if not callable(reader):
            return None
        try:
            return int(reader(subject_id, within_days=within_days))
        except Exception:
            return None

    def counterparty_for(self, subject_id: str) -> str | None:
        getter = getattr(self.store, "get", None)
        if not callable(getter):
            return None
        try:
            record = getter(subject_id)
        except Exception:
            return None
        for attr in ("customer_id", "supplier_id", "customer_name", "supplier_name"):
            value = getattr(record, attr, None)
            if isinstance(value, str) and value:
                return value
        return None

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        getter = getattr(self.store, "get", None)
        if not callable(getter):
            return None
        try:
            record = getter(subject_id)
        except Exception:
            return None
        for attr in ("payment_details_changed", "bank_details_changed"):
            value = getattr(record, attr, None)
            if isinstance(value, bool):
                return value
        return None

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        """Read trusted scalars straight off the record.

        Restricted to plain scalars on purpose. A nested structure could
        carry anything, and the resolver flattens extras into names policy
        refers to by name — a scalar-only rule keeps that mapping honest.
        """
        getter = getattr(self.store, "get", None)
        if not callable(getter):
            return {}
        try:
            record = getter(subject_id)
        except Exception:
            return {}
        out: dict[str, Any] = {}
        for attr in self.EXTRA_FACT_ATTRS:
            value = getattr(record, attr, None)
            # bool is a subclass of int, so it passes the scalar test below;
            # listing it explicitly documents that flags are intended facts.
            if isinstance(value, (str, int, float, Decimal, bool)):
                out[attr] = value
        return out


class ChainedFactSource:
    """Try sources in order and take the first non-None answer.

    Lets an app layer a remote knowledge-graph client over a local cache
    without the rules knowing either exists.
    """

    def __init__(self, sources: list[FactSource]) -> None:
        self.sources = list(sources)

    def _first(self, method: str, *args: Any) -> Any:
        for source in self.sources:
            value = getattr(source, method)(*args)
            if value is not None:
                return value
        return None

    def amount_for(self, subject_id: str) -> Decimal | None:
        return self._first("amount_for", subject_id)

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        return self._first("contact_count_for", subject_id, within_days)

    def counterparty_for(self, subject_id: str) -> str | None:
        return self._first("counterparty_for", subject_id)

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        return self._first("payment_details_changed_for", subject_id)

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        """Merge extras from every source; earlier sources win on conflict.

        Merging rather than first-wins, because different sources legitimately
        know different facts about one subject. On a key both define, the
        earlier (more local, more authoritative) source is kept.
        """
        merged: dict[str, Any] = {}
        for source in reversed(self.sources):
            reader = getattr(source, "extra_facts_for", None)
            if not callable(reader):
                continue
            try:
                values = reader(subject_id)
            except Exception:
                continue
            if isinstance(values, dict):
                merged.update(values)
        return merged


class FactResolver:
    """Builds `AuditFacts` for a request by querying a fact source.

    Subject ids come from the request's arguments — the agent names the
    record it wants to act on — but every value attached to that id comes
    from the fact source. Naming a subject is not the same as asserting
    facts about it.
    """

    #: Argument keys that identify a business subject.
    SUBJECT_KEYS: tuple[str, ...] = (
        "invoice_id",
        "bill_id",
        "customer_id",
        "supplier_id",
        "subject_id",
        "payment_id",
    )

    def __init__(
        self,
        source: FactSource | None = None,
        *,
        contact_window_days: int = 7,
    ) -> None:
        self.source = source
        self.contact_window_days = contact_window_days

    def subject_ids(self, arguments: dict[str, Any]) -> list[str]:
        """Subject ids named in the arguments, in declaration order."""
        found: list[str] = []
        for key in self.SUBJECT_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value and value not in found:
                found.append(value)
        return found

    def resolve(
        self,
        arguments: dict[str, Any],
        *,
        extra_subjects: list[str] | None = None,
        tool_call_counts: dict[str, int] | None = None,
    ) -> AuditFacts:
        """Query the fact source for every subject in scope.

        **Nothing here is caller-supplied except the subject ids.** That is a
        deliberate and load-bearing restriction. The arguments and context an
        action agent sends are *claims*: the agent names the invoice it wants
        to act on, and that naming is useful because it selects which record
        to look up. But every value attached to that record — the amount, the
        contact count, whether the payee's bank details changed — comes from
        the fact source.

        An earlier version of this method accepted an `extras` mapping so
        callers could seed facts directly. That was a hole wide enough to
        drive the whole platform through: an agent could pass
        `{"amount_due": "1.00"}` and satisfy any amount limit, or
        `{"payment_details_changed": False}` to switch off the fraud check,
        because a limit bound to a fact would find the fact it was told to
        find. Facts you can supply are not facts.

        With no fact source configured the result is empty, and rules that
        need facts then fail closed. That is the intended behaviour for a
        deployment that forgot to wire one up: refuse, do not assume.
        """
        facts = AuditFacts(
            contact_window_days=self.contact_window_days,
            # Call counts come from the audit agent's own replay guard, not
            # from the request, so they are trusted.
            tool_call_counts=dict(tool_call_counts or {}),
        )
        subjects = list(
            dict.fromkeys(self.subject_ids(arguments) + list(extra_subjects or []))
        )
        if self.source is None:
            return facts
        for subject in subjects:
            amount = self.source.amount_for(subject)
            if amount is not None:
                facts.amounts[subject] = amount
            count = self.source.contact_count_for(subject, self.contact_window_days)
            if count is not None:
                facts.recent_contact_counts[subject] = count
            counterparty = self.source.counterparty_for(subject)
            if counterparty is not None:
                facts.subjects[subject] = counterparty
            changed = self.source.payment_details_changed_for(subject)
            if changed is not None:
                facts.payment_details_changed[subject] = changed
            reader = getattr(self.source, "extra_facts_for", None)
            if callable(reader):
                try:
                    resolved_extras = reader(subject)
                except Exception:
                    resolved_extras = {}
                if isinstance(resolved_extras, dict):
                    for key, value in resolved_extras.items():
                        # Namespaced per subject so two subjects in one call
                        # cannot overwrite each other's facts.
                        facts.extras[f"{key}@{subject}"] = value
                        facts.extras.setdefault(key, value)

        # A single-subject task also exposes its facts under stable keys, so
        # generated policy can say `fact: amount_due` without knowing the
        # subject identifier. Only applied when unambiguous.
        if len(facts.amounts) == 1:
            only_subject, only_amount = next(iter(facts.amounts.items()))
            facts.extras.setdefault("amount_due", only_amount)
            facts.extras.setdefault("subject_id", only_subject)
        if len(facts.payment_details_changed) == 1:
            facts.extras.setdefault(
                "payment_details_changed",
                next(iter(facts.payment_details_changed.values())),
            )
        return facts


def _to_decimal(value: Any) -> Decimal | None:
    """Coerce to Decimal without ever going through float.

    `Decimal(0.1)` would bake in binary rounding, so floats are routed
    through their string form first.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


__all__ = [
    "ChainedFactSource",
    "FactResolver",
    "FactSource",
    "StaticFactSource",
    "StoreFactSource",
]

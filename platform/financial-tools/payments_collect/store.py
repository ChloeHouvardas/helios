"""In-memory payment store — the six provider-agnostic tables.

Development stub, same role as the spreadsheets suite's KnowledgeGraph
stub: single-threaded, in-memory, swappable for a real database later
without touching the service layer. Reads never mutate; every write goes
through a store method that enforces the data-model rules:

* raw webhook payloads are stored before processing and never edited
  (append-only `ProviderEvent`, idempotent on (provider, event_id));
* reconciliation keys off our own `out_trade_no`, never the provider's
  order id;
* amounts are server-side — a caller-supplied amount that disagrees with
  the link raises `amount_mismatch`;
* every journal posting balances (dual-entry);
* state transitions are guarded;
* refunds never exceed the captured amount.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .errors import (
    HeliosError,
    amount_mismatch,
    event_conflict,
    invalid_transition,
    link_inactive,
    not_found,
    refund_exceeds_payment,
    unbalanced_entry,
    validation,
)
from .models import (
    ATTEMPT_TRANSITIONS,
    LINK_TRANSITIONS,
    RECEIVABLE_TRANSITIONS,
    SETTLEMENT_TRANSITIONS,
    LedgerEntry,
    PaymentAttempt,
    PaymentLink,
    ProviderEvent,
    Receivable,
    SHANGHAI,
    Settlement,
)

EventHandler = Callable[[dict[str, Any]], None]


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, default=str)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PaymentStore:
    """In-memory store for the six payment tables (single-threaded)."""

    def __init__(self, clock: Callable[[], datetime] | None = None, *,
                 clearing_account: str = "provider:clearing",
                 merchant_account: str = "merchant:main"):
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self.clearing_account = clearing_account
        self.merchant_account = merchant_account

        self.receivables: dict[str, Receivable] = {}
        self.links: dict[str, PaymentLink] = {}
        self.links_by_token: dict[str, str] = {}
        self.attempts: dict[str, PaymentAttempt] = {}
        self.attempts_by_ref: dict[str, str] = {}
        self.provider_events: dict[tuple[str, str], ProviderEvent] = {}
        self.ledger: list[LedgerEntry] = []
        self.settlements: dict[str, Settlement] = {}
        self._settlements_by_attempt: dict[str, str] = {}
        self._idempotency: dict[tuple[str, str], Any] = {}
        self._subscribers: dict[str, EventHandler] = {}
        self.handler_errors: list[str] = []

    # ------------------------------------------------------------- time & ids

    def now(self) -> datetime:
        return self._clock()

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(8)}"

    def new_token(self) -> str:
        """Unguessable capability behind the cashier URL."""
        return secrets.token_urlsafe(24)

    def new_out_trade_no(self) -> str:
        """Our own order reference — the reconciliation key."""
        stamp = self.now().strftime("%Y%m%d%H%M%S")
        return f"HL{stamp}{secrets.token_hex(4).upper()}"

    # ------------------------------------------------------------ receivables

    def add_receivable(self, *, business_id: str = "dev", customer_id: str,
                       amount: int, currency: str = "CNY",
                       invoice_no: str | None = None,
                       due_date: datetime | None = None,
                       meta: dict[str, Any] | None = None) -> Receivable:
        rec = Receivable(
            id=self.new_id("rcv"), business_id=business_id,
            customer_id=customer_id, amount=amount, currency=currency,
            invoice_no=invoice_no, due_date=due_date, meta=meta or {})
        self.receivables[rec.id] = rec
        return rec

    def get_receivable(self, receivable_id: str, business_id: str) -> Receivable:
        rec = self.receivables.get(receivable_id)
        if rec is None or rec.business_id != business_id:
            raise not_found("receivable", receivable_id)
        return rec

    def list_receivables(self, business_id: str,
                         status: str | None = None) -> list[Receivable]:
        recs = [r for r in self.receivables.values()
                if r.business_id == business_id]
        if status is not None:
            recs = [r for r in recs if r.status == status]
        return recs

    def apply_payment(self, receivable_id: str, amount: int, *,
                      business_id: str) -> Receivable:
        """Money arrived: bump `amount_paid` and recompute the status."""
        rec = self.get_receivable(receivable_id, business_id)
        if rec.status == "void":
            raise validation(
                f"receivable '{receivable_id}' is void; payments cannot be applied")
        if not isinstance(amount, int) or amount <= 0:
            raise validation(
                "applied amount must be a positive integer (minor units)")
        rec.amount_paid += amount
        rec.status = "paid" if rec.amount_paid >= rec.amount else "partial"
        self._emit_receivable(rec, business_id)
        return rec

    def set_receivable_status(self, receivable_id: str, status: str, *,
                              business_id: str) -> Receivable:
        rec = self.get_receivable(receivable_id, business_id)
        if status not in RECEIVABLE_TRANSITIONS.get(rec.status, frozenset()):
            raise invalid_transition("receivable", receivable_id, rec.status,
                                     status)
        rec.status = status
        self._emit_receivable(rec, business_id)
        return rec

    def _emit_receivable(self, rec: Receivable, business_id: str) -> None:
        self._emit({"event": "receivable.status_changed",
                    "business_id": business_id, "receivable_id": rec.id,
                    "status": rec.status, "amount_paid": rec.amount_paid,
                    "amount": rec.amount})

    # ------------------------------------------------------------------ links

    def create_link(self, *, business_id: str, receivable_id: str,
                    amount: int | None = None,
                    expires_at: datetime | None = None,
                    one_time: bool = True,
                    meta: dict[str, Any] | None = None) -> PaymentLink:
        """Issue a payment link. The amount is server-side: it defaults to
        the receivable's outstanding balance and can never exceed it."""
        rec = self.get_receivable(receivable_id, business_id)
        if rec.status in ("paid", "void"):
            raise validation(
                f"receivable '{receivable_id}' is {rec.status}; "
                "no further payment links")
        outstanding = rec.amount - rec.amount_paid
        if amount is None:
            amount = outstanding
        if not isinstance(amount, int) or amount <= 0 or amount > outstanding:
            raise validation(
                f"link amount must be between 1 and {outstanding} minor units")
        link = PaymentLink(
            id=self.new_id("plk"), business_id=business_id,
            receivable_id=receivable_id, amount=amount,
            currency=rec.currency, expires_at=expires_at,
            one_time=one_time, token=self.new_token(), meta=meta or {})
        self.links[link.id] = link
        self.links_by_token[link.token] = link.id
        self._emit({"event": "link.created", "business_id": business_id,
                    "link_id": link.id, "receivable_id": receivable_id,
                    "amount": amount, "currency": link.currency})
        return link

    def get_link(self, link_id: str, business_id: str) -> PaymentLink:
        link = self.links.get(link_id)
        if link is None or link.business_id != business_id:
            raise not_found("payment link", link_id)
        return link

    def get_link_by_token(self, token: str) -> PaymentLink:
        """Token lookup for the cashier page — the token itself is the
        capability, so there is no business scoping here."""
        link_id = self.links_by_token.get(token)
        if link_id is None:
            raise not_found("payment link token", token)
        return self.links[link_id]

    def list_links(self, business_id: str,
                   receivable_id: str | None = None) -> list[PaymentLink]:
        out = [l for l in self.links.values() if l.business_id == business_id]
        if receivable_id is not None:
            out = [l for l in out if l.receivable_id == receivable_id]
        return out

    def mark_link(self, link_id: str, status: str, *,
                  business_id: str) -> PaymentLink:
        link = self.get_link(link_id, business_id)
        if status not in LINK_TRANSITIONS.get(link.status, frozenset()):
            raise invalid_transition("payment link", link_id, link.status,
                                     status)
        link.status = status
        return link

    # --------------------------------------------------------------- attempts

    def create_attempt(self, *, business_id: str, link_id: str, provider: str,
                       amount: int | None = None,
                       idempotency_key: str | None = None) -> PaymentAttempt:
        """Open an attempt on a link. The amount comes from the link, never
        from the caller; a disagreeing amount raises `amount_mismatch`."""
        link = self.get_link(link_id, business_id)
        if link.status != "issued":
            raise link_inactive(f"payment link '{link_id}' is {link.status}")
        if link.expires_at is not None and self.now() > link.expires_at:
            self.mark_link(link_id, "expired", business_id=business_id)
            raise link_inactive(
                f"payment link '{link_id}' expired at "
                f"{link.expires_at.isoformat()}")
        if amount is not None and amount != link.amount:
            raise amount_mismatch(link.amount, amount,
                                  "request amount does not match the payment link")
        att = PaymentAttempt(
            id=self.new_id("pay"), business_id=business_id, link_id=link_id,
            provider=provider, out_trade_no=self.new_out_trade_no(),
            amount=link.amount, currency=link.currency,
            idempotency_key=idempotency_key)
        self.attempts[att.id] = att
        self.attempts_by_ref[att.out_trade_no] = att.id
        return att

    def get_attempt(self, attempt_id: str, business_id: str) -> PaymentAttempt:
        att = self.attempts.get(attempt_id)
        if att is None or att.business_id != business_id:
            raise not_found("payment", attempt_id)
        return att

    def find_attempt_by_out_trade_no(self,
                                     out_trade_no: str) -> PaymentAttempt | None:
        att_id = self.attempts_by_ref.get(out_trade_no)
        return self.attempts.get(att_id) if att_id else None

    def list_attempts(self, business_id: str, *, link_id: str | None = None,
                      state: str | None = None) -> list[PaymentAttempt]:
        out = [a for a in self.attempts.values()
               if a.business_id == business_id]
        if link_id is not None:
            out = [a for a in out if a.link_id == link_id]
        if state is not None:
            out = [a for a in out if a.state == state]
        return out

    def attach_provider_ref(self, attempt_id: str, provider_order_ref: str,
                            *, business_id: str) -> PaymentAttempt:
        att = self.get_attempt(attempt_id, business_id)
        att.provider_order_ref = provider_order_ref
        att.updated_at = self.now()
        return att

    def transition_attempt(self, attempt_id: str, new_state: str, *,
                           business_id: str,
                           reason: str = "") -> PaymentAttempt:
        att = self.get_attempt(attempt_id, business_id)
        old = att.state
        if new_state not in ATTEMPT_TRANSITIONS.get(old, frozenset()):
            raise invalid_transition("payment", attempt_id, old, new_state)
        att.state = new_state
        att.updated_at = self.now()
        if new_state == "failed":
            att.failure_reason = reason
        self._emit({"event": "payment.state_changed",
                    "business_id": business_id, "attempt_id": att.id,
                    "link_id": att.link_id, "provider": att.provider,
                    "from": old, "to": new_state,
                    "at": self.now().isoformat()})
        return att

    # --------------------------------------------------------- provider events

    def record_provider_event(self, *, provider: str, event_id: str,
                              payload: dict[str, Any] | None = None,
                              payload_json: str | None = None,
                              ) -> tuple[ProviderEvent, bool]:
        """Store the raw payload BEFORE processing. Returns (event, created).

        Idempotent on (provider, event_id): the same event again is a no-op;
        the same event_id with a different payload is an `event_conflict`,
        because stored payloads are never edited.
        """
        if (payload is None) == (payload_json is None):
            raise validation("pass exactly one of payload or payload_json")
        raw = payload_json if payload_json is not None else _canonical(payload)
        digest = _hash(raw)
        key = (provider, event_id)
        existing = self.provider_events.get(key)
        if existing is not None:
            if existing.payload_hash != digest:
                raise event_conflict(provider, event_id)
            return existing, False
        ev = ProviderEvent(
            id=self.new_id("pev"), provider=provider, event_id=event_id,
            payload_hash=digest, payload_json=raw, received_at=self.now())
        self.provider_events[key] = ev
        return ev, True

    def complete_provider_event(self, provider: str,
                                event_id: str) -> ProviderEvent:
        """Stamp `processed_at` — the only metadata written after the
        insert; the payload stays untouched."""
        ev = self.provider_events.get((provider, event_id))
        if ev is None:
            raise not_found("provider event", f"{provider}/{event_id}")
        if ev.processed_at is None:
            ev.processed_at = self.now()
        return ev

    # ----------------------------------------------------------------- ledger

    def post_journal(self, *, business_id: str, attempt_id: str,
                     entries: list[tuple[str, int, str]],
                     memo: str = "") -> list[LedgerEntry]:
        """Post one dual-entry journal; the legs must balance."""
        att = self.get_attempt(attempt_id, business_id)
        if not entries:
            raise validation(
                "a journal posting needs at least one debit and one credit leg")
        legs: list[tuple[str, int, str]] = []
        for etype, amount, account in entries:
            if etype not in ("debit", "credit"):
                raise validation(
                    f"entry type must be 'debit' or 'credit', got {etype!r}")
            if not isinstance(amount, int) or amount <= 0:
                raise validation(
                    "entry amounts must be positive integers (minor units)")
            if not account or not isinstance(account, str):
                raise validation("entry account must be a non-empty string")
            legs.append((etype, amount, account))
        debits = sum(a for t, a, _ in legs if t == "debit")
        credits = sum(a for t, a, _ in legs if t == "credit")
        if debits != credits:
            raise unbalanced_entry(debits, credits)
        journal_id = self.new_id("jrn")
        posted = [LedgerEntry(
            id=self.new_id("led"), business_id=business_id,
            attempt_id=attempt_id, journal_id=journal_id, type=t, amount=a,
            account=acct, currency=att.currency, memo=memo,
            posted_at=self.now()) for t, a, acct in legs]
        self.ledger.extend(posted)
        return posted

    def ledger_for_attempt(self, attempt_id: str,
                            business_id: str) -> list[LedgerEntry]:
        self.get_attempt(attempt_id, business_id)  # tenancy gate
        return [e for e in self.ledger if e.attempt_id == attempt_id]

    # ------------------------------------------------------------- settlements

    def add_settlement(self, *, business_id: str, attempt_id: str,
                       to_account: str, rule: str = "default") -> Settlement:
        att = self.get_attempt(attempt_id, business_id)
        if att.state not in ("succeeded", "settled", "partially_refunded",
                             "refunded"):
            raise invalid_transition("payment", attempt_id, att.state,
                                     "settled")
        s = Settlement(
            id=self.new_id("stl"), business_id=business_id,
            attempt_id=attempt_id, to_account=to_account, rule=rule)
        self.settlements[s.id] = s
        self._settlements_by_attempt[attempt_id] = s.id
        return s

    def settlement_for_attempt(self, attempt_id: str, *,
                               business_id: str) -> Settlement | None:
        att = self.get_attempt(attempt_id, business_id)
        sid = self._settlements_by_attempt.get(att.id)
        s = self.settlements.get(sid) if sid else None
        if s is not None and s.business_id != business_id:
            return None
        return s

    def mark_settlement(self, settlement_id: str, status: str, *,
                        business_id: str) -> Settlement:
        s = self.settlements.get(settlement_id)
        if s is None or s.business_id != business_id:
            raise not_found("settlement", settlement_id)
        if status not in SETTLEMENT_TRANSITIONS.get(s.status, frozenset()):
            raise invalid_transition("settlement", settlement_id, s.status,
                                     status)
        s.status = status
        return s

    # ------------------------------------------------------------- composites

    def record_payment_success(self, attempt_id: str, *, business_id: str,
                               provider_order_ref: str | None = None,
                               ) -> PaymentAttempt:
        """Apply a captured payment: transition to `succeeded`, close a
        one-time link, update the receivable, post the capture journal and
        open the settlement.

        Idempotent: if this payment already succeeded (duplicate notify or a
        poll racing the webhook), nothing is applied a second time.
        """
        att = self.get_attempt(attempt_id, business_id)
        if att.state in ("succeeded", "settled", "partially_refunded",
                         "refunded", "closed"):
            return att
        if att.state not in ("customer_authenticating", "processing"):
            raise invalid_transition("payment", attempt_id, att.state,
                                     "succeeded")
        link = self.get_link(att.link_id, business_id)
        if att.amount != link.amount or att.currency != link.currency:
            raise amount_mismatch(link.amount, att.amount,
                                  "payment does not match its link")
        self.transition_attempt(attempt_id, "succeeded", business_id=business_id)
        if provider_order_ref is not None:
            self.attach_provider_ref(attempt_id, provider_order_ref,
                                     business_id=business_id)
        if link.one_time and link.status == "issued":
            self.mark_link(link.id, "closed", business_id=business_id)
        self.apply_payment(link.receivable_id, att.amount,
                           business_id=business_id)
        self.post_journal(
            business_id=business_id, attempt_id=attempt_id,
            entries=[("debit", att.amount, self.clearing_account),
                     ("credit", att.amount, self.merchant_account)],
            memo=f"capture {att.out_trade_no}")
        self.add_settlement(business_id=business_id, attempt_id=attempt_id,
                            to_account=self.merchant_account, rule="default")
        return self.attempts[attempt_id]

    def record_refund(self, attempt_id: str, *, amount: int, business_id: str,
                      reason: str = "") -> PaymentAttempt:
        """Apply a refund: reverse journal, bump `refunded_amount`,
        transition to `partially_refunded` / `refunded`.

        The receivable is deliberately not reopened — whether the customer
        still owes the money is Collect-app policy, not ledger policy.
        """
        att = self.get_attempt(attempt_id, business_id)
        if not isinstance(amount, int) or amount <= 0:
            raise validation(
                "refund amount must be a positive integer (minor units)")
        if att.refunded_amount + amount > att.amount:
            raise refund_exceeds_payment(attempt_id, att.refunded_amount,
                                         amount, att.amount)
        target = ("refunded" if att.refunded_amount + amount == att.amount
                  else "partially_refunded")
        if target not in ATTEMPT_TRANSITIONS.get(att.state, frozenset()):
            raise invalid_transition("payment", attempt_id, att.state, target)
        att.refunded_amount += amount
        self.post_journal(
            business_id=business_id, attempt_id=attempt_id,
            entries=[("debit", amount, self.merchant_account),
                     ("credit", amount, self.clearing_account)],
            memo=f"refund {att.out_trade_no}")
        return self.transition_attempt(attempt_id, target,
                                       business_id=business_id, reason=reason)

    def settle(self, attempt_id: str, *, business_id: str) -> PaymentAttempt:
        """Route the settlement and move the payment to `settled`."""
        att = self.get_attempt(attempt_id, business_id)
        if att.state == "settled":
            return att
        s = self.settlement_for_attempt(attempt_id, business_id=business_id)
        if s is None:
            raise not_found("settlement for payment", attempt_id)
        if s.status == "pending":
            self.mark_settlement(s.id, "routed", business_id=business_id)
        return self.transition_attempt(attempt_id, "settled",
                                       business_id=business_id)

    # ------------------------------------------- idempotency & subscriptions

    def idempotent(self, key: str, fn: Callable[[], Any], *,
                   business_id: str = "dev") -> Any:
        """Run `fn` once per (business, key); repeats return the first
        result. A raise is never cached, so the caller may retry."""
        cache_key = (business_id, key)
        if cache_key in self._idempotency:
            return self._idempotency[cache_key]
        result = fn()
        self._idempotency[cache_key] = result
        return result

    def subscribe(self, handler: EventHandler) -> str:
        sub_id = self.new_id("sub")
        self._subscribers[sub_id] = handler
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        self._subscribers.pop(sub_id, None)

    def _emit(self, event: dict[str, Any]) -> None:
        for sub_id, handler in list(self._subscribers.items()):
            try:
                handler(event)
            except Exception as exc:
                # a broken subscriber must never break a payment write
                self.handler_errors.append(f"{sub_id}: {exc!r}")

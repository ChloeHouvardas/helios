"""Receivables family (`collect.*`) over the imported `payments_collect` store.

This module is the adapter between the fin_tools tool contract and the
teammates' `PaymentStore`. Three things it is responsible for that the store
does not do:

1. **Tenancy.** Every store method takes a `business_id`. It comes from the
   bundle's tenant (`require_tenant`), never from an argument, and there is no
   default. The store's own models default `business_id` to the string `"dev"`;
   we never rely on that, because a silent shared default would put one tenant's
   receivables where any other caller can read and refund them.

2. **The refund gate.** `PaymentStore.record_refund` has no authorization
   parameter — it refunds whatever it is told. So `collect.refund` verifies and
   consumes an intent auth code bound to (amount, currency, customer) BEFORE
   calling it, exactly as `payments.send` does. Without that, an agent could
   refund money out with no human having authorized it.

3. **One ledger.** The store keeps its own dual-entry journal. So that the
   fin_tools ledger is not a second, contradicting source of truth, a captured
   payment is mirrored in via `payments.receive` and a refund out via
   `Payments.record_refund_mirror` — each exactly once, guarded below.

Webhook-only operations are deliberately NOT tools. `record_payment_success` and
provider-event handling are reachable only through `Collect.apply_provider_event`
(a facade method an app's webhook handler calls), so an agent can never mark its
own payment received. Money is integer minor units throughout; for CNY the
store's "fen" and fin_tools' "cents" are the same 1/100 unit, so the integer
passes through unchanged.

The `payments_collect` imports are lazy so importing this module (and therefore
the registry) does not pull in pydantic until a collect tool actually runs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .contracts import REGISTRY, require_tenant
from .intent_verification import VerificationError

#: Simulated cashier URL. `.test` is a reserved non-routable TLD, so this can
#: never accidentally hit a real payment page. Money is simulated end to end.
CASHIER_URL = "https://pay.helios.test/l/{link_id}"

#: Provider statuses that mean "the customer's money was captured".
SUCCESS_STATUSES = frozenset({"succeeded", "success", "paid", "captured"})

#: The account a capture settles into and a refund leaves from. Must match the
#: store's `merchant_account` so the two ledgers describe the same money.
MERCHANT_ACCOUNT = "merchant:main"

#: Legal forward walk to get an attempt into a state `record_payment_success`
#: accepts. A success webhook implies these intermediate states happened.
_WALK_TO_PROCESSABLE = {
    "created": "intent_created",
    "intent_created": "customer_authenticating",
    "customer_authenticating": "processing",
}
_ALREADY_CAPTURED = frozenset(
    {"succeeded", "settled", "partially_refunded", "refunded", "closed"})


def _dump(model: Any) -> Dict[str, Any]:
    """A pydantic model (or dict) as JSON-safe plain data.

    `mode="json"` turns the store's tz-aware datetimes into ISO strings, which
    the service's `json.dumps` can serialize — a raw datetime would raise there.
    """
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if isinstance(model, dict):
        return dict(model)
    return {"value": model}


def _parse_when(raw: Optional[str], *, field_name: str) -> Optional[datetime]:
    """Parse an ISO date or datetime string into a store-shaped datetime."""
    if raw is None or raw == "":
        return None
    from payments_collect.models import SHANGHAI

    text = str(raw).strip()
    for parser in (datetime.fromisoformat, date.fromisoformat):
        try:
            parsed = parser(text)
        except ValueError:
            continue
        if isinstance(parsed, datetime):
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)
        return datetime(parsed.year, parsed.month, parsed.day, tzinfo=SHANGHAI)
    raise ValueError(
        f"{field_name} must be an ISO date or datetime like '2026-11-01' or "
        f"'2026-11-01T09:00:00+08:00', got {raw!r}")


def _translate(exc: Any) -> ValueError:
    """Turn a store `HeliosError` into a model-actionable `ValueError`.

    The service envelope already marks a raised ValueError as a rejection, so
    this keeps the store's own `{code, message}` detail while making it a type
    the rest of fin_tools handles uniformly.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if code:
        return ValueError(f"[{code}] {message}")
    return ValueError(message)


@dataclass
class Collect:
    """Facade over one tenant's `PaymentStore`, bound to a fin_tools bundle.

    The store is built lazily so importing this module needs no pydantic; pass
    `store` to inject one (a fake clock, a seeded fixture) in tests.
    """
    tenant: Optional[str]
    payments: Any                      # fin_tools.payments.Payments
    intents: Any                       # fin_tools.intent_verification.IntentService
    merchant_account: str = MERCHANT_ACCOUNT
    _store: Optional[Any] = field(default=None, repr=False)
    #: out_trade_no values already mirrored into the fin_tools ledger, so a
    #: replayed or duplicated provider event cannot post the same money twice.
    _mirrored: set = field(default_factory=set, repr=False)

    @property
    def store(self) -> Any:
        if self._store is None:
            from payments_collect.store import PaymentStore

            self._store = PaymentStore(
                merchant_account=self.merchant_account)
        return self._store

    # -- tenant -------------------------------------------------------------

    def _tenant(self, tool: str) -> str:
        return require_tenant(self, tool=tool)

    # -- receivables --------------------------------------------------------

    def add_receivable(self, *, customer_id: str, amount_cents: int,
                       currency: str = "CNY", invoice_no: Optional[str] = None,
                       due_date: Optional[str] = None,
                       meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Record an amount a customer owes. Gated: it writes tenant state."""
        tenant = self._tenant("collect.add_receivable")
        amount_cents = int(amount_cents)
        if amount_cents <= 0:
            raise ValueError(
                f"amount_cents must be a positive integer, got {amount_cents!r}")
        try:
            rec = self.store.add_receivable(
                business_id=tenant, customer_id=customer_id, amount=amount_cents,
                currency=currency, invoice_no=invoice_no,
                due_date=_parse_when(due_date, field_name="due_date"),
                meta=meta)
        except Exception as exc:                       # noqa: BLE001 - store errors
            if getattr(exc, "code", None):
                raise _translate(exc) from exc
            raise
        return _dump(rec)

    def list_receivables(self, *, status: Optional[str] = None) -> Dict[str, Any]:
        """List this tenant's receivables, optionally filtered by status."""
        tenant = self._tenant("collect.list_receivables")
        try:
            recs = self.store.list_receivables(tenant, status=status)
        except Exception as exc:                       # noqa: BLE001
            if getattr(exc, "code", None):
                raise _translate(exc) from exc
            raise
        return {"receivables": [_dump(r) for r in recs], "count": len(recs)}

    def status(self, *, receivable_id: str) -> Dict[str, Any]:
        """One receivable with its links, attempts and outstanding balance."""
        tenant = self._tenant("collect.status")
        try:
            rec = self.store.get_receivable(receivable_id, tenant)
            links = self.store.list_links(tenant, receivable_id=receivable_id)
            attempts = [a for link in links
                        for a in self.store.list_attempts(tenant, link_id=link.id)]
        except Exception as exc:                       # noqa: BLE001
            if getattr(exc, "code", None):
                raise _translate(exc) from exc
            raise
        outstanding = int(rec.amount) - int(rec.amount_paid)
        data = _dump(rec)
        data["outstanding_cents"] = outstanding
        data["links"] = [_dump(l) for l in links]
        data["attempts"] = [_dump(a) for a in attempts]
        return data

    # -- payment links ------------------------------------------------------

    def create_payment_link(self, *, receivable_id: str,
                            amount_cents: Optional[int] = None,
                            expires_at: Optional[str] = None,
                            one_time: bool = True,
                            idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Issue a simulated cashier link for a receivable. Gated.

        The amount is server-side: it defaults to the receivable's outstanding
        balance and the store refuses one that exceeds it. `idempotency_key`
        makes a repeated request return the FIRST link rather than minting a new
        one, so a retry cannot leave a customer with two live links for one debt.
        """
        tenant = self._tenant("collect.create_payment_link")

        def _make() -> Dict[str, Any]:
            try:
                link = self.store.create_link(
                    business_id=tenant, receivable_id=receivable_id,
                    amount=(int(amount_cents) if amount_cents is not None else None),
                    expires_at=_parse_when(expires_at, field_name="expires_at"),
                    one_time=bool(one_time))
            except Exception as exc:                   # noqa: BLE001
                if getattr(exc, "code", None):
                    raise _translate(exc) from exc
                raise
            return self._link_view(link)

        if idempotency_key:
            # The store's idempotent() caches the first result per (tenant, key)
            # and never caches a raise, so a failed attempt can be retried.
            return self.store.idempotent(
                f"create_link:{idempotency_key}", _make, business_id=tenant)
        return _make()

    def _link_view(self, link: Any) -> Dict[str, Any]:
        data = _dump(link)
        # NOTE: the URL carries the link id per the p1-tools spec. The store's
        # `token` (returned alongside) is the unguessable capability; a
        # production cashier URL should use the token, not the enumerable id.
        # Flagged in HANDOFF-TO-P2-P3.md. Simulated money on a .test domain.
        data["url"] = CASHIER_URL.format(link_id=link.id)
        data["token"] = link.token
        return data

    # -- refunds (gated by an intent auth code) -----------------------------

    def refund(self, *, attempt_id: str, amount_cents: int,
               auth_code: Optional[str] = None, currency: Optional[str] = None,
               reason: str = "") -> Dict[str, Any]:
        """Refund a captured payment. Requires a verified intent auth code.

        The auth code must bind (amount_cents, currency, customer) — the same
        PSD2-style dynamic linking `payments.send` uses. The customer is derived
        from the attempt's receivable, so the human authorizes a refund TO a
        specific party and a mismatched amount or party is refused. The code is
        consumed only after the store accepts the refund, so a rejected refund
        leaves the code reusable.
        """
        tenant = self._tenant("collect.refund")
        amount_cents = int(amount_cents)
        if amount_cents <= 0:
            raise ValueError(
                f"amount_cents must be a positive integer, got {amount_cents!r}")

        try:
            att = self.store.get_attempt(attempt_id, tenant)
            link = self.store.get_link(att.link_id, tenant)
            rec = self.store.get_receivable(link.receivable_id, tenant)
        except Exception as exc:                       # noqa: BLE001
            if getattr(exc, "code", None):
                raise _translate(exc) from exc
            raise

        currency = (currency or att.currency).upper()
        payee = rec.customer_id            # a refund goes back to the customer

        # Gate: no verified intent -> no refund. Mirrors payments.send.
        if auth_code is None:
            raise VerificationError(
                "collect.refund requires a verified intent auth_code bound to "
                "(amount, currency, customer); none was provided")
        intent = self.intents.verify(
            auth_code, amount_cents=amount_cents, currency=currency, payee=payee)

        try:
            updated = self.store.record_refund(
                attempt_id, amount=amount_cents, business_id=tenant, reason=reason)
        except Exception as exc:                       # noqa: BLE001
            if getattr(exc, "code", None):
                raise _translate(exc) from exc
            raise

        # The refund happened, so the authorization is now spent.
        self.intents.consume(auth_code)
        # Keep the fin_tools ledger truthful: money left the merchant.
        self.payments.record_refund_mirror(
            payer=self.merchant_account, payee=payee, amount_cents=amount_cents,
            currency=currency, reference=att.out_trade_no)

        data = _dump(updated)
        data["intent_id"] = intent.intent_id
        return data

    # -- webhook-only entry point (NOT a tool) ------------------------------

    def apply_provider_event(self, *, provider: str, event_id: str,
                             payload: Optional[Dict[str, Any]] = None,
                             payload_json: Optional[str] = None) -> Dict[str, Any]:
        """Handle one provider webhook for this tenant. Not agent-callable.

        Stores the raw payload append-only (idempotent on provider+event_id) and,
        if it reports a capture, drives the attempt to `succeeded` and mirrors the
        money into the fin_tools ledger exactly once. An app calls this from its
        webhook handler; it is deliberately absent from the tool registry so an
        agent can never mark its own payment received.

        Expected payload shape (provider-agnostic; the real Alipay adapter maps
        into it): `{"out_trade_no": <our order ref>, "status": <one of
        SUCCESS_STATUSES>, "provider_order_ref": <optional>}`. Reconciliation
        keys off OUR `out_trade_no`, never the provider's id.
        """
        tenant = self._tenant("collect.apply_provider_event")
        store = self.store
        try:
            event, created = store.record_provider_event(
                provider=provider, event_id=event_id,
                payload=payload, payload_json=payload_json)
        except Exception as exc:                       # noqa: BLE001
            if getattr(exc, "code", None):
                raise _translate(exc) from exc
            raise

        result: Dict[str, Any] = {
            "provider": event.provider, "event_id": event.event_id,
            "created": created, "processed": False,
        }

        # A replayed event was already processed the first time; do not re-apply.
        if not created:
            return result

        try:
            data = json.loads(event.payload_json)
        except (json.JSONDecodeError, TypeError):
            data = {}
        out_trade_no = data.get("out_trade_no")
        status = str(data.get("status") or "").lower()

        if status in SUCCESS_STATUSES and out_trade_no:
            att = store.find_attempt_by_out_trade_no(str(out_trade_no))
            if att is None:
                raise ValueError(
                    f"provider event references unknown out_trade_no "
                    f"{out_trade_no!r}; no matching payment attempt for tenant "
                    f"{tenant!r}")
            already = att.state in _ALREADY_CAPTURED or \
                str(out_trade_no) in self._mirrored
            att = self._walk_to_processable(att, tenant)
            updated = store.record_payment_success(
                att.id, business_id=tenant,
                provider_order_ref=data.get("provider_order_ref"))
            # Mirror once, even if two different event ids report one capture.
            if not already and str(out_trade_no) not in self._mirrored:
                self._mirror_capture(updated, tenant)
                self._mirrored.add(str(out_trade_no))
            result["processed"] = True
            result["payment_id"] = updated.id
            result["amount_cents"] = int(updated.amount)

        try:
            store.complete_provider_event(provider, event_id)
        except Exception as exc:                       # noqa: BLE001
            if getattr(exc, "code", None):
                raise _translate(exc) from exc
            raise
        return result

    def _walk_to_processable(self, att: Any, tenant: str) -> Any:
        while att.state in _WALK_TO_PROCESSABLE:
            att = self.store.transition_attempt(
                att.id, _WALK_TO_PROCESSABLE[att.state], business_id=tenant)
        return att

    def _mirror_capture(self, att: Any, tenant: str) -> None:
        """Post a captured payment into the fin_tools ledger as a receive."""
        link = self.store.get_link(att.link_id, tenant)
        rec = self.store.get_receivable(link.receivable_id, tenant)
        self.payments.receive_payment(
            payer=rec.customer_id, payee=self.merchant_account,
            amount_cents=int(att.amount), currency=att.currency,
            reference=att.out_trade_no)


# --------------------------------------------------------------------------
# JSON Schemas (real ones: the store has enums and nullable fields the flat map
# cannot express, and the auditor validates arguments against these)
# --------------------------------------------------------------------------

_CURRENCY = {"type": "string", "description": "ISO 4217 code; CNY default."}

_ADD_RECEIVABLE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "customer_id": {"type": "string", "minLength": 1},
        "amount_cents": {"type": "integer", "minimum": 1,
                         "description": "Amount owed, in integer minor units."},
        "currency": _CURRENCY,
        "invoice_no": {"type": ["string", "null"]},
        "due_date": {"type": ["string", "null"],
                     "description": "ISO date or datetime."},
        "meta": {"type": ["object", "null"]},
    },
    "required": ["customer_id", "amount_cents"],
    "additionalProperties": False,
}

_LIST_RECEIVABLES_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": ["string", "null"],
                   "enum": ["open", "partial", "paid", "void", None]},
    },
    "required": [],
    "additionalProperties": False,
}

_STATUS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"receivable_id": {"type": "string", "minLength": 1}},
    "required": ["receivable_id"],
    "additionalProperties": False,
}

_CREATE_LINK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "receivable_id": {"type": "string", "minLength": 1},
        "amount_cents": {"type": ["integer", "null"], "minimum": 1,
                         "description": "Defaults to the outstanding balance; "
                                        "cannot exceed it."},
        "expires_at": {"type": ["string", "null"],
                       "description": "ISO datetime; null never expires."},
        "one_time": {"type": "boolean"},
        "idempotency_key": {"type": ["string", "null"],
                            "description": "Replaying returns the first link."},
    },
    "required": ["receivable_id"],
    "additionalProperties": False,
}

_REFUND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "attempt_id": {"type": "string", "minLength": 1},
        "amount_cents": {"type": "integer", "minimum": 1},
        "auth_code": {"type": ["string", "null"],
                      "description": "Verified intent auth code bound to "
                                     "(amount, currency, customer). Required."},
        "currency": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["attempt_id", "amount_cents"],
    "additionalProperties": False,
}


def register(registry=REGISTRY, collect: Optional[Collect] = None) -> None:
    """Register the `collect.*` tools bound to one `Collect` facade.

    `collect` binds the closures to THIS bundle's store, tenant and intent
    service. Webhook-only operations are intentionally not registered here.
    """

    def _bound() -> Collect:
        if collect is None:
            raise ValueError(
                "collect tools were registered without a bound Collect facade; "
                "call build_world() (which supplies one) rather than register() "
                "directly.")
        return collect

    @registry.tool(
        "collect.add_receivable",
        "Record an amount a customer owes. Side-effecting: writes tenant "
        "receivables state. The tenant comes from the bundle, never an argument.",
        {"customer_id": "str", "amount_cents": "int", "currency": "str|None",
         "invoice_no": "str|None", "due_date": "str|None", "meta": "dict|None"},
        {"id": "str", "customer_id": "str", "amount": "int", "status": "str"},
        side_effects=True, tags=("collect",),
        input_json_schema=_ADD_RECEIVABLE_SCHEMA, tenant_scoped=True,
    )
    def _add_receivable(customer_id: str, amount_cents: int,
                        currency: str = "CNY", invoice_no: Optional[str] = None,
                        due_date: Optional[str] = None,
                        meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return _bound().add_receivable(
            customer_id=customer_id, amount_cents=amount_cents, currency=currency,
            invoice_no=invoice_no, due_date=due_date, meta=meta)

    @registry.tool(
        "collect.list_receivables",
        "List this tenant's receivables, optionally filtered by status. Read-only.",
        {"status": "str|None"},
        {"receivables": "list[dict]", "count": "int"},
        side_effects=False, tags=("collect",),
        input_json_schema=_LIST_RECEIVABLES_SCHEMA, tenant_scoped=True,
    )
    def _list_receivables(status: Optional[str] = None) -> Dict[str, Any]:
        return _bound().list_receivables(status=status)

    @registry.tool(
        "collect.status",
        "Read one receivable with its payment links, attempts and outstanding "
        "balance. Read-only.",
        {"receivable_id": "str"},
        {"id": "str", "status": "str", "amount": "int", "amount_paid": "int",
         "outstanding_cents": "int", "links": "list[dict]",
         "attempts": "list[dict]"},
        side_effects=False, tags=("collect",),
        input_json_schema=_STATUS_SCHEMA, tenant_scoped=True,
    )
    def _status(receivable_id: str) -> Dict[str, Any]:
        return _bound().status(receivable_id=receivable_id)

    @registry.tool(
        "collect.create_payment_link",
        "Issue a simulated cashier link for a receivable. Side-effecting. The "
        "amount is server-side (defaults to the outstanding balance, cannot "
        "exceed it); an idempotency_key makes a retry return the first link.",
        {"receivable_id": "str", "amount_cents": "int|None",
         "expires_at": "str|None", "one_time": "bool|None",
         "idempotency_key": "str|None"},
        {"id": "str", "url": "str", "token": "str", "amount": "int",
         "receivable_id": "str"},
        side_effects=True, tags=("collect",),
        input_json_schema=_CREATE_LINK_SCHEMA, tenant_scoped=True,
    )
    def _create_payment_link(receivable_id: str,
                             amount_cents: Optional[int] = None,
                             expires_at: Optional[str] = None,
                             one_time: bool = True,
                             idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        return _bound().create_payment_link(
            receivable_id=receivable_id, amount_cents=amount_cents,
            expires_at=expires_at, one_time=one_time,
            idempotency_key=idempotency_key)

    @registry.tool(
        "collect.refund",
        "Refund a captured payment. Side-effecting and gated: requires a verified "
        "intent auth_code bound to (amount, currency, customer), consumed on "
        "success. The refund is mirrored out of the fin_tools ledger so one "
        "ledger stays truthful.",
        {"attempt_id": "str", "amount_cents": "int", "auth_code": "str|None",
         "currency": "str|None", "reason": "str|None"},
        {"id": "str", "state": "str", "refunded_amount": "int",
         "intent_id": "str"},
        side_effects=True, tags=("collect",),
        input_json_schema=_REFUND_SCHEMA, tenant_scoped=True,
    )
    def _refund(attempt_id: str, amount_cents: int,
                auth_code: Optional[str] = None, currency: Optional[str] = None,
                reason: str = "") -> Dict[str, Any]:
        return _bound().refund(
            attempt_id=attempt_id, amount_cents=amount_cents, auth_code=auth_code,
            currency=currency, reason=reason or "")

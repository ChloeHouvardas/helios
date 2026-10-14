"""Payments: mock ledger, send + receive. Offline prototype, no real money.

Design rules:
  - integer cents only; balances start at 0 and go negative allowed (it's a mock)
  - idempotency keys: the same key never double-pays
  - **settlement requires a verified intent auth code** (intent_verification).
    A payment call without one, or with a mismatched one, is rejected — the
    intent-to-execution integrity gate (arXiv:2605.16976).
  - every state transition appended to an in-memory audit trail
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .contracts import REGISTRY, World
from .intent_verification import IntentService, VerificationError


class PaymentState(str, Enum):
    PROPOSED = "proposed"
    SETTLED = "settled"
    REJECTED = "rejected"
    RECEIVED = "received"


@dataclass
class Payments:
    world: World
    intents: IntentService
    clock: Callable[[], float] = time.time
    trail: List[Dict[str, Any]] = field(default_factory=list)
    _seen_keys: Dict[str, str] = field(default_factory=dict)  # idem key -> payment_id

    def _log(self, payment_id: str, event: str, detail: str = "") -> None:
        self.trail.append({"t": self.clock(), "payment_id": payment_id,
                           "event": event, "detail": detail})

    # -- send ---------------------------------------------------------------
    def send_payment(self, *, payer: str, payee: str, amount_cents: int,
                     currency: str, auth_code: Optional[str] = None,
                     idempotency_key: Optional[str] = None,
                     memo: str = "") -> Dict[str, Any]:
        amount_cents = int(amount_cents)
        if amount_cents <= 0:
            raise VerificationError("amount must be positive integer cents")
        key = idempotency_key or secrets.token_hex(8)
        if key in self._seen_keys:
            return self.world.ledger[self._seen_keys[key]]

        pid = f"pay_{secrets.token_hex(6)}"
        record: Dict[str, Any] = {
            "payment_id": pid, "direction": "send", "payer": payer, "payee": payee,
            "amount_cents": amount_cents, "currency": currency.upper(),
            "memo": memo, "state": PaymentState.PROPOSED.value,
            "idempotency_key": key, "t": self.clock(),
        }
        self.world.ledger[pid] = record
        self._seen_keys[key] = pid
        self._log(pid, "proposed", memo)

        # Gate: no verified intent -> no settlement. Ever.
        if auth_code is None:
            record["state"] = PaymentState.REJECTED.value
            record["reject_reason"] = "missing verified intent auth code"
            self._log(pid, "rejected", "missing auth code")
            return record
        try:
            intent = self.intents.verify(
                auth_code, amount_cents=amount_cents,
                currency=currency, payee=payee, now=self.clock(),
            )
        except VerificationError as exc:
            record["state"] = PaymentState.REJECTED.value
            record["reject_reason"] = str(exc)
            self._log(pid, "rejected", str(exc))
            return record

        record["state"] = PaymentState.SETTLED.value
        record["intent_id"] = intent.intent_id
        self.world.balances[payer] = self.world.balances.get(payer, 0) - amount_cents
        self.world.balances[payee] = self.world.balances.get(payee, 0) + amount_cents
        self.intents.consume(auth_code)
        self._log(pid, "settled", f"intent={intent.intent_id}")
        return record

    # -- receive ------------------------------------------------------------
    def receive_payment(self, *, payer: str, payee: str, amount_cents: int,
                        currency: str, reference: str = "") -> Dict[str, Any]:
        """Inbound money needs no intent code (we are the beneficiary)."""
        amount_cents = int(amount_cents)
        if amount_cents <= 0:
            raise VerificationError("amount must be positive integer cents")
        pid = f"pay_{secrets.token_hex(6)}"
        record = {
            "payment_id": pid, "direction": "receive", "payer": payer, "payee": payee,
            "amount_cents": amount_cents, "currency": currency.upper(),
            "reference": reference, "state": PaymentState.RECEIVED.value,
            "t": self.clock(),
        }
        self.world.ledger[pid] = record
        self.world.balances[payee] = self.world.balances.get(payee, 0) + amount_cents
        self._log(pid, "received", reference)
        return record

    def balance(self, account: str) -> int:
        return self.world.balances.get(account, 0)


# ---------------------------------------------------------------------------
# Registered tool views (the developer interface surface)
# ---------------------------------------------------------------------------

def register(registry=REGISTRY) -> None:
    """Register the payment tools. Called by fin_tools.build_world()."""

    @registry.tool(
        "payments.send", "Send a payment. Side-effecting: requires a verified "
        "intent auth code bound to (amount, currency, payee).",
        {"payer": "str", "payee": "str", "amount_cents": "int", "currency": "str",
         "auth_code": "str|None", "idempotency_key": "str|None", "memo": "str|None"},
        {"payment_id": "str", "state": "str", "reject_reason": "str|None"},
        side_effects=True, tags=("payments",),
    )
    def _send(payer: str, payee: str, amount_cents: int, currency: str,
              auth_code: Optional[str] = None,
              idempotency_key: Optional[str] = None, memo: str = "") -> Dict[str, Any]:
        return _payments().send_payment(
            payer=payer, payee=payee, amount_cents=amount_cents, currency=currency,
            auth_code=auth_code, idempotency_key=idempotency_key,
            memo=memo or "")

    @registry.tool(
        "payments.receive", "Record an inbound payment. Side-effecting (ledger).",
        {"payer": "str", "payee": "str", "amount_cents": "int", "currency": "str",
         "reference": "str|None"},
        {"payment_id": "str", "state": "str"},
        side_effects=True, tags=("payments",),
    )
    def _receive(payer: str, payee: str, amount_cents: int, currency: str,
                 reference: str = "") -> Dict[str, Any]:
        return _payments().receive_payment(
            payer=payer, payee=payee, amount_cents=amount_cents,
            currency=currency, reference=reference or "")

    @registry.tool(
        "payments.balance", "Read an account balance (cents). Read-only.",
        {"account": "str"}, {"balance_cents": "int"},
        side_effects=False, tags=("payments",),
    )
    def _balance(account: str) -> Dict[str, int]:
        return {"balance_cents": _payments().balance(account)}


_INSTANCE: Dict[str, Payments] = {}


def _payments() -> Payments:
    return _INSTANCE["payments"]


def bind(payments: Payments) -> None:
    _INSTANCE["payments"] = payments

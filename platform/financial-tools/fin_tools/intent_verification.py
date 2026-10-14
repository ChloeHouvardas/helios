"""Intent verification: PSD2-style dynamic linking for agent-initiated payments.

Model (see research/README.md §1):
  1. An intent is captured as an immutable record: what the *human* asked for
     (amount, currency, payee, purpose).
  2. The human confirmation moment issues an **auth code**: an HMAC over
     (intent_id, amount, currency, payee, nonce). PSD2 Art. 97(2) RTS: any change
     to amount or payee invalidates the code — so the code is computed over
     exactly those fields.
  3. A side-effecting payment may settle only while holding a *verified, unexpired,
     unconsumed* auth code whose bound fields match the call (intent-to-execution
     integrity, arXiv:2605.16976; verify-before-execute, arXiv:2604.02767).

Deterministic only: HMAC + expiry + single-use state. An optional LLM assessor may
propose a natural-language summary for display, but never decides (probabilistic
proposes, deterministic disposes).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class IntentState(str, Enum):
    CAPTURED = "captured"          # intent recorded, not yet confirmed by human
    CONFIRMED = "confirmed"        # auth code issued
    CONSUMED = "consumed"          # auth code spent by exactly one execution
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True)
class Intent:
    intent_id: str
    principal: str          # the human on whose behalf the agent acts
    amount_cents: int
    currency: str
    payee: str
    purpose: str
    created_at: float
    state: IntentState = IntentState.CAPTURED


@dataclass(frozen=True)
class AuthCode:
    intent_id: str
    code: str               # hex HMAC
    issued_at: float
    expires_at: float
    nonce: str

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


class VerificationError(Exception):
    """Raised when a call cannot be tied to a valid verified intent."""


@dataclass
class IntentService:
    """Issues and checks intent artifacts. Fail-closed everywhere."""

    secret: bytes = b"dev-only-secret-rotate-me"
    ttl_seconds: float = 300.0
    clock: Callable[[], float] = time.time
    intents: Dict[str, Intent] = field(default_factory=dict)
    codes: Dict[str, AuthCode] = field(default_factory=dict)   # code -> AuthCode
    _states: Dict[str, IntentState] = field(default_factory=dict)
    #: Optional LLM hook: intent -> human-readable summary shown at confirmation.
    #: May propose text only; a None/raising assessor changes nothing.
    intent_assessor: Optional[Callable[[Intent], str]] = None

    # -- capture ----------------------------------------------------------
    def capture_intent(self, principal: str, amount_cents: int, currency: str,
                       payee: str, purpose: str) -> Intent:
        if amount_cents <= 0:
            raise VerificationError("amount must be positive integer cents")
        intent = Intent(
            intent_id=f"int_{secrets.token_hex(6)}",
            principal=principal,
            amount_cents=int(amount_cents),
            currency=currency.upper(),
            payee=payee,
            purpose=purpose,
            created_at=self.clock(),
        )
        self.intents[intent.intent_id] = intent
        self._states[intent.intent_id] = IntentState.CAPTURED
        return intent

    def summarize(self, intent: Intent) -> str:
        """Display text for the human confirmation step."""
        if self.intent_assessor is not None:
            try:
                return self.intent_assessor(intent)
            except Exception:
                pass  # probabilistic layer is advisory only
        return (f"{intent.principal} authorizes {intent.amount_cents/100:.2f} "
                f"{intent.currency} to {intent.payee}: {intent.purpose}")

    # -- confirm (the SCA moment) ------------------------------------------
    def confirm(self, intent_id: str, *, by: str) -> AuthCode:
        intent = self._must_get(intent_id)
        if by != intent.principal:
            raise VerificationError("confirmation must come from the intent principal")
        if self.state(intent_id) is not IntentState.CAPTURED:
            raise VerificationError(f"intent not confirmable: {self.state(intent_id)}")
        nonce = secrets.token_hex(8)
        now = self.clock()
        code = AuthCode(
            intent_id=intent_id,
            code=self._hmac(intent, nonce),
            issued_at=now,
            expires_at=now + self.ttl_seconds,
            nonce=nonce,
        )
        self.codes[code.code] = code
        self._states[intent_id] = IntentState.CONFIRMED
        return code

    # -- verify at execution time ------------------------------------------
    def verify(self, code: str, *, amount_cents: int, currency: str, payee: str,
               now: Optional[float] = None) -> Intent:
        """Dynamic linking check: the code must bind *these exact* fields."""
        now = self.clock() if now is None else now
        auth = self.codes.get(code)
        if auth is None:
            raise VerificationError("unknown auth code")
        intent = self._must_get(auth.intent_id)
        state = self.state(intent.intent_id)
        if state is IntentState.CONSUMED:
            raise VerificationError("auth code already consumed (single-use)")
        if state in (IntentState.REVOKED, IntentState.EXPIRED):
            raise VerificationError(f"intent {state.value}")
        if auth.expired(now):
            self._states[intent.intent_id] = IntentState.EXPIRED
            raise VerificationError("auth code expired")
        expected = self._hmac(intent, auth.nonce)
        if not hmac.compare_digest(expected, auth.code) or not hmac.compare_digest(expected, code):
            raise VerificationError("auth code does not match intent binding")
        # PSD2 dynamic linking: any change to amount or payee invalidates.
        if int(amount_cents) != intent.amount_cents:
            raise VerificationError("amount differs from confirmed intent")
        if currency.upper() != intent.currency:
            raise VerificationError("currency differs from confirmed intent")
        if payee != intent.payee:
            raise VerificationError("payee differs from confirmed intent")
        return intent

    def consume(self, code: str) -> None:
        """Mark the code spent. Call only after the action actually executed."""
        auth = self.codes.get(code)
        if auth is None:
            raise VerificationError("unknown auth code")
        self._states[auth.intent_id] = IntentState.CONSUMED

    def revoke(self, intent_id: str) -> None:
        self._must_get(intent_id)
        self._states[intent_id] = IntentState.REVOKED

    def state(self, intent_id: str) -> IntentState:
        return self._states.get(intent_id, IntentState.REVOKED)

    # -- internals ----------------------------------------------------------
    def _must_get(self, intent_id: str) -> Intent:
        try:
            return self.intents[intent_id]
        except KeyError:
            raise VerificationError(f"unknown intent: {intent_id}") from None

    def _hmac(self, intent: Intent, nonce: str) -> str:
        msg = "|".join([
            intent.intent_id, str(intent.amount_cents), intent.currency,
            intent.payee, nonce,
        ]).encode()
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

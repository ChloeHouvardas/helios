"""Error codes for the payments tool.

Every error returns `{code, message, retryable}` — the same wire shape as
the other Helios tools.
"""

from __future__ import annotations

# Only provider failures are retryable.
RETRYABLE_CODES = frozenset({"rate_limited", "upstream_unavailable"})

STATUS_CODES = {
    "validation_error": 400,
    "permission_denied": 403,
    "not_found": 404,
    "amount_mismatch": 400,
    "link_inactive": 409,
    "invalid_transition": 409,
    "event_conflict": 409,
    "refund_exceeds_payment": 400,
    "unbalanced_entry": 500,
    "rate_limited": 429,
    "upstream_unavailable": 503,
}


class HeliosError(Exception):
    """Tool error carrying the {code, message, retryable} shape."""

    def __init__(self, code: str, message: str, retryable: bool | None = None,
                 details: dict | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable
        self.details = details
        self.retry_after = retry_after

    def payload(self) -> dict:
        """The wire shape every error returns."""
        body = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.details:
            body["details"] = self.details
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body

    @property
    def http_status(self) -> int:
        return STATUS_CODES.get(self.code, 400)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HeliosError({self.code!r}, {self.message!r}, retryable={self.retryable})"


def validation(message: str, details: dict | None = None) -> HeliosError:
    """A parameter is missing or the wrong type; `message` names it."""
    return HeliosError("validation_error", message, details=details)


def permission_denied(tool: str, allowed: list[str] | None = None) -> HeliosError:
    """The app's manifest does not list this tool.

    Scoped access: calls outside `permissions.collect` return this error.
    """
    details: dict = {"tool": tool}
    if allowed is not None:
        details["allowed"] = sorted(allowed)
    return HeliosError(
        "permission_denied",
        f"app manifest does not permit '{tool}'; add it to permissions.collect "
        "and redeploy",
        details=details)


def not_found(kind: str, key: str) -> HeliosError:
    """Unknown id — also the answer when the id belongs to another business,
    so existence is never leaked across tenants."""
    return HeliosError(
        "not_found", f"{kind} '{key}' was not found",
        details={"kind": kind, "key": key})


def amount_mismatch(expected: int, got: int, context: str) -> HeliosError:
    """A caller- or provider-supplied amount disagrees with the server-side
    amount. Server-side amounts are the only ones that count."""
    return HeliosError(
        "amount_mismatch",
        f"{context}: expected {expected} minor units, got {got}",
        details={"expected": expected, "got": got})


def link_inactive(reason: str) -> HeliosError:
    """The payment link is closed, already used or expired; issue a new one."""
    return HeliosError("link_inactive", reason)


def invalid_transition(kind: str, key: str, old: str, new: str) -> HeliosError:
    """An illegal state-machine move; the record stays in its old state."""
    return HeliosError(
        "invalid_transition",
        f"{kind} '{key}' cannot move from '{old}' to '{new}'",
        details={"kind": kind, "key": key, "from": old, "to": new})


def event_conflict(provider: str, event_id: str) -> HeliosError:
    """A provider event_id was redeclared with a different payload. The
    stored payload is append-only and is never edited."""
    return HeliosError(
        "event_conflict",
        f"{provider} event '{event_id}' was already stored with a different "
        "payload; the stored payload is never edited",
        details={"provider": provider, "event_id": event_id})


def refund_exceeds_payment(attempt_id: str, refunded: int, requested: int,
                           captured: int) -> HeliosError:
    return HeliosError(
        "refund_exceeds_payment",
        f"refund of {requested} would exceed the captured {captured} "
        f"(already refunded {refunded}) on payment '{attempt_id}'",
        details={"attempt_id": attempt_id, "refunded": refunded,
                 "requested": requested, "captured": captured})


def unbalanced_entry(debits: int, credits: int) -> HeliosError:
    """A journal posting whose legs do not balance — an internal bug, never
    a caller's fault."""
    return HeliosError(
        "unbalanced_entry",
        f"journal does not balance: debits {debits} vs credits {credits}",
        details={"debits": debits, "credits": credits})


def rate_limited(message: str, retry_after: int | None = None) -> HeliosError:
    """Retry after `retry_after` seconds."""
    return HeliosError("rate_limited", message, retry_after=retry_after)


def upstream_unavailable(component: str, retry_after: int | None = None) -> HeliosError:
    """The payment provider did not respond. Retry with the same key."""
    return HeliosError(
        "upstream_unavailable",
        f"{component} did not respond; retry with the same idempotency_key",
        retry_after=retry_after)

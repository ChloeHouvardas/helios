"""Error codes from the Developer Guide's error table.

Every error returns `{code, message, retryable}` — guide § Conventions.
"""

from __future__ import annotations

# Guide § Errors, limits and security. Only these two are retryable.
RETRYABLE_CODES = frozenset({"rate_limited", "upstream_unavailable"})

# HTTP status per code. Guide codes map to their natural status; anything
# unrecognised falls back to 400.
STATUS_CODES = {
    # The app's manifest does not list this tool.
    "permission_denied": 403,
    # A connection_id's stored credentials expired or were revoked.
    "connection_expired": 401,
    # The file is not a readable XLSX or CSV.
    "unsupported_format": 400,
    # import_sheet could not infer the columns.
    "mapping_ambiguous": 400,
    # A computed column's formula failed in the sheet engine.
    "invalid_formula": 400,
    # A direct write hit a protected column; submit it as a proposal instead.
    "protected_column": 400,
    # Retry after `retry_after` seconds.
    "rate_limited": 429,
    # The sheet engine or the KG did not respond; retry with the same key.
    "upstream_unavailable": 503,
    # A parameter is missing or the wrong type.
    "validation_error": 400,
}


class HeliosError(Exception):
    """Tool error carrying the guide's {code, message, retryable} shape."""

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

    Scoped access — guide § Conventions. Calls outside `permissions.sheets`
    return this error.
    """
    details = {"tool": tool}
    if allowed is not None:
        details["allowed"] = sorted(allowed)
    return HeliosError(
        "permission_denied",
        f"app manifest does not permit '{tool}'; add it to permissions.sheets "
        "and redeploy",
        details=details)


def protected_column(field: str, view_id: str | None = None) -> HeliosError:
    """A direct write hit a protected column. Submit it as a proposal instead."""
    return HeliosError(
        "protected_column",
        f"'{field}' is a protected column; submit the edit as a proposal",
        details={"field": field, "view_id": view_id})


def invalid_formula(column: str, reason: str = "") -> HeliosError:
    """A computed column's formula failed; `message` names the column."""
    msg = f"formula for column '{column}' is invalid"
    if reason:
        msg = f"{msg}: {reason}"
    return HeliosError("invalid_formula", msg, details={"column": column})


def unsupported_format(reason: str) -> HeliosError:
    """The file is not a readable XLSX or CSV. Ask for a re-export."""
    return HeliosError("unsupported_format", reason)


def mapping_ambiguous(message: str, preview: dict | None = None) -> HeliosError:
    """import_sheet could not infer the columns; pass an explicit column_map."""
    return HeliosError("mapping_ambiguous", message, details=preview)


def upstream_unavailable(component: str, retry_after: int | None = None) -> HeliosError:
    """The sheet engine or the KG did not respond. Retry with the same key."""
    return HeliosError(
        "upstream_unavailable",
        f"{component} did not respond; retry with the same idempotency_key",
        retry_after=retry_after)


def rate_limited(message: str, retry_after: int | None = None) -> HeliosError:
    """Retry after `retry_after` seconds."""
    return HeliosError("rate_limited", message, retry_after=retry_after)

"""Exceptions raised by the audit agent.

Everything here is a *fail-closed* condition unless documented otherwise.
The design rule from `specs/decisions.md` is that unknown tools, malformed
arguments and unrecognised constraints are never allowed through, so these
exceptions are caught at the decision boundary and converted into a
`reject` verdict rather than propagating as a crash.
"""

from __future__ import annotations


class AuditAgentError(Exception):
    """Base class for every audit agent error."""


# --- Policy configuration ------------------------------------------------


class PolicyError(AuditAgentError):
    """A policy pack is malformed or cannot be composed."""


class UnknownTaskTypeError(PolicyError):
    """No policy matched the requested task type. Fail closed."""


class UnknownToolError(PolicyError):
    """A policy or request referenced a tool that is not registered."""


class PolicyLooseningError(PolicyError):
    """An app pack tried to loosen a protected baseline rule.

    Baseline packs ship with the platform and may be extended but never
    weakened or deleted by an application. Attempting it is an error, not
    a silent override.
    """


# --- Grants --------------------------------------------------------------


class GrantError(AuditAgentError):
    """A tool grant is unusable."""


class GrantInvalidError(GrantError):
    """Structure, signature, expiry, scope or usage check failed."""


class SigningKeyMissingError(GrantError):
    """Grants must be signed but no signing key was configured.

    The key is read from the environment; it is never hard-coded and never
    logged. Local development may opt out explicitly with
    `allow_unsigned=True`, which is recorded on every grant it produces.
    """


# --- Requests and transport ---------------------------------------------


class RequestError(AuditAgentError):
    """A request could not be parsed or is missing required fields."""


class TransportError(AuditAgentError):
    """The file-based inbox/outbox transport failed."""


# --- Log integrity -------------------------------------------------------


class AuditLogError(AuditAgentError):
    """The append-only audit log is malformed."""


class ChainBreakError(AuditLogError):
    """Hash-chain verification found tampering or a gap.

    Raised only by explicit verification; normal appending never raises
    this, it records the break as an event so evidence is preserved.
    """

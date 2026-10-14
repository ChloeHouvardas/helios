"""Helios Audit Agent Builder.

A standalone audit agent that decides what an action agent may do, and
records every decision as tamper-evident evidence.

The design principle, shared with the action side: **the model proposes,
code decides.** An action agent can ask for anything; what it gets is
bounded by policy written as data and enforced by deterministic rules that
fail closed.

Start here
----------
`build_audit_agent(manifest)` turns a YAML manifest into a running auditor.
`AuditManifest` is the manifest format. `contracts` holds the wire models
the two agents exchange. See `README.md` for the full guide.

Why standalone
--------------
The audit agent shares no code with the action agent at runtime. They
exchange the models in `contracts` over a transport (`transport`), so the
auditor can run in-process, in a sibling process, or in its own container.
That separability is what makes it trustworthy: an app cannot modify the
code that audits it.

Security properties enforced here
---------------------------------
- **Least authority.** A grant lists specific tools with specific limits.
  Anything not granted is denied; capability is never assumed.
- **Fail closed.** Unknown tools, malformed arguments, unverifiable facts
  and invalid grants are refused, not passed through.
- **Facts are not claims.** Values a rule depends on are resolved from a
  `FactSource` the action agent does not control. An agent cannot widen its
  own authority by describing the world favourably.
- **Signed, scoped, short-lived grants.** Bound to app, agent, principal and
  task type; expiry enforced; single-use for side effects; replay refused.
- **Baseline cannot be weakened.** The platform's rules apply to every app.
  An app may extend them; disabling or redefining one is refused at load.
- **Evidence is tamper-evident.** The append-only log is hash-chained, and
  credential-shaped values are redacted before they reach disk.
"""

from __future__ import annotations

from .advisory import (
    AdvisoryReviewer,
    CombinedReviewer,
    HeuristicReviewer,
    NoOpReviewer,
    QwenAdvisoryReviewer,
)
from .agent import AuditAgent
from .audit_log import AuditEvent, AuditLog, EventType, GENESIS_HASH
from .builder import build_audit_agent, build_reviewer, describe
from .contracts import (
    ActionDecision,
    ActionRequest,
    AuthorizationRequest,
    Decision,
    Finding,
    HoldRecord,
    Precedence,
    Severity,
    Signature,
    TaskAuthorization,
    ToolPermission,
    canonical_json,
    redact,
    utcnow,
)
from .errors import (
    AuditAgentError,
    AuditLogError,
    ChainBreakError,
    GrantError,
    GrantInvalidError,
    PolicyError,
    PolicyLooseningError,
    RequestError,
    SigningKeyMissingError,
    TransportError,
    UnknownTaskTypeError,
    UnknownToolError,
)
from .facts import (
    ChainedFactSource,
    FactResolver,
    FactSource,
    StaticFactSource,
    StoreFactSource,
)
from .manifest import (
    AuditManifest,
    ManifestRule,
    ManifestSettings,
    ManifestTask,
    ManifestTool,
    format_validation_error,
    validate_manifest_file,
    validate_manifest_text,
)
from .policy import (
    AuditFacts,
    BASELINE_RULE_IDS,
    DeclarativeRule,
    GrantDraft,
    GrantVerifier,
    InMemoryReplayGuard,
    Keyring,
    LimitSpec,
    PolicyEngine,
    PolicyPack,
    ReplayGuard,
    RuleOutcome,
    TaskPolicy,
    ToolDescriptor,
    ToolOverride,
    ToolRegistry,
)
from .transport import (
    AuditClient,
    FileTransport,
    FileTransportServer,
    HttpTransport,
    HttpTransportServer,
    InProcessTransport,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # Agent and construction
    "AuditAgent",
    "build_audit_agent",
    "build_reviewer",
    "describe",
    "AuditManifest",
    "ManifestRule",
    "ManifestSettings",
    "ManifestTask",
    "ManifestTool",
    "format_validation_error",
    "validate_manifest_file",
    "validate_manifest_text",
    # Wire contract
    "ActionDecision",
    "ActionRequest",
    "AuthorizationRequest",
    "Decision",
    "Finding",
    "HoldRecord",
    "Precedence",
    "Severity",
    "Signature",
    "TaskAuthorization",
    "ToolPermission",
    "canonical_json",
    "redact",
    "utcnow",
    # Policy
    "AuditFacts",
    "BASELINE_RULE_IDS",
    "DeclarativeRule",
    "GrantDraft",
    "GrantVerifier",
    "InMemoryReplayGuard",
    "Keyring",
    "LimitSpec",
    "PolicyEngine",
    "PolicyPack",
    "ReplayGuard",
    "RuleOutcome",
    "TaskPolicy",
    "ToolDescriptor",
    "ToolOverride",
    "ToolRegistry",
    # Facts
    "ChainedFactSource",
    "FactResolver",
    "FactSource",
    "StaticFactSource",
    "StoreFactSource",
    # Log
    "AuditEvent",
    "AuditLog",
    "EventType",
    "GENESIS_HASH",
    # Advisory
    "AdvisoryReviewer",
    "CombinedReviewer",
    "HeuristicReviewer",
    "NoOpReviewer",
    "QwenAdvisoryReviewer",
    # Transport
    "AuditClient",
    "FileTransport",
    "FileTransportServer",
    "HttpTransport",
    "HttpTransportServer",
    "InProcessTransport",
    # Errors
    "AuditAgentError",
    "AuditLogError",
    "ChainBreakError",
    "GrantError",
    "GrantInvalidError",
    "PolicyError",
    "PolicyLooseningError",
    "RequestError",
    "SigningKeyMissingError",
    "TransportError",
    "UnknownTaskTypeError",
    "UnknownToolError",
]

"""The wire contract between an action agent and the audit agent.

Two agents, two processes, one contract. The action agent asks the audit
agent what it may do; the audit agent answers with a signed, time-boxed
grant. Neither side imports the other's code — they exchange these
models. That is what makes the audit agent standalone.

The two interactions, from `specs/interfaces.md` §3 and the deployment
requirement that the audit agent decides which tools an action agent may
use for a task:

1. **Task authorization** (`AuthorizationRequest` -> `TaskAuthorization`)
   Before work starts, the action agent declares its task type, what it
   intends to touch and why. The audit agent returns the exact set of
   tools it may use, the argument constraints on each, and how long the
   grant lasts. This is the "which tools for which task" decision.

2. **Action adjudication** (`ActionRequest` -> `ActionDecision`)
   At runtime, before a specific side effect, the action agent presents
   the concrete call. The audit agent checks it against the grant plus
   policy and returns allow, deny or hold.

Design rules these models encode:

- **Fail closed.** Every field the audit agent relies on for a decision is
  required or has a restrictive default. Absent evidence is not permission.
- **Claims are separated from evidence.** The action agent supplies
  *claims*; the audit agent resolves *facts* from its own sources. A claim
  never widens a grant on its own.
- **Grants are signed and scoped.** An authorization is useless unless it
  was issued to this agent, for this app, for this task type, and has not
  expired or been replayed.
- **Money is Decimal, never float.** Inherited from the Build 1 contract;
  rounding on financial thresholds is not acceptable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --- Serialisation helpers -----------------------------------------------

#: Fields that must never appear in a log line, a grant, or an error
#: message. The audit agent sees a lot of financial context; credential
#: hygiene is a competition rule, not a nicety.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "authorization",
        "cookie",
        "private_key",
        "client_secret",
        "dashscope_api_key",
    }
)

#: What replaces a redacted value.
REDACTED = "***REDACTED***"


def canonical_json(payload: Any) -> str:
    """Deterministic JSON text, used as the signing payload.

    Key order, separators and non-ASCII handling are all pinned so that two
    processes on two machines produce byte-identical text for the same
    data. Without this, signatures would not verify across the wire.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def redact(mapping: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of `mapping` with credential-shaped keys masked.

    Shallow on purpose: context values are scalars or small structures by
    contract, and a deep walk would silently rewrite nested keys that
    happen to be named `token` for legitimate business reasons.
    """
    if not mapping:
        return {}
    return {
        k: (REDACTED if k.lower() in REDACTED_KEYS else v)
        for k, v in mapping.items()
    }


def utcnow() -> datetime:
    """Timezone-aware current time. Naive datetimes are rejected below."""
    return datetime.now(timezone.utc)


# --- Enums ---------------------------------------------------------------


class Decision(str, Enum):
    """What the audit agent concluded.

    Ordered from least to most restrictive in the sense that `DENY` and
    `HOLD` both stop the action; only `ALLOW` lets it proceed.
    """

    #: Permitted. The action agent may proceed, within the grant.
    ALLOW = "allow"

    #: Not permitted. A machine-readable reason is returned so the action
    #: agent can adjust and retry — this is a normal outcome, not a crash.
    DENY = "deny"

    #: A human owner must decide. The action agent pauses; the audit agent
    #: records the pending item and can be resumed later.
    HOLD = "hold"


class Severity(str, Enum):
    """How serious a finding is. Drives precedence when rules conflict."""

    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


#: Rule effect on the final decision.
Effect = Literal["allow", "deny", "hold", "require_approval"]


# --- Findings ------------------------------------------------------------


class Finding(BaseModel):
    """One rule's contribution to a decision.

    Findings are the audit trail. A decision without them is an
    unexplainable verdict, which is exactly what a financial audit must
    not produce.
    """

    rule_id: str = Field(min_length=1, description="Stable identifier of the rule that fired")
    effect: Effect = Field(description="What this rule wants to happen")
    severity: Severity = Severity.INFO
    message: str = Field(min_length=1, description="Human-readable explanation")
    #: Which policy pack supplied the rule: `baseline` (platform, cannot be
    #: weakened) or `app` (application-specific extension).
    source: Literal["baseline", "app"] = "baseline"
    #: Structured evidence: the value that tripped the rule, the limit it
    #: tripped, the tool involved. Free-form so rules can explain
    #: themselves without a schema change per rule.
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("details", mode="before")
    @classmethod
    def _redact_details(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return redact(value)
        return value


class Precedence:
    """How competing findings combine into one decision.

    Written as a class rather than scattered `if`s so the ordering is
    reviewable in one place — this is the kind of logic a judge or an
    auditor should be able to read and confirm.
    """

    #: Most restrictive effect wins. A single CRITICAL deny outranks a
    #: hundred allows.
    ORDER: tuple[Effect, ...] = ("deny", "hold", "require_approval", "allow")

    @classmethod
    def resolve(cls, findings: list[Finding]) -> Decision:
        """Collapse findings into a single decision.

        No findings at all means nothing authorised the action: that is a
        deny, not an allow. Fail closed.
        """
        if not findings:
            return Decision.DENY
        effects = {f.effect for f in findings}
        for effect in cls.ORDER:
            if effect in effects:
                if effect == "deny":
                    return Decision.DENY
                if effect in ("hold", "require_approval"):
                    return Decision.HOLD
                return Decision.ALLOW
        return Decision.DENY  # pragma: no cover - ORDER covers every Effect


# --- Tool permissions ----------------------------------------------------


class ToolPermission(BaseModel):
    """What an action agent may do with one tool, under one grant.

    This is the unit of the audit agent's answer to "which tools can you
    use for this task". A tool absent from the grant is unusable, which is
    the point: capability is granted, never assumed.
    """

    tool: str = Field(min_length=1)

    #: Side-effect tools are gated per call; read-only tools run directly.
    #: Mirrors `ToolContract.has_side_effect` from the Build 1 contract so
    #: the two agents agree on which calls need checking.
    side_effect: bool = False

    #: JSON Schema fragment the arguments must satisfy. Enforced with
    #: `jsonschema`; unknown or malformed arguments are rejected.
    argument_constraints: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema the call arguments must validate against",
    )

    #: Numeric ceilings resolved by rules at decision time, e.g.
    #: {"max_extension_days": 14, "max_amount": "5000.00"}. Kept as data so
    #: the platform can cap app-declared limits without parsing schemas.
    limits: dict[str, Any] = Field(default_factory=dict)

    #: Hard ceiling on how many times this tool may be called under the
    #: grant. None means the policy default applies.
    max_calls: int | None = Field(default=None, ge=1)

    #: True when every call needs an owner decision, regardless of limits.
    requires_approval: bool = False

    #: Free-text condition shown to the owner in the approval queue, e.g.
    #: "extension offers may not exceed 14 days without owner approval".
    condition: str | None = None


# --- Requests ------------------------------------------------------------


class BaseRequest(BaseModel):
    """Fields every request carries, for identity, replay and audit."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, description="Caller-generated unique id")
    app_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    #: Who the agent acts on behalf of. Held grants go to this principal.
    principal: str = Field(
        min_length=1,
        default="owner",
        description="Owner/account the agent represents",
    )
    issued_at: datetime = Field(default_factory=utcnow)

    @field_validator("issued_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        """Naive datetimes make expiry comparisons silently wrong across
        timezones. Reject them rather than guessing UTC."""
        if value.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware")
        return value


class AuthorizationRequest(BaseRequest):
    """Pre-flight: what may this agent do for this task?

    The action agent declares intent. It does not get to declare its own
    permissions — `requested_tools` is a *request*, and the audit agent
    returns the subset it is willing to grant, with constraints attached.
    """

    task_type: str = Field(
        min_length=1,
        description="Registered task type, e.g. 'collect.reminder'",
    )
    task_summary: str = Field(
        default="",
        max_length=2000,
        description="Plain-language statement of what the agent intends to do",
    )
    requested_tools: list[str] = Field(
        default_factory=list,
        description="Tools the agent would like; the grant may be narrower",
    )
    #: Business context the rules may inspect: subject ids, amounts,
    #: counterparties. Values are claims until a fact source confirms them.
    context: dict[str, Any] = Field(default_factory=dict)
    #: Evidence supporting the task, e.g. an invoice id the agent already
    #: looked up. Rules can require evidence before widening a grant.
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("context", "evidence", mode="before")
    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return redact(value)
        return value


class ActionRequest(BaseRequest):
    """Runtime: may this specific call go ahead?

    Carries the grant it is acting under so the audit agent can verify the
    call is inside the authority it already issued, rather than trusting
    the action agent's memory of it.
    """

    task_type: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: The signed grant this call relies on. Required for side-effect
    #: tools; a missing grant is a deny.
    authorization: TaskAuthorization | None = None
    #: Facts the runtime already resolved (trusted store lookups). The
    #: audit agent re-verifies anything a rule depends on.
    resolved_facts: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments", "resolved_facts", mode="before")
    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return redact(value)
        return value


# --- Grants and decisions ------------------------------------------------


class Signature(BaseModel):
    """HMAC-SHA256 over the canonical JSON of the signed body."""

    algorithm: Literal["HMAC-SHA256"] = "HMAC-SHA256"
    key_id: str = Field(
        min_length=1,
        description="Identifies which key signed this; never the key itself",
    )
    value: str = Field(min_length=1, description="Hex digest")


class TaskAuthorization(BaseModel):
    """The audit agent's answer: a signed, scoped, expiring grant.

    Scope is deliberately narrow. A grant is bound to one app, one agent,
    one task type and one principal, and it expires. Stealing a grant from
    a log gets you nothing you can use, and a stale grant cannot be
    replayed into a new task.
    """

    model_config = ConfigDict(extra="forbid")

    authorization_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    principal: str = Field(min_length=1)
    task_type: str = Field(min_length=1)

    decision: Decision
    permissions: list[ToolPermission] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)

    issued_at: datetime = Field(default_factory=utcnow)
    #: Short-lived by default. A long grant is a standing capability, and
    #: standing capabilities are what audits exist to remove.
    expires_at: datetime
    #: Single-use for side-effect calls unless the policy raises this.
    max_uses: int = Field(default=1, ge=0, description="0 means unlimited")

    #: What the owner must be told when a call under this grant is held.
    owner_brief: str = Field(
        default="",
        max_length=4000,
        description="Plain-language summary for the approval queue",
    )
    #: True when this grant was produced without a signing key. Recorded,
    #: never inferred, so a deployment cannot quietly run unsigned.
    unsigned: bool = False
    signature: Signature | None = None

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    # -- Signing ----------------------------------------------------------

    def signable_body(self) -> dict[str, Any]:
        """Everything the signature covers.

        Excludes `signature` and `unsigned` — a signature cannot cover
        itself, and the unsigned flag is set by the signer.
        """
        return self.model_dump(
            mode="json",
            exclude={"signature", "unsigned"},
        )

    def canonical_body(self) -> str:
        return canonical_json(self.signable_body())

    def digest(self) -> str:
        """SHA-256 of the canonical body, used for hash-chaining the log."""
        return hashlib.sha256(self.canonical_body().encode("utf-8")).hexdigest()

    def sign(self, key: bytes, key_id: str) -> "TaskAuthorization":
        """Return a signed copy. Never mutates in place."""
        mac = hmac.new(key, self.canonical_body().encode("utf-8"), hashlib.sha256)
        return self.model_copy(
            update={
                "signature": Signature(key_id=key_id, value=mac.hexdigest()),
                "unsigned": False,
            }
        )

    def verify(self, key: bytes) -> bool:
        """Recompute the HMAC and compare in constant time."""
        if self.signature is None:
            return False
        expected = hmac.new(
            key, self.canonical_body().encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, self.signature.value)

    # -- Convenience ------------------------------------------------------

    def permission_for(self, tool: str) -> ToolPermission | None:
        for permission in self.permissions:
            if permission.tool == tool:
                return permission
        return None

    def tool_names(self) -> list[str]:
        """The direct answer to 'which tools may I use for this task'."""
        return [p.tool for p in self.permissions]

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return now >= self.expires_at


class ActionDecision(BaseModel):
    """The audit agent's runtime verdict on one concrete call."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    authorization_id: str | None = None
    app_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    task_type: str = Field(min_length=1)

    decision: Decision
    findings: list[Finding] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=utcnow)

    #: When a rule grants less than was asked (e.g. a 60-day extension
    #: capped to 14), the approved value goes here. The action agent must
    #: execute the granted value, never its original request — the same
    #: rule Build 1's `extension_days_granted` encodes.
    granted_arguments: dict[str, Any] | None = None

    #: Why the decision was made, in one sentence the action agent can
    #: feed back to the model so it can adjust.
    reason: str = ""
    #: Set when decision is HOLD: what the owner needs to decide, and the
    #: id used to resume.
    hold_id: str | None = None
    owner_question: str | None = None
    #: Advisory-only model commentary. Never affects `decision`.
    advisory_notes: list[str] = Field(default_factory=list)

    @field_validator("granted_arguments", mode="before")
    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return redact(value)
        return value

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


class HoldRecord(BaseModel):
    """A held action awaiting an owner decision, resumable later."""

    model_config = ConfigDict(extra="forbid")

    hold_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    principal: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    owner_question: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    #: Decisions are scoped to the exact action that was held, so the
    #: resume carries the original arguments for comparison.
    status: Literal["pending", "approved", "rejected"] = "pending"
    decided_by: str | None = None
    decided_at: datetime | None = None

    @field_validator("arguments", mode="before")
    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return redact(value)
        return value

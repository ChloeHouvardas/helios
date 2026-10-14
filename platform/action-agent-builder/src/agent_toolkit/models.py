"""Data shapes for proposals, verdicts, and mandates.

Shared by the runtime, the audit gate, the action log, and agent manifests.
Plain data with strict (de)serialization: anything malformed raises, it is
never silently repaired.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

APPROVE = "approve"
HOLD = "hold"
REJECT = "reject"
DECISIONS = (APPROVE, HOLD, REJECT)


def new_proposal_id() -> str:
    return f"prop-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Proposal:
    """A side-effect tool call that was intercepted before execution."""

    proposal_id: str
    agent: str
    tool: str
    arguments: Mapping[str, Any]

    @classmethod
    def create(cls, agent: str, tool: str, arguments: Mapping[str, Any]) -> "Proposal":
        return cls(
            proposal_id=new_proposal_id(),
            agent=agent,
            tool=tool,
            arguments=dict(arguments),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "agent": self.agent,
            "tool": self.tool,
            "arguments": dict(self.arguments),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Proposal":
        return cls(
            proposal_id=str(data["proposal_id"]),
            agent=str(data["agent"]),
            tool=str(data["tool"]),
            arguments=dict(data.get("arguments") or {}),
        )


@dataclass(frozen=True)
class Verdict:
    """The audit gate's decision on a proposal.

    Attributes:
        decision: one of "approve", "hold", "reject".
        rule: identifier of the deterministic rule that produced the decision.
        reason: human-readable explanation; returned to the agent on reject
            and recorded in the action log for every verdict.
    """

    decision: str
    rule: str
    reason: str

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"invalid verdict decision: {self.decision!r}")

    @property
    def approved(self) -> bool:
        return self.decision == APPROVE

    @property
    def held(self) -> bool:
        return self.decision == HOLD

    @property
    def rejected(self) -> bool:
        return self.decision == REJECT

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "rule": self.rule, "reason": self.reason}


_LIMIT_KEYS = (
    "max_extension_days",
    "extension_owner_approval_days",
    "min_days_between_contacts",
    "invoice_owner_approval_amount",
)


@dataclass(frozen=True)
class MandateLimits:
    """Numeric limits an agent's mandate may set.

    A missing limit means "no limit of this kind". Zero is meaningful
    (for example, an approval threshold of 0 means every case needs the
    owner's approval).
    """

    max_extension_days: int | None = None
    extension_owner_approval_days: int | None = None
    min_days_between_contacts: int | None = None
    invoice_owner_approval_amount: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "max_extension_days": self.max_extension_days,
                "extension_owner_approval_days": self.extension_owner_approval_days,
                "min_days_between_contacts": self.min_days_between_contacts,
                "invoice_owner_approval_amount": self.invoice_owner_approval_amount,
            }.items()
            if value is not None
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "MandateLimits":
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ValueError("mandate limits must be an object")
        unknown = sorted(set(data) - set(_LIMIT_KEYS))
        if unknown:
            raise ValueError(
                f"unknown mandate limit(s): {unknown}; allowed: {list(_LIMIT_KEYS)}"
            )
        values: dict[str, int | float] = {}
        for key in _LIMIT_KEYS:
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"mandate limit {key!r} must be a number")
            if value < 0:
                raise ValueError(f"mandate limit {key!r} must not be negative")
            values[key] = value
        max_days = values.get("max_extension_days")
        approval_days = values.get("extension_owner_approval_days")
        if (
            isinstance(max_days, int)
            and isinstance(approval_days, int)
            and approval_days > max_days
        ):
            raise ValueError(
                "extension_owner_approval_days must not exceed max_extension_days"
            )
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Mandate:
    """What an agent is allowed to do: allowed tools plus limits."""

    allowed_tools: tuple[str, ...]
    limits: MandateLimits = MandateLimits()

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "limits": self.limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Mandate":
        if not isinstance(data, Mapping):
            raise ValueError("mandate must be an object")
        unknown = sorted(set(data) - {"allowed_tools", "limits"})
        if unknown:
            raise ValueError(f"unknown mandate field(s): {unknown}")
        allowed = data.get("allowed_tools")
        if (
            not isinstance(allowed, (list, tuple))
            or not allowed
            or not all(isinstance(tool, str) and tool for tool in allowed)
        ):
            raise ValueError("mandate.allowed_tools must be a non-empty list of tool names")
        if len(set(allowed)) != len(allowed):
            raise ValueError("mandate.allowed_tools contains duplicates")
        limits = MandateLimits.from_dict(data.get("limits"))
        return cls(allowed_tools=tuple(allowed), limits=limits)

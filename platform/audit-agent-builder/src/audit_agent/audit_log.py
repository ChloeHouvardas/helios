"""Append-only, hash-chained audit log.

`specs/decisions.md` lists the append-only action log with hash chaining as
the likely design; this implements it for the audit side. Every
authorization issued, every decision made and every owner verdict is an
event, and each event commits to the one before it.

Why chaining matters here rather than being decoration: the audit agent's
output *is* the compliance evidence. If a line can be edited or removed
after the fact without anyone noticing, the audit proves nothing. A
modified or deleted entry breaks verification at that point and every
entry after it.

Three rules the implementation enforces:

- **Append only.** There is no update or delete method. The file is opened
  in append mode and entries are written whole.
- **Deterministic bytes.** Each entry is canonical JSON on one line, so the
  hash does not depend on dict ordering or float formatting.
- **Credential-free.** Values pass through `redact()` on the way in, so a
  secret in a context blob never reaches disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import (
    ActionDecision,
    Decision,
    Finding,
    TaskAuthorization,
    canonical_json,
    redact,
    utcnow,
)
from .errors import AuditLogError, ChainBreakError

#: Hash used for the first entry's `prev_hash`. A constant rather than
#: empty string so the genesis hash is not confusable with "missing".
GENESIS_HASH = "0" * 64


class EventType(str, Enum):
    """What happened. Kept as an enum so log queries are type-safe."""

    #: A task authorization was requested.
    AUTHORIZATION_REQUESTED = "authorization_requested"
    #: A grant (or refusal) was issued.
    AUTHORIZATION_ISSUED = "authorization_issued"
    #: A concrete side-effect call was adjudicated.
    ACTION_DECIDED = "action_decided"
    #: An action was held for the owner.
    HOLD_CREATED = "hold_created"
    #: The owner decided on a hold.
    HOLD_RESOLVED = "hold_resolved"
    #: The log itself was found to be broken. Recorded, never raised
    #: silently, so tampering leaves a trace.
    INTEGRITY = "integrity"
    #: Advisory-only model commentary. Never a decision.
    ADVISORY = "advisory"


class AuditEvent(BaseModel):
    """One line of the log."""

    model_config = ConfigDict(extra="forbid")

    #: Monotonic within a log file; restarts are detectable as duplicates.
    sequence: int = Field(ge=0)
    event_type: EventType
    recorded_at: datetime
    #: Identity of whatever this concerns.
    app_id: str = ""
    agent_id: str = ""
    principal: str = ""
    task_type: str = ""
    tool: str = ""
    request_id: str = ""
    authorization_id: str = ""
    hold_id: str = ""
    #: The outcome, when the event has one.
    decision: Decision | None = None
    #: Rule findings that produced the decision — the explanation.
    findings: list[Finding] = Field(default_factory=list)
    #: Structured detail. Redacted on write.
    details: dict[str, Any] = Field(default_factory=dict)
    #: Hash of the previous entry. Genesis uses GENESIS_HASH.
    prev_hash: str = Field(min_length=64, max_length=64)
    #: SHA-256 over this entry's content plus `prev_hash`.
    hash: str = Field(min_length=64, max_length=64)

    @field_validator("details", mode="before")
    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return redact(value)
        return value

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware")
        return value

    # -- Hashing ----------------------------------------------------------

    def hashed_body(self) -> dict[str, Any]:
        """The fields the hash covers: everything except `hash` itself."""
        return self.model_dump(mode="json", exclude={"hash"})

    @staticmethod
    def compute_hash(body: dict[str, Any], prev_hash: str) -> str:
        """SHA-256 over canonical JSON of the body, with prev_hash included.

        `prev_hash` is in the body already, but is also mixed in explicitly
        so the chain cannot be broken by a body that happens to omit it.
        """
        payload = canonical_json(body)
        return hashlib.sha256(
            (prev_hash + "|" + payload).encode("utf-8")
        ).hexdigest()

    def verify_against(self, expected_prev_hash: str) -> bool:
        """Recompute this entry's hash and check the link."""
        if self.prev_hash != expected_prev_hash:
            return False
        return self.hash == self.compute_hash(self.hashed_body(), self.prev_hash)


class AuditLog:
    """Append-only JSONL log with hash chaining.

    Thread-safe: two agents in one process can share a log without
    interleaving writes, and the sequence stays monotonic.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sequence = self._resume_sequence()
        self._head = self._resume_head()

    # -- Construction -----------------------------------------------------

    def _read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return [
            line
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _resume_sequence(self) -> int:
        lines = self._read_lines()
        return len(lines)

    def _resume_head(self) -> str:
        """Hash of the last entry, or genesis for an empty log."""
        lines = self._read_lines()
        if not lines:
            return GENESIS_HASH
        try:
            entry = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise AuditLogError(f"{self.path}: last line is not valid JSON: {exc}") from exc
        head = entry.get("hash")
        if not isinstance(head, str) or len(head) != 64:
            raise AuditLogError(f"{self.path}: last line has no usable hash")
        return head

    # -- Writing ----------------------------------------------------------

    @property
    def head(self) -> str:
        return self._head

    @property
    def sequence(self) -> int:
        """Number of entries written so far."""
        return self._sequence

    def append(
        self,
        event_type: EventType,
        *,
        app_id: str = "",
        agent_id: str = "",
        principal: str = "",
        task_type: str = "",
        tool: str = "",
        request_id: str = "",
        authorization_id: str = "",
        hold_id: str = "",
        decision: Decision | None = None,
        findings: list[Finding] | None = None,
        details: dict[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> AuditEvent:
        """Append one event and return it, hash included."""
        with self._lock:
            event = AuditEvent(
                sequence=self._sequence,
                event_type=event_type,
                recorded_at=recorded_at or utcnow(),
                app_id=app_id,
                agent_id=agent_id,
                principal=principal,
                task_type=task_type,
                tool=tool,
                request_id=request_id,
                authorization_id=authorization_id,
                hold_id=hold_id,
                decision=decision,
                findings=findings or [],
                details=redact(details or {}),
                prev_hash=self._head,
                # Placeholder; computed below. Pydantic requires a value.
                hash=GENESIS_HASH,
            )
            body = event.hashed_body()
            event = event.model_copy(
                update={"hash": AuditEvent.compute_hash(body, self._head)}
            )
            line = canonical_json(event.model_dump(mode="json"))
            # Append mode plus a single write keeps concurrent processes
            # from interleaving partial lines.
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._head = event.hash
            self._sequence += 1
            return event

    # -- Convenience writers ---------------------------------------------

    def record_authorization(
        self, request_id: str, authorization: TaskAuthorization
    ) -> AuditEvent:
        return self.append(
            EventType.AUTHORIZATION_ISSUED,
            app_id=authorization.app_id,
            agent_id=authorization.agent_id,
            principal=authorization.principal,
            task_type=authorization.task_type,
            request_id=request_id,
            authorization_id=authorization.authorization_id,
            decision=authorization.decision,
            findings=authorization.findings,
            details={
                "tools": authorization.tool_names(),
                "expires_at": authorization.expires_at.isoformat(),
                "unsigned": authorization.unsigned,
                "grant_digest": authorization.digest(),
            },
        )

    def record_decision(
        self, request_id: str, decision: ActionDecision
    ) -> AuditEvent:
        return self.append(
            EventType.ACTION_DECIDED,
            app_id=decision.app_id,
            agent_id=decision.agent_id,
            task_type=decision.task_type,
            tool=decision.tool,
            request_id=request_id,
            authorization_id=decision.authorization_id or "",
            decision=decision.decision,
            findings=decision.findings,
            details={
                "reason": decision.reason,
                "granted_arguments": decision.granted_arguments or {},
                "hold_id": decision.hold_id or "",
            },
        )

    # -- Reading ----------------------------------------------------------

    def read(self) -> list[AuditEvent]:
        """Parse every entry. Raises on malformed lines — a log that cannot
        be read is not evidence."""
        events: list[AuditEvent] = []
        for index, line in enumerate(self._read_lines()):
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditLogError(
                    f"{self.path}:{index + 1}: invalid JSON: {exc}"
                ) from exc
            try:
                events.append(AuditEvent.model_validate(data))
            except Exception as exc:
                raise AuditLogError(
                    f"{self.path}:{index + 1}: invalid audit event: {exc}"
                ) from exc
        return events

    def iter_events(self) -> Iterator[AuditEvent]:
        yield from self.read()

    def verify_chain(self) -> tuple[bool, list[str]]:
        """Walk the chain and report every break.

        Returns (ok, problems). Never raises, so a health check can call it
        on a possibly-damaged log. Checks, in order: hash correctness, link
        correctness, sequence monotonicity, and duplicate sequence numbers.
        """
        problems: list[str] = []
        expected_prev = GENESIS_HASH
        seen_sequences: set[int] = set()
        for index, event in enumerate(self.read()):
            label = f"entry {index} (sequence {event.sequence})"
            if event.sequence in seen_sequences:
                problems.append(f"{label}: duplicate sequence number")
            seen_sequences.add(event.sequence)
            if event.sequence != index:
                problems.append(
                    f"{label}: sequence does not match position ({index})"
                )
            if event.prev_hash != expected_prev:
                problems.append(
                    f"{label}: prev_hash does not match the previous entry"
                )
            if event.hash != AuditEvent.compute_hash(
                event.hashed_body(), event.prev_hash
            ):
                problems.append(f"{label}: hash does not match its content")
            expected_prev = event.hash
        return (not problems), problems

    def assert_intact(self) -> None:
        """Raise `ChainBreakError` if the log has been altered."""
        ok, problems = self.verify_chain()
        if not ok:
            raise ChainBreakError(
                f"{self.path}: audit log integrity failure: " + "; ".join(problems)
            )

    def findings_for(self, request_id: str) -> list[Finding]:
        """Every finding recorded against one request, for explaining a verdict."""
        out: list[Finding] = []
        for event in self.read():
            if event.request_id == request_id:
                out.extend(event.findings)
        return out

    def decisions(
        self, decision: Decision | None = None
    ) -> list[AuditEvent]:
        events = [e for e in self.read() if e.event_type is EventType.ACTION_DECIDED]
        if decision is None:
            return events
        return [e for e in events if e.decision is decision]


__all__ = [
    "AuditEvent",
    "AuditLog",
    "EventType",
    "GENESIS_HASH",
]

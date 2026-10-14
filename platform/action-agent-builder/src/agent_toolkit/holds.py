"""Holds: pausing a run for the owner, and resuming it afterwards.

When the audit gate returns "hold", the runtime stops before executing
the proposal and persists everything needed to continue exactly where it
stopped:

- the proposal itself,
- the conversation history at that point (including the assistant's
  function call that produced the proposal), and
- any later calls in the same batch that were not yet processed.

The owner's decision is recorded in the action log and applies only to
the exact held proposal — not to the agent, not to future calls. After
the decision the hold file is removed; the log keeps the full story.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .models import Proposal


@dataclass(frozen=True)
class HoldRecord:
    run_id: str
    agent: str
    proposal: Proposal
    reason: str
    messages: list[dict[str, Any]]
    remaining_calls: list[dict[str, Any]] = field(default_factory=list)
    function_id: str = "1"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "proposal": self.proposal.to_dict(),
            "reason": self.reason,
            "messages": self.messages,
            "remaining_calls": self.remaining_calls,
            "function_id": self.function_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HoldRecord":
        return cls(
            run_id=str(data["run_id"]),
            agent=str(data["agent"]),
            proposal=Proposal.from_dict(data["proposal"]),
            reason=str(data["reason"]),
            messages=list(data["messages"]),
            remaining_calls=list(data.get("remaining_calls") or []),
            function_id=str(data.get("function_id") or "1"),
            created_at=str(data.get("created_at") or ""),
        )


class HoldStore:
    """Open holds for a runs directory: .runs/<run_id>/holds/<proposal_id>.json."""

    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)

    def _run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def _path(self, run_id: str, proposal_id: str) -> Path:
        return self._run_dir(run_id) / "holds" / f"{proposal_id}.json"

    def save(self, record: HoldRecord) -> Path:
        path = self._path(record.run_id, record.proposal.proposal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def load(self, run_id: str, proposal_id: str) -> HoldRecord:
        path = self._path(run_id, proposal_id)
        if not path.exists():
            raise FileNotFoundError(
                f"no open hold for proposal {proposal_id} in run {run_id}"
            )
        return HoldRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_for_run(self, run_id: str) -> list[HoldRecord]:
        holds_dir = self._run_dir(run_id) / "holds"
        if not holds_dir.exists():
            return []
        records = [
            HoldRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(holds_dir.glob("*.json"))
        ]
        return records

    def delete(self, run_id: str, proposal_id: str) -> None:
        path = self._path(run_id, proposal_id)
        path.unlink(missing_ok=True)

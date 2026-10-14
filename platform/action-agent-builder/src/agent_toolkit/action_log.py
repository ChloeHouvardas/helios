"""Append-only action log: one JSONL file per run, under .runs/<run_id>/.

Every step of every run is recorded — tool calls, proposals, verdicts,
results, holds, owner decisions, resume, run start/finish. Entries are
never modified or deleted after being written; each entry gets a
monotonic sequence number, a timestamp, the run id, and the agent name.

Entry kinds and their fields:
  run_started    manifest, model
  run_finished   status (completed | held | error), detail
  tool_call      tool, arguments, side_effect
  proposal       proposal_id, tool, arguments
  verdict        proposal_id, decision, rule, reason
  action_result  tool, arguments, executed, result? | error? | reason?, proposal_id?
  hold           proposal_id, rule, reason
  owner_decision proposal_id, decision (approved | rejected), note
  resumed        proposal_id
  error          message
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

LOG_FILENAME = "action-log.jsonl"


class ActionLog:
    def __init__(self, run_dir: Path, run_id: str, agent: str):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / LOG_FILENAME
        self.run_id = run_id
        self.agent = agent
        self._seq = self._count_entries()

    def _count_entries(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        entry = {
            "seq": self._seq,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "agent": self.agent,
            "kind": kind,
        }
        entry.update(fields)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
        return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    entries.append(json.loads(line))
        return entries

    @staticmethod
    def read(run_dir: Path) -> list[dict[str, Any]]:
        return ActionLog(Path(run_dir), run_id="", agent="").entries()


def find_runs(runs_dir: Path) -> list[Path]:
    """All run directories that contain an action log, oldest first."""
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return []
    return sorted(
        (child for child in runs_dir.iterdir() if (child / LOG_FILENAME).exists()),
        key=lambda path: path.name,
    )


def entries_by_kind(entries: list[Mapping[str, Any]], kind: str) -> list[Mapping[str, Any]]:
    return [entry for entry in entries if entry.get("kind") == kind]

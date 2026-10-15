"""Append-only idempotency store for the email sender.

The store records the response produced for each idempotency key as JSON
lines so that a repeated request can replay the original response instead of
sending a second email. It is file-backed and single-process; the tool layer
is expected to give each tenant its own store file so keys cannot collide
across SMEs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class DuplicateKeyError(Exception):
    """A record with this idempotency key is already stored."""


def derive_key(invoice_ref: str, template_id: str, step: int | None = None) -> str:
    """Derive the default idempotency key for a send request.

    The inputs are canonicalised to JSON before hashing so that different
    (invoice_ref, template_id, step) combinations cannot collide the way
    plain string concatenation can.
    """
    payload = json.dumps(
        {"invoice_ref": invoice_ref, "step": step, "template_id": template_id},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """Key-to-response log backed by one append-only JSONL file."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> dict[str, Any] | None:
        """Return the stored response for ``key``, or None if it is unknown."""
        for record in self._records():
            if record["key"] == key:
                return record["response"]
        return None

    def put(self, key: str, response: dict[str, Any]) -> None:
        """Append a record for ``key``; refuses to overwrite an existing key."""
        if self.get(key) is not None:
            raise DuplicateKeyError(f"idempotency key already stored: {key}")
        record = json.dumps({"key": key, "response": response}, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record + "\n")

    def _records(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Skip partial lines left behind by an interrupted append.
                    continue
                if isinstance(record, dict) and "key" in record and "response" in record:
                    records.append(record)
        return records

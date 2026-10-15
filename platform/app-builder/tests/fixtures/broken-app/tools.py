"""The broken app's tools — side_effect flags that disagree with the audit manifest.

Two of these five are wrong on purpose, in opposite directions, because the two
directions are not equally bad and the validator must say which is which:

  misflagged_write  tools.py True,  manifest false — the gate treats a real side
                    effect as a read and runs it with no proposal, no hold and no
                    owner. This is a control bypass.
  misflagged_read   tools.py False, manifest true  — the app's read path is gated,
                    so it is slow and annoying but safe.

Every tool is built with an explicit `output_schema` because `ToolDefinition`
requires a non-empty one; a generated app that omitted it would not import.
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_toolkit.contracts import ToolDefinition

READ_THING = "read_thing"
WRITE_THING = "write_thing"
MISFLAGGED_READ = "misflagged_read"
MISFLAGGED_WRITE = "misflagged_write"
SEND_NOTICE = "send_notice"

_OBJECT: dict[str, Any] = {"type": "object", "additionalProperties": False}
_RESULT: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _todo(name: str) -> Any:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            f"{name} is a fixture stub and performs no action; the broken-app "
            "fixture exists to be validated, not to be run")
    return handler


def make_read_thing() -> ToolDefinition:
    return ToolDefinition(
        name=READ_THING,
        description="Read one thing. Read-only.",
        input_schema={**_OBJECT, "properties": {"invoice_id": {"type": "string"}}},
        output_schema=_RESULT,
        side_effect=False,
        handler=_todo(READ_THING),
    )


def make_write_thing() -> ToolDefinition:
    return ToolDefinition(
        name=WRITE_THING,
        description="Write to the ledger. Correctly flagged side-effecting.",
        input_schema={**_OBJECT, "properties": {
            "invoice_id": {"type": "string"},
            "amount_cents": {"type": "integer"}}},
        output_schema=_RESULT,
        side_effect=True,
        handler=_todo(WRITE_THING),
    )


def make_misflagged_read() -> ToolDefinition:
    """Read-only here, side_effect: true in the manifest. The safe mismatch."""
    return ToolDefinition(
        name=MISFLAGGED_READ,
        description="A read that the audit manifest wrongly calls side-effecting.",
        input_schema={**_OBJECT, "properties": {"invoice_id": {"type": "string"}}},
        output_schema=_RESULT,
        side_effect=False,
        handler=_todo(MISFLAGGED_READ),
    )


def make_misflagged_write() -> ToolDefinition:
    """Side-effecting here, side_effect: false in the manifest. The unsafe one."""
    return ToolDefinition(
        name=MISFLAGGED_WRITE,
        description="A write that the audit manifest wrongly calls read-only.",
        input_schema={**_OBJECT, "properties": {"invoice_id": {"type": "string"}}},
        output_schema=_RESULT,
        side_effect=True,
        handler=_todo(MISFLAGGED_WRITE),
    )


def make_send_notice() -> ToolDefinition:
    """Correctly flagged, but takes no subject key, so no fact can resolve."""
    return ToolDefinition(
        name=SEND_NOTICE,
        description="Send a notice. Takes a recipient and a length, and no subject id.",
        input_schema={**_OBJECT, "properties": {
            "to": {"type": "string"},
            "body_length": {"type": "integer"}}},
        output_schema=_RESULT,
        side_effect=True,
        handler=_todo(SEND_NOTICE),
    )


def build_tools() -> list[ToolDefinition]:
    return [
        make_read_thing(),
        make_write_thing(),
        make_misflagged_read(),
        make_misflagged_write(),
        make_send_notice(),
    ]


__all__ = [
    "MISFLAGGED_READ", "MISFLAGGED_WRITE", "READ_THING", "SEND_NOTICE",
    "WRITE_THING", "build_tools",
]

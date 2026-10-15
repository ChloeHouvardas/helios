"""Helios Spreadsheet Tools — the Layer 1 financial tool for live sheets.

Apps call these tools through the Helios SDK as `helios.sheets.<tool>()`, or
over HTTP at `POST /v1/sheets/<tool>` (see `app.py`).

    render_sheet   build a live sheet from a KG query
    import_sheet   map an existing Excel/CSV into KG records
    reconcile      match payments to the invoices and bills they settle
    watch          notify an app's agent when a record or cell changes
    edit proposals turn protected-column edits into approvals

One rule governs every tool: **sheets are views, never the source of truth.**
The KG holds the records; a sheet only displays them. An edit to an important
cell becomes a proposal, and once Intent Verification approves it, the tool that
owns the record writes it back.

`KnowledgeGraph` is a development stub for the centrally hosted Shared State &
Knowledge Graph service, kept behind one class so it can be swapped without
callers changing. See `store.py`.
"""

from .engine import LocalSheetEngine, SheetEngine
from .errors import HeliosError
from .filestore import FileStore
from .jobs import JobRunner
from .models import (
    Counterparty,
    EditProposal,
    Match,
    Obligation,
    SheetView,
    Transaction,
)
from .qwen import NullQwen, QwenClient
from .store import KnowledgeGraph, MultiTenantGraph
from .tools import AuditLog, Sheets, SheetsService


def __getattr__(name: str):
    """Resolve `create_app` lazily.

    `app.py` imports FastAPI, which the tool core does not need. Importing it
    eagerly made the whole package unimportable on a machine without FastAPI,
    even for callers that only want `Sheets`. Deferring keeps the offline and
    test paths dependency-free; the HTTP app is still available by the same
    name, and a missing FastAPI now surfaces only when an app is actually built.
    """
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "create_app",
    "Sheets",
    "SheetsService",
    "AuditLog",
    "KnowledgeGraph",
    "MultiTenantGraph",
    "FileStore",
    "LocalSheetEngine",
    "SheetEngine",
    "QwenClient",
    "NullQwen",
    "JobRunner",
    "HeliosError",
    "Transaction",
    "Obligation",
    "Match",
    "Counterparty",
    "SheetView",
    "EditProposal",
]

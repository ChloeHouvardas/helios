"""SQLite persistence for the tool service's world state.

The deployment engine's core container runs this service across restarts, so the
in-memory `World` (ledger, balances, outbox, graph, edges) must survive a
restart or a redeploy would silently forget every payment and every verified
fact. This module snapshots that state to SQLite under `HELIOS_DATA_DIR` and
restores it on startup.

Scope, stated honestly: it persists the `World` (the fin_tools ledger, balances,
outbox and knowledge graph) and the receivables store's serialisable tables. The
imported `payments_collect.PaymentStore` keeps richer in-memory state (attempt
machines, provider events, idempotency) that is not round-tripped here yet; that
gap is recorded in HANDOFF-TO-P2-P3.md rather than papered over with a partial
restore that could resurrect a half-applied payment.

One table, one row per named snapshot, JSON payload. Not a normalized schema:
this is a durability checkpoint for a prototype world, not a queryable database,
and a single blob per key keeps save/restore atomic and trivially correct.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from .contracts import World

#: Environment variable naming the directory the SQLite file lives in.
DATA_DIR_ENV = "HELIOS_DATA_DIR"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS world_state (
    name    TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    saved_at REAL NOT NULL
)
"""


class SqlitePersistence:
    """Snapshots a bundle's `World` to a SQLite file and restores it."""

    def __init__(self, path: Optional[str] = None, *, data_dir: Optional[str] = None,
                 clock: Any = None) -> None:
        import time

        self._clock = clock or time.time
        base = path
        if base is None:
            resolved_dir = data_dir if data_dir is not None else os.environ.get(DATA_DIR_ENV)
            base = str(Path(resolved_dir) / "world_state.sqlite3") if resolved_dir else None
        self._path = base
        self._conn: Optional[sqlite3.Connection] = None
        if self._path is not None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    @property
    def path(self) -> Optional[str]:
        """The SQLite file path, or None when persistence is disabled."""
        return self._path

    # -- save ---------------------------------------------------------------

    def save(self, bundle: Any) -> None:
        if self._conn is None:
            return
        world: World = bundle.world
        snapshot: Dict[str, Any] = {
            "ledger": world.ledger,
            "outbox": world.outbox,
            "invoices": world.invoices,
            "graph": world.graph,
            "edges": world.edges,
            "balances": world.balances,
        }
        # The knowledge graph object mirrors world.graph but is the live store the
        # graph tools read; persist its nodes/edges too so a restart keeps them.
        graph = getattr(bundle, "graph", None)
        if graph is not None:
            snapshot["kg_nodes"] = getattr(graph, "nodes", {})
            snapshot["kg_edges"] = getattr(graph, "edges", [])

        self._conn.execute(
            "INSERT OR REPLACE INTO world_state(name, payload, saved_at) VALUES (?,?,?)",
            ("world", json.dumps(snapshot, default=str), self._clock()))
        self._conn.commit()

    # -- restore ------------------------------------------------------------

    def restore(self, bundle: Any) -> bool:
        """Load the last snapshot into `bundle`. True if a snapshot was found."""
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT payload FROM world_state WHERE name = 'world'").fetchone()
        if row is None:
            return False
        try:
            snapshot = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return False

        world: World = bundle.world
        world.ledger.update(snapshot.get("ledger", {}))
        world.outbox.extend(snapshot.get("outbox", []))
        world.invoices.extend(snapshot.get("invoices", []))
        world.graph.update(snapshot.get("graph", {}))
        world.edges.extend(snapshot.get("edges", []))
        for account, cents in snapshot.get("balances", {}).items():
            world.balances[account] = int(cents)

        graph = getattr(bundle, "graph", None)
        if graph is not None:
            graph.nodes.update(snapshot.get("kg_nodes", {}))
            graph.edges.extend(snapshot.get("kg_edges", []))
        return True

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

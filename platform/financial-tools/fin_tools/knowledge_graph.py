"""Knowledge graph stub (specs/interfaces.md §4).

Two node provenances, enforced:
  - `verified`   : came from a system of record (bank feed, ERP, confirmed payment)
  - `observation`: an agent's claim ("customer said they'd pay Friday")

Rules:
  - an observation can NEVER overwrite a verified fact (write is rejected)
  - verified writes carry a source reference
  - edges link nodes (invoice -> customer, payment -> invoice, ...)
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .contracts import REGISTRY


class GraphWriteError(Exception):
    pass


@dataclass
class KnowledgeGraph:
    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    edges: List[Dict[str, Any]] = field(default_factory=list)

    def upsert_verified(self, kind: str, key: str, attrs: Dict[str, Any],
                        *, source: str) -> Dict[str, Any]:
        node_id = f"{kind}:{key}"
        node = {"node_id": node_id, "kind": kind, "key": key, "attrs": dict(attrs),
                "provenance": "verified", "source": source}
        self.nodes[node_id] = node
        return node

    def add_observation(self, kind: str, key: str, attrs: Dict[str, Any],
                        *, observer: str) -> Dict[str, Any]:
        node_id = f"{kind}:{key}"
        existing = self.nodes.get(node_id)
        if existing is not None and existing["provenance"] == "verified":
            raise GraphWriteError(
                f"observation cannot overwrite verified node {node_id}")
        node = {"node_id": node_id, "kind": kind, "key": key, "attrs": dict(attrs),
                "provenance": "observation", "observer": observer}
        self.nodes[node_id] = node
        return node

    def link(self, from_id: str, rel: str, to_id: str) -> Dict[str, Any]:
        for nid in (from_id, to_id):
            if nid not in self.nodes:
                raise GraphWriteError(f"unknown node: {nid}")
        edge = {"edge_id": f"e_{secrets.token_hex(4)}",
                "from": from_id, "rel": rel, "to": to_id}
        self.edges.append(edge)
        return edge

    def get(self, node_id: str) -> Optional[Dict[str, Any]]:
        return self.nodes.get(node_id)

    def neighbours(self, node_id: str, rel: Optional[str] = None) -> List[Dict[str, Any]]:
        out = []
        for e in self.edges:
            if e["from"] == node_id and (rel is None or e["rel"] == rel):
                out.append(self.nodes[e["to"]])
        return out

    def observations_for(self, node_id: str) -> List[Dict[str, Any]]:
        return [n for n in self.nodes.values()
                if n["provenance"] == "observation" and n["node_id"] == node_id]


def register(registry=REGISTRY, graph: Optional[KnowledgeGraph] = None) -> None:
    holder = {"g": graph}

    def _g() -> KnowledgeGraph:
        if holder["g"] is None:
            holder["g"] = KnowledgeGraph()
        return holder["g"]

    @registry.tool(
        "graph.publish_verified", "Publish a verified business fact. "
        "Side-effecting (shared state).",
        {"kind": "str", "key": "str", "attrs": "dict", "source": "str"},
        {"node_id": "str"},
        side_effects=True, tags=("graph",),
    )
    def _pub(kind: str, key: str, attrs: Dict[str, Any], source: str) -> Dict[str, str]:
        return {"node_id": _g().upsert_verified(kind, key, attrs, source=source)["node_id"]}

    @registry.tool(
        "graph.observe", "Record an agent observation. Rejected if it would "
        "overwrite a verified fact. Side-effecting.",
        {"kind": "str", "key": "str", "attrs": "dict", "observer": "str"},
        {"node_id": "str"},
        side_effects=True, tags=("graph",),
    )
    def _obs(kind: str, key: str, attrs: Dict[str, Any], observer: str) -> Dict[str, str]:
        return {"node_id": _g().add_observation(kind, key, attrs, observer=observer)["node_id"]}

    @registry.tool(
        "graph.read", "Read a node by id. Read-only.",
        {"node_id": "str"}, {"node": "dict|None"},
        side_effects=False, tags=("graph",),
    )
    def _read(node_id: str) -> Dict[str, Any]:
        return {"node": _g().get(node_id)}

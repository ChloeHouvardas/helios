import pytest

from fin_tools import build_world
from fin_tools.knowledge_graph import GraphWriteError


def test_verified_then_observation_blocked():
    bundle = build_world()
    node = bundle.graph.upsert_verified("invoice", "INV-1",
                                        {"amount_cents": 100}, source="erp")
    with pytest.raises(GraphWriteError):
        bundle.graph.add_observation("invoice", "INV-1", {"amount_cents": 1},
                                     observer="agent")
    assert bundle.graph.get(node["node_id"])["attrs"]["amount_cents"] == 100


def test_observation_can_be_overwritten_by_observation():
    bundle = build_world()
    bundle.graph.add_observation("invoice", "INV-1", {"note": "a"}, observer="x")
    node = bundle.graph.add_observation("invoice", "INV-1", {"note": "b"},
                                        observer="y")
    assert node["attrs"]["note"] == "b"


def test_verified_overwrite_allowed():
    bundle = build_world()
    bundle.graph.upsert_verified("invoice", "INV-1", {"amount_cents": 1},
                                 source="erp")
    node = bundle.graph.upsert_verified("invoice", "INV-1", {"amount_cents": 2},
                                        source="bank-feed")
    assert node["attrs"]["amount_cents"] == 2


def test_link_requires_existing_nodes():
    bundle = build_world()
    a = bundle.graph.upsert_verified("invoice", "I", {}, source="erp")
    with pytest.raises(GraphWriteError):
        bundle.graph.link(a["node_id"], "settles", "payment:nope")


def test_neighbours():
    bundle = build_world()
    inv = bundle.graph.upsert_verified("invoice", "I", {}, source="erp")
    pay = bundle.graph.upsert_verified("payment", "P", {}, source="ledger")
    bundle.graph.link(pay["node_id"], "settles", inv["node_id"])
    assert [n["node_id"] for n in bundle.graph.neighbours(pay["node_id"])] == \
        [inv["node_id"]]

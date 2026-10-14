import pytest

from fin_tools import build_world


def test_catalog_shape_matches_tool_contract():
    bundle = build_world()
    for decl in bundle.registry.catalog():
        assert {"name", "description", "input_schema", "output_schema",
                "side_effects"} <= set(decl)


def test_expected_tools_registered():
    bundle = build_world()
    names = {t.name for t in bundle.registry.list()}
    assert {"payments.send", "payments.receive", "payments.balance",
            "email.send", "email.outbox", "spreadsheet.load",
            "spreadsheet.save", "spreadsheet.aggregate",
            "graph.publish_verified", "graph.observe", "graph.read",
            "simulation.forecast"} <= names


def test_side_effect_flags():
    bundle = build_world()
    assert bundle.registry.get("payments.send").side_effects is True
    assert bundle.registry.get("payments.balance").side_effects is False
    assert bundle.registry.get("email.send").side_effects is True
    assert bundle.registry.get("spreadsheet.load").side_effects is False


def test_duplicate_registration_rejected():
    bundle = build_world()
    with pytest.raises(ValueError):
        bundle.registry.register(bundle.registry.get("payments.send"))


def test_registry_dispatch_read_only():
    bundle = build_world()
    bundle.payments.receive_payment(payer="c", payee="us", amount_cents=42,
                                    currency="CNY")
    out = bundle.registry.call("payments.balance", account="us")
    assert out == {"balance_cents": 42}


def test_unknown_tool():
    bundle = build_world()
    with pytest.raises(KeyError):
        bundle.registry.get("nope")

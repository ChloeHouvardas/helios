import pytest

from fin_tools import build_world


def authed(bundle, amount=5000, payee="supplier"):
    intent = bundle.intents.capture_intent("chloe", amount, "CNY", payee, "p")
    return bundle.intents.confirm(intent.intent_id, by="chloe").code


def test_send_without_intent_is_rejected():
    bundle = build_world()
    rec = bundle.payments.send_payment(payer="ops", payee="supplier",
                                       amount_cents=5000, currency="CNY")
    assert rec["state"] == "rejected"
    assert bundle.payments.balance("supplier") == 0


def test_send_with_valid_intent_settles_and_moves_money():
    bundle = build_world()
    rec = bundle.payments.send_payment(payer="ops", payee="supplier",
                                       amount_cents=5000, currency="CNY",
                                       auth_code=authed(bundle))
    assert rec["state"] == "settled"
    assert bundle.payments.balance("supplier") == 5000
    assert bundle.payments.balance("ops") == -5000


def test_tampered_amount_rejected():
    bundle = build_world()
    code = authed(bundle, amount=5000)
    rec = bundle.payments.send_payment(payer="ops", payee="supplier",
                                       amount_cents=5001, currency="CNY",
                                       auth_code=code)
    assert rec["state"] == "rejected"
    assert bundle.payments.balance("supplier") == 0


def test_idempotency_key_prevents_double_pay():
    bundle = build_world()
    code = authed(bundle)
    first = bundle.payments.send_payment(payer="ops", payee="supplier",
                                         amount_cents=5000, currency="CNY",
                                         auth_code=code, idempotency_key="k1")
    second = bundle.payments.send_payment(payer="ops", payee="supplier",
                                          amount_cents=5000, currency="CNY",
                                          auth_code="stale", idempotency_key="k1")
    assert first["payment_id"] == second["payment_id"]
    assert bundle.payments.balance("supplier") == 5000


def test_receive_needs_no_intent():
    bundle = build_world()
    rec = bundle.payments.receive_payment(payer="customer", payee="us",
                                          amount_cents=9000, currency="CNY",
                                          reference="INV-1")
    assert rec["state"] == "received"
    assert bundle.payments.balance("us") == 9000


def test_negative_amount_rejected():
    bundle = build_world()
    with pytest.raises(Exception):
        bundle.payments.send_payment(payer="ops", payee="s", amount_cents=-1,
                                     currency="CNY")


def test_trail_records_lifecycle():
    bundle = build_world()
    bundle.payments.send_payment(payer="ops", payee="s", amount_cents=1,
                                 currency="CNY")
    events = [e["event"] for e in bundle.payments.trail]
    assert events == ["proposed", "rejected"]

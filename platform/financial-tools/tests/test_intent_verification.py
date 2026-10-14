import pytest

from fin_tools import build_world
from fin_tools.intent_verification import VerificationError


def make(bundle=None):
    bundle = bundle or build_world()
    intent = bundle.intents.capture_intent(
        principal="chloe", amount_cents=10000, currency="CNY",
        payee="supplier", purpose="test")
    return bundle, intent


def test_capture_requires_positive_amount():
    bundle = build_world()
    with pytest.raises(VerificationError):
        bundle.intents.capture_intent("chloe", 0, "CNY", "x", "y")


def test_confirm_requires_principal():
    bundle, intent = make()
    with pytest.raises(VerificationError):
        bundle.intents.confirm(intent.intent_id, by="attacker")


def test_verify_ok_with_exact_fields():
    bundle, intent = make()
    code = bundle.intents.confirm(intent.intent_id, by="chloe").code
    got = bundle.intents.verify(code, amount_cents=10000, currency="CNY",
                                payee="supplier")
    assert got.intent_id == intent.intent_id


@pytest.mark.parametrize("mut", [
    dict(amount_cents=10001),
    dict(currency="USD"),
    dict(payee="supplier-evil"),
])
def test_dynamic_linking_rejects_any_field_change(mut):
    """PSD2 dynamic linking: any change to amount/payee/currency invalidates."""
    bundle, intent = make()
    code = bundle.intents.confirm(intent.intent_id, by="chloe").code
    kwargs = dict(amount_cents=10000, currency="CNY", payee="supplier")
    kwargs.update(mut)
    with pytest.raises(VerificationError):
        bundle.intents.verify(code, **kwargs)


def test_single_use():
    bundle, intent = make()
    code = bundle.intents.confirm(intent.intent_id, by="chloe").code
    bundle.intents.verify(code, amount_cents=10000, currency="CNY", payee="supplier")
    bundle.intents.consume(code)
    with pytest.raises(VerificationError):
        bundle.intents.verify(code, amount_cents=10000, currency="CNY",
                              payee="supplier")


def test_expiry():
    bundle, intent = make()
    now = [1000.0]
    bundle.intents.clock = lambda: now[0]
    code = bundle.intents.confirm(intent.intent_id, by="chloe").code
    now[0] += 301  # ttl 300
    with pytest.raises(VerificationError):
        bundle.intents.verify(code, amount_cents=10000, currency="CNY",
                              payee="supplier", now=now[0])


def test_unknown_code_and_intent():
    bundle, _ = make()
    with pytest.raises(VerificationError):
        bundle.intents.verify("deadbeef", amount_cents=1, currency="CNY", payee="x")
    with pytest.raises(VerificationError):
        bundle.intents.confirm("int_nope", by="chloe")


def test_revocation():
    bundle, intent = make()
    bundle.intents.revoke(intent.intent_id)
    with pytest.raises(VerificationError):
        bundle.intents.confirm(intent.intent_id, by="chloe")


def test_assessor_is_advisory_only():
    bundle, intent = make()

    def bad_assessor(i):
        raise RuntimeError("llm down")

    bundle.intents.intent_assessor = bad_assessor
    text = bundle.intents.summarize(intent)
    assert "chloe" in text and "supplier" in text

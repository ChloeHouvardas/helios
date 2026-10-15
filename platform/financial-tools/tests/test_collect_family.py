"""Task 3: the receivables family (`collect.*`) over the imported PaymentStore.

What these tests pin down:

- **Tenancy is never defaulted.** The store's models default `business_id` to
  `"dev"`; a collect tool must take the tenant from the bundle and refuse
  without one. Two tenants cannot see or refund each other's receivables.
- **The refund gate.** `PaymentStore.record_refund` takes no authorization, so
  `collect.refund` verifies and consumes an intent auth code bound to
  (amount, currency, customer) first. A missing, wrong-amount or replayed code
  is refused.
- **One ledger.** A captured payment mirrors into the fin_tools ledger exactly
  once — a replayed provider event, or a second event id reporting the same
  capture, must not double-post.
- **Idempotency.** A repeated `create_payment_link` with the same key returns the
  first link.
- **Webhook ops are not tools.** `record_payment_success` / provider events are
  reachable only through the `Collect.apply_provider_event` facade, never the
  registry, so an agent cannot mark its own payment received.

Money is integer minor units throughout. Offline: no network, no real provider.
"""
from __future__ import annotations

import pytest

from fin_tools import build_world
from fin_tools.collect import CASHIER_URL, MERCHANT_ACCOUNT, Collect
from fin_tools.intent_verification import VerificationError

TENANT = "acme"


@pytest.fixture
def bundle(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    return build_world(tenant=TENANT)


def _seed_captured(bundle, *, customer="c1", amount=100000, provider="alipay",
                   event_id="evt_1", status="succeeded"):
    """Create a receivable -> link -> attempt, then capture it via the webhook.

    Returns (receivable_id, link_id, attempt). Mirrors how a real app drives the
    store: the attempt exists before the provider calls back.
    """
    rec = bundle.collect.add_receivable(customer_id=customer, amount_cents=amount,
                                        invoice_no="INV-1")
    link = bundle.collect.create_payment_link(receivable_id=rec["id"])
    store = bundle.collect.store
    att = store.create_attempt(business_id=TENANT, link_id=link["id"],
                               provider=provider)
    bundle.collect.apply_provider_event(
        provider=provider, event_id=event_id,
        payload={"out_trade_no": att.out_trade_no, "status": status,
                 "provider_order_ref": "PROV-1"})
    return rec["id"], link["id"], att


# --------------------------------------------------------------------------
# registration + tenancy
# --------------------------------------------------------------------------

def test_collect_tools_are_registered_with_the_right_flags(bundle):
    reg = bundle.registry
    assert reg.get("collect.add_receivable").side_effects is True
    assert reg.get("collect.create_payment_link").side_effects is True
    assert reg.get("collect.refund").side_effects is True
    assert reg.get("collect.list_receivables").side_effects is False
    assert reg.get("collect.status").side_effects is False
    for name in ("collect.add_receivable", "collect.list_receivables",
                 "collect.status", "collect.create_payment_link",
                 "collect.refund"):
        assert reg.get(name).tenant_scoped is True


def test_webhook_operations_are_not_tools(bundle):
    """The point of the whole design: an agent cannot confirm its own payment."""
    names = {t.name for t in bundle.registry.list()}
    assert not any("provider" in n or "apply_provider" in n for n in names)
    assert "collect.record_payment_success" not in names
    assert "collect.apply_provider_event" not in names
    # but the facade method exists for an app's webhook handler
    assert callable(bundle.collect.apply_provider_event)


def test_add_receivable_needs_a_tenant(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    bundle = build_world()                      # tenant is None
    with pytest.raises(ValueError, match="tenant"):
        bundle.registry.call("collect.add_receivable", customer_id="c1",
                             amount_cents=1000)


def test_tenant_never_defaults_to_dev(bundle):
    """The store would accept business_id='dev'; we must not send that."""
    rec = bundle.collect.add_receivable(customer_id="c1", amount_cents=5000)
    store_rec = bundle.collect.store.get_receivable(rec["id"], TENANT)
    assert store_rec.business_id == TENANT
    assert store_rec.business_id != "dev"


def test_tenants_are_isolated(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    acme = build_world(tenant="acme")
    beta = build_world(tenant="beta")
    rec = acme.collect.add_receivable(customer_id="c1", amount_cents=5000)

    # beta cannot see it, and cannot read it by id (not_found, not leaked)
    assert beta.collect.list_receivables()["count"] == 0
    from payments_collect.errors import HeliosError
    with pytest.raises(ValueError) as exc:
        beta.collect.status(receivable_id=rec["id"])
    assert "not_found" in str(exc.value)


# --------------------------------------------------------------------------
# receivables
# --------------------------------------------------------------------------

def test_add_and_list_and_status(bundle):
    rec = bundle.collect.add_receivable(customer_id="c1", amount_cents=100000,
                                        invoice_no="INV-1", due_date="2026-11-01")
    assert rec["status"] == "open"
    assert rec["amount"] == 100000

    listed = bundle.collect.list_receivables()
    assert listed["count"] == 1
    assert listed["receivables"][0]["id"] == rec["id"]

    status = bundle.collect.status(receivable_id=rec["id"])
    assert status["outstanding_cents"] == 100000
    assert status["due_date"].startswith("2026-11-01")


def test_list_filters_by_status(bundle):
    bundle.collect.add_receivable(customer_id="c1", amount_cents=100)
    assert bundle.collect.list_receivables(status="open")["count"] == 1
    assert bundle.collect.list_receivables(status="paid")["count"] == 0


def test_add_receivable_rejects_a_nonpositive_amount(bundle):
    with pytest.raises(ValueError, match="amount_cents"):
        bundle.collect.add_receivable(customer_id="c1", amount_cents=0)


def test_bad_due_date_is_actionable(bundle):
    with pytest.raises(ValueError, match="due_date"):
        bundle.collect.add_receivable(customer_id="c1", amount_cents=100,
                                      due_date="not-a-date")


# --------------------------------------------------------------------------
# payment links
# --------------------------------------------------------------------------

def test_create_link_returns_a_simulated_url_and_token(bundle):
    rec = bundle.collect.add_receivable(customer_id="c1", amount_cents=100000)
    link = bundle.collect.create_payment_link(receivable_id=rec["id"])
    assert link["url"] == CASHIER_URL.format(link_id=link["id"])
    assert link["url"].startswith("https://pay.helios.test/l/")
    assert link["token"]
    assert link["amount"] == 100000          # defaults to outstanding


def test_link_amount_is_server_side_and_capped(bundle):
    rec = bundle.collect.add_receivable(customer_id="c1", amount_cents=100000)
    # cannot exceed the outstanding balance
    with pytest.raises(ValueError) as exc:
        bundle.collect.create_payment_link(receivable_id=rec["id"],
                                           amount_cents=200000)
    assert "validation_error" in str(exc.value)


def test_create_link_is_idempotent_on_the_key(bundle):
    rec = bundle.collect.add_receivable(customer_id="c1", amount_cents=100000)
    first = bundle.collect.create_payment_link(receivable_id=rec["id"],
                                               idempotency_key="k1")
    second = bundle.collect.create_payment_link(receivable_id=rec["id"],
                                                idempotency_key="k1")
    assert first["id"] == second["id"]
    assert first["token"] == second["token"]
    # a different key mints a different link
    third = bundle.collect.create_payment_link(receivable_id=rec["id"],
                                               idempotency_key="k2")
    assert third["id"] != first["id"]


def test_no_link_for_a_paid_receivable(bundle):
    rec_id, _, _ = _seed_captured(bundle, amount=100000)
    with pytest.raises(ValueError) as exc:
        bundle.collect.create_payment_link(receivable_id=rec_id)
    assert "validation_error" in str(exc.value)


# --------------------------------------------------------------------------
# webhook capture -> one ledger
# --------------------------------------------------------------------------

def test_capture_mirrors_into_the_fin_tools_ledger_once(bundle):
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    assert bundle.payments.balance(MERCHANT_ACCOUNT) == 100000
    # the receivable itself is marked paid by the store
    assert bundle.collect.status(receivable_id=rec_id)["status"] == "paid"


def test_replayed_provider_event_does_not_double_post(bundle):
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    # Identical payload to the one _seed_captured stored: a true retry.
    payload = {"out_trade_no": att.out_trade_no, "status": "succeeded",
               "provider_order_ref": "PROV-1"}
    again = bundle.collect.apply_provider_event(provider="alipay",
                                                event_id="evt_1", payload=payload)
    assert again["created"] is False
    assert again["processed"] is False
    assert bundle.payments.balance(MERCHANT_ACCOUNT) == 100000


def test_second_event_id_for_one_capture_does_not_double_post(bundle):
    """A provider may send a retry under a NEW event id; money is mirrored once."""
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    payload = {"out_trade_no": att.out_trade_no, "status": "succeeded",
               "provider_order_ref": "PROV-1"}
    bundle.collect.apply_provider_event(provider="alipay", event_id="evt_retry",
                                        payload=payload)
    assert bundle.payments.balance(MERCHANT_ACCOUNT) == 100000


def test_conflicting_payload_for_one_event_id_is_rejected(bundle):
    """Append-only: the stored payload is never edited, so a conflict errors."""
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    with pytest.raises(ValueError) as exc:
        bundle.collect.apply_provider_event(
            provider="alipay", event_id="evt_1",
            payload={"out_trade_no": att.out_trade_no, "status": "FAILED-DIFFERENT"})
    assert "event_conflict" in str(exc.value)


def test_webhook_for_an_unknown_order_ref_is_rejected(bundle):
    with pytest.raises(ValueError, match="out_trade_no"):
        bundle.collect.apply_provider_event(
            provider="alipay", event_id="evt_x",
            payload={"out_trade_no": "HL-does-not-exist", "status": "succeeded"})


def test_webhook_needs_a_tenant(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    bundle = build_world()
    with pytest.raises(ValueError, match="tenant"):
        bundle.collect.apply_provider_event(provider="alipay", event_id="e",
                                            payload={"out_trade_no": "x",
                                                     "status": "succeeded"})


# --------------------------------------------------------------------------
# refunds — the intent gate
# --------------------------------------------------------------------------

def _auth_for(bundle, amount, payee, currency="CNY", principal="owner"):
    intent = bundle.intents.capture_intent(principal, amount, currency, payee,
                                           "refund")
    return bundle.intents.confirm(intent.intent_id, by=principal)


def test_refund_without_auth_code_is_refused(bundle):
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    with pytest.raises(VerificationError, match="auth_code"):
        bundle.collect.refund(attempt_id=att.id, amount_cents=40000)


def test_refund_with_a_valid_code_succeeds_and_mirrors_out(bundle):
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    code = _auth_for(bundle, 40000, "c1")
    result = bundle.collect.refund(attempt_id=att.id, amount_cents=40000,
                                   auth_code=code.code, reason="damaged")
    assert result["state"] == "partially_refunded"
    assert result["refunded_amount"] == 40000
    assert result["intent_id"]
    # merchant ledger reflects the money going back out: 100000 - 40000
    assert bundle.payments.balance(MERCHANT_ACCOUNT) == 60000


def test_refund_auth_code_is_bound_to_the_amount(bundle):
    """PSD2 dynamic linking: a code for 40000 cannot authorize 50000."""
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    code = _auth_for(bundle, 40000, "c1")
    with pytest.raises(VerificationError):
        bundle.collect.refund(attempt_id=att.id, amount_cents=50000,
                              auth_code=code.code)


def test_refund_auth_code_is_bound_to_the_customer(bundle):
    rec_id, link_id, att = _seed_captured(bundle, amount=100000, customer="c1")
    code = _auth_for(bundle, 40000, "someone-else")
    with pytest.raises(VerificationError):
        bundle.collect.refund(attempt_id=att.id, amount_cents=40000,
                              auth_code=code.code)


def test_refund_auth_code_is_single_use(bundle):
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    code = _auth_for(bundle, 40000, "c1")
    bundle.collect.refund(attempt_id=att.id, amount_cents=40000, auth_code=code.code)
    # reuse the same code for another refund -> refused (single-use)
    with pytest.raises(VerificationError):
        bundle.collect.refund(attempt_id=att.id, amount_cents=10000,
                              auth_code=code.code)


def test_refund_cannot_exceed_the_captured_amount(bundle):
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    code = _auth_for(bundle, 200000, "c1")
    with pytest.raises(ValueError) as exc:
        bundle.collect.refund(attempt_id=att.id, amount_cents=200000,
                              auth_code=code.code)
    assert "refund_exceeds_payment" in str(exc.value)


def test_refund_needs_a_tenant(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    bundle = build_world()
    with pytest.raises(ValueError, match="tenant"):
        bundle.collect.refund(attempt_id="pay_x", amount_cents=100,
                              auth_code="whatever")


# --------------------------------------------------------------------------
# the facade rejects a refund the store would have allowed
# --------------------------------------------------------------------------

def test_store_refund_alone_has_no_gate_but_the_tool_does(bundle):
    """Demonstrates WHY collect.refund adds the gate: the store has none.

    Calling the store directly refunds with no authorization. The tool is what
    enforces the human-in-the-loop; this asserts the gap the adapter closes.
    """
    rec_id, link_id, att = _seed_captured(bundle, amount=100000)
    # the raw store would happily refund with no code:
    bundle.collect.store.record_refund(att.id, amount=10000, business_id=TENANT)
    # but the tool refuses without one:
    with pytest.raises(VerificationError):
        bundle.collect.refund(attempt_id=att.id, amount_cents=10000)

"""Store-level rules: tenancy, idempotency, append-only provider events,
the guarded state machine, the dual-entry ledger, refunds and settlement."""

from datetime import datetime, timedelta

import pytest

from payments_collect.errors import HeliosError
from payments_collect.models import SHANGHAI
from payments_collect.store import PaymentStore

BIZ = "acme"


@pytest.fixture
def store() -> PaymentStore:
    return PaymentStore()


def _seed(store: PaymentStore, business_id: str = BIZ,
          amount: int = 100_00):
    """A fresh receivable, link and attempt, mid-flow."""
    rec = store.add_receivable(business_id=business_id, customer_id="cus_1",
                               amount=amount, invoice_no="INV-001")
    link = store.create_link(business_id=business_id, receivable_id=rec.id)
    att = store.create_attempt(business_id=business_id, link_id=link.id,
                               provider="mock")
    return rec, link, att


def _authenticate(store: PaymentStore, att, business_id: str = BIZ):
    """Walk the attempt to the cashier."""
    store.transition_attempt(att.id, "intent_created", business_id=business_id)
    return store.transition_attempt(att.id, "customer_authenticating",
                                    business_id=business_id)


class FakeClock:
    """Fixed clock that tests can advance."""

    def __init__(self, start: datetime):
        self.t = start

    def tick(self, **kwargs) -> datetime:
        self.t = self.t + timedelta(**kwargs)
        return self.t

    def __call__(self) -> datetime:
        return self.t


# --------------------------------------------------------------- receivables


def test_receivable_partial_then_paid(store):
    rec = store.add_receivable(business_id=BIZ, customer_id="cus_1",
                               amount=100_00, invoice_no="INV-1")
    assert rec.status == "open" and rec.currency == "CNY"
    rec = store.apply_payment(rec.id, 40_00, business_id=BIZ)
    assert rec.status == "partial" and rec.amount_paid == 40_00
    rec = store.apply_payment(rec.id, 60_00, business_id=BIZ)
    assert rec.status == "paid" and rec.amount_paid == 100_00


def test_void_receivable_blocks_new_links_and_payments(store):
    rec = store.add_receivable(business_id=BIZ, customer_id="cus_1",
                               amount=100_00)
    store.set_receivable_status(rec.id, "void", business_id=BIZ)
    with pytest.raises(HeliosError) as err:
        store.create_link(business_id=BIZ, receivable_id=rec.id)
    assert err.value.code == "validation_error"
    with pytest.raises(HeliosError) as err:
        store.apply_payment(rec.id, 10, business_id=BIZ)
    assert err.value.code == "validation_error"


def test_paid_receivable_blocks_new_links(store):
    rec = store.add_receivable(business_id=BIZ, customer_id="cus_1",
                               amount=100_00)
    store.apply_payment(rec.id, 100_00, business_id=BIZ)
    with pytest.raises(HeliosError) as err:
        store.create_link(business_id=BIZ, receivable_id=rec.id)
    assert err.value.code == "validation_error"


# --------------------------------------------------------------------- links


def test_create_link_defaults_to_outstanding(store):
    rec = store.add_receivable(business_id=BIZ, customer_id="cus_1",
                               amount=100_00)
    link = store.create_link(business_id=BIZ, receivable_id=rec.id)
    assert link.amount == 100_00
    assert link.currency == "CNY"
    assert link.one_time and link.status == "issued"
    assert store.get_link_by_token(link.token).id == link.id


def test_link_amount_capped_at_outstanding(store):
    rec = store.add_receivable(business_id=BIZ, customer_id="cus_1",
                               amount=100_00)
    store.apply_payment(rec.id, 30_00, business_id=BIZ)
    with pytest.raises(HeliosError) as err:
        store.create_link(business_id=BIZ, receivable_id=rec.id,
                          amount=70_00 + 1)
    assert err.value.code == "validation_error"
    link = store.create_link(business_id=BIZ, receivable_id=rec.id)
    assert link.amount == 70_00  # defaults to the outstanding remainder


def test_two_links_have_distinct_tokens(store):
    rec = store.add_receivable(business_id=BIZ, customer_id="cus_1",
                               amount=100_00)
    a = store.create_link(business_id=BIZ, receivable_id=rec.id)
    b = store.create_link(business_id=BIZ, receivable_id=rec.id)
    assert a.token != b.token


def test_unknown_token_is_not_found(store):
    with pytest.raises(HeliosError) as err:
        store.get_link_by_token("nope")
    assert err.value.code == "not_found"


# ------------------------------------------------------------------ attempts


def test_attempt_uses_the_server_side_amount(store):
    rec, link, att = _seed(store)
    assert att.amount == link.amount
    assert att.currency == link.currency
    assert store.find_attempt_by_out_trade_no(att.out_trade_no).id == att.id
    with pytest.raises(HeliosError) as err:
        store.create_attempt(business_id=BIZ, link_id=link.id,
                             provider="mock", amount=link.amount + 1)
    assert err.value.code == "amount_mismatch"


def test_attempt_on_closed_link_is_inactive(store):
    rec, link, att = _seed(store)
    store.mark_link(link.id, "closed", business_id=BIZ)
    with pytest.raises(HeliosError) as err:
        store.create_attempt(business_id=BIZ, link_id=link.id, provider="mock")
    assert err.value.code == "link_inactive"


def test_attempt_on_expired_link_is_inactive_and_marks_the_link():
    clock = FakeClock(datetime(2026, 10, 14, 12, 0, tzinfo=SHANGHAI))
    store = PaymentStore(clock=clock)
    rec = store.add_receivable(business_id=BIZ, customer_id="cus_1",
                               amount=100_00)
    link = store.create_link(business_id=BIZ, receivable_id=rec.id,
                             expires_at=clock.t + timedelta(hours=1))
    clock.tick(hours=2)
    with pytest.raises(HeliosError) as err:
        store.create_attempt(business_id=BIZ, link_id=link.id, provider="mock")
    assert err.value.code == "link_inactive"
    assert store.get_link(link.id, BIZ).status == "expired"


# ------------------------------------------------------------- state machine


def test_invalid_transition_is_rejected_and_leaves_state_unchanged(store):
    rec, link, att = _seed(store)
    with pytest.raises(HeliosError) as err:
        store.transition_attempt(att.id, "succeeded", business_id=BIZ)
    assert err.value.code == "invalid_transition"
    assert store.attempts[att.id].state == "created"


def test_failed_records_reason_and_is_terminal(store):
    rec, link, att = _seed(store)
    att = store.transition_attempt(att.id, "failed", business_id=BIZ,
                                   reason="payer cancelled")
    assert att.state == "failed" and att.failure_reason == "payer cancelled"
    with pytest.raises(HeliosError):
        store.transition_attempt(att.id, "succeeded", business_id=BIZ)


# ----------------------------------------------------------- payment success


def test_record_payment_success_applies_everything(store):
    seen: list[dict] = []
    store.subscribe(seen.append)
    rec, link, att = _seed(store)
    _authenticate(store, att)

    done = store.record_payment_success(att.id, business_id=BIZ,
                                        provider_order_ref="MOCK-1")
    assert done.state == "succeeded"
    assert done.provider_order_ref == "MOCK-1"

    # the one-time link closed
    assert store.get_link(link.id, BIZ).status == "closed"

    # the receivable is paid
    rec = store.get_receivable(rec.id, BIZ)
    assert rec.status == "paid" and rec.amount_paid == rec.amount

    # a balanced capture journal: clearing -> merchant
    legs = store.ledger_for_attempt(att.id, BIZ)
    assert len(legs) == 2
    assert legs[0].type == "debit"
    assert legs[0].account == "provider:clearing"
    assert legs[1].type == "credit"
    assert legs[1].account == "merchant:main"
    assert legs[0].journal_id == legs[1].journal_id
    assert legs[0].amount == legs[1].amount == att.amount

    # the settlement is open and pending
    s = store.settlement_for_attempt(att.id, business_id=BIZ)
    assert s is not None and s.status == "pending"
    assert s.to_account == "merchant:main"

    assert any(e["event"] == "payment.state_changed"
               and e["to"] == "succeeded" for e in seen)
    assert any(e["event"] == "receivable.status_changed" for e in seen)


def test_record_payment_success_is_idempotent(store):
    rec, link, att = _seed(store)
    _authenticate(store, att)
    store.record_payment_success(att.id, business_id=BIZ)
    ledger_len = len(store.ledger)

    again = store.record_payment_success(att.id, business_id=BIZ)
    assert again.state == "succeeded"
    assert len(store.ledger) == ledger_len
    assert store.get_receivable(rec.id, BIZ).amount_paid == rec.amount


# ------------------------------------------------------------ provider events


def test_provider_event_is_append_only_and_idempotent(store):
    payload = {"out_trade_no": "HL1", "total_amount": "100.00",
               "trade_status": "TRADE_SUCCESS"}
    ev, created = store.record_provider_event(provider="alipay_sandbox",
                                              event_id="N1", payload=payload)
    assert created and ev.processed_at is None

    again, created2 = store.record_provider_event(
        provider="alipay_sandbox", event_id="N1", payload=payload)
    assert not created2 and again.id == ev.id
    assert again.payload_json == ev.payload_json


def test_event_id_reused_with_a_different_payload_conflicts(store):
    store.record_provider_event(provider="mock", event_id="N1",
                                payload={"a": 1})
    with pytest.raises(HeliosError) as err:
        store.record_provider_event(provider="mock", event_id="N1",
                                    payload={"a": 2})
    assert err.value.code == "event_conflict"


def test_raw_payload_json_round_trips_untouched(store):
    ev, _ = store.record_provider_event(provider="mock", event_id="R1",
                                        payload_json='{"b":1, "a":2}')
    assert ev.payload_json == '{"b":1, "a":2}'


def test_complete_provider_event_stamps_once(store):
    store.record_provider_event(provider="mock", event_id="N1",
                                payload={"a": 1})
    done = store.complete_provider_event("mock", "N1")
    assert done.processed_at is not None
    again = store.complete_provider_event("mock", "N1")
    assert again.processed_at == done.processed_at
    with pytest.raises(HeliosError) as err:
        store.complete_provider_event("mock", "missing")
    assert err.value.code == "not_found"


def test_event_needs_exactly_one_payload_form(store):
    with pytest.raises(HeliosError) as err:
        store.record_provider_event(provider="mock", event_id="X")
    assert err.value.code == "validation_error"


# -------------------------------------------------------------------- ledger


def test_unbalanced_posting_is_rejected(store):
    rec, link, att = _seed(store)
    with pytest.raises(HeliosError) as err:
        store.post_journal(business_id=BIZ, attempt_id=att.id,
                           entries=[("debit", 100, "provider:clearing")])
    assert err.value.code == "unbalanced_entry"
    assert store.ledger == []


def test_empty_posting_is_rejected(store):
    rec, link, att = _seed(store)
    with pytest.raises(HeliosError) as err:
        store.post_journal(business_id=BIZ, attempt_id=att.id, entries=[])
    assert err.value.code == "validation_error"


# ------------------------------------------------------------------- refunds


def test_partial_then_full_refund(store):
    rec, link, att = _seed(store)
    _authenticate(store, att)
    store.record_payment_success(att.id, business_id=BIZ)

    att = store.record_refund(att.id, amount=40_00, business_id=BIZ,
                              reason="partial credit")
    assert att.state == "partially_refunded"
    assert att.refunded_amount == 40_00

    legs = store.ledger_for_attempt(att.id, BIZ)
    assert len(legs) == 4  # capture pair + refund pair
    refunds = [l for l in legs if l.memo.startswith("refund")]
    assert refunds[0].type == "debit"
    assert refunds[0].account == "merchant:main"
    assert refunds[1].type == "credit"
    assert refunds[1].account == "provider:clearing"
    assert refunds[0].journal_id == refunds[1].journal_id

    att = store.record_refund(att.id, amount=60_00, business_id=BIZ)
    assert att.state == "refunded"
    assert att.refunded_amount == att.amount

    with pytest.raises(HeliosError) as err:
        store.record_refund(att.id, amount=1, business_id=BIZ)
    assert err.value.code == "refund_exceeds_payment"


def test_refund_before_success_is_rejected(store):
    rec, link, att = _seed(store)
    _authenticate(store, att)
    with pytest.raises(HeliosError) as err:
        store.record_refund(att.id, amount=10, business_id=BIZ)
    assert err.value.code == "invalid_transition"
    assert store.ledger == []
    assert store.attempts[att.id].refunded_amount == 0


# ---------------------------------------------------------------- settlement


def test_settle_routes_funds_and_is_idempotent(store):
    rec, link, att = _seed(store)
    _authenticate(store, att)
    store.record_payment_success(att.id, business_id=BIZ)

    att = store.settle(att.id, business_id=BIZ)
    assert att.state == "settled"
    s = store.settlement_for_attempt(att.id, business_id=BIZ)
    assert s is not None and s.status == "routed"

    again = store.settle(att.id, business_id=BIZ)
    assert again.state == "settled"


def test_settle_without_a_settlement_is_not_found(store):
    rec, link, att = _seed(store)
    for state in ("intent_created", "customer_authenticating",
                  "processing", "succeeded"):
        att = store.transition_attempt(att.id, state, business_id=BIZ)
    with pytest.raises(HeliosError) as err:
        store.settle(att.id, business_id=BIZ)
    assert err.value.code == "not_found"


# ------------------------------------------------------------------- tenancy


def test_cross_tenant_reads_are_not_found(store):
    rec, link, att = _seed(store, business_id="acme")
    for fn in (
        lambda: store.get_receivable(rec.id, "other"),
        lambda: store.get_link(link.id, "other"),
        lambda: store.get_attempt(att.id, "other"),
        lambda: store.ledger_for_attempt(att.id, "other"),
    ):
        with pytest.raises(HeliosError) as err:
            fn()
        assert err.value.code == "not_found"


# --------------------------------------------------------------- idempotency


def test_idempotent_replays_the_first_result(store):
    calls = []

    def work():
        calls.append(1)
        return {"ok": len(calls)}

    first = store.idempotent("key", work, business_id=BIZ)
    second = store.idempotent("key", work, business_id=BIZ)
    assert first == second == {"ok": 1}
    assert len(calls) == 1


def test_idempotency_is_scoped_per_business(store):
    calls = []

    def work():
        calls.append(1)
        return {"ok": len(calls)}

    a = store.idempotent("key", work, business_id="acme")
    b = store.idempotent("key", work, business_id="other")
    assert a == {"ok": 1} and b == {"ok": 2}


def test_idempotency_never_caches_a_failure(store):
    tries = []

    def flaky():
        tries.append(1)
        if len(tries) == 1:
            raise HeliosError("upstream_unavailable", "try again")
        return "ok"

    with pytest.raises(HeliosError):
        store.idempotent("key", flaky, business_id=BIZ)
    assert store.idempotent("key", flaky, business_id=BIZ) == "ok"


# -------------------------------------------------------------- subscriptions


def test_subscribers_see_state_changes(store):
    seen: list[dict] = []
    store.subscribe(seen.append)
    rec, link, att = _seed(store)  # link.created already emitted
    _authenticate(store, att)      # two payment.state_changed events
    kinds = [e["event"] for e in seen]
    assert "link.created" in kinds
    assert kinds.count("payment.state_changed") == 2


def test_broken_subscribers_never_break_writes(store):
    def boom(_event):
        raise RuntimeError("nope")

    store.subscribe(boom)
    rec = store.add_receivable(business_id=BIZ, customer_id="cus_1",
                               amount=100_00)
    link = store.create_link(business_id=BIZ, receivable_id=rec.id)
    assert link.id in store.links
    assert store.handler_errors and "nope" in store.handler_errors[0]


def test_unsubscribe_stops_delivery(store):
    seen: list[dict] = []
    sub = store.subscribe(seen.append)
    store.unsubscribe(sub)
    rec = store.add_receivable(business_id=BIZ, customer_id="cus_1",
                               amount=100_00)
    store.create_link(business_id=BIZ, receivable_id=rec.id)
    assert seen == []

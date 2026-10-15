"""Model-level invariants: money fields, transition tables, defaults."""

from datetime import timedelta
from typing import get_args

import pytest
from pydantic import ValidationError

from payments_collect.models import (
    ATTEMPT_TRANSITIONS,
    LINK_TRANSITIONS,
    RECEIVABLE_TRANSITIONS,
    SETTLEMENT_TRANSITIONS,
    AttemptState,
    LinkStatus,
    PaymentAttempt,
    PaymentLink,
    ProviderEvent,
    Receivable,
    Settlement,
)


def test_attempt_transition_table_covers_every_state():
    assert set(ATTEMPT_TRANSITIONS) == set(get_args(AttemptState))


def test_terminal_attempt_states_never_reopen():
    for terminal in ("closed", "failed", "expired"):
        assert ATTEMPT_TRANSITIONS[terminal] == frozenset()
    assert "settled" in ATTEMPT_TRANSITIONS["succeeded"]
    assert "refunded" in ATTEMPT_TRANSITIONS["succeeded"]


def test_canonical_chain_is_walkable():
    chain = ["created", "intent_created", "customer_authenticating",
             "processing", "succeeded", "settled", "closed"]
    for old, new in zip(chain, chain[1:]):
        assert new in ATTEMPT_TRANSITIONS[old], f"{old} -> {new} must be allowed"


def test_transition_tables_match_their_literals():
    assert set(LINK_TRANSITIONS) == set(get_args(LinkStatus))
    assert set(RECEIVABLE_TRANSITIONS) == {"open", "partial", "paid", "void"}
    assert set(SETTLEMENT_TRANSITIONS) == {"pending", "routed", "failed"}


def test_money_fields_reject_nonpositive_amounts():
    with pytest.raises(ValidationError):
        Receivable(id="r", customer_id="c", amount=0)
    with pytest.raises(ValidationError):
        PaymentLink(id="l", receivable_id="r", amount=-5, token="t")
    with pytest.raises(ValidationError):
        PaymentAttempt(id="a", link_id="l", provider="mock",
                       out_trade_no="o", amount=0)


def test_defaults_are_cny_shanghai_and_fresh():
    rec = Receivable(id="r", customer_id="c", amount=100)
    assert rec.currency == "CNY"
    assert rec.status == "open"
    assert rec.amount_paid == 0
    assert rec.created_at.utcoffset() == timedelta(hours=8)

    att = PaymentAttempt(id="a", link_id="l", provider="mock",
                         out_trade_no="o", amount=100)
    assert att.state == "created"
    assert att.refunded_amount == 0
    assert att.updated_at.utcoffset() == timedelta(hours=8)

    ev = ProviderEvent(id="e", provider="mock", event_id="n1",
                       payload_hash="h", payload_json="{}")
    assert ev.processed_at is None

    stl = Settlement(id="s", business_id="b", attempt_id="a",
                     to_account="acc")
    assert stl.status == "pending"
    assert stl.rule == "default"

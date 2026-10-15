"""P1 tests: behavioural timing models — shrinkage, fallback ladder, promise credibility."""
import pytest

from helios_forecast import (
    offset_profile_for,
    parse_flow,
    parse_payment_record,
    parse_promise_record,
    promise_profiles,
    promise_rate_for,
    segment_offset_profiles,
    customer_offset_profiles,
)


def rec(cust, seg, due, paid):
    return parse_payment_record(
        {"customer_id": cust, "segment": seg, "due": due, "paid": paid}
    )


def w(profile):
    return {d: w for d, w in profile["offset_days"]}


# Segment A: mostly 7 days late. Customer a1 pays on time twice.
SEGA = [
    rec("a1", "retail", "2026-01-01", "2026-01-01"),
    rec("a1", "retail", "2026-02-01", "2026-02-01"),
    rec("a2", "retail", "2026-01-01", "2026-01-08"),
    rec("a2", "retail", "2026-02-01", "2026-02-08"),
    rec("a2", "retail", "2026-03-01", "2026-03-08"),
]


def test_segment_profiles_pool_all_segments():
    profiles = segment_offset_profiles(SEGA)
    assert profiles["retail"]["n"] == 5
    assert w(profiles["retail"])[0] == pytest.approx(2 / 5)
    assert w(profiles["retail"])[7] == pytest.approx(3 / 5)
    assert profiles["global"]["n"] == 5


def test_cold_start_customer_gets_segment_profile():
    profile = offset_profile_for(SEGA, "new-customer", "retail")
    assert profile["level"] == "segment"
    assert w(profile)[7] == pytest.approx(0.6)


def test_cold_start_unknown_segment_gets_global():
    profile = offset_profile_for(SEGA, "new-customer", "wholesale")
    assert profile["level"] == "global"


def test_cold_start_no_history_gets_default():
    profile = offset_profile_for([], "new-customer", "retail")
    assert profile["level"] == "default"
    assert w(profile)[0] == pytest.approx(0.5)


def test_shrinkage_mixes_customer_with_segment():
    # a1: n=2 all on time; segment: 40% on time, 60% +7 (k=5).
    profiles = customer_offset_profiles(SEGA, prior_strength=5.0)
    p = w(profiles["a1"])
    # w(0) = (2*1 + 5*0.4) / 7, w(7) = (2*0 + 5*0.6) / 7
    assert p[0] == pytest.approx((2 * 1.0 + 5 * 0.4) / 7)
    assert p[7] == pytest.approx((5 * 0.6) / 7)
    assert profiles["a1"]["n"] == 2
    # More evidence -> closer to the customer's own pattern.
    profiles_small_k = customer_offset_profiles(SEGA, prior_strength=0.5)
    assert w(profiles_small_k["a1"])[0] > p[0]


def test_more_evidence_pulls_toward_customer_pattern():
    many = [rec("a1", "retail", f"2026-{m:02d}-01", f"2026-{m:02d}-01") for m in range(1, 11)]
    profiles = customer_offset_profiles(many + SEGA[2:], prior_strength=5.0)
    assert w(profiles["a1"])[0] > 0.9  # 10 own records dominate the prior


def test_profile_plugs_into_parse_flow():
    profile = offset_profile_for(SEGA, "a2", "retail")
    flow = parse_flow(
        {
            "id": "inv-1",
            "direction": "inflow",
            "amount": 1000.0,
            "date": {"due": "2026-06-01", "offset_days": profile["offset_days"]},
        }
    )
    # Segment A's dominant behaviour (+7) carries the most weight.
    assert max(flow.date.offset_days, key=lambda p: p[1])[0] == 7


def test_promise_shrinkage():
    promises = [
        parse_promise_record({"customer_id": "p1", "segment": "retail", "kept": True}),
        parse_promise_record({"customer_id": "p2", "segment": "retail", "kept": False}),
        parse_promise_record({"customer_id": "p2", "segment": "retail", "kept": False}),
    ]
    profiles = promise_profiles(promises, prior_strength=2.0)
    # Segment rate 1/3; p1: (1 + 2/3) / 3, p2: (0 + 2/3) / 4... check formula:
    assert profiles["p1"]["promise_kept"] == pytest.approx((1 + 2 * (1 / 3)) / (1 + 2))
    assert profiles["p2"]["promise_kept"] == pytest.approx((0 + 2 * (1 / 3)) / (2 + 2))


def test_promise_fallback_ladder():
    promises = [
        parse_promise_record({"customer_id": "p2", "segment": "retail", "kept": False}),
        parse_promise_record({"customer_id": "p2", "segment": "retail", "kept": True}),
    ]
    known = promise_rate_for(promises, "p2", "retail")
    assert known["level"] == "customer"
    same_seg = promise_rate_for(promises, "stranger", "retail")
    assert same_seg["level"] == "segment"
    assert same_seg["promise_kept"] == pytest.approx(0.5)
    no_history = promise_rate_for([], "stranger", "retail")
    assert no_history["level"] == "default"
    assert no_history["promise_kept"] == 0.5


def test_deterministic_and_parse_errors():
    assert customer_offset_profiles(SEGA) == customer_offset_profiles(SEGA)
    with pytest.raises(Exception):
        parse_payment_record({"customer_id": "x", "segment": "s", "paid": "2026-01-01", "due": "2026-02-01"})
    with pytest.raises(Exception):
        parse_promise_record({"customer_id": "x", "segment": "s", "kept": "yes"})

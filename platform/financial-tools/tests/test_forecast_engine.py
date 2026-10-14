"""P0 tests: parsing, deterministic projection, seeded Monte Carlo, scenarios."""
from datetime import date

import numpy as np
import pytest

from helios_forecast import (
    Delta,
    ForecastError,
    apply_delta,
    balance_at,
    compare,
    expected_total,
    first_shortfall,
    first_shortfall_stats,
    flow_to_dict,
    min_balance,
    min_balance_percentiles,
    parse_flow,
    probability_below,
    project,
    run_scenario,
    simulate,
    state_hash,
)

START = date(2026, 1, 1)


def inflow(fid, amount, day, **kw):
    raw = {"id": fid, "direction": "inflow", "amount": amount, "date": f"2026-01-{day:02d}"}
    raw.update(kw)
    return parse_flow(raw)


def outflow(fid, amount, day, **kw):
    raw = {"id": fid, "direction": "outflow", "amount": amount, "date": f"2026-01-{day:02d}"}
    raw.update(kw)
    return parse_flow(raw)


# --- parsing ---------------------------------------------------------------


def test_parse_roundtrip():
    raw = {
        "id": "inv-001",
        "account": "main",
        "direction": "inflow",
        "amount": {"distribution": [[5000, 0.6], [2500, 0.4]]},
        "date": {"due": "2026-11-05", "offset_days": [[0, 0.5], [7, 0.5]]},
        "probability": 0.9,
        "recurrence": {"freq": "monthly", "count": 3},
        "source": "kg:invoice/inv-001",
        "customer_id": None,
        "segment": None,
    }
    assert flow_to_dict(parse_flow(raw)) == raw


def test_parse_errors():
    with pytest.raises(ForecastError):
        parse_flow({"id": "x", "amount": 1, "date": "2026-01-01", "direction": "sideways"})
    with pytest.raises(ForecastError):
        parse_flow({"id": "x", "amount": 1, "date": "not-a-date"})
    with pytest.raises(ForecastError):
        parse_flow({"id": "x", "amount": 1, "date": "2026-01-01", "probability": 2.0})
    with pytest.raises(ForecastError):
        parse_flow({"id": "x", "amount": 1, "date": "2026-01-01", "recurrence": {"freq": "yearly"}})


# --- deterministic projection ----------------------------------------------


def test_fixed_flow_lands_on_day():
    p = project({"main": 1000.0}, [inflow("f1", 500, 5)], START, 30)
    assert balance_at(p, date(2026, 1, 4)) == 1000.0
    assert balance_at(p, date(2026, 1, 5)) == 1500.0
    assert balance_at(p, date(2026, 1, 30)) == 1500.0


def test_expected_value_applies_probability_and_distribution():
    p = project({"main": 0.0}, [inflow("f1", 100, 3, probability=0.5)], START, 10)
    assert balance_at(p, date(2026, 1, 3)) == pytest.approx(50.0)
    p2 = project(
        {"main": 0.0},
        [parse_flow({"id": "f2", "direction": "inflow", "amount": {"distribution": [[100, 0.25], [200, 0.75]]}, "date": "2026-01-03"})],
        START,
        10,
    )
    assert balance_at(p2, date(2026, 1, 3)) == pytest.approx(175.0)


def test_offset_expected_date():
    flow = parse_flow(
        {"id": "f", "direction": "inflow", "amount": 10, "date": {"due": "2026-01-05", "offset_days": [[0, 0.5], [10, 0.5]]}}
    )
    p = project({"main": 0.0}, [flow], START, 30)
    assert balance_at(p, date(2026, 1, 9)) == 0.0
    assert balance_at(p, date(2026, 1, 10)) == pytest.approx(10.0)


def test_monthly_recurrence():
    rent = outflow("rent", 1000, 1, recurrence={"freq": "monthly", "count": 3})
    p = project({"main": 10000.0}, [rent], START, 90)
    assert balance_at(p, date(2026, 1, 1)) == 9000.0
    assert balance_at(p, date(2026, 2, 1)) == 8000.0
    assert balance_at(p, date(2026, 3, 1)) == 7000.0
    assert balance_at(p, date(2026, 3, 31)) == 7000.0


def test_monthly_day_clamp():
    rent = outflow("rent", 100, 31, recurrence={"freq": "monthly", "count": 3})
    p = project({"main": 1000.0}, [rent], date(2026, 1, 31), 60)
    assert balance_at(p, date(2026, 2, 28)) == 800.0  # Jan 31 + Feb 28 outflows
    assert balance_at(p, date(2026, 3, 31)) == 700.0


def test_multi_account_totals():
    flow = parse_flow({"id": "t", "account": "tax", "direction": "inflow", "amount": 10, "date": "2026-01-02"})
    p = project({"main": 100.0, "tax": 50.0}, [flow], START, 10)
    assert p.balances["tax"][1] == pytest.approx(60.0)
    assert p.total[1] == pytest.approx(160.0)


def test_first_shortfall_and_min_balance():
    p = project({"main": 100.0}, [outflow("bill", 150, 10)], START, 30)
    assert first_shortfall(p) == date(2026, 1, 10)
    mb = min_balance(p, window=(date(2026, 1, 8), date(2026, 1, 12)))
    assert mb["value"] == pytest.approx(-50.0)
    assert mb["date"] == "2026-01-10"
    assert first_shortfall(p, threshold=-100.0) is None


def test_expected_total_window():
    flows = [inflow("a", 100, 2), inflow("b", 200, 5), inflow("c", 50, 20)]
    p = project({"main": 0.0}, flows, START, 30)
    assert expected_total(p, "inflow", window=(START, date(2026, 1, 4))) == pytest.approx(100.0)
    assert expected_total(p, "inflow") == pytest.approx(350.0)
    assert expected_total(p, "outflow") == 0.0


# --- Monte Carlo -----------------------------------------------------------


def test_mc_reproducible():
    flows = [
        inflow("i1", 500, 5, probability=0.8),
        outflow("o1", 300, 10),
        parse_flow(
            {"id": "d1", "direction": "inflow", "amount": {"distribution": [[100, 0.5], [300, 0.5]]},
             "date": {"due": "2026-01-15", "offset_days": [[0, 0.5], [3, 0.5]]}}
        ),
    ]
    a = simulate({"main": 100.0}, flows, START, 30, n_paths=500, seed=7)
    b = simulate({"main": 100.0}, flows, START, 30, n_paths=500, seed=7)
    c = simulate({"main": 100.0}, flows, START, 30, n_paths=500, seed=8)
    assert np.array_equal(a.total_paths, b.total_paths)
    assert not np.array_equal(a.total_paths, c.total_paths)
    assert a.metadata["seed"] == 7
    assert a.metadata["inputs_hash"] == b.metadata["inputs_hash"]


def test_mc_deterministic_flows_match_projection():
    flows = [inflow("i1", 500, 5), outflow("o1", 300, 10)]
    p = project({"main": 100.0}, flows, START, 30)
    mc = simulate({"main": 100.0}, flows, START, 30, n_paths=200, seed=3)
    for key in ("p5", "p50", "p95"):
        assert mc.percentiles[key] == pytest.approx(p.total)


def test_probability_below_matches_bernoulli():
    # A single outflow of 1000 vs balance 500: shortfall iff it occurs (p=0.25).
    flows = [outflow("o1", 1000, 10, probability=0.25)]
    mc = simulate({"main": 500.0}, flows, START, 30, n_paths=5000, seed=1)
    assert probability_below(mc, 0.0) == pytest.approx(0.25, abs=0.03)
    stats = first_shortfall_stats(mc, 0.0)
    assert stats["probability"] == pytest.approx(0.25, abs=0.03)
    assert stats["median_date"] == "2026-01-10"
    mbd = min_balance_percentiles(mc)
    assert mbd["p5"] == pytest.approx(-500.0)  # 25% of paths dip to -500
    assert mbd["p50"] == pytest.approx(500.0)  # most paths never dip


def test_mc_metadata_hash_changes_with_inputs():
    flows_a = [inflow("i1", 500, 5)]
    flows_b = [inflow("i1", 600, 5)]
    mc_a = simulate({"main": 100.0}, flows_a, START, 10, n_paths=50, seed=0)
    mc_b = simulate({"main": 100.0}, flows_b, START, 10, n_paths=50, seed=0)
    assert mc_a.metadata["inputs_hash"] != mc_b.metadata["inputs_hash"]
    assert state_hash({"main": 100.0}, flows_a) == state_hash({"main": 100.0}, flows_a)


# --- scenarios -------------------------------------------------------------


def test_scenario_shift_moves_shortfall():
    opening = {"main": 1000.0}
    flows = [outflow("bill", 1200, 10)]
    delta = Delta.parse({"operations": [{"op": "shift_date", "flow_id": "bill", "days": 5}]})
    baseline, scenario, diff = run_scenario(opening, flows, delta, START, 30)
    assert baseline is not None and scenario is not None
    assert diff["first_shortfall"]["baseline"] == "2026-01-10"
    assert diff["first_shortfall"]["scenario"] == "2026-01-15"
    assert diff["first_shortfall"]["change_days"] == 5


def test_scenario_remove_resolves_shortfall():
    opening = {"main": 1000.0}
    flows = [outflow("bill", 1200, 10)]
    delta = Delta.parse({"operations": [{"op": "remove_flow", "flow_id": "bill"}]})
    _, _, diff = run_scenario(opening, flows, delta, START, 30)
    assert diff["first_shortfall"]["scenario_resolves_baseline_shortfall"] is True
    assert diff["min_balance"]["scenario"] == pytest.approx(1000.0)
    assert diff["final_balance"]["change"] == pytest.approx(1200.0)


def test_scenario_resize_and_probability():
    opening = {"main": 1000.0}
    flows = [outflow("bill", 1200, 10)]
    delta = Delta.parse({"operations": [{"op": "resize_amount", "flow_id": "bill", "factor": 0.5}]})
    _, scenario, _ = run_scenario(opening, flows, delta, START, 30)
    assert balance_at(scenario, date(2026, 1, 10)) == pytest.approx(400.0)

    delta2 = Delta.parse({"operations": [{"op": "set_probability", "flow_id": "bill", "probability": 0.0}]})
    _, scenario2, _ = run_scenario(opening, flows, delta2, START, 30)
    assert balance_at(scenario2, date(2026, 1, 10)) == pytest.approx(1000.0)


def test_scenario_add_flow():
    delta = Delta.parse(
        {"operations": [{"op": "add_flow", "flow": {"id": "new", "direction": "inflow", "amount": 700, "date": "2026-01-09"}}]}
    )
    opening, flows = {"main": 100.0}, [outflow("bill", 1200, 10)]
    _, scenario, diff = run_scenario(opening, flows, delta, START, 30)
    assert balance_at(scenario, date(2026, 1, 10)) == pytest.approx(-400.0)
    assert diff["final_balance"]["change"] == pytest.approx(700.0)


def test_delta_errors():
    with pytest.raises(ForecastError):
        Delta.parse({"operations": [{"op": "explode"}]})
    # Unknown flow ids are caught at apply time, not parse time.
    with pytest.raises(ForecastError):
        apply_delta({}, [], Delta.parse({"operations": [{"op": "shift_date", "flow_id": "ghost", "days": 1}]}))
    with pytest.raises(ForecastError):
        apply_delta({}, [], Delta.parse({"operations": [{"op": "set_probability", "flow_id": "ghost", "probability": 0.5}]}))


def test_compare_requires_same_grid():
    p1 = project({"main": 0.0}, [], START, 10)
    p2 = project({"main": 0.0}, [], START, 20)
    with pytest.raises(ForecastError):
        compare(p1, p2)


# --- coverage gaps: recurrence variants, set_amount, roundtrips, account MC ---


def test_weekly_recurrence():
    pay = inflow("wage", 400, 5, recurrence={"freq": "weekly", "count": 3})
    p = project({"main": 0.0}, [pay], START, 30)
    assert balance_at(p, date(2026, 1, 4)) == 0.0
    assert balance_at(p, date(2026, 1, 5)) == 400.0
    assert balance_at(p, date(2026, 1, 12)) == 800.0
    assert balance_at(p, date(2026, 1, 19)) == 1200.0
    assert balance_at(p, date(2026, 1, 26)) == 1200.0  # count=3 stops after 3


def test_recurrence_until():
    pay = inflow("sub", 100, 1, recurrence={"freq": "daily", "until": "2026-01-03"})
    p = project({"main": 0.0}, [pay], START, 10)
    assert balance_at(p, date(2026, 1, 3)) == 300.0
    assert balance_at(p, date(2026, 1, 4)) == 300.0


def test_scenario_set_amount():
    delta = Delta.parse({"operations": [{"op": "set_amount", "flow_id": "bill", "amount": 900.0}]})
    opening, flows = {"main": 1000.0}, [outflow("bill", 1200, 10)]
    _, scenario, _ = run_scenario(opening, flows, delta, START, 30)
    assert balance_at(scenario, date(2026, 1, 10)) == pytest.approx(100.0)


def test_delta_to_dict_roundtrip():
    raw = {"operations": [
        {"op": "add_flow", "flow": {"id": "n", "account": "main", "direction": "inflow", "amount": 5,
                                    "date": "2026-01-02", "probability": 1.0,
                                    "recurrence": None, "source": None,
                                    "customer_id": None, "segment": None}},
        {"op": "shift_date", "flow_id": "n", "days": 3},
        {"op": "resize_amount", "flow_id": "n", "factor": 0.5},
        {"op": "set_probability", "flow_id": "n", "probability": 0.25},
        {"op": "set_amount", "flow_id": "n", "amount": 7},
        {"op": "remove_flow", "flow_id": "n"},
    ]}
    assert Delta.parse(raw).to_dict() == raw


def test_mc_account_percentiles():
    flow = parse_flow({"id": "t", "account": "tax", "direction": "inflow", "amount": 10, "date": "2026-01-02"})
    mc = simulate({"main": 100.0, "tax": 50.0}, [flow], START, 10, n_paths=50, seed=2)
    assert mc.account_percentiles["tax"]["p50"] == pytest.approx([50.0] + [60.0] * 9)
    assert mc.account_percentiles["main"]["p50"] == pytest.approx([100.0] * 10)


def test_expected_total_outflow_window():
    flows = [outflow("a", 100, 2), outflow("b", 200, 5)]
    p = project({"main": 1000.0}, flows, START, 30)
    assert expected_total(p, "outflow", window=(START, date(2026, 1, 4))) == pytest.approx(100.0)
    assert expected_total(p, "outflow") == pytest.approx(300.0)


# --- common shock (D5) -----------------------------------------------------


def test_common_shock_omitted_is_bit_identical():
    flows = [inflow("a", 100, 2), outflow("b", 60, 1)]
    base = simulate({"main": 0.0}, flows, START, 10, n_paths=64, seed=3)
    explicit = simulate({"main": 0.0}, flows, START, 10, n_paths=64, seed=3, common_shock=None)
    assert np.array_equal(base.total_paths, explicit.total_paths)
    assert "common_shock" not in base.metadata


def test_common_shock_shifts_all_dates():
    # Degenerate +7 shock: every path, every flow, lands 7 days later.
    mc = simulate(
        {"main": 0.0}, [inflow("a", 100, 3)], START, 10, n_paths=32, seed=5,
        common_shock={"offset_days": [[7, 1.0]]},
    )
    assert mc.percentiles["p50"] == pytest.approx([0.0] * 9 + [100.0])
    assert mc.metadata["common_shock"] == {"offset_days": [[7, 1.0]]}


def test_common_shock_moves_all_accounts_together():
    # 50/50 shock (0 or +7 days): within a path, both accounts shift together.
    flows = [inflow("x", 100, 1, account="a"), inflow("y", 100, 1, account="b")]
    mc = simulate(
        {"a": 0.0, "b": 0.0}, flows, START, 10, n_paths=2000, seed=7,
        common_shock={"offset_days": [[0, 0.5], [7, 0.5]]},
    )
    a0 = mc.account_paths["a"][:, 0]
    b0 = mc.account_paths["b"][:, 0]
    assert np.array_equal(a0, b0)  # correlated: shared shock per path
    assert a0.mean() == pytest.approx(50.0, abs=5.0)  # ~50% of paths unshifted


def test_common_shock_validation():
    flows = [inflow("a", 100, 2)]
    for bad in (
        {},
        {"offset_days": []},
        {"offset_days": [["7", 1.0]]},
        {"offset_days": [[7, 0.0]]},
        {"offset_days": [[7, 1.0, "extra"]]},
    ):
        with pytest.raises(ValueError):
            simulate({"main": 0.0}, flows, START, 10, n_paths=4, seed=0, common_shock=bad)

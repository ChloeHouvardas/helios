"""Task 4: the forecasting family (`forecast.*`, `behavior.*`).

The imported `helios_forecast` engine speaks floats; Helios money is integer
cents. These tests pin the conversion boundary:

- **Cents in, cents out**, exact for integer-cent inputs. A projection's final
  balance is arithmetic the test can recompute by hand.
- **Determinism by seed** survives the round trip through the engine.
- **Non-money numbers are never scaled.** `offset_days` day counts, `probability`
  and a `resize_amount` factor pass through at their true values; scaling one by
  100 would silently corrupt the forecast.
- **Scenario deltas** compare correctly.
- **`simulation.forecast`** is re-boded onto the shared engine but keeps its
  signature and `{summary, curve}` shape, preserving expected value.

All offline: the engine is pure and seeded, no network, no provider.
"""
from __future__ import annotations

import pytest

from fin_tools import PayerProfile, build_world
from fin_tools.forecast import to_cents, to_engine_units

OPENING = [{"account": "main", "balance_cents": 500_000}]

# rent 800.00 monthly x3 out; one invoice 3000.00 in with a timing spread.
FLOWS = [
    {"id": "rent", "direction": "outflow", "amount_cents": 80_000,
     "date": {"due": "2026-11-01"}, "recurrence": {"freq": "monthly", "count": 3}},
    {"id": "inv1", "direction": "inflow", "amount_cents": 300_000,
     "date": {"due": "2026-11-10", "offset_days": [[0, 0.3], [7, 0.4], [21, 0.3]]}},
]


@pytest.fixture
def bundle():
    return build_world()


# --------------------------------------------------------------------------
# the conversion boundary itself
# --------------------------------------------------------------------------

def test_cents_to_float_and_back_is_exact_for_integer_cents():
    for cents in (0, 1, 99, 100, 123_456, 1_000_000, -50_000):
        assert to_cents(to_engine_units(cents)) == cents


def test_half_even_rounding_on_the_way_back():
    # 0.125 units -> 12.5 cents -> half-even -> 12; 0.135 -> 13.5 -> 14
    assert to_cents(0.125) == 12
    assert to_cents(0.135) == 14


def test_engine_units_divide_by_one_hundred():
    # 250000 cents == 2500.00 units (the `_` is only a digit separator)
    assert to_engine_units(250_000) == pytest.approx(2500.0)
    assert to_engine_units(1) == pytest.approx(0.01)


# --------------------------------------------------------------------------
# forecast.project
# --------------------------------------------------------------------------

def test_project_90_day_known_inputs(bundle):
    out = bundle.registry.call("forecast.project", opening=OPENING, flows=FLOWS,
                               start="2026-11-01", days=90)
    # 5000.00 opening - 3*800.00 rent + 3000.00 invoice, in cents
    assert out["final_balance_cents"] == 500_000 - 240_000 + 300_000
    assert out["days"] == 90
    assert out["expected_outflows_cents"] == 240_000
    assert out["expected_inflows_cents"] == 300_000
    assert out["inputs_hash"]


def test_project_reports_every_value_in_cents_not_floats(bundle):
    out = bundle.registry.call("forecast.project", opening=OPENING, flows=FLOWS,
                               start="2026-11-01", days=90)
    assert isinstance(out["final_balance_cents"], int)
    assert all(isinstance(v, int) for v in out["total_cents"])
    assert all(isinstance(v, int) for v in out["inflows_cents"])
    assert all(isinstance(v, int) for v in out["outflows_cents"])


def test_project_first_shortfall_when_opening_is_too_small(bundle):
    thin = [{"account": "main", "balance_cents": 10_000}]   # 100.00 opening
    out = bundle.registry.call("forecast.project", opening=thin, flows=FLOWS,
                               start="2026-11-01", days=90)
    # rent of 800.00 on day 1 pushes it negative immediately
    assert out["first_shortfall"] == "2026-11-01"
    assert out["min_balance_cents"] < 0


def test_project_is_read_only(bundle):
    assert bundle.registry.get("forecast.project").side_effects is False


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------

def test_simulate_is_deterministic_by_seed(bundle):
    a = bundle.registry.call("forecast.simulate", opening=OPENING, flows=FLOWS,
                             start="2026-11-01", days=90, n_paths=300, seed=42)
    b = bundle.registry.call("forecast.simulate", opening=OPENING, flows=FLOWS,
                             start="2026-11-01", days=90, n_paths=300, seed=42)
    assert a["percentiles"] == b["percentiles"]
    assert a["probability_below"] == b["probability_below"]
    assert a["inputs_hash"] == b["inputs_hash"]


def test_simulate_shortfall_is_genuinely_stochastic(bundle):
    """Engineered so whether the trough dips below zero depends on WHEN the
    invoice arrives: a payroll on day 15 lands in the gap only for the late
    invoice paths. So P(balance<0) sits strictly between 0 and 1 -- proof the
    Monte Carlo is sampling, not returning a degenerate answer.

    Percentile bands alone can look seed-invariant here: over a discrete timing
    distribution with a large n_paths they converge to weight-determined
    thresholds. That is correct engine behaviour, which is why determinism is
    asserted separately (same seed -> identical result) rather than by expecting
    different seeds to move a percentile.
    """
    opening = [{"account": "main", "balance_cents": 90_000}]     # 900.00
    flows = [
        {"id": "rent", "direction": "outflow", "amount_cents": 80_000,
         "date": {"due": "2026-11-01"}},                          # day 0
        {"id": "payroll", "direction": "outflow", "amount_cents": 20_000,
         "date": {"due": "2026-11-16"}},                          # day 15
        {"id": "inv", "direction": "inflow", "amount_cents": 300_000,
         "date": {"due": "2026-11-10",
                  "offset_days": [[0, 0.34], [7, 0.33], [21, 0.33]]}},
    ]
    out = bundle.registry.call("forecast.simulate", opening=opening, flows=flows,
                               start="2026-11-01", days=90, n_paths=400, seed=1,
                               threshold_cents=0)
    assert 0.0 < out["probability_below"] < 1.0


def test_simulate_percentile_bands_are_cents(bundle):
    out = bundle.registry.call("forecast.simulate", opening=OPENING, flows=FLOWS,
                               start="2026-11-01", days=90, n_paths=200, seed=5)
    for band in out["percentiles"]:
        assert isinstance(band["level"], int)
        assert all(isinstance(v, int) for v in band["values"])
    # probability is NOT money: stays a float in [0, 1]
    assert isinstance(out["probability_below"], float)
    assert 0.0 <= out["probability_below"] <= 1.0


# --------------------------------------------------------------------------
# non-money numbers must not be scaled
# --------------------------------------------------------------------------

def test_offset_days_are_day_counts_not_money(bundle):
    """A 7-day offset must stay 7 days, never 700. If the converter scaled it,
    the invoice would land far outside the horizon and the balance would drop."""
    flows = [{"id": "inv", "direction": "inflow", "amount_cents": 100_000,
              "date": {"due": "2026-11-01", "offset_days": [[7, 1.0]]}}]
    out = bundle.registry.call("forecast.project",
                               opening=[{"account": "main", "balance_cents": 0}],
                               flows=flows, start="2026-11-01", days=30)
    # money arrives on day 7 (index 7), not day 700
    assert out["total_cents"][6] == 0
    assert out["total_cents"][7] == 100_000


def test_probability_is_not_scaled(bundle):
    """A 50% flow contributes half its amount in expectation, not 50x."""
    flows = [{"id": "maybe", "direction": "inflow", "amount_cents": 100_000,
              "probability": 0.5, "date": {"due": "2026-11-01"}}]
    out = bundle.registry.call("forecast.project",
                               opening=[{"account": "main", "balance_cents": 0}],
                               flows=flows, start="2026-11-01", days=10)
    assert out["expected_inflows_cents"] == 50_000


def test_resize_factor_is_not_scaled(bundle):
    """resize_amount.factor is a multiplier; halving must halve, not x100."""
    out = bundle.registry.call(
        "forecast.scenario", opening=OPENING, flows=FLOWS, start="2026-11-01",
        days=90, delta={"operations": [{"op": "resize_amount", "flow_id": "inv1",
                                        "factor": 0.5}]})
    # invoice expected inflow halves from 3000.00 -> 1500.00; final drops by that
    assert out["final_balance_change_cents"] == -150_000


# --------------------------------------------------------------------------
# forecast.scenario
# --------------------------------------------------------------------------

def test_scenario_delaying_an_inflow_lowers_min_balance(bundle):
    out = bundle.registry.call(
        "forecast.scenario", opening=OPENING, flows=FLOWS, start="2026-11-01",
        days=90, delta={"operations": [{"op": "shift_date", "flow_id": "inv1",
                                        "days": 30}]})
    # pushing the invoice later lets the rent hit first, so the trough deepens
    assert out["scenario_min_balance_cents"] < out["baseline_min_balance_cents"]
    # final balance is unchanged: the same money still arrives within 90 days
    assert out["final_balance_change_cents"] == 0
    assert out["inputs_hash"]


def test_scenario_add_flow(bundle):
    out = bundle.registry.call(
        "forecast.scenario", opening=OPENING, flows=FLOWS, start="2026-11-01",
        days=90, delta={"operations": [
            {"op": "add_flow", "flow": {"id": "bonus", "direction": "inflow",
                                        "amount_cents": 50_000,
                                        "date": {"due": "2026-11-15"}}}]})
    assert out["final_balance_change_cents"] == 50_000


def test_scenario_reports_shortfall_resolution(bundle):
    thin = [{"account": "main", "balance_cents": 10_000}]
    out = bundle.registry.call(
        "forecast.scenario", opening=thin, flows=FLOWS, start="2026-11-01",
        days=90, delta={"operations": [{"op": "shift_date", "flow_id": "rent",
                                        "days": 60}]})
    assert "scenario_resolves_baseline_shortfall" in out


# --------------------------------------------------------------------------
# forecast.query — five queries behind one tool
# --------------------------------------------------------------------------

def test_query_balance_at(bundle):
    out = bundle.registry.call("forecast.query", query="balance_at",
                               opening=OPENING, flows=FLOWS, start="2026-11-01",
                               days=90, on="2026-11-30")
    assert out["query"] == "balance_at"
    assert isinstance(out["balance_cents"], int)


def test_query_min_balance_matches_project(bundle):
    q = bundle.registry.call("forecast.query", query="min_balance", opening=OPENING,
                             flows=FLOWS, start="2026-11-01", days=90)
    p = bundle.registry.call("forecast.project", opening=OPENING, flows=FLOWS,
                             start="2026-11-01", days=90)
    assert q["min_balance_cents"] == p["min_balance_cents"]


def test_query_first_shortfall(bundle):
    thin = [{"account": "main", "balance_cents": 10_000}]
    out = bundle.registry.call("forecast.query", query="first_shortfall",
                               opening=thin, flows=FLOWS, start="2026-11-01",
                               days=90, threshold_cents=0)
    assert out["first_shortfall"] == "2026-11-01"


def test_query_expected_total_needs_a_direction(bundle):
    with pytest.raises(ValueError, match="direction"):
        bundle.registry.call("forecast.query", query="expected_total",
                             opening=OPENING, flows=FLOWS, start="2026-11-01",
                             days=90)


def test_query_expected_total_inflow(bundle):
    out = bundle.registry.call("forecast.query", query="expected_total",
                               opening=OPENING, flows=FLOWS, start="2026-11-01",
                               days=90, direction="inflow")
    assert out["expected_total_cents"] == 300_000


def test_query_probability_below_runs_monte_carlo(bundle):
    thin = [{"account": "main", "balance_cents": 10_000}]
    out = bundle.registry.call("forecast.query", query="probability_below",
                               opening=thin, flows=FLOWS, start="2026-11-01",
                               days=90, threshold_cents=0, n_paths=200, seed=3)
    assert 0.0 <= out["probability_below"] <= 1.0
    assert out["probability_below"] > 0.0     # thin opening does dip below zero


def test_query_unknown_name_is_actionable(bundle):
    with pytest.raises(ValueError, match="balance_at"):
        bundle.registry.call("forecast.query", query="nope", opening=OPENING,
                             flows=FLOWS, start="2026-11-01", days=90)


def test_query_balance_at_needs_on(bundle):
    with pytest.raises(ValueError, match="'on'"):
        bundle.registry.call("forecast.query", query="balance_at", opening=OPENING,
                             flows=FLOWS, start="2026-11-01", days=90)


def test_all_forecast_tools_are_read_only(bundle):
    for name in ("forecast.project", "forecast.simulate", "forecast.scenario",
                 "forecast.query", "behavior.profile", "behavior.promise_rate"):
        assert bundle.registry.get(name).side_effects is False, name


# --------------------------------------------------------------------------
# behavior.* — no money, delegated straight through
# --------------------------------------------------------------------------

def test_behavior_profile_from_records(bundle):
    records = [{"customer_id": "c1", "segment": "retail",
                "due": "2026-09-01", "paid": "2026-09-08"}]
    out = bundle.registry.call("behavior.profile", records=records,
                               customer_id="c1", segment="retail")
    assert out["level"] == "customer"
    assert out["offset_days"] == [[7, 1.0]]      # paid 7 days late


def test_behavior_profile_falls_back_to_default(bundle):
    out = bundle.registry.call("behavior.profile", records=[], customer_id="nobody",
                               segment="unknown")
    assert out["level"] == "default"


def test_behavior_promise_rate(bundle):
    records = [{"customer_id": "c1", "segment": "retail", "kept": True},
               {"customer_id": "c1", "segment": "retail", "kept": True},
               {"customer_id": "c2", "segment": "retail", "kept": False}]
    out = bundle.registry.call("behavior.promise_rate", records=records,
                               customer_id="c1", segment="retail")
    assert 0.0 <= out["promise_kept"] <= 1.0
    assert out["n"] == 2


# --------------------------------------------------------------------------
# simulation.forecast — the re-boded compatibility shim
# --------------------------------------------------------------------------

def test_simulation_forecast_keeps_its_shape(bundle):
    bundle.profiles["acme"] = PayerProfile(customer="acme", mean_delay_days=1,
                                           delay_sd_days=0.5, p_dispute=0.0,
                                           p_partial=0.0)
    invoices = [{"invoice_id": "I1", "customer": "acme", "amount_cents": 100_000,
                 "due_date": "2026-10-20"}]
    out = bundle.registry.call("simulation.forecast", invoices=invoices,
                               policy=None, n_runs=100, seed=7)
    assert set(out) == {"summary", "curve"}
    for key in ("booked_cents", "expected_collected_cents", "p10_cents",
                "p50_cents", "p90_cents", "horizon_days", "n_runs", "seed"):
        assert key in out["summary"], key


def test_simulation_forecast_preserves_expected_value(bundle):
    """Delegation must not lose or invent money: collected == booked here."""
    bundle.profiles["acme"] = PayerProfile(customer="acme", mean_delay_days=1,
                                           delay_sd_days=0.5, p_dispute=0.0,
                                           p_partial=0.0)
    invoices = [{"invoice_id": "I1", "customer": "acme", "amount_cents": 100_000,
                 "due_date": "2026-10-20"}]
    out = bundle.registry.call("simulation.forecast", invoices=invoices,
                               policy=None, n_runs=100, seed=7)
    assert out["summary"]["booked_cents"] == 100_000
    assert out["summary"]["expected_collected_cents"] == 100_000
    assert out["summary"]["expected_collected_cents"] <= out["summary"]["booked_cents"]


def test_simulation_forecast_is_deterministic(bundle):
    bundle.profiles["acme"] = PayerProfile(customer="acme", mean_delay_days=3,
                                           delay_sd_days=2.0)
    invoices = [{"invoice_id": "I1", "customer": "acme", "amount_cents": 100_000,
                 "due_date": "2026-10-20"}]
    a = bundle.registry.call("simulation.forecast", invoices=invoices,
                             policy=None, n_runs=150, seed=11)
    b = bundle.registry.call("simulation.forecast", invoices=invoices,
                             policy=None, n_runs=150, seed=11)
    assert a == b


def test_simulation_forecast_partial_payer_preserves_expected_value(bundle):
    """The two-instalment path: EV of first + deferred remainder == the amount."""
    bundle.profiles["part"] = PayerProfile(customer="part", mean_delay_days=3,
                                           delay_sd_days=1.0, p_dispute=0.0,
                                           p_partial=0.5, partial_fraction=0.5)
    invoices = [{"invoice_id": "P1", "customer": "part", "amount_cents": 100_000,
                 "due_date": "2026-10-20"}]
    out = bundle.registry.call("simulation.forecast", invoices=invoices,
                               policy=None, n_runs=100, seed=3)
    assert out["summary"]["expected_collected_cents"] == 100_000


def test_simulation_forecast_is_still_registered_and_read_only(bundle):
    spec = bundle.registry.get("simulation.forecast")
    assert spec.side_effects is False
    assert "simulation" in spec.tags

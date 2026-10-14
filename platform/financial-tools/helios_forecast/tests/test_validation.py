"""D8 tests: validation harness — declarative scenario expectations."""
from datetime import date

import pytest

from helios_forecast import ForecastError, parse_flow, run_validation

START = date(2026, 1, 1)


def rent_flow(day=1, amount=800.0):
    return parse_flow({"id": "rent", "direction": "outflow", "amount": amount,
                       "date": f"2026-01-{day:02d}"})


def test_baseline_case_passes():
    report = run_validation(
        {"main": 1000.0}, [rent_flow()], START, 30,
        cases=[{
            "name": "solvent",
            "delta": None,
            "expect": {"no_shortfall": True, "min_balance_at_least": 200.0,
                       "final_balance_at_least": 200.0},
        }],
    )
    assert report["all_passed"] is True
    assert report == {**report, "total": 1, "passed": 1, "failed": 0}
    case = report["cases"][0]
    assert case["name"] == "solvent" and case["passed"] is True
    assert all(c["passed"] for c in case["checks"])
    assert case["checks"][0]["predicate"] == "no_shortfall"


def test_failing_expectations_are_reported():
    report = run_validation(
        {"main": 1000.0}, [rent_flow()], START, 30,
        cases=[{"name": "too rich", "delta": None,
                "expect": {"min_balance_at_least": 500.0}}],
    )
    assert report["all_passed"] is False
    assert report["failed"] == 1
    check = report["cases"][0]["checks"][0]
    assert check["passed"] is False
    assert check["actual"] == 200.0 and check["expected"] == 500.0


def test_scenario_delta_drives_the_verdict():
    # Baseline: 500 opening vs 800 rent on day 0 → shortfall immediately.
    # Delta: push the rent 10 days out → first shortfall moves to day 10.
    cases = [
        {"name": "baseline fails", "delta": None,
         "expect": {"first_shortfall_no_earlier_than": "2026-01-05"}},
        {"name": "shifted passes", "delta": {"operations": [
            {"op": "shift_date", "flow_id": "rent", "days": 10}]},
         "expect": {"first_shortfall_no_earlier_than": "2026-01-05"}},
    ]
    report = run_validation({"main": 500.0}, [rent_flow(day=1)], START, 30, cases=cases)
    assert report["total"] == 2 and report["passed"] == 1 and report["failed"] == 1
    baseline, shifted = report["cases"]
    assert baseline["passed"] is False
    assert baseline["checks"][0]["actual"] == "2026-01-01"
    assert shifted["passed"] is True
    assert shifted["checks"][0]["actual"] == "2026-01-11"


def test_no_shortfall_passes_when_never_below():
    report = run_validation(
        {"main": 2000.0}, [rent_flow()], START, 30,
        cases=[{"name": "never", "delta": None,
                "expect": {"no_shortfall": True, "first_shortfall_no_earlier_than": "2026-01-01"}}],
    )
    case = report["cases"][0]
    assert case["checks"][0]["actual"] is True
    assert case["checks"][1]["actual"] is None  # no shortfall at all → passes
    assert case["passed"] is True


def test_deterministic_report():
    cases = [{"name": "c", "delta": {"operations": [{"op": "resize_amount",
                                                     "flow_id": "rent", "factor": 0.5}]},
              "expect": {"final_balance_at_least": 600.0, "min_balance_at_most": 700.0}}]
    first = run_validation({"main": 1000.0}, [rent_flow()], START, 30, cases=cases)
    second = run_validation({"main": 1000.0}, [rent_flow()], START, 30, cases=cases)
    assert first == second
    assert first["cases"][0]["passed"] is True


def test_unknown_predicate_raises():
    with pytest.raises(ForecastError, match="unknown expectation predicate"):
        run_validation({"main": 100.0}, [], START, 10,
                       cases=[{"name": "x", "delta": None, "expect": {"no_debt": True}}])
    with pytest.raises(ForecastError, match="name"):
        run_validation({"main": 100.0}, [], START, 10, cases=[{"delta": None, "expect": {}}])

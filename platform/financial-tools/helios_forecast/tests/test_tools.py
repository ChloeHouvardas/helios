"""D7 tests: tool registry + dispatcher (interfaces.md §6 contract)."""
import pytest

from helios_forecast import ForecastError, call_tool, get_tool, list_tools

START = "2026-01-01"

OPENING = [{"account": "main", "balance": 500.0}]
RENT = {
    "id": "rent",
    "direction": "outflow",
    "amount": 800.0,
    "date": {"due": "2026-01-01"},
}

_FORBIDDEN = {"$ref", "if", "then", "else", "not", "contains", "uniqueItems", "additionalProperties"}


# --- registry contract ------------------------------------------------------


def test_list_tools_follows_contract():
    tools = list_tools()
    assert [t["name"] for t in tools] == [
        "forecast_project", "forecast_simulate", "forecast_scenario",
        "behavior_profile", "promise_rate",
    ]
    for t in tools:
        assert isinstance(t["name"], str) and t["description"]
        assert t["input_schema"]["type"] == "object"
        assert t["output_schema"]["type"] == "object"
        assert t["side_effect"] is False  # read-only: runs directly, still action-logged


def test_schemas_stay_in_gbnf_safe_subset():
    def walk(node):
        if isinstance(node, dict):
            for key in node:
                assert key not in _FORBIDDEN, f"schema key {key!r} outside the T2 subset"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for t in list_tools():
        walk(t["input_schema"])
        walk(t["output_schema"])


def test_get_tool_and_unknown():
    assert get_tool("forecast_project")["name"] == "forecast_project"
    with pytest.raises(ForecastError, match="unknown tool"):
        get_tool("nope")
    with pytest.raises(ForecastError, match="unknown tool"):
        call_tool("nope", {})


# --- dispatch ---------------------------------------------------------------


def test_call_forecast_project():
    out = call_tool("forecast_project", {
        "opening": OPENING, "flows": [RENT], "start": START, "days": 30,
    })
    assert out["final_balance"] == pytest.approx(-300.0)
    assert out["min_balance"] == pytest.approx(-300.0)
    assert out["first_shortfall"] == START
    assert out["expected_outflows"] == pytest.approx(800.0)
    assert len(out["total"]) == 30
    assert out["inputs_hash"]


def test_call_forecast_project_amount_distribution():
    pay = {"id": "pay", "direction": "inflow",
           "amount_distribution": [[100.0, 1.0], [200.0, 3.0]],
           "date": {"due": "2026-01-02"}}
    out = call_tool("forecast_project", {
        "opening": OPENING, "flows": [pay], "start": START, "days": 5,
    })
    assert out["final_balance"] == pytest.approx(500.0 + 100.0 * 0.25 + 200.0 * 0.75)


def test_call_forecast_simulate():
    args = {"opening": OPENING, "flows": [RENT], "start": START, "days": 10,
            "n_paths": 200, "seed": 9}
    out = call_tool("forecast_simulate", args)
    assert out["n_paths"] == 200 and out["seed"] == 9
    assert [p["level"] for p in out["percentiles"]] == [5, 50, 95]
    assert out["percentiles"][1]["values"][0] == pytest.approx(-300.0)
    assert out["min_balance_percentiles"][0]["level"] == 5
    assert out["probability_below"] == pytest.approx(1.0)
    assert out["first_shortfall_median_date"] == START
    assert call_tool("forecast_simulate", args) == out  # seeded reproducibility


def test_call_forecast_simulate_common_shock():
    out = call_tool("forecast_simulate", {
        "opening": OPENING, "flows": [RENT], "start": START, "days": 10,
        "n_paths": 50, "seed": 1, "common_shock": {"offset_days": [[7, 1.0]]},
    })
    # Degenerate +7 shock: rent (due day 0) lands on day 7 of every path.
    assert out["percentiles"][1]["values"][0] == pytest.approx(500.0)
    assert out["percentiles"][1]["values"][7] == pytest.approx(-300.0)


def test_call_forecast_scenario():
    out = call_tool("forecast_scenario", {
        "opening": OPENING, "flows": [RENT], "start": START, "days": 30,
        "delta": {"operations": [{"op": "resize_amount", "flow_id": "rent", "factor": 0.5}]},
    })
    assert out["baseline_final_balance"] == pytest.approx(-300.0)
    assert out["scenario_final_balance"] == pytest.approx(100.0)
    assert out["final_balance_change"] == pytest.approx(400.0)
    assert out["baseline_first_shortfall"] == START
    assert out["scenario_first_shortfall"] is None
    assert out["scenario_resolves_baseline_shortfall"] is True


def test_call_forecast_scenario_add_flow():
    out = call_tool("forecast_scenario", {
        "opening": OPENING, "flows": [RENT], "start": START, "days": 30,
        "delta": {"operations": [
            {"op": "add_flow", "flow": {"id": "credit", "direction": "inflow",
                                        "amount": 800.0, "date": {"due": "2026-01-01"}}},
        ]},
    })
    assert out["scenario_final_balance"] == pytest.approx(500.0)
    assert out["scenario_resolves_baseline_shortfall"] is True


def test_call_behavior_profile():
    out = call_tool("behavior_profile", {
        "records": [{"customer_id": "c1", "segment": "retail",
                     "due": "2026-09-01", "paid": "2026-09-08"}],
        "customer_id": "c1", "segment": "retail",
    })
    assert out["level"] == "customer"
    assert out["n"] == 1
    assert out["offset_days"] == [[7, 1.0]]
    # Cold start: no history anywhere → conservative default rung.
    cold = call_tool("behavior_profile", {
        "records": [], "customer_id": "new", "segment": "retail",
    })
    assert cold["level"] == "default"
    assert cold["n"] == 0


# --- round-trip: service state -> tool call ---------------------------------
#
# Regression coverage for a real bug: ``flow_to_dict`` emits ``"date":
# "YYYY-MM-DD"`` for an on-time flow, which the tool adapter did not accept.
# That broke any app reading ``GET /businesses/{id}`` from the managed service
# and passing the flows straight to ``call_tool`` — the exact path a generated
# app is expected to take.


def test_call_tool_accepts_flow_to_dict_output():
    """The service's own serialization must be valid tool input."""
    from helios_forecast import flow_to_dict, parse_flow

    for raw in (
        {"id": "rent", "direction": "outflow", "amount": 800.0,
         "date": "2026-01-01"},
        {"id": "inv", "direction": "inflow",
         "amount": {"distribution": [[100.0, 1.0], [200.0, 3.0]]},
         "date": {"due": "2026-01-02", "offset_days": [[0, 0.5], [7, 0.5]]}},
        {"id": "monthly", "direction": "outflow", "amount": 50.0,
         "date": "2026-01-01", "recurrence": {"freq": "monthly", "count": 3}},
    ):
        flow = flow_to_dict(parse_flow(raw))
        out = call_tool("forecast_project", {
            "opening": OPENING, "flows": [flow], "start": START, "days": 30,
        })
        assert out["inputs_hash"], f"round-trip failed for {raw['id']!r}"


def test_flow_to_dict_string_date_matches_object_date():
    """``"date": "2026-01-01"`` and ``{"due": "2026-01-01"}`` agree exactly."""
    from helios_forecast import flow_to_dict, parse_flow

    string_form = flow_to_dict(parse_flow(
        {"id": "rent", "direction": "outflow", "amount": 800.0, "date": "2026-01-01"}))
    object_form = flow_to_dict(parse_flow(
        {"id": "rent", "direction": "outflow", "amount": 800.0,
         "date": {"due": "2026-01-01"}}))

    a = call_tool("forecast_project", {
        "opening": OPENING, "flows": [string_form], "start": START, "days": 30})
    b = call_tool("forecast_project", {
        "opening": OPENING, "flows": [object_form], "start": START, "days": 30})
    assert a == b


def test_scenario_accepts_flow_to_dict_output():
    """The scenario path enriches flows, then round-trips them through the tool."""
    from helios_forecast import flow_to_dict, parse_flow

    flows = [flow_to_dict(parse_flow(f)) for f in (
        {"id": "rent", "direction": "outflow", "amount": 800.0, "date": "2026-01-01"},
        {"id": "inv", "direction": "inflow", "amount": 1000.0, "date": "2026-01-15"},
    )]
    out = call_tool("forecast_scenario", {
        "opening": OPENING, "flows": flows, "start": START, "days": 30,
        "delta": {"operations": [{"op": "shift_date", "flow_id": "inv", "days": 7}]},
    })
    assert out["final_balance_change"] == pytest.approx(0.0)  # shifted within horizon


def test_missing_date_raises_forecast_error_not_type_error():
    """Bad input must name the field, not surface a raw TypeError."""
    with pytest.raises(ForecastError, match="date"):
        call_tool("forecast_project", {
            "opening": OPENING, "days": 5, "start": START,
            "flows": [{"id": "x", "direction": "outflow", "amount": 1.0}],
        })


def test_missing_amount_raises_forecast_error():
    with pytest.raises(ForecastError, match="amount"):
        call_tool("forecast_project", {
            "opening": OPENING, "days": 5, "start": START,
            "flows": [{"id": "x", "direction": "outflow", "date": "2026-01-01"}],
        })


def test_call_promise_rate():
    out = call_tool("promise_rate", {
        "records": [
            {"customer_id": "c1", "segment": "retail", "kept": True},
            {"customer_id": "c1", "segment": "retail", "kept": True},
            {"customer_id": "c2", "segment": "retail", "kept": False},
        ],
        "customer_id": "c1", "segment": "retail",
    })
    # (2 kept + k=2 × segment rate 2/3) / (2 + 2) = 5/6
    assert out["promise_kept"] == pytest.approx(5.0 / 6.0)
    assert out["n"] == 2

"""Helios forecasting & simulation SDK (P0).

Deterministic cash-flow projection, seeded Monte Carlo, and scenario deltas.
Pure functions over JSON-compatible inputs; same inputs give the same outputs
everywhere, so audit agents can re-run any forecast to verify it.

Quick example::

    from datetime import date
    from helios_forecast import parse_flow, project, simulate, probability_below

    flows = [
        parse_flow({"id": "rent", "direction": "outflow", "amount": 1200.0,
                    "date": "2026-11-01", "recurrence": {"freq": "monthly", "count": 3}}),
        parse_flow({"id": "inv-042", "direction": "inflow", "amount": 5000.0,
                    "date": {"due": "2026-11-10",
                             "offset_days": [[0, 0.3], [7, 0.4], [21, 0.3]]}}),
    ]
    proj = project({"main": 3000.0}, flows, date(2026, 11, 1), 90)
    mc = simulate({"main": 3000.0}, flows, date(2026, 11, 1), 90, n_paths=2000, seed=42)
    probability_below(mc, threshold=0.0)
"""
from .model import (
    AmountSpec,
    DateSpec,
    ENGINE_VERSION,
    Flow,
    FlowInstance,
    ForecastError,
    INFLOW,
    OUTFLOW,
    Recurrence,
    expand_flows,
    flow_to_dict,
    parse_flow,
    state_hash,
)
from .engine import (
    MonteCarloForecast,
    Projection,
    balance_at,
    expected_total,
    first_shortfall,
    first_shortfall_stats,
    min_balance,
    min_balance_percentiles,
    probability_below,
    project,
    simulate,
)
from .scenarios import Delta, apply_delta, compare, run_scenario
from .calibration import conformal_half_width, empirical_coverage
from .tools import TOOLS, call_tool, get_tool, list_tools
from .validation import run_validation
from .behavior import (
    PaymentRecord,
    PromiseRecord,
    customer_offset_profiles,
    offset_profile_for,
    parse_payment_record,
    parse_promise_record,
    promise_profiles,
    promise_rate_for,
    segment_offset_profiles,
)

__all__ = [
    "ENGINE_VERSION",
    "AmountSpec",
    "DateSpec",
    "Flow",
    "FlowInstance",
    "ForecastError",
    "INFLOW",
    "OUTFLOW",
    "Recurrence",
    "expand_flows",
    "flow_to_dict",
    "parse_flow",
    "state_hash",
    "Projection",
    "MonteCarloForecast",
    "project",
    "simulate",
    "balance_at",
    "min_balance",
    "first_shortfall",
    "expected_total",
    "probability_below",
    "first_shortfall_stats",
    "min_balance_percentiles",
    "Delta",
    "apply_delta",
    "compare",
    "run_scenario",
    "conformal_half_width",
    "empirical_coverage",
    "TOOLS",
    "call_tool",
    "get_tool",
    "list_tools",
    "run_validation",
    "PaymentRecord",
    "PromiseRecord",
    "customer_offset_profiles",
    "offset_profile_for",
    "parse_payment_record",
    "parse_promise_record",
    "promise_profiles",
    "promise_rate_for",
    "segment_offset_profiles",
]

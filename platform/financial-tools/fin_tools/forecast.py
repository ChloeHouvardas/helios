"""Forecasting family (`forecast.*`, `behavior.*`) over the imported `helios_forecast`.

`helios_forecast` is a deterministic, seeded cash-flow engine that speaks FLOATS.
Helios money is integer cents and must never be a float in our own layer, so this
module is the conversion boundary: cents in, converted to float only at the engine
call, results rounded half-even back to cents. The engine stays untouched.

Conversion is field-specific, never a blind numeric walk. A flow mixes money with
non-money numbers that must NOT be scaled:

    amount              money (cents)          -> float units
    amount_distribution [[value, weight], ...]  value is money, weight is not
    date.offset_days    [[days, weight], ...]   day counts + weights, never money
    probability         0..1                    never money
    delta resize factor a multiplier            never money

Scaling a weight or a day count by 100 would silently corrupt the forecast, so each
converter below names the fields it touches.

All tools are read-only (`side_effects=False`): forecasting never mutates state.
Determinism is inherited from the engine (same seed -> identical result) and
preserved here because cents->float->cents is exact for integer-cent inputs.

The `helios_forecast` imports are lazy so importing this module (and the registry)
does not pull in numpy until a forecast tool actually runs.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Dict, List, Optional, Tuple

from .contracts import REGISTRY

# --------------------------------------------------------------------------
# money conversion (the whole reason this module exists)
# --------------------------------------------------------------------------


def to_engine_units(cents: Any) -> float:
    """Integer cents -> the float unit the engine expects. Exact for ints."""
    return float(Decimal(int(cents)) / 100)


def to_cents(value: Any) -> int:
    """Engine float -> integer cents, rounded half-even (banker's rounding).

    `Decimal(repr(...))` avoids the binary-float surprises that a naive
    `round(x * 100)` can produce; ROUND_HALF_EVEN is the tie-break the brief
    asks for and keeps repeated conversions from drifting.
    """
    return int((Decimal(repr(float(value))) * 100).quantize(
        Decimal(1), rounding=ROUND_HALF_EVEN))


def _as_date(raw: Any, field_name: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except (ValueError, TypeError):
        raise ValueError(
            f"{field_name} must be an ISO date like '2026-11-01', got {raw!r}")


# --------------------------------------------------------------------------
# input converters: cents -> engine floats, field by field
# --------------------------------------------------------------------------


def _opening_to_engine(opening: Any) -> Dict[str, float]:
    if not isinstance(opening, list):
        raise ValueError(
            f"opening must be a list of {{account, balance_cents}} pairs, got "
            f"{opening!r}")
    out: Dict[str, float] = {}
    for pair in opening:
        out[str(pair["account"])] = to_engine_units(pair["balance_cents"])
    return out


def _flow_to_engine_raw(flow: Dict[str, Any]) -> Dict[str, Any]:
    """One cents-shaped flow -> the tool-shape raw dict, money scaled to float.

    Only `amount` and the value slot of `amount_distribution` are money. Every
    other numeric field (offset_days, probability, recurrence counts) is copied
    through untouched.
    """
    out = dict(flow)
    if "amount_cents" in out:
        out["amount"] = to_engine_units(out.pop("amount_cents"))
    elif "amount_distribution_cents" in out:
        # [[cents, weight], ...] -> [[float units, weight], ...]
        out["amount_distribution"] = [
            [to_engine_units(v), w] for v, w in out.pop("amount_distribution_cents")]
    return out


def _flows_to_engine(flows: Any) -> List[Dict[str, Any]]:
    if not isinstance(flows, list):
        raise ValueError(f"flows must be a list, got {flows!r}")
    return [_flow_to_engine_raw(f) for f in flows]


def _delta_to_engine(delta: Any) -> Dict[str, Any]:
    """A cents-shaped scenario delta -> engine shape, scaling only money ops."""
    if not isinstance(delta, dict):
        raise ValueError(f"delta must be an object with 'operations', got {delta!r}")
    ops: List[Dict[str, Any]] = []
    for op in delta.get("operations", []):
        op = dict(op)
        kind = op.get("op")
        if kind == "set_amount" and "amount_cents" in op:
            op["amount"] = to_engine_units(op.pop("amount_cents"))
        elif kind == "add_flow" and "flow" in op:
            op["flow"] = _flow_to_engine_raw(op["flow"])
        # shift_date.days, resize_amount.factor, set_probability.probability are
        # NOT money and are left exactly as given.
        ops.append(op)
    return {"operations": ops}


def _window(raw: Any) -> Optional[Tuple[date, date]]:
    if not raw:
        return None
    return (_as_date(raw["from"], "window.from"), _as_date(raw["to"], "window.to"))


def _engine_inputs(opening: Any, flows: Any, start: Any, days: Any):
    """Parse cents inputs into engine objects via the teammate's own parsers.

    `_parse_flows` is the same helper `helios_forecast.call_tool` uses, so a
    query built here parses flows identically to a projection -- importing it
    (rather than re-implementing flow parsing) keeps the two paths from
    drifting. `_opening_to_engine` already returns the `{account: float}` dict
    the engine wants, so the opening needs no further translation.
    """
    from helios_forecast.tools import _parse_flows

    return (_opening_to_engine(opening),
            _parse_flows(_flows_to_engine(flows)),
            _as_date(start, "start"), int(days))


# --------------------------------------------------------------------------
# forecast.project
# --------------------------------------------------------------------------


def forecast_project(*, opening: Any, flows: Any, start: Any, days: Any) -> Dict[str, Any]:
    from helios_forecast import first_shortfall, min_balance, project

    opening_e, flows_e, start_d, days_i = _engine_inputs(opening, flows, start, days)
    proj = project(opening_e, flows_e, start_d, days_i)
    mb = min_balance(proj)
    fs = first_shortfall(proj)
    return {
        "days": proj.days,
        "final_balance_cents": to_cents(proj.total[-1]),
        "min_balance_cents": to_cents(mb["value"]),
        "min_balance_date": mb["date"],
        "first_shortfall": fs.isoformat() if fs else None,
        "expected_inflows_cents": to_cents(sum(proj.inflows)),
        "expected_outflows_cents": to_cents(sum(proj.outflows)),
        # Per-day expected magnitudes. `total_cents` is a running BALANCE; these
        # are the flows behind it, which is what a cash-in curve needs.
        "inflows_cents": [to_cents(v) for v in proj.inflows],
        "outflows_cents": [to_cents(v) for v in proj.outflows],
        "total_cents": [to_cents(v) for v in proj.total],
        "inputs_hash": proj.metadata["inputs_hash"],
    }


# --------------------------------------------------------------------------
# forecast.simulate
# --------------------------------------------------------------------------

#: The `common_shock` symmetry caveat (the teammates' decision-log D12), stated
#: on the tool so a model reading the catalog cannot miss it.
COMMON_SHOCK_CAVEAT = (
    "common_shock is SYMMETRIC: it shifts every flow's date in a path, inflows "
    "AND contractual outflows alike. It is a timing shift, not inherently adverse "
    "-- a large symmetric shock can LOWER shortfall risk because delayed rent or "
    "payroll helps. To model a real squeeze (customers pay late while bills stay "
    "fixed) shift the inflow dates yourself via a scenario delta instead of "
    "relying on common_shock.")


def forecast_simulate(*, opening: Any, flows: Any, start: Any, days: Any,
                      n_paths: int = 2000, seed: int = 0,
                      threshold_cents: int = 0,
                      percentile_levels: Optional[List[int]] = None,
                      common_shock: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from helios_forecast import (first_shortfall_stats, min_balance_percentiles,
                                 probability_below, simulate)

    opening_e, flows_e, start_d, days_i = _engine_inputs(opening, flows, start, days)
    levels = tuple(percentile_levels or (5, 50, 95))
    threshold = to_engine_units(threshold_cents)
    mc = simulate(opening_e, flows_e, start_d, days_i, n_paths=int(n_paths),
                  seed=int(seed), percentile_levels=levels,
                  common_shock=common_shock)
    fss = first_shortfall_stats(mc, threshold)
    mbp = min_balance_percentiles(mc)
    return {
        "days": mc.days,
        "n_paths": mc.n_paths,
        "seed": mc.seed,
        # Percentile bands are balances -> money -> cents.
        "percentiles": [{"level": q, "values": [to_cents(v) for v in mc.percentiles[f"p{q}"]]}
                        for q in levels],
        "min_balance_percentiles_cents": [
            {"level": int(k[1:]), "value_cents": to_cents(v)}
            for k, v in sorted(mbp.items())],
        # A probability is 0..1, not money; keep it a float.
        "probability_below": probability_below(mc, threshold),
        "first_shortfall_probability": fss["probability"],
        "first_shortfall_median_date": fss["median_date"],
        "threshold_cents": int(threshold_cents),
        "inputs_hash": mc.metadata["inputs_hash"],
    }


# --------------------------------------------------------------------------
# forecast.scenario
# --------------------------------------------------------------------------


def forecast_scenario(*, opening: Any, flows: Any, delta: Any, start: Any,
                      days: Any) -> Dict[str, Any]:
    from helios_forecast import Delta, run_scenario

    opening_e, flows_e, start_d, days_i = _engine_inputs(opening, flows, start, days)
    parsed = Delta.parse(_delta_to_engine(delta))
    _baseline, _scenario, diff = run_scenario(opening_e, flows_e, parsed, start_d, days_i)
    return {
        "baseline_final_balance_cents": to_cents(diff["final_balance"]["baseline"]),
        "scenario_final_balance_cents": to_cents(diff["final_balance"]["scenario"]),
        "final_balance_change_cents": to_cents(diff["final_balance"]["change"]),
        "baseline_min_balance_cents": to_cents(diff["min_balance"]["baseline"]),
        "scenario_min_balance_cents": to_cents(diff["min_balance"]["scenario"]),
        "baseline_first_shortfall": diff["first_shortfall"]["baseline"],
        "scenario_first_shortfall": diff["first_shortfall"]["scenario"],
        "scenario_resolves_baseline_shortfall": diff["first_shortfall"].get(
            "scenario_resolves_baseline_shortfall"),
        "inputs_hash": _scenario.metadata["inputs_hash"],
    }


# --------------------------------------------------------------------------
# forecast.query — the five read queries behind one tool
# --------------------------------------------------------------------------

#: Which queries need a Monte Carlo run rather than a single projection.
_MC_QUERIES = frozenset({"probability_below"})


def forecast_query(*, query: str, opening: Any, flows: Any, start: Any, days: Any,
                   on: Optional[str] = None, threshold_cents: int = 0,
                   direction: Optional[str] = None, window: Optional[Dict[str, str]] = None,
                   n_paths: int = 2000, seed: int = 0) -> Dict[str, Any]:
    from helios_forecast import (balance_at, expected_total, first_shortfall,
                                 min_balance, probability_below, project, simulate)

    opening_e, flows_e, start_d, days_i = _engine_inputs(opening, flows, start, days)
    win = _window(window)

    if query == "balance_at":
        if not on:
            raise ValueError("forecast.query 'balance_at' needs 'on' (an ISO date)")
        value = balance_at(project(opening_e, flows_e, start_d, days_i),
                           _as_date(on, "on"))
        return {"query": query, "balance_cents": to_cents(value)}

    if query == "min_balance":
        mb = min_balance(project(opening_e, flows_e, start_d, days_i), win)
        return {"query": query, "min_balance_cents": to_cents(mb["value"]),
                "date": mb["date"]}

    if query == "first_shortfall":
        fs = first_shortfall(project(opening_e, flows_e, start_d, days_i),
                             to_engine_units(threshold_cents), win)
        return {"query": query, "first_shortfall": fs.isoformat() if fs else None,
                "threshold_cents": int(threshold_cents)}

    if query == "expected_total":
        if direction not in ("inflow", "outflow"):
            raise ValueError(
                f"forecast.query 'expected_total' needs direction 'inflow' or "
                f"'outflow', got {direction!r}")
        total = expected_total(project(opening_e, flows_e, start_d, days_i),
                               direction, win)
        return {"query": query, "direction": direction,
                "expected_total_cents": to_cents(total)}

    if query == "probability_below":
        mc = simulate(opening_e, flows_e, start_d, days_i, n_paths=int(n_paths),
                      seed=int(seed))
        prob = probability_below(mc, to_engine_units(threshold_cents), win)
        return {"query": query, "probability_below": prob,
                "threshold_cents": int(threshold_cents)}

    raise ValueError(
        f"unknown forecast query {query!r}; expected one of balance_at, "
        f"min_balance, first_shortfall, expected_total, probability_below")


# --------------------------------------------------------------------------
# behavior.* — no money, so no conversion; delegate to the teammate's tools
# --------------------------------------------------------------------------


def behavior_profile(*, records: Any, customer_id: str, segment: str,
                     prior_strength: float = 5.0) -> Dict[str, Any]:
    from helios_forecast import call_tool

    return call_tool("behavior_profile", {
        "records": records, "customer_id": customer_id, "segment": segment,
        "prior_strength": prior_strength})


def behavior_promise_rate(*, records: Any, customer_id: str, segment: str,
                          prior_strength: float = 2.0) -> Dict[str, Any]:
    from helios_forecast import call_tool

    return call_tool("promise_rate", {
        "records": records, "customer_id": customer_id, "segment": segment,
        "prior_strength": prior_strength})


# --------------------------------------------------------------------------
# JSON Schemas (cents). Lifted from the teammate's registry shape, with every
# money field retyped to integer cents so the auditor validates the unit we use.
# --------------------------------------------------------------------------

_OPENING = {
    "type": "array",
    "description": "Opening balances, one pair per account, in integer cents.",
    "items": {"type": "object",
              "properties": {"account": {"type": "string"},
                             "balance_cents": {"type": "integer"}},
              "required": ["account", "balance_cents"]},
}

_FLOW_ITEM = {
    "type": "object",
    "description": "One cash flow. Money is integer cents: use amount_cents for "
                   "a fixed amount, or amount_distribution_cents as "
                   "[[cents, weight], ...] for uncertainty. date.offset_days is "
                   "[[days, weight], ...] and is NOT money.",
    "properties": {
        "id": {"type": "string"},
        "account": {"type": "string"},
        "direction": {"type": "string", "enum": ["inflow", "outflow"]},
        "amount_cents": {"type": "integer"},
        "amount_distribution_cents": {
            "type": "array", "items": {"type": "array", "items": {"type": "number"}}},
        "date": {"type": "object",
                 "properties": {
                     "due": {"type": "string"},
                     "offset_days": {"type": "array",
                                     "items": {"type": "array",
                                               "items": {"type": "number"}}}},
                 "required": ["due"]},
        "probability": {"type": "number"},
        "recurrence": {"type": "object",
                       "properties": {"freq": {"type": "string",
                                               "enum": ["daily", "weekly", "monthly"]},
                                      "count": {"type": "integer"},
                                      "until": {"type": "string"}},
                       "required": ["freq"]},
        "customer_id": {"type": "string"},
        "segment": {"type": "string"},
        "source": {"type": "string"},
    },
    "required": ["id", "direction", "date"],
}

_DELTA = {
    "type": "object",
    "description": "Declarative scenario. set_amount uses amount_cents; "
                   "resize_amount.factor and shift_date.days are not money.",
    "properties": {"operations": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "op": {"type": "string",
                   "enum": ["add_flow", "remove_flow", "shift_date",
                            "resize_amount", "set_probability", "set_amount"]},
            "flow_id": {"type": "string"},
            "days": {"type": "integer"},
            "factor": {"type": "number"},
            "amount_cents": {"type": "integer"},
            "probability": {"type": "number"},
            "flow": _FLOW_ITEM,
        },
        "required": ["op"]}}},
    "required": ["operations"],
}

_BASE_PROPS = {"opening": _OPENING, "flows": {"type": "array", "items": _FLOW_ITEM},
               "start": {"type": "string"}, "days": {"type": "integer", "minimum": 1}}

_PROJECT_SCHEMA = {"type": "object", "properties": dict(_BASE_PROPS),
                   "required": ["opening", "flows", "start", "days"]}

_SIMULATE_SCHEMA = {"type": "object", "properties": {
    **_BASE_PROPS,
    "n_paths": {"type": "integer", "minimum": 1},
    "seed": {"type": "integer"},
    "threshold_cents": {"type": "integer"},
    "percentile_levels": {"type": "array", "items": {"type": "integer"}},
    "common_shock": {"type": "object",
                     "description": COMMON_SHOCK_CAVEAT,
                     "properties": {"offset_days": {
                         "type": "array",
                         "items": {"type": "array", "items": {"type": "number"}}}},
                     "required": ["offset_days"]},
}, "required": ["opening", "flows", "start", "days"]}

_SCENARIO_SCHEMA = {"type": "object", "properties": {
    **_BASE_PROPS, "delta": _DELTA},
    "required": ["opening", "flows", "delta", "start", "days"]}

_QUERY_SCHEMA = {"type": "object", "properties": {
    "query": {"type": "string",
              "enum": ["balance_at", "min_balance", "first_shortfall",
                       "expected_total", "probability_below"]},
    **_BASE_PROPS,
    "on": {"type": ["string", "null"], "description": "balance_at: the ISO date."},
    "threshold_cents": {"type": "integer"},
    "direction": {"type": ["string", "null"], "enum": ["inflow", "outflow", None]},
    "window": {"type": ["object", "null"],
               "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
               "required": ["from", "to"]},
    "n_paths": {"type": "integer", "minimum": 1},
    "seed": {"type": "integer"},
}, "required": ["query", "opening", "flows", "start", "days"]}

_RECORDS_PAYMENT = {"type": "array", "items": {
    "type": "object",
    "properties": {"customer_id": {"type": "string"}, "segment": {"type": "string"},
                   "due": {"type": "string"}, "paid": {"type": "string"}},
    "required": ["customer_id", "segment", "due", "paid"]}}

_RECORDS_PROMISE = {"type": "array", "items": {
    "type": "object",
    "properties": {"customer_id": {"type": "string"}, "segment": {"type": "string"},
                   "kept": {"type": "boolean"}},
    "required": ["customer_id", "segment", "kept"]}}

_PROFILE_SCHEMA = {"type": "object", "properties": {
    "records": _RECORDS_PAYMENT, "customer_id": {"type": "string"},
    "segment": {"type": "string"}, "prior_strength": {"type": "number"}},
    "required": ["records", "customer_id", "segment"]}

_PROMISE_SCHEMA = {"type": "object", "properties": {
    "records": _RECORDS_PROMISE, "customer_id": {"type": "string"},
    "segment": {"type": "string"}, "prior_strength": {"type": "number"}},
    "required": ["records", "customer_id", "segment"]}


def register(registry=REGISTRY) -> None:
    """Register the read-only forecasting and behavior tools.

    Stateless: these wrap pure engine functions, so no instance is bound and two
    bundles cannot interfere. All are `side_effects=False`.
    """

    @registry.tool(
        "forecast.project",
        "Deterministic expected-value cash projection over a daily grid. Money is "
        "integer cents in and out. Read-only, reproducible.",
        {"opening": "list[dict]", "flows": "list[dict]", "start": "str", "days": "int"},
        {"days": "int", "final_balance_cents": "int", "min_balance_cents": "int",
         "first_shortfall": "str|None", "total_cents": "list[int]",
         "inputs_hash": "str"},
        side_effects=False, tags=("forecast",), input_json_schema=_PROJECT_SCHEMA,
    )
    def _project(opening: List[Dict[str, Any]], flows: List[Dict[str, Any]],
                 start: str, days: int) -> Dict[str, Any]:
        return forecast_project(opening=opening, flows=flows, start=start, days=days)

    @registry.tool(
        "forecast.simulate",
        "Seeded Monte Carlo forecast: percentile bands, probability the balance "
        "dips below a threshold, first-shortfall statistics. Money is integer "
        "cents; the same seed gives identical results. Read-only. " + COMMON_SHOCK_CAVEAT,
        {"opening": "list[dict]", "flows": "list[dict]", "start": "str", "days": "int",
         "n_paths": "int|None", "seed": "int|None", "threshold_cents": "int|None",
         "percentile_levels": "list[int]|None", "common_shock": "dict|None"},
        {"days": "int", "n_paths": "int", "seed": "int",
         "probability_below": "float", "inputs_hash": "str"},
        side_effects=False, tags=("forecast",), input_json_schema=_SIMULATE_SCHEMA,
    )
    def _simulate(opening: List[Dict[str, Any]], flows: List[Dict[str, Any]],
                  start: str, days: int, n_paths: int = 2000, seed: int = 0,
                  threshold_cents: int = 0,
                  percentile_levels: Optional[List[int]] = None,
                  common_shock: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return forecast_simulate(opening=opening, flows=flows, start=start, days=days,
                                 n_paths=n_paths, seed=seed,
                                 threshold_cents=threshold_cents,
                                 percentile_levels=percentile_levels,
                                 common_shock=common_shock)

    @registry.tool(
        "forecast.scenario",
        "What-if analysis: apply a declarative delta (shift a payment, resize an "
        "amount, add or remove a flow) and compare min/final balance and first "
        "shortfall against the baseline. Money is integer cents. Read-only.",
        {"opening": "list[dict]", "flows": "list[dict]", "delta": "dict",
         "start": "str", "days": "int"},
        {"baseline_final_balance_cents": "int", "scenario_final_balance_cents": "int",
         "final_balance_change_cents": "int", "inputs_hash": "str"},
        side_effects=False, tags=("forecast",), input_json_schema=_SCENARIO_SCHEMA,
    )
    def _scenario(opening: List[Dict[str, Any]], flows: List[Dict[str, Any]],
                  delta: Dict[str, Any], start: str, days: int) -> Dict[str, Any]:
        return forecast_scenario(opening=opening, flows=flows, delta=delta,
                                 start=start, days=days)

    @registry.tool(
        "forecast.query",
        "One read query over a projection: balance_at, min_balance, "
        "first_shortfall, expected_total, or probability_below (the last runs a "
        "seeded Monte Carlo). Money is integer cents. Read-only.",
        {"query": "str", "opening": "list[dict]", "flows": "list[dict]",
         "start": "str", "days": "int", "on": "str|None",
         "threshold_cents": "int|None", "direction": "str|None",
         "window": "dict|None", "n_paths": "int|None", "seed": "int|None"},
        {"query": "str"},
        side_effects=False, tags=("forecast",), input_json_schema=_QUERY_SCHEMA,
    )
    def _query(query: str, opening: List[Dict[str, Any]],
               flows: List[Dict[str, Any]], start: str, days: int,
               on: Optional[str] = None, threshold_cents: int = 0,
               direction: Optional[str] = None,
               window: Optional[Dict[str, str]] = None,
               n_paths: int = 2000, seed: int = 0) -> Dict[str, Any]:
        return forecast_query(query=query, opening=opening, flows=flows, start=start,
                              days=days, on=on, threshold_cents=threshold_cents,
                              direction=direction, window=window, n_paths=n_paths,
                              seed=seed)

    @registry.tool(
        "behavior.profile",
        "Empirical-Bayes payment-timing profile for a customer, shrunk toward "
        "its segment then the global default. Returns an offset_days "
        "distribution ready to feed a flow. Read-only, no money.",
        {"records": "list[dict]", "customer_id": "str", "segment": "str",
         "prior_strength": "float|None"},
        {"offset_days": "list[list]", "n": "int", "level": "str"},
        side_effects=False, tags=("behavior",), input_json_schema=_PROFILE_SCHEMA,
    )
    def _profile(records: List[Dict[str, Any]], customer_id: str, segment: str,
                 prior_strength: float = 5.0) -> Dict[str, Any]:
        return behavior_profile(records=records, customer_id=customer_id,
                                segment=segment, prior_strength=prior_strength)

    @registry.tool(
        "behavior.promise_rate",
        "Beta-Binomial promise-keeping rate for a customer, shrunk toward its "
        "segment. Read-only, no money.",
        {"records": "list[dict]", "customer_id": "str", "segment": "str",
         "prior_strength": "float|None"},
        {"promise_kept": "float", "n": "int", "level": "str"},
        side_effects=False, tags=("behavior",), input_json_schema=_PROMISE_SCHEMA,
    )
    def _promise(records: List[Dict[str, Any]], customer_id: str, segment: str,
                 prior_strength: float = 2.0) -> Dict[str, Any]:
        return behavior_promise_rate(records=records, customer_id=customer_id,
                                     segment=segment, prior_strength=prior_strength)

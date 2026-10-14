"""Tool registry (D7 / P4): the forecasting engine as interfaces.md §6 tools.

Each descriptor declares name, description, input_schema, output_schema, and
side_effect — always False here: forecasting is read-only, so under the tool
contract (``specs/interfaces.md`` §6) calls run directly and are still written
to the append-only action log.

Schemas deliberately stay inside the GBNF-safe subset verified as claims T2
(``specs/forecasting-research-claims.md``): no ``$ref``, no conditionals, no
``additionalProperties``, no dynamic-key maps. Concretely, account balances
travel as ``[{"account", "balance"}]`` pairs, percentile bands as
``[{"level", "values"}]`` pairs, and flow dates as fixed ``{"due",
"offset_days"}`` objects — every object has a fixed key set, so llama.cpp /
xgrammar constrained decoding can enforce these schemas later (T1/T3).
"""
from __future__ import annotations

from .behavior import offset_profile_for, parse_payment_record, parse_promise_record, promise_rate_for
from .engine import first_shortfall, first_shortfall_stats, min_balance, min_balance_percentiles, probability_below, project, simulate
from .model import ForecastError, _as_date, parse_flow
from .scenarios import Delta, run_scenario

_OPENING_PAIRS = {
    "type": "array",
    "description": "Opening balances, one pair per account.",
    "items": {
        "type": "object",
        "properties": {
            "account": {"type": "string"},
            "balance": {"type": "number"},
        },
        "required": ["account", "balance"],
    },
}

_FLOW_ITEM = {
    "type": "object",
    "description": "One cash flow. Use 'amount' for a fixed amount, or "
    "'amount_distribution' as [[value, weight], ...] for uncertainty.",
    "properties": {
        "id": {"type": "string", "description": "Unique flow id (referenced by deltas)."},
        "account": {"type": "string", "description": "Account name; defaults to 'main'."},
        "direction": {"type": "string", "enum": ["inflow", "outflow"]},
        "amount": {"type": "number", "description": "Fixed non-negative amount."},
        "amount_distribution": {
            "type": "array",
            "description": "Discrete amount distribution [[value, weight], ...].",
            "items": {"type": "array", "items": {"type": "number"}},
        },
        "date": {
            "type": "object",
            "properties": {
                "due": {"type": "string", "description": "Due date, YYYY-MM-DD."},
                "offset_days": {
                    "type": "array",
                    "description": "Payment-timing distribution [[days, weight], ...]; "
                    "defaults to on-time.",
                    "items": {"type": "array", "items": {"type": "number"}},
                },
            },
            "required": ["due"],
        },
        "probability": {"type": "number", "description": "Probability the flow occurs (0..1)."},
        "recurrence": {
            "type": "object",
            "description": "Optional repeat rule.",
            "properties": {
                "freq": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
                "count": {"type": "integer"},
                "until": {"type": "string"},
            },
            "required": ["freq"],
        },
        "customer_id": {"type": "string", "description": "Paying counterparty (behaviour lookup)."},
        "segment": {"type": "string", "description": "Customer segment (behaviour lookup)."},
        "source": {"type": "string", "description": "Reference to the record this flow came from."},
    },
    "required": ["id", "direction", "date"],
}

_DELTA = {
    "type": "object",
    "description": "Declarative scenario: operations applied to the baseline.",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "description": "One operation. Fields beyond 'op' and 'flow_id' apply "
                "per operation type; extra ones are ignored.",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["add_flow", "remove_flow", "shift_date",
                                 "resize_amount", "set_probability", "set_amount"],
                    },
                    "flow_id": {"type": "string"},
                    "days": {"type": "integer", "description": "shift_date: days to move."},
                    "factor": {"type": "number", "description": "resize_amount: multiplier."},
                    "amount": {"type": "number", "description": "set_amount: new fixed amount."},
                    "probability": {"type": "number", "description": "set_probability: new probability."},
                    "flow": dict(_FLOW_ITEM, description="add_flow: the new flow."),
                },
                "required": ["op"],
            },
        }
    },
    "required": ["operations"],
}

TOOLS = [
    {
        "name": "forecast_project",
        "description": "Deterministic expected-value cash projection over a daily grid: "
        "opening balances plus flows in, balances out. Use for 'what will the balance "
        "be' questions that need exact, reproducible numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "opening": _OPENING_PAIRS,
                "flows": {"type": "array", "items": _FLOW_ITEM},
                "start": {"type": "string", "description": "First day of the horizon, YYYY-MM-DD."},
                "days": {"type": "integer", "minimum": 1, "description": "Horizon length in days."},
            },
            "required": ["opening", "flows", "start", "days"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer"},
                "final_balance": {"type": "number"},
                "min_balance": {"type": "number"},
                "min_balance_date": {"type": "string"},
                "first_shortfall": {"type": ["string", "null"], "description": "ISO date or null."},
                "expected_inflows": {"type": "number"},
                "expected_outflows": {"type": "number"},
                "total": {"type": "array", "items": {"type": "number"}, "description": "End-of-day total per day."},
                "inputs_hash": {"type": "string"},
            },
            "required": ["days", "final_balance", "min_balance", "inputs_hash"],
        },
        "side_effect": False,
    },
    {
        "name": "forecast_simulate",
        "description": "Seeded Monte Carlo forecast: percentile bands, probability of the "
        "balance dipping below a threshold, and first-shortfall statistics. "
        "Optionally a common_shock shifts all payment dates together on some paths "
        "(correlated macro stress). Same seed gives identical results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "opening": _OPENING_PAIRS,
                "flows": {"type": "array", "items": _FLOW_ITEM},
                "start": {"type": "string"},
                "days": {"type": "integer", "minimum": 1},
                "n_paths": {"type": "integer", "minimum": 1, "description": "Simulation paths; default 2000."},
                "seed": {"type": "integer", "description": "RNG seed; default 0."},
                "threshold": {"type": "number", "description": "Shortfall threshold; default 0."},
                "common_shock": {
                    "type": "object",
                    "description": "Optional shared stress: one offset drawn per path, "
                    "applied to every flow's date in that path.",
                    "properties": {
                        "offset_days": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "number"}},
                            "description": "[[days, weight], ...]",
                        }
                    },
                    "required": ["offset_days"],
                },
            },
            "required": ["opening", "flows", "start", "days"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer"},
                "n_paths": {"type": "integer"},
                "seed": {"type": "integer"},
                "percentiles": {
                    "type": "array",
                    "description": "One entry per requested level.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "level": {"type": "integer"},
                            "values": {"type": "array", "items": {"type": "number"}},
                        },
                        "required": ["level", "values"],
                    },
                },
                "min_balance_percentiles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "level": {"type": "integer"},
                            "value": {"type": "number"},
                        },
                        "required": ["level", "value"],
                    },
                },
                "probability_below": {"type": "number"},
                "first_shortfall_probability": {"type": "number"},
                "first_shortfall_median_date": {"type": ["string", "null"]},
                "inputs_hash": {"type": "string"},
            },
            "required": ["days", "n_paths", "seed", "probability_below", "inputs_hash"],
        },
        "side_effect": False,
    },
    {
        "name": "forecast_scenario",
        "description": "What-if analysis: apply a declarative delta (shift a payment, "
        "resize an amount, add or remove a flow) to the baseline and compare "
        "min balance / final balance / first shortfall.",
        "input_schema": {
            "type": "object",
            "properties": {
                "opening": _OPENING_PAIRS,
                "flows": {"type": "array", "items": _FLOW_ITEM},
                "delta": _DELTA,
                "start": {"type": "string"},
                "days": {"type": "integer", "minimum": 1},
            },
            "required": ["opening", "flows", "delta", "start", "days"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "baseline_final_balance": {"type": "number"},
                "scenario_final_balance": {"type": "number"},
                "final_balance_change": {"type": "number"},
                "baseline_min_balance": {"type": "number"},
                "scenario_min_balance": {"type": "number"},
                "baseline_first_shortfall": {"type": ["string", "null"]},
                "scenario_first_shortfall": {"type": ["string", "null"]},
                "scenario_resolves_baseline_shortfall": {"type": "boolean"},
                "inputs_hash": {"type": "string"},
            },
            "required": ["baseline_final_balance", "scenario_final_balance", "final_balance_change"],
        },
        "side_effect": False,
    },
    {
        "name": "behavior_profile",
        "description": "Payment-timing profile for a customer from their paid-invoice "
        "history, with segment/global fallbacks (empirical-Bayes shrinkage). "
        "The offset distribution slots into a flow's date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "records": {
                    "type": "array",
                    "description": "Past payments: customer, segment, due date, paid date.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "customer_id": {"type": "string"},
                            "segment": {"type": "string"},
                            "due": {"type": "string"},
                            "paid": {"type": "string"},
                        },
                        "required": ["customer_id", "segment", "due", "paid"],
                    },
                },
                "customer_id": {"type": "string"},
                "segment": {"type": "string"},
                "prior_strength": {"type": "number", "description": "Shrinkage pseudo-count; default 5."},
            },
            "required": ["records", "customer_id", "segment"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "offset_days": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                "n": {"type": "integer"},
                "level": {"type": "string", "description": "customer | segment | global | default"},
            },
            "required": ["offset_days", "n", "level"],
        },
        "side_effect": False,
    },
    {
        "name": "promise_rate",
        "description": "Promise-keeping rate for a customer from kept/broken promise "
        "records, with Beta-Binomial shrinkage toward the segment rate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "customer_id": {"type": "string"},
                            "segment": {"type": "string"},
                            "kept": {"type": "boolean"},
                        },
                        "required": ["customer_id", "segment", "kept"],
                    },
                },
                "customer_id": {"type": "string"},
                "segment": {"type": "string"},
                "prior_strength": {"type": "number", "description": "Shrinkage pseudo-count; default 2."},
            },
            "required": ["records", "customer_id", "segment"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "promise_kept": {"type": "number"},
                "n": {"type": "integer"},
                "level": {"type": "string"},
            },
            "required": ["promise_kept", "n"],
        },
        "side_effect": False,
    },
]

_BY_NAME = {t["name"]: t for t in TOOLS}


def list_tools() -> list:
    """All tool descriptors (the interfaces.md §6 contract)."""
    return [dict(t) for t in TOOLS]


def get_tool(name: str) -> dict:
    tool = _BY_NAME.get(name)
    if tool is None:
        raise ForecastError(f"unknown tool {name!r}; known: {sorted(_BY_NAME)}")
    return dict(tool)


def call_tool(name: str, arguments: dict) -> dict:
    """Dispatch a tool call by name. Read-only, deterministic, JSON in/out."""
    if name not in _DISPATCH:
        get_tool(name)  # raises the descriptive ForecastError
    return _DISPATCH[name](arguments)


# --- argument translation --------------------------------------------------

def _opening_from_pairs(pairs) -> dict:
    if not isinstance(pairs, list):
        raise ForecastError(f"opening: expected a list of {{account, balance}} pairs, got {pairs!r}")
    out: dict[str, float] = {}
    for pair in pairs:
        out[str(pair["account"])] = float(pair["balance"])
    return out


def _flow_raw_from_tool(raw: dict) -> dict:
    """Canonical tool flow → the raw dict ``parse_flow`` accepts (nested date, amount form).

    Accepts the tool-schema shape (``date`` as an object) *and* the shape
    ``flow_to_dict`` emits, which is what the managed service returns from
    ``GET /businesses/{id}``: ``date`` may be a plain ``"YYYY-MM-DD"`` string
    for an on-time flow, or ``{"due", "offset_days"}`` otherwise. Without the
    string branch, feeding service state back into ``call_tool`` raised
    ``TypeError: string indices must be integers``.

    Missing/invalid fields are left for ``parse_flow``, which raises a
    ``ForecastError`` naming the offending field — an LLM can act on that.
    """
    out = {k: raw[k] for k in
           ("id", "account", "direction", "probability", "source", "customer_id", "segment",
            "recurrence") if k in raw}

    d = raw.get("date")
    if isinstance(d, dict):
        if d.get("offset_days") is not None:
            out["date"] = {"due": d.get("due"), "offset_days": d["offset_days"]}
        else:
            out["date"] = d.get("due")
    elif d is not None:
        out["date"] = d  # "YYYY-MM-DD" — DateSpec.parse handles the string form

    if raw.get("amount_distribution") is not None:
        out["amount"] = {"distribution": raw["amount_distribution"]}
    elif "amount" in raw:
        out["amount"] = raw["amount"]  # number, or {"distribution": [...]} from flow_to_dict
    return out


def _flow_from_tool(raw: dict) -> object:
    return parse_flow(_flow_raw_from_tool(raw))


def _parse_flows(raw_flows) -> list:
    if not isinstance(raw_flows, list):
        raise ForecastError(f"flows: expected a list, got {raw_flows!r}")
    return [_flow_from_tool(f) for f in raw_flows]


# --- tool implementations --------------------------------------------------

def _tool_forecast_project(args: dict) -> dict:
    proj = project(
        _opening_from_pairs(args["opening"]),
        _parse_flows(args["flows"]),
        _as_date(args["start"], "start"),
        int(args["days"]),
    )
    mb = min_balance(proj)
    fs = first_shortfall(proj)
    return {
        "days": proj.days,
        "final_balance": proj.total[-1],
        "min_balance": mb["value"],
        "min_balance_date": mb["date"],
        "first_shortfall": fs.isoformat() if fs else None,
        "expected_inflows": sum(proj.inflows),
        "expected_outflows": sum(proj.outflows),
        "total": proj.total,
        "inputs_hash": proj.metadata["inputs_hash"],
    }


def _tool_forecast_simulate(args: dict) -> dict:
    levels = tuple(args.get("percentile_levels", (5, 50, 95)))
    mc = simulate(
        _opening_from_pairs(args["opening"]),
        _parse_flows(args["flows"]),
        _as_date(args["start"], "start"),
        int(args["days"]),
        n_paths=int(args.get("n_paths", 2000)),
        seed=int(args.get("seed", 0)),
        percentile_levels=levels,
        common_shock=args.get("common_shock"),
    )
    threshold = args.get("threshold", 0.0)
    fss = first_shortfall_stats(mc, threshold)
    mbp = min_balance_percentiles(mc)
    return {
        "days": mc.days,
        "n_paths": mc.n_paths,
        "seed": mc.seed,
        "percentiles": [
            {"level": q, "values": mc.percentiles[f"p{q}"]} for q in levels
        ],
        "min_balance_percentiles": [
            {"level": int(k[1:]), "value": v} for k, v in sorted(mbp.items())
        ],
        "probability_below": probability_below(mc, threshold),
        "first_shortfall_probability": fss["probability"],
        "first_shortfall_median_date": fss["median_date"],
        "inputs_hash": mc.metadata["inputs_hash"],
    }


def _tool_forecast_scenario(args: dict) -> dict:
    delta_raw = args["delta"]
    ops = []
    for op in delta_raw.get("operations", []):
        op = dict(op)
        if "flow" in op:
            op["flow"] = _flow_raw_from_tool(op["flow"])
        ops.append(op)
    delta = Delta.parse({"operations": ops})
    baseline, scenario, diff = run_scenario(
        _opening_from_pairs(args["opening"]),
        _parse_flows(args["flows"]),
        delta,
        _as_date(args["start"], "start"),
        int(args["days"]),
    )
    return {
        "baseline_final_balance": diff["final_balance"]["baseline"],
        "scenario_final_balance": diff["final_balance"]["scenario"],
        "final_balance_change": diff["final_balance"]["change"],
        "baseline_min_balance": diff["min_balance"]["baseline"],
        "scenario_min_balance": diff["min_balance"]["scenario"],
        "baseline_first_shortfall": diff["first_shortfall"]["baseline"],
        "scenario_first_shortfall": diff["first_shortfall"]["scenario"],
        "scenario_resolves_baseline_shortfall": diff["first_shortfall"].get(
            "scenario_resolves_baseline_shortfall"
        ),
        "inputs_hash": scenario.metadata["inputs_hash"],
    }


def _tool_behavior_profile(args: dict) -> dict:
    records = [parse_payment_record(r) for r in args["records"]]
    return offset_profile_for(
        records, args["customer_id"], args["segment"],
        float(args.get("prior_strength", 5.0)),
    )


def _tool_promise_rate(args: dict) -> dict:
    records = [parse_promise_record(r) for r in args["records"]]
    return promise_rate_for(
        records, args["customer_id"], args["segment"],
        float(args.get("prior_strength", 2.0)),
    )


_DISPATCH = {
    "forecast_project": _tool_forecast_project,
    "forecast_simulate": _tool_forecast_simulate,
    "forecast_scenario": _tool_forecast_scenario,
    "behavior_profile": _tool_behavior_profile,
    "promise_rate": _tool_promise_rate,
}

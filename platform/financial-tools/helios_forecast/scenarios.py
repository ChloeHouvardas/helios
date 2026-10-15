"""Scenario machinery: structured deltas applied to a baseline, plus a
deterministic comparison of baseline vs scenario projections.

A delta is the contract between app-level AI (which translates a user's
natural-language question into operations) and platform math (which evaluates
it). Deltas are plain JSON so an LLM can construct them; unknown operations or
unknown flow ids raise :class:`ForecastError` rather than being ignored.

Delta (dict form)::

    {"operations": [
      {"op": "add_flow", "flow": { ...flow dict... }},
      {"op": "remove_flow", "flow_id": "inv-001"},
      {"op": "shift_date", "flow_id": "inv-001", "days": 7},
      {"op": "resize_amount", "flow_id": "inv-001", "factor": 0.9},
      {"op": "set_probability", "flow_id": "inv-001", "probability": 0.5},
      {"op": "set_amount", "flow_id": "inv-001", "amount": 2500.0}
    ]}
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from .engine import Projection, first_shortfall, min_balance, project
from .model import AmountSpec, DateSpec, Flow, ForecastError, flow_to_dict, parse_flow


@dataclass(frozen=True)
class AddFlow:
    flow: Flow


@dataclass(frozen=True)
class RemoveFlow:
    flow_id: str


@dataclass(frozen=True)
class ShiftDate:
    flow_id: str
    days: int


@dataclass(frozen=True)
class ResizeAmount:
    flow_id: str
    factor: float


@dataclass(frozen=True)
class SetProbability:
    flow_id: str
    probability: float


@dataclass(frozen=True)
class SetAmount:
    flow_id: str
    amount: AmountSpec


Operation = AddFlow | RemoveFlow | ShiftDate | ResizeAmount | SetProbability | SetAmount


@dataclass(frozen=True)
class Delta:
    operations: tuple[Operation, ...]

    @classmethod
    def parse(cls, raw: object) -> "Delta":
        if isinstance(raw, Delta):
            return raw
        if not isinstance(raw, dict) or not isinstance(raw.get("operations"), list):
            raise ForecastError("delta: expected {'operations': [...]}, got {!r}".format(raw))
        ops: list[Operation] = []
        for entry in raw["operations"]:
            if not isinstance(entry, dict) or "op" not in entry:
                raise ForecastError(f"delta operation: expected an object with 'op', got {entry!r}")
            kind = entry["op"]
            if kind == "add_flow":
                if "flow" not in entry:
                    raise ForecastError("add_flow: missing 'flow'")
                ops.append(AddFlow(parse_flow(entry["flow"])))
            elif kind == "remove_flow":
                ops.append(RemoveFlow(_need_id(entry)))
            elif kind == "shift_date":
                ops.append(ShiftDate(_need_id(entry), int(_need(entry, "days"))))
            elif kind == "resize_amount":
                factor = float(_need(entry, "factor"))
                if factor < 0:
                    raise ForecastError(f"resize_amount: factor must be >= 0, got {factor}")
                ops.append(ResizeAmount(_need_id(entry), factor))
            elif kind == "set_probability":
                probability = float(_need(entry, "probability"))
                if not 0.0 <= probability <= 1.0:
                    raise ForecastError(f"set_probability: must be within 0..1, got {probability}")
                ops.append(SetProbability(_need_id(entry), probability))
            elif kind == "set_amount":
                if "amount" not in entry:
                    raise ForecastError("set_amount: missing 'amount'")
                ops.append(SetAmount(_need_id(entry), AmountSpec.parse(entry["amount"])))
            else:
                raise ForecastError(f"delta operation: unknown op {kind!r}")
        return cls(operations=tuple(ops))

    def to_dict(self) -> dict:
        out = []
        for op in self.operations:
            if isinstance(op, AddFlow):
                out.append({"op": "add_flow", "flow": flow_to_dict(op.flow)})
            elif isinstance(op, RemoveFlow):
                out.append({"op": "remove_flow", "flow_id": op.flow_id})
            elif isinstance(op, ShiftDate):
                out.append({"op": "shift_date", "flow_id": op.flow_id, "days": op.days})
            elif isinstance(op, ResizeAmount):
                out.append({"op": "resize_amount", "flow_id": op.flow_id, "factor": op.factor})
            elif isinstance(op, SetProbability):
                out.append({"op": "set_probability", "flow_id": op.flow_id, "probability": op.probability})
            else:
                out.append({"op": "set_amount", "flow_id": op.flow_id, "amount": op.amount.to_dict()})
        return {"operations": out}


def _need(entry: dict, key: str) -> object:
    if key not in entry:
        raise ForecastError(f"{entry.get('op', 'operation')}: missing required field {key!r}")
    return entry[key]


def _need_id(entry: dict) -> str:
    flow_id = _need(entry, "flow_id")
    if not isinstance(flow_id, str) or not flow_id:
        raise ForecastError(f"{entry['op']}: 'flow_id' must be a non-empty string, got {flow_id!r}")
    return flow_id


def _replace_flow(flows: list[Flow], flow_id: str, fn) -> list[Flow]:
    out, found = [], False
    for f in flows:
        if f.id == flow_id:
            found = True
            out.append(fn(f))
        else:
            out.append(f)
    if not found:
        raise ForecastError(f"no flow with id {flow_id!r}")
    return out


def apply_delta(
    opening: dict[str, float], flows: list[Flow], delta: Delta
) -> tuple[dict[str, float], list[Flow]]:
    """Apply a delta to a baseline state; returns the scenario state."""
    from dataclasses import replace

    current = list(flows)
    for op in delta.operations:
        if isinstance(op, AddFlow):
            if any(f.id == op.flow.id for f in current):
                raise ForecastError(f"add_flow: a flow with id {op.flow.id!r} already exists")
            current.append(op.flow)
        elif isinstance(op, RemoveFlow):
            current = [f for f in current if f.id != op.flow_id]
        elif isinstance(op, ShiftDate):
            current = _replace_flow(
                current,
                op.flow_id,
                lambda f: replace(f, date=DateSpec(f.date.due + timedelta(days=op.days), f.date.offset_days)),
            )
        elif isinstance(op, ResizeAmount):
            current = _replace_flow(
                current, op.flow_id, lambda f: replace(f, amount=f.amount.scaled(op.factor))
            )
        elif isinstance(op, SetProbability):
            current = _replace_flow(
                current, op.flow_id, lambda f: replace(f, probability=op.probability)
            )
        elif isinstance(op, SetAmount):
            current = _replace_flow(current, op.flow_id, lambda f: replace(f, amount=op.amount))
    return dict(opening), current


def compare(baseline: Projection, scenario: Projection) -> dict:
    """Deterministic diff between two projections over the same grid."""
    if (baseline.start, baseline.days) != (scenario.start, scenario.days):
        raise ForecastError("compare: projections must share the same start date and horizon")

    b_min, s_min = min_balance(baseline), min_balance(scenario)
    b_short, s_short = first_shortfall(baseline), first_shortfall(scenario)
    b_final, s_final = baseline.total[-1], scenario.total[-1]

    if b_short is not None and s_short is not None:
        change_days = (s_short - b_short).days
    else:
        change_days = None

    return {
        "min_balance": {
            "baseline": b_min["value"],
            "scenario": s_min["value"],
            "change": s_min["value"] - b_min["value"],
        },
        "first_shortfall": {
            "baseline": b_short.isoformat() if b_short else None,
            "scenario": s_short.isoformat() if s_short else None,
            "change_days": change_days,
            "scenario_resolves_baseline_shortfall": b_short is not None and s_short is None,
        },
        "final_balance": {
            "baseline": b_final,
            "scenario": s_final,
            "change": s_final - b_final,
        },
    }


def run_scenario(
    opening: dict[str, float],
    flows: list[Flow],
    delta: Delta,
    start,
    days: int,
) -> tuple[Projection, Projection, dict]:
    """Project baseline and scenario, and return both plus their diff."""
    scenario_opening, scenario_flows = apply_delta(opening, flows, delta)
    baseline = project(opening, flows, start, days)
    scenario = project(scenario_opening, scenario_flows, start, days)
    return baseline, scenario, compare(baseline, scenario)

"""Typed input model for the Helios forecasting SDK.

All inputs are plain JSON-compatible dicts so an LLM (or any app) can construct
and inspect them; this module parses them into immutable typed objects with
validation. Untrusted input is expected: every parse failure raises
:class:`ForecastError` with a message aimed at fixing the input.

Flow (dict form)::

    {
      "id": "inv-001",
      "account": "main",
      "direction": "inflow",            # or "outflow"
      "amount": 5000.0,                  # number, or {"distribution": [[value, weight], ...]}
      "date": "2026-11-05",              # "YYYY-MM-DD", or {"due": "...", "offset_days": [[days, weight], ...]}
      "probability": 1.0,                # 0..1 chance the flow occurs at all
      "recurrence": null,                # or {"freq": "daily"|"weekly"|"monthly", "interval": 1, "count": 6, "until": "YYYY-MM-DD"}
      "source": "kg:invoice/inv-001"     # reference to the record this came from
    }

Weights need not sum to 1; they are normalized. Offsets are days relative to
the due date and may be negative (early payment).
"""
from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Optional

ENGINE_VERSION = "0.1.0"

INFLOW = "inflow"
OUTFLOW = "outflow"


class ForecastError(ValueError):
    """Raised when an input cannot be interpreted."""


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise ForecastError(msg)


def _as_date(raw: object, what: str) -> date:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    raise ForecastError(f"{what}: expected a date as 'YYYY-MM-DD', got {raw!r}")


def _as_number(raw: object, what: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ForecastError(f"{what}: expected a number, got {raw!r}")
    return float(raw)


def _parse_weighted_pairs(raw: object, what: str):
    """Parse [[value, weight], ...] into (values, weights) tuples."""
    _expect(
        isinstance(raw, list) and len(raw) > 0,
        f"{what}: expected a non-empty list of [value, weight] pairs",
    )
    values, weights = [], []
    for pair in raw:
        _expect(
            isinstance(pair, (list, tuple)) and len(pair) == 2,
            f"{what}: each entry must be [value, weight], got {pair!r}",
        )
        values.append(_as_number(pair[0], f"{what} value"))
        weights.append(_as_number(pair[1], f"{what} weight"))
        _expect(weights[-1] > 0, f"{what}: weights must be positive, got {weights[-1]!r}")
    return tuple(values), tuple(weights)


@dataclass(frozen=True)
class AmountSpec:
    """A fixed amount or a discrete distribution over amounts."""

    fixed: Optional[float] = None
    distribution: tuple[tuple[float, float], ...] = ()  # (value, weight)

    @classmethod
    def parse(cls, raw: object) -> "AmountSpec":
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            _expect(raw >= 0, f"amount: must be non-negative, got {raw}")
            return cls(fixed=float(raw))
        if isinstance(raw, dict):
            if "distribution" in raw:
                values, weights = _parse_weighted_pairs(raw["distribution"], "amount.distribution")
                return cls(distribution=tuple(zip(values, weights)))
            if "values" in raw and "weights" in raw:
                _expect(
                    len(raw["values"]) == len(raw["weights"]) and len(raw["values"]) > 0,
                    "amount: 'values' and 'weights' must be non-empty and the same length",
                )
                pairs = [[v, w] for v, w in zip(raw["values"], raw["weights"])]
                values, weights = _parse_weighted_pairs(pairs, "amount")
                return cls(distribution=tuple(zip(values, weights)))
        raise ForecastError(f"amount: expected a number or {{'distribution': [...] }}, got {raw!r}")

    def expected(self) -> float:
        if self.fixed is not None:
            return self.fixed
        total_w = sum(w for _, w in self.distribution)
        return sum(v * w for v, w in self.distribution) / total_w

    def sample(self, u: float) -> float:
        """Map a uniform [0,1) draw to an amount via the inverse CDF."""
        if self.fixed is not None:
            return self.fixed
        total_w = sum(w for _, w in self.distribution)
        acc, target = 0.0, u * total_w
        for v, w in self.distribution:
            acc += w
            if target < acc:
                return v
        return self.distribution[-1][0]

    def scaled(self, factor: float) -> "AmountSpec":
        if self.fixed is not None:
            return AmountSpec(fixed=self.fixed * factor)
        return AmountSpec(distribution=tuple((v * factor, w) for v, w in self.distribution))

    def to_dict(self) -> object:
        if self.fixed is not None:
            return self.fixed
        return {"distribution": [[v, w] for v, w in self.distribution]}


@dataclass(frozen=True)
class DateSpec:
    """A due date plus a discrete distribution of day offsets."""

    due: date
    offset_days: tuple[tuple[int, float], ...] = ((0, 1.0),)  # (days, weight)

    @classmethod
    def parse(cls, raw: object) -> "DateSpec":
        if isinstance(raw, (str, date)):
            return cls(due=_as_date(raw, "date"))
        if isinstance(raw, dict):
            due = _as_date(raw.get("due"), "date.due")
            offsets = ((0, 1.0),)
            if "offset_days" in raw and raw["offset_days"] is not None:
                values, weights = _parse_weighted_pairs(raw["offset_days"], "date.offset_days")
                _expect(
                    all(float(v).is_integer() for v in values),
                    "date.offset_days: offsets must be whole numbers of days",
                )
                offsets = tuple(zip((int(v) for v in values), weights))
            return cls(due=due, offset_days=offsets)
        raise ForecastError(f"date: expected 'YYYY-MM-DD' or {{'due': ..., 'offset_days': [...]}}, got {raw!r}")

    def expected_offset(self) -> float:
        total_w = sum(w for _, w in self.offset_days)
        return sum(d * w for d, w in self.offset_days) / total_w

    def expected_date(self) -> date:
        return self.due + timedelta(days=int(round(self.expected_offset())))

    def sample(self, u: float) -> date:
        total_w = sum(w for _, w in self.offset_days)
        acc, target = 0.0, u * total_w
        for d, w in self.offset_days:
            acc += w
            if target < acc:
                return self.due + timedelta(days=d)
        return self.due + timedelta(days=self.offset_days[-1][0])

    def to_dict(self) -> object:
        if self.offset_days == ((0, 1.0),):
            return self.due.isoformat()
        return {
            "due": self.due.isoformat(),
            "offset_days": [[d, w] for d, w in self.offset_days],
        }


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


@dataclass(frozen=True)
class Recurrence:
    """A repeat rule: every `interval` days / weeks / months."""

    freq: str  # "daily" | "weekly" | "monthly"
    interval: int = 1
    count: Optional[int] = None  # max occurrences total
    until: Optional[date] = None  # stop after this date

    @classmethod
    def parse(cls, raw: object) -> "Recurrence":
        _expect(isinstance(raw, dict), f"recurrence: expected an object, got {raw!r}")
        freq = raw.get("freq")
        _expect(freq in ("daily", "weekly", "monthly"), f"recurrence.freq: expected daily/weekly/monthly, got {freq!r}")
        interval = int(raw.get("interval", 1))
        _expect(interval >= 1, f"recurrence.interval: must be >= 1, got {interval}")
        count = raw.get("count")
        if count is not None:
            count = int(count)
            _expect(count >= 1, f"recurrence.count: must be >= 1, got {count}")
        until = raw.get("until")
        if until is not None:
            until = _as_date(until, "recurrence.until")
        return cls(freq=freq, interval=interval, count=count, until=until)

    def _next(self, d: date) -> date:
        if self.freq == "daily":
            return d + timedelta(days=self.interval)
        if self.freq == "weekly":
            return d + timedelta(weeks=self.interval)
        return _add_months(d, self.interval)

    def occurrences(self, first: date, horizon_end: date) -> list[date]:
        out: list[date] = []
        d = first
        while True:
            if self.count is not None and len(out) >= self.count:
                break
            if self.until is not None and d > self.until:
                break
            if d > horizon_end:
                break
            out.append(d)
            d = self._next(d)
        return out

    def to_dict(self) -> dict:
        out = {"freq": self.freq}
        if self.interval != 1:
            out["interval"] = self.interval
        if self.count is not None:
            out["count"] = self.count
        if self.until is not None:
            out["until"] = self.until.isoformat()
        return out


@dataclass(frozen=True)
class Flow:
    """One expected cash flow, recurring or single."""

    id: str
    date: DateSpec
    account: str = "main"
    direction: str = INFLOW
    amount: AmountSpec = AmountSpec(fixed=0.0)
    probability: float = 1.0
    recurrence: Optional[Recurrence] = None
    source: Optional[str] = None
    customer_id: Optional[str] = None
    segment: Optional[str] = None


def parse_flow(raw: object) -> Flow:
    _expect(isinstance(raw, dict), f"flow: expected an object, got {raw!r}")
    flow_id = raw.get("id")
    _expect(isinstance(flow_id, str) and flow_id, f"flow.id: expected a non-empty string, got {flow_id!r}")
    direction = raw.get("direction", INFLOW)
    _expect(direction in (INFLOW, OUTFLOW), f"flow.direction: expected inflow/outflow, got {direction!r}")
    _expect("amount" in raw, f"flow {flow_id!r}: missing required field 'amount'")
    _expect("date" in raw, f"flow {flow_id!r}: missing required field 'date'")
    probability = _as_number(raw.get("probability", 1.0), f"flow {flow_id!r} probability")
    _expect(0.0 <= probability <= 1.0, f"flow {flow_id!r} probability: must be within 0..1, got {probability}")
    account = raw.get("account", "main")
    _expect(isinstance(account, str) and account, f"flow {flow_id!r} account: expected a string, got {account!r}")
    source = raw.get("source")
    _expect(source is None or isinstance(source, str), f"flow {flow_id!r} source: expected a string, got {source!r}")
    customer_id = raw.get("customer_id")
    _expect(customer_id is None or isinstance(customer_id, str), f"flow {flow_id!r} customer_id: expected a string, got {customer_id!r}")
    segment = raw.get("segment")
    _expect(segment is None or isinstance(segment, str), f"flow {flow_id!r} segment: expected a string, got {segment!r}")
    recurrence = raw.get("recurrence")
    return Flow(
        id=flow_id,
        account=account,
        direction=direction,
        amount=AmountSpec.parse(raw["amount"]),
        date=DateSpec.parse(raw["date"]),
        probability=probability,
        recurrence=Recurrence.parse(recurrence) if recurrence else None,
        source=source,
        customer_id=customer_id,
        segment=segment,
    )


def flow_to_dict(flow: Flow) -> dict:
    return {
        "id": flow.id,
        "account": flow.account,
        "direction": flow.direction,
        "amount": flow.amount.to_dict(),
        "date": flow.date.to_dict(),
        "probability": flow.probability,
        "recurrence": flow.recurrence.to_dict() if flow.recurrence else None,
        "source": flow.source,
        "customer_id": flow.customer_id,
        "segment": flow.segment,
    }


@dataclass(frozen=True)
class FlowInstance:
    """A single dated occurrence of a flow (recurrences are expanded)."""

    flow_id: str
    account: str
    direction: str
    amount: AmountSpec
    date: DateSpec
    probability: float
    source: Optional[str] = None


def expand_flows(flows: list[Flow], horizon_end: date) -> list[FlowInstance]:
    instances: list[FlowInstance] = []
    for f in flows:
        if f.recurrence:
            due_dates = f.recurrence.occurrences(f.date.due, horizon_end)
        else:
            due_dates = [f.date.due]
        for d in due_dates:
            instances.append(
                FlowInstance(
                    flow_id=f.id,
                    account=f.account,
                    direction=f.direction,
                    amount=f.amount,
                    date=replace(f.date, due=d),
                    probability=f.probability,
                    source=f.source,
                )
            )
    return instances


def state_hash(opening: dict[str, float], flows: list[Flow]) -> str:
    """Stable hash of the exact inputs, for audit metadata."""
    payload = json.dumps(
        {
            "opening": {k: float(v) for k, v in sorted(opening.items())},
            "flows": [flow_to_dict(f) for f in flows],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

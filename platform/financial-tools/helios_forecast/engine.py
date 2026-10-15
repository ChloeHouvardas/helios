"""Projection engine: deterministic expected-value run and seeded Monte Carlo.

Design properties (see specs/forecasting-simulation.md):

- Pure functions: identical inputs give identical outputs, everywhere.
- The Monte Carlo kernel is seeded; results are reproducible for audit re-runs.
- Every result carries metadata: engine version, seed, path count, input hash.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import numpy as np

from .model import (
    ENGINE_VERSION,
    INFLOW,
    OUTFLOW,
    Flow,
    expand_flows,
    state_hash,
)


def _window_indices(start: date, days: int, window: Optional[tuple[date, date]]) -> tuple[int, int]:
    """Clamp a (from, to) inclusive window to [0, days-1]; None = whole horizon."""
    if window is None:
        return 0, days - 1
    lo = max(0, (window[0] - start).days)
    hi = min(days - 1, (window[1] - start).days)
    return lo, max(lo, hi)


@dataclass
class Projection:
    """Deterministic expected-value projection on a daily grid.

    ``balances[a][i]`` is account ``a``'s end-of-day balance on ``start + i``
    days; ``total[i]`` is the sum across accounts. ``inflows``/``outflows``
    hold expected per-day magnitudes for ``expected_total``.
    """

    start: date
    days: int
    balances: dict[str, list[float]]
    total: list[float]
    inflows: list[float]
    outflows: list[float]
    metadata: dict


def project(opening: dict[str, float], flows: list[Flow], start: date, days: int) -> Projection:
    if days < 1:
        raise ValueError("days must be >= 1")
    end = start + timedelta(days=days - 1)
    instances = expand_flows(flows, end)
    accounts = sorted(set(opening) | {i.account for i in instances})

    delta = {a: [0.0] * days for a in accounts}
    inflows = [0.0] * days
    outflows = [0.0] * days

    for inst in instances:
        idx = (inst.date.expected_date() - start).days
        if not (0 <= idx < days):
            continue
        expected = inst.probability * inst.amount.expected()
        delta[inst.account][idx] += expected if inst.direction == INFLOW else -expected
        if inst.direction == INFLOW:
            inflows[idx] += expected
        else:
            outflows[idx] += expected

    balances: dict[str, list[float]] = {}
    for a in accounts:
        run, bal = float(opening.get(a, 0.0)), []
        for d in delta[a]:
            run += d
            bal.append(run)
        balances[a] = bal
    total = [sum(balances[a][i] for a in accounts) for i in range(days)]

    return Projection(
        start=start,
        days=days,
        balances=balances,
        total=total,
        inflows=inflows,
        outflows=outflows,
        metadata={
            "engine_version": ENGINE_VERSION,
            "mode": "deterministic",
            "start": start.isoformat(),
            "days": days,
            "flow_instances": len(instances),
            "inputs_hash": state_hash(opening, flows),
        },
    )


@dataclass
class MonteCarloForecast:
    """Seeded Monte Carlo forecast over the same daily grid.

    ``total_paths`` has shape (n_paths, days) of end-of-day total balances;
    queries (shortfall probabilities, percentile bands) are computed from it.
    """

    start: date
    days: int
    n_paths: int
    seed: int
    accounts: list[str]
    total_paths: np.ndarray
    account_paths: dict[str, np.ndarray]
    percentiles: dict[str, list[float]]
    account_percentiles: dict[str, dict[str, list[float]]]
    metadata: dict


def _sample_discrete(u: np.ndarray, values: np.ndarray, cumw: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(cumw, u, side="right")
    np.clip(idx, 0, len(values) - 1, out=idx)
    return values[idx]


def _parse_common_shock(common_shock) -> tuple[np.ndarray, np.ndarray]:
    """Validate a common-shock spec {"offset_days": [[days, weight], ...]}.

    Returns (values, cumulative weights) for one shared offset draw per path.
    """
    if (
        not isinstance(common_shock, dict)
        or not isinstance(common_shock.get("offset_days"), list)
        or not common_shock["offset_days"]
    ):
        raise ValueError(
            "common_shock must be {'offset_days': [[days, weight], ...]} with at least one entry"
        )
    values: list[float] = []
    weights: list[float] = []
    for entry in common_shock["offset_days"]:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError(f"common_shock offset entry must be [days, weight], got {entry!r}")
        d, w = entry
        if isinstance(d, bool) or not isinstance(d, int) or not isinstance(w, (int, float)) or w <= 0:
            raise ValueError(
                f"common_shock offset entry must be [int days, positive weight], got {entry!r}"
            )
        values.append(float(d))
        weights.append(float(w))
    cumw = np.cumsum(np.array(weights, dtype=float) / sum(weights))
    return np.array(values, dtype=float), cumw


def simulate(
    opening: dict[str, float],
    flows: list[Flow],
    start: date,
    days: int,
    n_paths: int = 2000,
    seed: int = 0,
    percentile_levels: tuple[int, ...] = (5, 50, 95),
    common_shock: Optional[dict] = None,
) -> MonteCarloForecast:
    """Seeded Monte Carlo forecast; optionally with a shared common-shock factor.

    ``common_shock = {"offset_days": [[days, weight], ...]}`` draws ONE offset
    per simulated path and shifts *all* flow dates in that path together:
    a shared macro stress factor → correlated payment delays → correlated
    shortfall risk. This is a Vasicek-style single-factor analogy (common
    factor drives correlated defaults), not an equivalence — defaults are not
    modelled here, only date shifts. When omitted, the draw never happens, so
    the RNG stream — and therefore every result — reproduces bit-identically.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    if n_paths < 1:
        raise ValueError("n_paths must be >= 1")
    rng = np.random.default_rng(seed)
    end = start + timedelta(days=days - 1)
    instances = expand_flows(flows, end)
    accounts = sorted(set(opening) | {i.account for i in instances})
    shock_spec = _parse_common_shock(common_shock) if common_shock is not None else None

    matrices = {a: np.zeros((n_paths, days)) for a in accounts}
    if not instances:
        u_shape = (n_paths, 0)
        rng.random(u_shape)  # keep the draw sequence deterministic even with no flows
    else:
        u_off = rng.random((n_paths, len(instances)))
        u_amt = rng.random((n_paths, len(instances)))
        u_occ = rng.random((n_paths, len(instances)))
        shock = None
        if shock_spec is not None:
            shock_values, shock_cumw = shock_spec
            shock = _sample_discrete(rng.random(n_paths), shock_values, shock_cumw).astype(int)
        for i, inst in enumerate(instances):
            off_values = np.array([d for d, _ in inst.date.offset_days], dtype=float)
            off_weights = np.array([w for _, w in inst.date.offset_days], dtype=float)
            off_cumw = np.cumsum(off_weights / off_weights.sum())
            amt_values = (
                np.array([inst.amount.fixed], dtype=float)
                if inst.amount.fixed is not None
                else np.array([v for v, _ in inst.amount.distribution], dtype=float)
            )
            amt_weights = (
                np.array([1.0])
                if inst.amount.fixed is not None
                else np.array([w for _, w in inst.amount.distribution], dtype=float)
            )
            amt_cumw = np.cumsum(amt_weights / amt_weights.sum())

            offset = _sample_discrete(u_off[:, i], off_values, off_cumw)
            amount = _sample_discrete(u_amt[:, i], amt_values, amt_cumw)
            amount = np.where(u_occ[:, i] < inst.probability, amount, 0.0)
            day = (inst.date.due - start).days + offset.astype(int)
            if shock is not None:
                day = day + shock
            sign = 1.0 if inst.direction == INFLOW else -1.0
            valid = (day >= 0) & (day < days)
            if valid.any():
                mat = matrices[inst.account]
                np.add.at(mat, (np.nonzero(valid)[0], day[valid]), sign * amount[valid])

    account_paths: dict[str, np.ndarray] = {}
    for a in accounts:
        account_paths[a] = float(opening.get(a, 0.0)) + np.cumsum(matrices[a], axis=1)
    total_paths = np.zeros((n_paths, days))
    for a in accounts:
        total_paths += account_paths[a]

    pct_keys = [f"p{q}" for q in percentile_levels]
    pct_array = np.percentile(total_paths, percentile_levels, axis=0)
    percentiles = {k: pct_array[j].tolist() for j, k in enumerate(pct_keys)}
    account_percentiles = {
        a: {
            k: np.percentile(account_paths[a], percentile_levels, axis=0)[j].tolist()
            for j, k in enumerate(pct_keys)
        }
        for a in accounts
    }

    meta = {
        "engine_version": ENGINE_VERSION,
        "mode": "montecarlo",
        "start": start.isoformat(),
        "days": days,
        "n_paths": n_paths,
        "seed": seed,
        "flow_instances": len(instances),
        "inputs_hash": state_hash(opening, flows),
    }
    if common_shock is not None:
        meta["common_shock"] = common_shock
    return MonteCarloForecast(
        start=start,
        days=days,
        n_paths=n_paths,
        seed=seed,
        accounts=accounts,
        total_paths=total_paths,
        account_paths=account_paths,
        percentiles=percentiles,
        account_percentiles=account_percentiles,
        metadata=meta,
    )


# --- queries ---------------------------------------------------------------


def balance_at(p: Projection, on: date) -> float:
    """Total balance at end of `on` (clamped to the horizon)."""
    idx = min(max((on - p.start).days, 0), p.days - 1)
    return p.total[idx]


def min_balance(p: Projection, window: Optional[tuple[date, date]] = None) -> dict:
    """Minimum total balance over a window: {"value", "date"}."""
    lo, hi = _window_indices(p.start, p.days, window)
    values = p.total[lo : hi + 1]
    i = min(range(len(values)), key=lambda j: values[j])
    return {"value": values[i], "date": (p.start + timedelta(days=lo + i)).isoformat()}


def first_shortfall(
    p: Projection, threshold: float = 0.0, window: Optional[tuple[date, date]] = None
) -> Optional[date]:
    """First day the total balance drops below `threshold`, or None."""
    lo, hi = _window_indices(p.start, p.days, window)
    for i in range(lo, hi + 1):
        if p.total[i] < threshold:
            return p.start + timedelta(days=i)
    return None


def expected_total(
    p: Projection, direction: str, window: Optional[tuple[date, date]] = None
) -> float:
    """Sum of expected inflow or outflow magnitudes over a window."""
    if direction not in (INFLOW, OUTFLOW):
        raise ValueError("direction must be 'inflow' or 'outflow'")
    lo, hi = _window_indices(p.start, p.days, window)
    series = p.inflows if direction == INFLOW else p.outflows
    return sum(series[lo : hi + 1])


def probability_below(
    mc: MonteCarloForecast, threshold: float, window: Optional[tuple[date, date]] = None
) -> float:
    """Fraction of paths whose total balance dips below `threshold` in the window."""
    lo, hi = _window_indices(mc.start, mc.days, window)
    segment = mc.total_paths[:, lo : hi + 1]
    return float((segment.min(axis=1) < threshold).mean())


def first_shortfall_stats(
    mc: MonteCarloForecast, threshold: float = 0.0, window: Optional[tuple[date, date]] = None
) -> dict:
    """Distribution of the first shortfall day across paths."""
    lo, hi = _window_indices(mc.start, mc.days, window)
    below = mc.total_paths[:, lo : hi + 1] < threshold
    any_short = below.any(axis=1)
    if not any_short.any():
        return {"probability": 0.0, "median_date": None}
    first_idx = below.argmax(axis=1)[any_short]
    median_idx = int(round(float(np.median(first_idx))))
    return {
        "probability": float(any_short.mean()),
        "median_date": (mc.start + timedelta(days=lo + median_idx)).isoformat(),
    }


def min_balance_percentiles(
    mc: MonteCarloForecast, window: Optional[tuple[date, date]] = None
) -> dict:
    """Percentile distribution of the per-path minimum total balance."""
    lo, hi = _window_indices(mc.start, mc.days, window)
    minima = mc.total_paths[:, lo : hi + 1].min(axis=1)
    return {f"p{q}": float(np.percentile(minima, q)) for q in (5, 50, 95)}

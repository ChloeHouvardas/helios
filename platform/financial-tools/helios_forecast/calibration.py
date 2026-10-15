"""Conformal calibration (D6): distribution-free error bands for point forecasts.

Split-conformal intuition: calibrate on past absolute errors of a point
forecast, then quote ``prediction ± half_width`` around future points. Under
exchangeability of errors, the interval covers a new point with probability
at least ``1 − alpha`` (finite-sample, no distributional assumptions).

This is a utility, not the primary uncertainty interface — Monte Carlo
percentile bands remain the engine's main representation (spec §3.2).
Use this when an app wants a guaranteed-coverage interval around a *point*
forecast, and ``empirical_coverage`` to backtest the guarantee on held-out
data.
"""
from __future__ import annotations

import math

from .model import ForecastError


def conformal_half_width(errors, alpha: float = 0.1) -> float | None:
    """Half-width of a split-conformal interval from calibration errors.

    ``errors`` are past absolute residuals ``|actual − predicted|``. Returns
    the ``ceil((n+1)(1−alpha))``-th smallest error — the finite-sample
    corrected quantile — or ``None`` when there is too little calibration
    data to support the requested level (``ceil((n+1)(1−alpha)) > n``).
    """
    if not isinstance(alpha, (int, float)) or not (0.0 < alpha < 1.0):
        raise ForecastError(f"alpha must be strictly between 0 and 1, got {alpha!r}")
    errors = list(errors)
    n = len(errors)
    k = math.ceil((n + 1) * (1.0 - alpha))
    if k > n:
        return None
    return float(sorted(errors)[k - 1])


def empirical_coverage(errors, half_width: float) -> float:
    """Fraction of absolute errors within ``half_width`` — backtests a band."""
    errors = list(errors)
    if not errors:
        raise ForecastError("errors must be non-empty")
    if not isinstance(half_width, (int, float)):
        raise ForecastError(f"half_width must be a number, got {half_width!r}")
    within = sum(1 for e in errors if e <= half_width)
    return within / len(errors)

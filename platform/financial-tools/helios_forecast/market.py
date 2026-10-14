"""Tushare market-data adapter (P2) — borrowing-cost series for the engine.

Serves spec §3.6 (market data) from `services/financial-api/`: the externally
fetched, centrally maintained part of the forecasting capability. The adapter
is stdlib-only (no new dependencies), reads the Tushare token from the
``TUSHARE_TOKEN`` environment variable (never hard-coded, never logged), and
keeps the network layer injectable so tests run without a token or network.

What it produces is engine-native: ``interest_flows`` returns flow dicts that
``helios_forecast.parse_flow`` consumes, so borrowing-cost questions become
ordinary scenario flows.

Note: Tushare interface names (``lpr``, ``shibor``), field names
(``LPR1Y``, ``ON``), and the percent-unit convention are recorded as
*unverified* in ``specs/forecasting-research-claims.md`` (T5–T9) — confirm
against the official Tushare Pro docs before trusting them in production.

Example::

    from datetime import date
    from financial_api import fetch_lpr, current_rate, interest_flows

    series = fetch_lpr()                      # reads TUSHARE_TOKEN from env
    rate = current_rate(series)               # e.g. 0.0345 (decimal)
    flows = interest_flows(principal=20000, annual_rate=rate + 0.02,
                           start=date(2026, 11, 1), months=6)
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import date, datetime, timedelta
from typing import Callable, Optional

TUSHARE_ENDPOINT = "http://api.tushare.pro"
TUSHARE_TOKEN_ENV = "TUSHARE_TOKEN"

# A post_json callable: (url, payload dict) -> parsed response dict.
PostJson = Callable[[str, dict], dict]


class MarketDataError(RuntimeError):
    """Raised when market data cannot be fetched or interpreted."""


def _default_post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_tushare(
    api_name: str,
    params: Optional[dict] = None,
    fields: str = "",
    token: Optional[str] = None,
    endpoint: str = TUSHARE_ENDPOINT,
    post_json: Optional[PostJson] = None,
    timeout: int = 15,
) -> dict:
    """Call a Tushare Pro interface; returns the raw response dict.

    The token comes from the ``token`` argument or the ``TUSHARE_TOKEN``
    environment variable. It is sent only in the request body and never
    logged or included in error messages.
    """
    token = token or os.environ.get(TUSHARE_TOKEN_ENV)
    if not token:
        raise MarketDataError(
            f"no Tushare token: set the {TUSHARE_TOKEN_ENV} environment variable"
        )
    post = post_json or _default_post_json
    payload = {
        "api_name": api_name,
        "token": token,
        "params": params or {},
        "fields": fields,
    }
    try:
        response = post(endpoint, payload)
    except MarketDataError:
        raise
    except Exception as exc:  # network / HTTP layer
        raise MarketDataError(f"Tushare request for {api_name!r} failed: {exc}") from exc
    if not isinstance(response, dict) or response.get("code") != 0:
        detail = response.get("msg") if isinstance(response, dict) else response
        raise MarketDataError(f"Tushare {api_name!r} returned an error: {detail!r}")
    return response


def _parse_tushare_date(raw: object, what: str) -> date:
    if isinstance(raw, str):
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    raise MarketDataError(f"{what}: expected a date like '20261013', got {raw!r}")


def parse_rate_series(
    response: dict,
    value_field: str,
    date_field: str = "TRADE_DATE",
    percent: bool = True,
) -> list:
    """Extract a Tushare response into [(date, decimal_rate), ...] sorted by date.

    Tushare quotes rates in percent (e.g. 3.45); ``percent=True`` converts to
    decimals (0.0345). Rows with a missing value are skipped.
    """
    data = response.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    for required in (date_field, value_field):
        if required not in fields:
            raise MarketDataError(
                f"response fields {fields!r} do not include {required!r}"
            )
    di, vi = fields.index(date_field), fields.index(value_field)
    series: list = []
    for row in items:
        value = row[vi]
        if value is None:
            continue
        rate = float(value)
        if percent:
            rate /= 100.0
        series.append((_parse_tushare_date(row[di], date_field), rate))
    series.sort(key=lambda pair: pair[0])
    return series


def fetch_lpr(
    token: Optional[str] = None,
    params: Optional[dict] = None,
    value_field: str = "LPR1Y",
    post_json: Optional[PostJson] = None,
) -> list:
    """Loan Prime Rate series (1Y by default) as [(date, decimal_rate), ...]."""
    response = fetch_tushare("lpr", params=params, token=token, post_json=post_json)
    return parse_rate_series(response, value_field)


def fetch_shibor(
    token: Optional[str] = None,
    params: Optional[dict] = None,
    value_field: str = "ON",
    post_json: Optional[PostJson] = None,
) -> list:
    """SHIBOR series (overnight by default) as [(date, decimal_rate), ...]."""
    response = fetch_tushare("shibor", params=params, token=token, post_json=post_json)
    return parse_rate_series(response, value_field)


def current_rate(series: list) -> float:
    """The latest rate in a [(date, rate), ...] series."""
    if not series:
        raise MarketDataError("rate series is empty")
    return series[-1][1]


def mean_rate(series: list, window_days: Optional[int] = None) -> float:
    """Mean rate over the trailing ``window_days`` ending at the latest date."""
    if not series:
        raise MarketDataError("rate series is empty")
    if window_days is None:
        return sum(r for _, r in series) / len(series)
    latest = series[-1][0]
    cutoff = latest - timedelta(days=window_days)
    window = [r for d, r in series if d >= cutoff]
    if not window:
        raise MarketDataError(f"no rates within {window_days} days of {latest.isoformat()}")
    return sum(window) / len(window)


def constant_rate_series(rate: float, days: int, end: date) -> list:
    """Offline fallback: a flat rate series of ``days`` daily points ending at ``end``."""
    if rate < 0:
        raise MarketDataError(f"rate must be non-negative, got {rate}")
    if days < 1:
        raise MarketDataError(f"days must be >= 1, got {days}")
    return [(end - timedelta(days=i), rate) for i in range(days)][::-1]


def interest_flows(
    principal: float,
    annual_rate: float,
    start: date,
    months: int,
    flow_id: str = "credit-line-interest",
    account: str = "main",
) -> list:
    """Monthly interest outflows for borrowing ``principal`` at ``annual_rate``.

    Returns flow dicts directly consumable by ``helios_forecast.parse_flow``:
    a monthly outflow of ``principal * annual_rate / 12`` starting at ``start``.
    """
    if principal <= 0:
        raise MarketDataError(f"principal must be positive, got {principal}")
    if annual_rate < 0:
        raise MarketDataError(f"annual_rate must be non-negative, got {annual_rate}")
    if months < 1:
        raise MarketDataError(f"months must be >= 1, got {months}")
    monthly_interest = round(principal * annual_rate / 12.0, 2)
    return [
        {
            "id": flow_id,
            "account": account,
            "direction": "outflow",
            "amount": monthly_interest,
            "date": start.isoformat(),
            "recurrence": {"freq": "monthly", "count": months},
        }
    ]

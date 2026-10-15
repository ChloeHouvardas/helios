"""market.daily_quotes and market.trading_calendar — quote data, not money.

Two read-only reads over Tushare's `daily` and `trade_cal` endpoints, served
through the same resolver as the rate and registry tools so fixture, core and
cached-live modes all work and every result carries provenance.

Why no field here is named `*_cents`: a share price is a float in CNY per share,
meaningful only as of a trade date. A balance is integer cents. The two must
never be added together, and the first line of defence is that nothing in this
module produces a value that looks like money. `tests/test_market_quotes.py`
greps for it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import MarketDataResolver, _to_iso


def _iso(value: Any) -> Optional[str]:
    """Tushare compact date -> ISO, or None when upstream sent nothing."""
    if value is None or value == "":
        return None
    return _to_iso(value)

DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
CALENDAR_FIELDS = "exchange,cal_date,is_open,pretrade_date"

#: Units for every numeric field in a quote row. Returned with each result so a
#: caller never has to guess whether `vol` is shares or lots.
QUOTE_UNITS = {
    "open": "CNY per share (float)",
    "high": "CNY per share (float)",
    "low": "CNY per share (float)",
    "close": "CNY per share (float)",
    "pre_close": "CNY per share (float)",
    "change": "CNY per share (float)",
    "pct_chg": "percent (float)",
    "vol_lots": "手 / lots of 100 shares (float)",
    "amount_thousand_cny": "千元 / thousands of CNY (float)",
}


def _compact(iso: Optional[str]) -> Optional[str]:
    """'2026-10-15' -> '20261015'. Tushare takes compact dates."""
    if not iso:
        return None
    return str(iso).replace("-", "")


class QuotesService:
    def __init__(self, resolver: MarketDataResolver) -> None:
        self._resolver = resolver

    def daily_quotes(self, *, ts_code: str, start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     trade_date: Optional[str] = None) -> Dict[str, Any]:
        """Daily OHLCV for one listed security, oldest row first.

        Upstream ordering is not a contract (the live API returns newest-first),
        so rows are sorted here. Prices are floats with explicit units.
        """
        if not ts_code or not str(ts_code).strip():
            raise ValueError("ts_code must be a non-empty security code such as "
                             "'600519.SH' or '000001.SZ'")
        params: Dict[str, Any] = {"ts_code": str(ts_code).strip().upper()}
        if trade_date:
            params["trade_date"] = _compact(trade_date)
        else:
            if start_date:
                params["start_date"] = _compact(start_date)
            if end_date:
                params["end_date"] = _compact(end_date)

        result = self._resolver.fetch("daily", params, DAILY_FIELDS,
                                      category="quote",
                                      describe=f"daily quotes for {params['ts_code']}")
        rows = sorted(result.rows(), key=lambda r: str(r.get("trade_date", "")))
        quotes: List[Dict[str, Any]] = [
            {
                "trade_date": _iso(r.get("trade_date")),
                "open": r.get("open"), "high": r.get("high"),
                "low": r.get("low"), "close": r.get("close"),
                "pre_close": r.get("pre_close"),
                "change": r.get("change"), "pct_chg": r.get("pct_chg"),
                "vol_lots": r.get("vol"),
                "amount_thousand_cny": r.get("amount"),
            }
            for r in rows
        ]
        provenance = dict(result.provenance)
        if quotes:
            provenance["as_of"] = quotes[-1]["trade_date"]
        return {
            "ts_code": params["ts_code"],
            "count": len(quotes),
            "quotes": quotes,
            "units": dict(QUOTE_UNITS),
            "provenance": provenance,
        }

    def trading_calendar(self, *, start_date: str, end_date: str,
                         exchange: str = "SSE") -> Dict[str, Any]:
        """Trading days in a window, with upstream's own previous-trading-day.

        `is_open` arrives as 1/0 and is returned as a bool. Sorted oldest first.
        """
        if not start_date or not end_date:
            raise ValueError("trading_calendar needs both start_date and end_date "
                             "(ISO dates)")
        exchange = (exchange or "SSE").upper()
        params = {"exchange": exchange, "start_date": _compact(start_date),
                  "end_date": _compact(end_date)}
        result = self._resolver.fetch("trade_cal", params, CALENDAR_FIELDS,
                                      category="calendar",
                                      describe=f"{exchange} trading calendar "
                                               f"{start_date}..{end_date}")
        rows = sorted(result.rows(), key=lambda r: str(r.get("cal_date", "")))
        days = [
            {
                "date": _iso(r.get("cal_date")),
                "is_open": bool(int(r.get("is_open") or 0)),
                "pretrade_date": _iso(r.get("pretrade_date")),
            }
            for r in rows
        ]
        provenance = dict(result.provenance)
        if days:
            provenance["as_of"] = days[-1]["date"]
        return {
            "exchange": exchange,
            "count": len(days),
            "open_days": [d["date"] for d in days if d["is_open"]],
            "days": days,
            "provenance": provenance,
        }

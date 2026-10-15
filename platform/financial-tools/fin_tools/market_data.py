"""Live market data from Tushare — quote and reference-rate data only.

This module is the ONE place in `fin_tools` that may touch the network, and only
when a tool in this family is called with a token configured. Payments stay mock.

Three rules that are load-bearing rather than stylistic:

**1. Allow-listed endpoints, and an agent cannot name one.** `ALLOWED_APIS` is a
closed dict. No tool in this family accepts an `api_name` argument — the endpoint
is hard-coded per tool, so there is no path by which a caller-supplied string
reaches the request. A generic passthrough would let an agent reach any of
Tushare's several hundred endpoints, including ones that cost quota or return
data this system has no business holding.

**2. The token is never observable.** It is read from `TUSHARE_TOKEN` at call
time, sent only in the request body, never stored on an instance, never logged,
never placed in a `ToolSpec.declare()` payload, and redacted from every error
message. There is no `set_token()`: a token in a process global is a token in a
traceback.

**3. This is quote data, and it must never become money.** Tushare returns floats
(verified live: `close` 11.5, `pct_chg` -0.605). `contracts.World` says
"everything money-shaped is integer cents. No floats, ever." Both are right,
because they are different things — a share price is a *quote*, not a balance. The
separation is enforced structurally, not by convention:

  - this module never imports `World`, `Payments`, or anything that can write
    ledger or balance state;
  - every tool here declares `side_effects=False`, so the audit gate runs them
    directly and none of them can be a proposal that mutates state;
  - no field is named `*_cents`, because that name would imply integer money and
    invite a caller to add a price to a balance. Amounts carry their real unit in
    `unit`/`units`, and `provenance.as_of` says how old the number is.

An app that needs to *use* a price converts explicitly at its own boundary, in its
own code, and owns the rounding. See `specs/decisions.md` for the ratified rule.

Endpoint and field names were verified against the live API, not inherited from
the teammate adapter: its `lpr` / `TRADE_DATE` / `LPR1Y` names return Tushare
error 40101 "请指定正确的接口名".
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contracts import REGISTRY
from .service import ServiceUnavailable, UpstreamError

#: Environment variable the token is read from. Read at call time, never imported
#: into a module-level constant that could end up in a repr or a traceback.
TOKEN_ENV = "TUSHARE_TOKEN"

#: Default endpoint. The brief names `http://api.tushare.pro`; this defaults to
#: HTTPS on the same host because the token travels in the request body and a
#: plaintext POST would put a live credential on the wire. HTTPS was verified
#: working against the real API. Override `endpoint=` if plain HTTP is required.
DEFAULT_ENDPOINT = "https://api.tushare.pro"

#: How far back to look when resolving "the rate as of date X". Verified
#: necessary: a point query for a date with no publication returns ZERO rows
#: (LPR is monthly — `shibor_lpr` for 20261015 came back empty while the latest
#: row was 20260820), so asking for one date is not the same as asking for the
#: rate in force on that date.
RATE_LOOKBACK_DAYS = 400


class MarketDataError(UpstreamError):
    """The upstream answered, but refused or returned something unusable.

    Subclasses `UpstreamError` so `_wrap` reports it as `unavailable`, not
    `rejected`: Tushare saying "no such endpoint" or "no data in that window" is
    not an audit gate refusing an action, and recording it as a rejection would
    put a decision in the audit log that nobody made.

    Distinct from `ServiceUnavailable` in that a retry will not help — the
    upstream gave a definitive answer. Carries the upstream `code`/`msg`. Never
    carries the token.
    """

    def __init__(self, message: str, *, code: Optional[Any] = None) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# the allow-list
# ---------------------------------------------------------------------------

#: Closed set of endpoints this module may call. Adding one is a deliberate act
#: that must be verified against the live API first.
ALLOWED_APIS: Dict[str, Dict[str, Any]] = {
    # Trading calendar. Known-good per the competition's data guide.
    "trade_cal": {
        "fields": "exchange,cal_date,is_open,pretrade_date",
        "params": ("exchange", "start_date", "end_date"),
        "note": "is_open is int 1/0; pretrade_date is the previous trading day",
    },
    # Daily OHLCV quotes. Known-good per the competition's data guide.
    "daily": {
        "fields": ("ts_code,trade_date,open,high,low,close,pre_close,"
                   "change,pct_chg,vol,amount"),
        "params": ("ts_code", "trade_date", "start_date", "end_date"),
        "note": "prices CNY/share float; vol in 手 (lots, 1 lot = 100 shares); "
                "amount in 千元 (thousands of CNY); pct_chg in percent",
    },
    # Loan Prime Rate. Verified live: fields are date,1y,5y.
    "shibor_lpr": {
        "fields": "date,1y,5y",
        "params": ("date", "start_date", "end_date"),
        "note": "monthly publication; rates in percent",
    },
    # Shanghai Interbank Offered Rate. Verified live tenor set.
    "shibor": {
        "fields": "date,on,1w,2w,1m,3m,6m,9m,1y",
        "params": ("date", "start_date", "end_date"),
        "note": "rates in percent",
    },
    # Listed-company registry, for counterparty verification.
    "stock_basic": {
        "fields": "ts_code,symbol,name,fullname,industry,list_date,list_status",
        "params": ("ts_code", "name", "exchange", "list_status"),
        "note": "snapshot of ~5500 listed companies",
    },
}

#: Rate series this module exposes, mapped to (api, response field).
RATE_SERIES: Dict[str, Tuple[str, str]] = {
    "lpr_1y": ("shibor_lpr", "1y"),
    "lpr_5y": ("shibor_lpr", "5y"),
    "shibor_on": ("shibor", "on"),
    "shibor_1w": ("shibor", "1w"),
    "shibor_1m": ("shibor", "1m"),
    "shibor_3m": ("shibor", "3m"),
    "shibor_6m": ("shibor", "6m"),
    "shibor_1y": ("shibor", "1y"),
}


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

#: Injectable transport: (url, payload) -> parsed response dict. Tests pass a
#: fake; production uses urllib. Same shape as the `clock=` injection in
#: `build_world`.
PostJson = Callable[[str, Dict[str, Any]], Dict[str, Any]]


def _default_post_json(url: str, payload: Dict[str, Any],
                       timeout: float = 20.0) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _compact(iso: Any) -> str:
    """'2026-10-15' -> '20261015'. Tushare's date format, verified live."""
    return str(iso).replace("-", "")


def _iso_date(compact: Any) -> Optional[str]:
    """'20261015' -> '2026-10-15'. Passes through anything already ISO."""
    if compact is None:
        return None
    text = str(compact)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _describe(exc: BaseException) -> str:
    """An exception described without its args, which may echo the payload."""
    reason = getattr(exc, "reason", None)
    if reason is not None:                      # URLError
        return f"{type(exc).__name__}: {reason}"
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    return type(exc).__name__


def _redact(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, "[REDACTED]")
    return text


@dataclass
class TushareClient:
    """Minimal Tushare Pro client: stdlib urllib, plain dicts, no SDK.

    The token is read from the environment on every call rather than held on the
    instance, so it cannot appear in a `repr`, a pickle, or a log of this object.
    """

    endpoint: str = DEFAULT_ENDPOINT
    #: Injectable for offline tests. `None` means real urllib.
    post_json: Optional[PostJson] = None
    timeout: float = 20.0

    def fetch(self, api_name: str, params: Optional[Dict[str, Any]] = None,
              fields: str = "") -> List[Dict[str, Any]]:
        """Call one allow-listed endpoint and return a list of row dicts.

        Raises:
            ValueError: the api is not allow-listed, or a param is not permitted
                for it. A caller/coding error, not an outage.
            ServiceUnavailable: no token, or the network/HTTP layer failed. A
                retry may succeed, so this must not be reported as a rejection.
            MarketDataError: the upstream answered with a non-zero code.
        """
        spec = ALLOWED_APIS.get(api_name)
        if spec is None:
            raise ValueError(
                f"api {api_name!r} is not on the fin_tools allow-list; permitted "
                f"endpoints are {sorted(ALLOWED_APIS)}. This module never "
                f"forwards a caller-supplied api name to Tushare.")

        params = dict(params or {})
        allowed_params = set(spec["params"])
        unexpected = set(params) - allowed_params
        if unexpected:
            raise ValueError(
                f"api {api_name!r} does not accept param(s) "
                f"{sorted(unexpected)}; permitted params are "
                f"{sorted(allowed_params)}")

        for key in ("date", "start_date", "end_date"):
            if params.get(key):
                params[key] = _compact(params[key])

        token = os.environ.get(TOKEN_ENV) or ""
        if not token:
            # No credential is an outage from the caller's point of view, not a
            # policy rejection: the tool could not be reached.
            raise ServiceUnavailable(
                f"market data unavailable: {TOKEN_ENV} is not set, so no Tushare "
                f"call can be made. Set it, or use offline fixtures.")

        payload = {"api_name": api_name, "token": token, "params": params,
                   "fields": fields or spec["fields"]}
        post = self.post_json or _default_post_json

        try:
            response = post(self.endpoint, payload)
        except (HTTPError, URLError, socket.timeout, TimeoutError,
                ConnectionError, OSError) as exc:
            # Never interpolate `exc` raw: some HTTP errors echo the request
            # body, which contains the token.
            raise ServiceUnavailable(
                f"market data unavailable: Tushare {api_name} request failed "
                f"({_redact(_describe(exc), token)}). Retry later.") from None
        except (ValueError, TypeError) as exc:
            raise MarketDataError(
                f"Tushare {api_name} returned a response that was not valid "
                f"JSON: {_redact(str(exc), token)}") from None

        if not isinstance(response, dict):
            raise MarketDataError(
                f"Tushare {api_name} returned {type(response).__name__}, "
                f"expected a JSON object")

        code = response.get("code")
        if code != 0:
            raise MarketDataError(
                f"Tushare {api_name} refused the request (code {code}): "
                f"{_redact(str(response.get('msg') or ''), token)}",
                code=code)

        data = response.get("data") or {}
        resp_fields = list(data.get("fields") or [])
        items = data.get("items") or []
        if not resp_fields:
            return []
        return [dict(zip(resp_fields, row)) for row in items]


# ---------------------------------------------------------------------------
# helpers shared by the tools
# ---------------------------------------------------------------------------

def _latest_on_or_before(rows: Sequence[Dict[str, Any]], date_field: str,
                         value_field: str,
                         as_of: Optional[str]) -> Optional[Dict[str, Any]]:
    """The newest row with a non-null value, on or before `as_of`.

    Sorts explicitly because upstream order is not a contract — a `shibor_lpr`
    range was verified to come back newest-first, but relying on that would mean
    silently returning the OLDEST rate if that ever changed.
    """
    usable = [r for r in rows if r.get(value_field) is not None
              and r.get(date_field) is not None]
    if not usable:
        return None
    if as_of:
        target = _compact(as_of)
        usable = [r for r in usable if _compact(str(r[date_field])) <= target]
    if not usable:
        return None
    usable.sort(key=lambda r: _compact(str(r[date_field])))
    return usable[-1]


def _provenance(api: str, as_of: Optional[str], rows: int,
                endpoint: str) -> Dict[str, Any]:
    """Where a number came from, and how old it is.

    Always present on every result. A rate resolved by looking backwards carries
    the date it was actually published, so a caller can see that "today's LPR" is
    really the one published weeks ago rather than assuming it is current.
    """
    return {
        "publisher": "Tushare Pro",
        "api_name": api,
        "as_of": as_of,
        "rows_returned": rows,
        "retrieved_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "source_url": endpoint,
        "mode": "live",
    }


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def reference_rate(client: TushareClient, *, series: str,
                   as_of: Optional[str] = None) -> Dict[str, Any]:
    """A public reference rate in percent, as of a date.

    Resolves by looking back up to `RATE_LOOKBACK_DAYS` and taking the newest
    publication on or before `as_of`, because these series are published on their
    own schedule — a point query for an arbitrary date returns zero rows.
    `provenance.as_of` is the date the rate was actually published.
    """
    if series not in RATE_SERIES:
        raise ValueError(
            f"unknown rate series {series!r}; expected one of "
            f"{sorted(RATE_SERIES)}")
    api, field = RATE_SERIES[series]

    end = as_of or date.today().isoformat()
    start = (date.fromisoformat(end)
             - timedelta(days=RATE_LOOKBACK_DAYS)).isoformat()
    rows = client.fetch(api, {"start_date": start, "end_date": end})
    row = _latest_on_or_before(rows, "date", field, end)
    if row is None:
        # An honest unknown. Not zero, not a stale guess: these tools are
        # read-only, so "no data" is the correct answer.
        raise MarketDataError(
            f"no published value for {series!r} on or before {end} "
            f"(searched {api} back to {start}). Either the date precedes "
            f"publication or the series was not published in that window.")

    return {
        "series": series,
        "value_pct": float(row[field]),
        "unit": "percent per annum",
        "as_of": _iso_date(row["date"]),
        "requested_as_of": end,
        "provenance": _provenance(api, _iso_date(row["date"]), len(rows),
                                  client.endpoint),
    }


def daily_quotes(client: TushareClient, *, ts_code: str,
                 start_date: Optional[str] = None,
                 end_date: Optional[str] = None,
                 trade_date: Optional[str] = None) -> Dict[str, Any]:
    """Daily OHLCV quotes for one listed security.

    Prices are CNY per share as floats; `vol` is in 手 (lots of 100 shares) and
    `amount` in 千元 (thousands of CNY). These are quotes, not money — see the
    module docstring for why no field here is named `*_cents`.
    """
    if not ts_code or not str(ts_code).strip():
        raise ValueError("ts_code must be a non-empty security code such as "
                         "'600519.SH' or '000001.SZ'")

    params: Dict[str, Any] = {"ts_code": str(ts_code).strip().upper()}
    if trade_date:
        params["trade_date"] = trade_date
    else:
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

    rows = client.fetch("daily", params)
    # Sort oldest -> newest so a caller can read the series in time order
    # regardless of upstream ordering.
    rows.sort(key=lambda r: _compact(str(r.get("trade_date", ""))))
    return {
        "ts_code": params["ts_code"],
        "count": len(rows),
        "quotes": [
            {
                "trade_date": _iso_date(r.get("trade_date")),
                "open": r.get("open"), "high": r.get("high"),
                "low": r.get("low"), "close": r.get("close"),
                "pre_close": r.get("pre_close"),
                "change": r.get("change"), "pct_chg": r.get("pct_chg"),
                "vol_lots": r.get("vol"),
                "amount_thousand_cny": r.get("amount"),
            }
            for r in rows
        ],
        "units": {
            "open": "CNY per share (float)",
            "high": "CNY per share (float)",
            "low": "CNY per share (float)",
            "close": "CNY per share (float)",
            "change": "CNY per share (float)",
            "pct_chg": "percent (float)",
            "vol_lots": "手 / lots of 100 shares (float)",
            "amount_thousand_cny": "千元 / thousands of CNY (float)",
        },
        "provenance": _provenance("daily",
                                  _iso_date(rows[-1]["trade_date"]) if rows else None,
                                  len(rows), client.endpoint),
    }


def trading_calendar(client: TushareClient, *, start_date: str,
                     end_date: str, exchange: str = "SSE") -> Dict[str, Any]:
    """Trading days in a window, plus the previous trading day for each.

    `is_open` arrives as int 1/0 and is returned as a bool. `pretrade_date` is
    upstream's own previous-trading-day value, so resolving "the last session
    before this date" needs no arithmetic of ours.
    """
    rows = client.fetch("trade_cal", {"exchange": exchange,
                                      "start_date": start_date,
                                      "end_date": end_date})
    rows.sort(key=lambda r: _compact(str(r.get("cal_date", ""))))
    days = [
        {
            "date": _iso_date(r.get("cal_date")),
            "is_open": bool(r.get("is_open")),
            "pretrade_date": _iso_date(r.get("pretrade_date")),
        }
        for r in rows
    ]
    return {
        "exchange": exchange,
        "count": len(days),
        "open_days": [d["date"] for d in days if d["is_open"]],
        "days": days,
        "provenance": _provenance("trade_cal",
                                  days[-1]["date"] if days else None,
                                  len(rows), client.endpoint),
    }


def company_basic(client: TushareClient, *, query: Optional[str] = None,
                  ts_code: Optional[str] = None,
                  list_status: str = "L") -> Dict[str, Any]:
    """Listed-company registry facts, for counterparty verification.

    Looks up by exact `ts_code`, else by `name`. A miss returns an empty list
    rather than a fuzzy substitute: for a "does this counterparty match?" check,
    an invented match is worse than no answer.
    """
    params: Dict[str, Any] = {"list_status": list_status}
    if ts_code:
        params["ts_code"] = str(ts_code).strip().upper()
    elif query:
        params["name"] = str(query).strip()
    else:
        raise ValueError(
            "company_basic needs either ts_code (e.g. '600519.SH') or query "
            "(a registered company name); pass one of them")

    rows = client.fetch("stock_basic", params)
    matches = [
        {
            "ts_code": r.get("ts_code"), "symbol": r.get("symbol"),
            "name": r.get("name"), "fullname": r.get("fullname"),
            "industry": r.get("industry"),
            "list_date": _iso_date(r.get("list_date")),
            "list_status": r.get("list_status"),
        }
        for r in rows
    ]
    return {
        "count": len(matches),
        "matches": matches,
        "provenance": _provenance("stock_basic", None, len(rows),
                                  client.endpoint),
    }


# ---------------------------------------------------------------------------
# registration — follows the spreadsheet_tools.py pattern
# ---------------------------------------------------------------------------

def register(registry=REGISTRY, client: Optional[TushareClient] = None) -> None:
    """Register the `market.*` tools.

    `client` injects the transport, exactly as `build_world(clock=...)` injects a
    clock: tests pass a `TushareClient(post_json=fake)` and never reach a socket.
    Omit it and a default client reads `TUSHARE_TOKEN` at call time.

    All four tools are read-only. None can write ledger or balance state, and
    none is handed a `World`.
    """
    bound = client if client is not None else TushareClient()

    @registry.tool(
        "market.reference_rate",
        "A public reference rate (LPR or SHIBOR) in percent as of a date. "
        "Resolved as the newest publication on or before that date; "
        "provenance.as_of is the date it was actually published. Read-only quote "
        "data — never a balance, never integer cents. Requires TUSHARE_TOKEN.",
        {"series": "str", "as_of": "str|None"},
        {"series": "str", "value_pct": "float", "as_of": "str",
         "unit": "str", "provenance": "dict"},
        side_effects=False, tags=("market",),
    )
    def _reference_rate(series: str,
                        as_of: Optional[str] = None) -> Dict[str, Any]:
        return reference_rate(bound, series=series, as_of=as_of)

    @registry.tool(
        "market.daily_quotes",
        "Daily OHLCV quotes for one listed security. Prices are CNY per share as "
        "floats; vol is in lots of 100 shares and amount in thousands of CNY. "
        "Quote data only — it must never be added to a ledger balance. Requires "
        "TUSHARE_TOKEN.",
        {"ts_code": "str", "start_date": "str|None", "end_date": "str|None",
         "trade_date": "str|None"},
        {"ts_code": "str", "count": "int", "quotes": "list[dict]",
         "units": "dict", "provenance": "dict"},
        side_effects=False, tags=("market",),
    )
    def _daily_quotes(ts_code: str, start_date: Optional[str] = None,
                      end_date: Optional[str] = None,
                      trade_date: Optional[str] = None) -> Dict[str, Any]:
        return daily_quotes(bound, ts_code=ts_code, start_date=start_date,
                            end_date=end_date, trade_date=trade_date)

    @registry.tool(
        "market.trading_calendar",
        "Trading days in a window with each day's previous trading day. Use it to "
        "resolve 'as of' dates to a real session instead of guessing. Read-only. "
        "Requires TUSHARE_TOKEN.",
        {"start_date": "str", "end_date": "str", "exchange": "str|None"},
        {"exchange": "str", "count": "int", "open_days": "list[str]",
         "days": "list[dict]", "provenance": "dict"},
        side_effects=False, tags=("market",),
    )
    def _trading_calendar(start_date: str, end_date: str,
                          exchange: str = "SSE") -> Dict[str, Any]:
        return trading_calendar(bound, start_date=start_date,
                                end_date=end_date, exchange=exchange or "SSE")

    @registry.tool(
        "market.company_basic",
        "Listed-company registry facts by exact ts_code or registered name, for "
        "checking a counterparty. A miss returns an empty list rather than a "
        "fuzzy substitute. Read-only. Requires TUSHARE_TOKEN.",
        {"query": "str|None", "ts_code": "str|None", "list_status": "str|None"},
        {"count": "int", "matches": "list[dict]", "provenance": "dict"},
        side_effects=False, tags=("market",),
    )
    def _company_basic(query: Optional[str] = None,
                       ts_code: Optional[str] = None,
                       list_status: str = "L") -> Dict[str, Any]:
        return company_basic(bound, query=query, ts_code=ts_code,
                             list_status=list_status or "L")


__all__ = [
    "ALLOWED_APIS", "RATE_SERIES", "TOKEN_ENV", "DEFAULT_ENDPOINT",
    "MarketDataError", "TushareClient", "PostJson",
    "reference_rate", "daily_quotes", "trading_calendar", "company_basic",
    "register",
]

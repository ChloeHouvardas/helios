"""Registry wiring for the six `market.*` tools.

All read-only w.r.t. money (tags `("market",)`). `market.rate` additionally
publishes a verified graph node, so it is gated as a side effect on shared state;
the other five only read public data.

The resolver is bound per bundle, so a bundle's `HELIOS_MARKET_MODE` /
`HELIOS_DATA_DIR` / token environment is what its market tools use. Nothing here
invents a number: an unavailable series raises `MarketDataUnavailable`, which the
service surfaces as a rejection the model can act on.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..contracts import REGISTRY
from .client import MarketDataResolver, MarketDataUnavailable
from .rates import DISCLAIMER, SERIES, RateService
from .quotes import QuotesService
from .registry import RegistryService

_SERIES_ENUM = sorted(SERIES)

_RATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "series": {"type": "string", "enum": _SERIES_ENUM},
        "as_of": {"type": ["string", "null"],
                  "description": "ISO date; latest available on/before it. "
                                 "Null for the newest value."},
    },
    "required": ["series"],
    "additionalProperties": False,
}

_OPTION_KINDS = ["credit_line", "early_payment_discount", "delay_supplier",
                 "chase_customer"]
_COMPARE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "amount_cents": {"type": "integer", "minimum": 1},
        "days": {"type": "integer", "minimum": 1,
                 "description": "Horizon the cost covers."},
        "options": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": _OPTION_KINDS},
                    # credit_line
                    "base": {"type": "string", "enum": _SERIES_ENUM},
                    "spread_bps": {"type": "number"},
                    # early_payment_discount
                    "discount_pct": {"type": "number"},
                    "discount_days": {"type": "number"},
                    "net_days": {"type": "number"},
                    # delay_supplier
                    "late_fee_pct": {"type": ["number", "null"]},
                    # chase_customer
                    "customer_id": {"type": "string"},
                    "expected_days": {"type": ["number", "null"],
                                      "description": "From behavior.profile."},
                },
                "required": ["kind"],
            },
        },
    },
    "required": ["amount_cents", "days", "options"],
    "additionalProperties": False,
}

_COMPANY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1,
                             "description": "Company name, USCC or ts_code."}},
    "required": ["query"],
    "additionalProperties": False,
}

_QUOTES_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ts_code": {"type": "string", "minLength": 1,
                    "description": "Security code with exchange suffix, e.g. 600519.SH."},
        "start_date": {"type": ["string", "null"], "description": "ISO date."},
        "end_date": {"type": ["string", "null"], "description": "ISO date."},
        "trade_date": {"type": ["string", "null"],
                       "description": "One ISO date; overrides the range."},
    },
    "required": ["ts_code"],
    "additionalProperties": False,
}

_CALENDAR_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": "ISO date."},
        "end_date": {"type": "string", "description": "ISO date."},
        "exchange": {"type": ["string", "null"],
                     "description": "SSE (default), SZSE, BSE."},
    },
    "required": ["start_date", "end_date"],
    "additionalProperties": False,
}

_COLLECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "industry": {"type": ["string", "null"],
                     "description": "Shenwan industry name for the peer median."},
        "as_of": {"type": ["string", "null"], "description": "Report end date."},
    },
    "required": [],
    "additionalProperties": False,
}


def register(registry=REGISTRY, resolver: Optional[MarketDataResolver] = None,
             graph: Any = None) -> None:
    """Register the market tools bound to one resolver (and the graph, for rate).

    `resolver` defaults to `build_resolver()`, which reads the mode/token/data-dir
    from the environment at call time. Passing one in binds a specific source
    chain (a fixture resolver in tests, say).
    """
    if resolver is None:
        from .client import build_resolver

        resolver = build_resolver()
    rates = RateService(resolver, graph)
    registry_svc = RegistryService(resolver)
    quotes_svc = QuotesService(resolver)

    @registry.tool(
        "market.rate",
        "One public reference rate (LPR or SHIBOR) as of a date, in percent and "
        "basis points, with provenance. Publishes a VERIFIED knowledge-graph node "
        "market_rate:<series>@<date> so an app can cite an attested rate. "
        "Side-effecting (writes shared graph state). Returns MarketDataUnavailable "
        "-- never an invented number -- if no source has the series.",
        {"series": "str", "as_of": "str|None"},
        {"series": "str", "as_of": "str", "value_pct": "float",
         "value_bps": "int", "provenance": "dict", "node_id": "str"},
        side_effects=True, tags=("market",), input_json_schema=_RATE_SCHEMA,
    )
    def _rate(series: str, as_of: Optional[str] = None) -> Dict[str, Any]:
        return rates.rate(series=series, as_of=as_of)

    @registry.tool(
        "market.borrowing_cost_compare",
        "Price the real options behind a cash gap -- draw a credit line, take a "
        "supplier's early-payment discount, delay a bill, or chase a customer for "
        "free -- and return them ranked cheapest first. Each option carries its "
        "formula and inputs so the audit gate can recompute the cost from public "
        "numbers. Read-only w.r.t. money; the credit-line price uses today's "
        "LPR/SHIBOR. " + DISCLAIMER,
        {"amount_cents": "int", "days": "int", "options": "list[dict]"},
        {"options": "list[dict]", "cheapest": "str", "rate_used": "dict|None",
         "disclaimer": "str"},
        side_effects=False, tags=("market",), input_json_schema=_COMPARE_SCHEMA,
    )
    def _compare(amount_cents: int, days: int,
                 options: List[Dict[str, Any]]) -> Dict[str, Any]:
        return rates.borrowing_cost_compare(amount_cents=amount_cents, days=days,
                                            options=options)

    @registry.tool(
        "market.company_lookup",
        "Registry evidence for a listed company by name, Unified Social Credit "
        "Code (USCC) or ts_code: name, fullname, com_id, chairman, province, "
        "website, email, list_status, industry. Use it to check a claim such as "
        "'supplier announced a new bank account' against the registry. Read-only; "
        "a miss returns an empty list with provenance, not an invented match.",
        {"query": "str"},
        {"matches": "list[dict]", "count": "int", "provenance": "dict"},
        side_effects=False, tags=("market",), input_json_schema=_COMPANY_SCHEMA,
    )
    def _company(query: str) -> Dict[str, Any]:
        return registry_svc.company_lookup(query=query)

    @registry.tool(
        "market.collection_period",
        "National average receivables collection period (days), plus an industry "
        "peer median when an industry is given. The prior a forecast should assume "
        "for a NEW customer with no payment history -- and the result says so in "
        "'basis'. Read-only; returns MarketDataUnavailable rather than a guessed "
        "number if the source has no turnover data.",
        {"industry": "str|None", "as_of": "str|None"},
        {"national_average_days": "float", "provenance": "dict", "basis": "str"},
        side_effects=False, tags=("market",), input_json_schema=_COLLECTION_SCHEMA,
    )
    def _collection(industry: Optional[str] = None,
                    as_of: Optional[str] = None) -> Dict[str, Any]:
        return registry_svc.collection_period(industry=industry, as_of=as_of)

    @registry.tool(
        "market.daily_quotes",
        "Daily OHLCV quotes for one listed security, oldest first, with explicit "
        "units (CNY per share, lots, thousands of CNY) and provenance. Quote data, "
        "not money: no field is in cents. Read-only; raises MarketDataUnavailable "
        "rather than inventing a price.",
        {"ts_code": "str", "start_date": "str|None", "end_date": "str|None",
         "trade_date": "str|None"},
        {"ts_code": "str", "count": "int", "quotes": "list[dict]",
         "units": "dict", "provenance": "dict"},
        side_effects=False, tags=("market",), input_json_schema=_QUOTES_SCHEMA,
    )
    def _daily_quotes(ts_code: str, start_date: Optional[str] = None,
                      end_date: Optional[str] = None,
                      trade_date: Optional[str] = None) -> Dict[str, Any]:
        return quotes_svc.daily_quotes(ts_code=ts_code, start_date=start_date,
                                       end_date=end_date, trade_date=trade_date)

    @registry.tool(
        "market.trading_calendar",
        "Trading days in a window for an exchange (SSE default), each with "
        "upstream's own previous-trading-day, so 'the last session before this "
        "date' needs no arithmetic. Read-only.",
        {"start_date": "str", "end_date": "str", "exchange": "str|None"},
        {"exchange": "str", "count": "int", "open_days": "list[str]",
         "days": "list[dict]", "provenance": "dict"},
        side_effects=False, tags=("market",), input_json_schema=_CALENDAR_SCHEMA,
    )
    def _trading_calendar(start_date: str, end_date: str,
                          exchange: Optional[str] = None) -> Dict[str, Any]:
        return quotes_svc.trading_calendar(start_date=start_date, end_date=end_date,
                                           exchange=exchange or "SSE")

"""market.daily_quotes, market.trading_calendar, and the quote-is-not-money invariants.

The two tools were ported from an earlier single-module implementation. What is
pinned here, beyond their shapes:

- **Upstream ordering is not a contract.** The fixture is newest-first, as the
  live API returns it; the tools sort oldest-first. Trusting upstream order would
  hand a caller the oldest close as "today's".
- **Quote data never looks like money.** No output field of any market tool is
  named `*_cents`, and the market package never imports the ledger, payments or
  intent modules. Both are checked against the source, not by convention.
- **The caller never names an endpoint.** No registered tool takes `api_name`,
  and there is no `set_token` anywhere: a token in a process global is a token
  in a traceback.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from fin_tools import build_world
from fin_tools.contracts import ToolRegistry
from fin_tools.knowledge_graph import KnowledgeGraph
from fin_tools.market_data import build_resolver
from fin_tools.market_data.client import MarketDataResolver, MarketDataUnavailable
from fin_tools.market_data import tools as market_tools
from fin_tools.market_data.quotes import QUOTE_UNITS, QuotesService

PACKAGE = Path(market_tools.__file__).resolve().parent


@pytest.fixture
def reg():
    registry = ToolRegistry()
    market_tools.register(registry, build_resolver(mode="fixture"), KnowledgeGraph())
    return registry


def _walk(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{path}.{k}", k
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, path)


# -- daily_quotes -----------------------------------------------------------

def test_daily_quotes_and_calendar_are_registered_read_only(reg):
    for name in ("market.daily_quotes", "market.trading_calendar"):
        spec = reg.get(name)
        assert spec.side_effects is False, name
        assert "market" in spec.tags


def test_daily_quotes_sorted_oldest_first_despite_newest_first_upstream(reg):
    out = reg.call("market.daily_quotes", ts_code="000001.SZ",
                   start_date="2026-10-09", end_date="2026-10-15")
    dates = [q["trade_date"] for q in out["quotes"]]
    assert dates == sorted(dates)
    assert dates[0] == "2026-10-09" and dates[-1] == "2026-10-15"
    assert out["count"] == 5
    assert out["ts_code"] == "000001.SZ"


def test_daily_quotes_carry_units_and_fixture_provenance(reg):
    out = reg.call("market.daily_quotes", ts_code="000001.sz",
                   start_date="2026-10-09", end_date="2026-10-15")
    assert out["units"] == QUOTE_UNITS
    assert out["provenance"]["mode"] == "fixture"
    assert out["provenance"]["as_of"] == "2026-10-15"
    assert isinstance(out["quotes"][-1]["close"], float)


def test_daily_quotes_for_an_unknown_security_is_unavailable_not_invented(reg):
    with pytest.raises(MarketDataUnavailable) as exc:
        reg.call("market.daily_quotes", ts_code="999999.SZ",
                 start_date="2026-10-09", end_date="2026-10-15")
    assert "TUSHARE_TOKEN" in str(exc.value)


def test_daily_quotes_needs_a_security_code(reg):
    with pytest.raises(ValueError):
        reg.call("market.daily_quotes", ts_code="  ")


def test_a_point_query_sends_trade_date_not_a_range():
    captured = {}

    class Spy(MarketDataResolver):
        def __init__(self):
            super().__init__([])

        def fetch(self, api_name, params, fields, *, category="rate", describe=""):
            captured.update(api_name=api_name, params=params, category=category)
            raise MarketDataUnavailable("spy")

    with pytest.raises(MarketDataUnavailable):
        QuotesService(Spy()).daily_quotes(ts_code="600519.SH", trade_date="2026-10-15",
                                          start_date="2026-01-01")
    assert captured["api_name"] == "daily"
    assert captured["params"] == {"ts_code": "600519.SH", "trade_date": "20261015"}
    assert captured["category"] == "quote"


# -- trading_calendar -------------------------------------------------------

def test_trading_calendar_open_days_and_bool_flags(reg):
    out = reg.call("market.trading_calendar", start_date="2026-10-09",
                   end_date="2026-10-15")
    assert out["exchange"] == "SSE"
    assert out["open_days"] == ["2026-10-09", "2026-10-10", "2026-10-13",
                                "2026-10-14", "2026-10-15"]
    assert all(isinstance(d["is_open"], bool) for d in out["days"])
    weekend = next(d for d in out["days"] if d["date"] == "2026-10-11")
    assert weekend["is_open"] is False
    assert weekend["pretrade_date"] == "2026-10-10"
    assert out["provenance"]["mode"] == "fixture"


def test_trading_calendar_needs_both_dates(reg):
    with pytest.raises(ValueError):
        reg.call("market.trading_calendar", start_date="2026-10-09", end_date="")


# -- the invariants ---------------------------------------------------------

def test_no_market_result_has_a_cents_field(reg):
    results = [
        reg.call("market.daily_quotes", ts_code="000001.SZ",
                 start_date="2026-10-09", end_date="2026-10-15"),
        reg.call("market.trading_calendar", start_date="2026-10-09",
                 end_date="2026-10-15"),
        reg.call("market.rate", series="lpr_1y"),
        reg.call("market.company_lookup", query="平安"),
    ]
    for blob in results:
        for path, key in _walk(blob):
            assert not str(key).endswith("_cents"), path


def test_market_package_never_imports_money_or_intent_modules():
    forbidden = re.compile(r"^\s*(from|import)\s+(\.\.|fin_tools\.)?"
                           r"(payments|intent_verification|contracts\s+import[^\n]*\bWorld\b)",
                           re.M)
    for source in PACKAGE.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert not forbidden.search(text), source.name
        assert "World(" not in text and "Payments(" not in text, source.name


def test_no_registered_tool_accepts_an_endpoint_name():
    registry = build_world().registry
    for spec in registry.list():
        declared = spec.declare()
        schema = declared.get("input_json_schema") or declared.get("input_schema") or {}
        props = schema.get("properties", schema) if isinstance(schema, dict) else {}
        assert "api_name" not in props, spec.name


def test_there_is_no_set_token_anywhere():
    import fin_tools.market_data as md
    from fin_tools.market_data.client import TushareSource

    assert not hasattr(md, "set_token")
    assert not hasattr(TushareSource, "set_token")
    for source in PACKAGE.glob("*.py"):
        assert "def set_token" not in source.read_text(encoding="utf-8"), source.name

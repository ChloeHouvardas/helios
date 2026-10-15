"""Market data (`market.*`) — public reference rates and registry evidence.

Six read-only tools, each tied to a decision an owner already faces (see
`tools.py` for the three moments). Stdlib only. Everything is offline-testable
through fixtures and goes live when `TUSHARE_TOKEN` is set.

Submodules:
  client    transport, the MarketDataSource protocol, four sources, provenance,
            the mode resolver, token redaction
  cache     the SQLite TTL cache in front of the live source
  rates     market.rate + market.borrowing_cost_compare
  registry  market.company_lookup + market.collection_period
  quotes    market.daily_quotes + market.trading_calendar
  tools     the registry wiring for all six
"""
from __future__ import annotations

from .client import (
    CoreFileSource,
    FixtureSource,
    MarketDataResolver,
    MarketDataUnavailable,
    MarketResult,
    TushareSource,
    build_resolver,
    params_key,
)

__all__ = [
    "MarketDataUnavailable",
    "MarketResult",
    "MarketDataSource",
    "MarketDataResolver",
    "TushareSource",
    "CoreFileSource",
    "FixtureSource",
    "build_resolver",
    "params_key",
]


def __getattr__(name: str):
    if name == "MarketDataSource":
        from .client import MarketDataSource

        return MarketDataSource
    if name == "CachedSource":
        from .cache import CachedSource

        return CachedSource
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

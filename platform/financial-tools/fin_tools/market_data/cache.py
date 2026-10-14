"""SQLite TTL cache in front of the live Tushare source.

Rates are cached 24 h; registry/financial-indicator snapshots 7 d. The cache
lives under `HELIOS_DATA_DIR`; with no data dir it is a no-op pass-through, so
offline runs and tests never touch disk.

A cached hit is reported with `mode="cache"` and keeps the ORIGINAL provenance's
as_of date, so an app can see how old the number is -- a fallback is never
presented as live. When the wrapped source fails and a stale-but-present row is
available, it is served and flagged `stale=True`; that is the "on any live error
fall back to the newest cache" behaviour the brief asks for.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .client import (DATA_DIR_ENV, MarketDataUnavailable, MarketDataSource,
                     MarketResult, _is_stale, _provenance)

#: TTL in seconds per category.
TTL_SECONDS = {
    "rate": 24 * 60 * 60,        # 24 h
    "registry": 7 * 24 * 60 * 60,  # 7 d
    "indicator": 7 * 24 * 60 * 60,  # 7 d
}
DEFAULT_TTL = 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_cache (
    key        TEXT PRIMARY KEY,
    api_name   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    stored_at  REAL NOT NULL,
    ttl        REAL NOT NULL
)
"""


class CachedSource(MarketDataSource):
    """Wraps a source with a SQLite TTL cache. `mode` reports 'cache' on a hit."""

    mode = "cache"

    def __init__(self, inner: MarketDataSource, *, data_dir: Optional[str] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._inner = inner
        base = data_dir if data_dir is not None else os.environ.get(DATA_DIR_ENV)
        self._db_path = Path(base) / "market_cache.sqlite3" if base else None
        self._clock = clock
        self._conn: Optional[sqlite3.Connection] = None

    def _connect(self) -> Optional[sqlite3.Connection]:
        if self._db_path is None:
            return None
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.execute(_SCHEMA)
            self._conn.commit()
        return self._conn

    def _key(self, api_name: str, params: Dict[str, Any], fields: str) -> str:
        from .client import params_key

        return f"{api_name}:{params_key(api_name, params)}:{params_key('', {'fields': fields})}"

    def _get(self, key: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        if conn is None:
            return None
        row = conn.execute(
            "SELECT payload, stored_at, ttl FROM market_cache WHERE key = ?",
            (key,)).fetchone()
        if row is None:
            return None
        return {"payload": json.loads(row[0]), "stored_at": row[1], "ttl": row[2]}

    def _put(self, key: str, api_name: str, result: MarketResult, ttl: float) -> None:
        conn = self._connect()
        if conn is None:
            return
        payload = {"api_name": result.api_name, "params": result.params,
                   "fields": result.fields, "items": result.items,
                   "provenance": result.provenance}
        conn.execute(
            "INSERT OR REPLACE INTO market_cache(key, api_name, payload, stored_at, ttl) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, api_name, json.dumps(payload), self._clock(), ttl))
        conn.commit()

    def fetch(self, api_name: str, params: Dict[str, Any], fields: str,
              *, category: str = "rate") -> Optional[MarketResult]:
        key = self._key(api_name, params, fields)
        ttl = TTL_SECONDS.get(category, DEFAULT_TTL)
        cached = self._get(key)

        if cached is not None and (self._clock() - cached["stored_at"]) < cached["ttl"]:
            return self._from_payload(cached["payload"], fresh=True, category=category)

        try:
            result = self._inner.fetch(api_name, params, fields, category=category)
        except MarketDataUnavailable:
            # Live failed: serve the stale row if we have one, flagged stale.
            if cached is not None:
                return self._from_payload(cached["payload"], fresh=False,
                                          category=category)
            raise
        if result is None:
            return None
        self._put(key, api_name, result, ttl)
        return result

    def _from_payload(self, payload: Dict[str, Any], *, fresh: bool,
                      category: str) -> MarketResult:
        prov = dict(payload["provenance"])
        # A cache hit is mode 'cache', never 'live'. Recompute staleness from the
        # original as_of so an old number still reads as old.
        prov["mode"] = "cache"
        if not fresh:
            prov["stale"] = True
            prov["note"] = "served from cache after a live fetch failed"
        else:
            prov["stale"] = _is_stale(prov.get("as_of"), category)
        return MarketResult(api_name=payload["api_name"], params=payload["params"],
                            fields=payload["fields"], items=payload["items"],
                            provenance=prov)

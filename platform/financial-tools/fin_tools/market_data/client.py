"""Market-data transport, sources, provenance and the mode resolver.

One `MarketDataSource` protocol, four implementations, resolved by
`HELIOS_MARKET_MODE`:

  - `TushareSource`  live POST to https://api.tushare.pro (HTTPS only). Token
    from `TUSHARE_TOKEN`, never logged, redacted from every error string. A
    40-calls/min token bucket with retries and backoff.
  - `CoreFileSource` the organizers' snapshot files under `data/core/` (gitignored).
  - `FixtureSource`  raw envelopes under `fixtures/market/<api_name>/<sha1>.json`,
    labelled sample data, for offline tests and demos.
  - `CachedSource`   (cache.py) a SQLite TTL cache in front of a live source.

Every result carries `provenance` naming the publisher, api, as-of date, fetch
time, the mode it actually came from, whether it is stale, and the source URL. A
fallback is never presented as live: `mode` says which source answered. When no
source has the series, `MarketDataUnavailable` is raised with a model-actionable
message naming the series and the env var to set -- the tools are read-only, so
this is an honest "unknown", never an invented number.

Only these endpoints are used: `shibor_lpr`, `shibor`, `stock_basic`,
`stock_company`, `fina_indicator` (only `arturn_days`), `index_member_all`,
`daily` and `trade_cal`. There is deliberately no FX, CPI/PPI/PMI, yield-curve or
credit-tier support, and no tool accepts an endpoint name from its caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

#: Tushare Pro over HTTPS. The teammate adapter used plain http; that is wrong --
#: the token travels in the body and must not cross the wire unencrypted.
TUSHARE_URL = "https://api.tushare.pro"
TUSHARE_TOKEN_ENV = "TUSHARE_TOKEN"
PUBLISHER = "Tushare Pro"

#: Environment controls.
MODE_ENV = "HELIOS_MARKET_MODE"          # fixture | live | auto (default auto)
DATA_DIR_ENV = "HELIOS_DATA_DIR"         # cache + core-file root
CORE_DIR_NAME = "core"                   # <data_dir>/core/<dataset>.json

#: Token bucket: 40 calls/min (the brief's limit; stock_basic's own 50/min is
#: fetched once as a snapshot so the stricter shared budget is the binding one).
RATE_LIMIT_CALLS = 40
RATE_LIMIT_WINDOW_SECONDS = 60.0

#: How old an as-of date can be before a value is flagged stale. Rates move at
#: least monthly; registry/financial-indicator snapshots are annual-ish.
MAX_AGE_DAYS = {"rate": 40, "registry": 400, "indicator": 400,
                "quote": 7, "calendar": 400}


class MarketDataUnavailable(Exception):
    """No source had the requested series. Read-only tools answer 'unknown'."""


@dataclass(frozen=True)
class MarketResult:
    """Rows plus the provenance that says where they came from."""
    api_name: str
    params: Dict[str, Any]
    fields: List[str]
    items: List[List[Any]]
    provenance: Dict[str, Any]

    def rows(self) -> List[Dict[str, Any]]:
        return [dict(zip(self.fields, item)) for item in self.items]

    def first(self) -> Optional[Dict[str, Any]]:
        rows = self.rows()
        return rows[0] if rows else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def params_key(api_name: str, params: Dict[str, Any]) -> str:
    """Stable 12-hex key for a (api, params) pair -- the fixture filename."""
    canonical = json.dumps({"api_name": api_name, "params": params},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _redact(text: str, token: Optional[str]) -> str:
    """Strip the token from any string that might be logged or raised."""
    if token and token in text:
        return text.replace(token, "[REDACTED]")
    return text


def _provenance(*, api_name: str, as_of: Optional[str], mode: str,
                fetched_at: str, stale: bool, source_url: str,
                category: str, note: Optional[str] = None) -> Dict[str, Any]:
    prov: Dict[str, Any] = {
        "publisher": PUBLISHER,
        "api_name": api_name,
        "as_of": as_of,
        "fetched_at": fetched_at,
        "mode": mode,               # live | cache | core | fixture
        "stale": stale,
        "source_url": source_url,
    }
    if note:
        prov["note"] = note
    return prov


def _is_stale(as_of: Optional[str], category: str, now: Optional[date] = None) -> bool:
    if not as_of:
        return True
    try:
        when = date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc).date()
    return (now - when).days > MAX_AGE_DAYS.get(category, 40)


# --------------------------------------------------------------------------
# the source protocol
# --------------------------------------------------------------------------


class MarketDataSource:
    """Fetches one api/params/fields triple, or returns None if it has none.

    Returning None (rather than raising) lets the resolver fall through to the
    next source. Raising is reserved for a hard live failure the caller must see.
    """

    mode = "unknown"

    def fetch(self, api_name: str, params: Dict[str, Any], fields: str,
              *, category: str = "rate") -> Optional[MarketResult]:
        raise NotImplementedError


# --------------------------------------------------------------------------
# live: Tushare
# --------------------------------------------------------------------------


class _TokenBucket:
    """40 calls/min. `sleep` is injectable so tests never actually wait."""

    def __init__(self, *, limit: int = RATE_LIMIT_CALLS,
                 window: float = RATE_LIMIT_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._sleep = sleep
        self._stamps: Deque[float] = deque()

    def acquire(self) -> None:
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= self._window:
            self._stamps.popleft()
        if len(self._stamps) >= self._limit:
            wait = self._window - (now - self._stamps[0])
            if wait > 0:
                self._sleep(wait)
            now = self._clock()
            while self._stamps and now - self._stamps[0] >= self._window:
                self._stamps.popleft()
        self._stamps.append(self._clock())


class TushareSource(MarketDataSource):
    """Live Tushare Pro. HTTPS only; token from env; redacted everywhere."""

    mode = "live"

    def __init__(self, *, token: Optional[str] = None, url: str = TUSHARE_URL,
                 post_json: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
                 bucket: Optional[_TokenBucket] = None,
                 retries: int = 2, backoff: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 15.0) -> None:
        self._token = token if token is not None else os.environ.get(TUSHARE_TOKEN_ENV, "")
        self._url = url
        self._post_json = post_json
        self._bucket = bucket or _TokenBucket(sleep=sleep)
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _default_post(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def fetch(self, api_name: str, params: Dict[str, Any], fields: str,
              *, category: str = "rate") -> Optional[MarketResult]:
        if not self._token:
            return None            # not configured -> let the resolver fall through
        post = self._post_json or self._default_post
        payload = {"api_name": api_name, "token": self._token,
                   "params": params, "fields": fields}

        last_error: Optional[str] = None
        for attempt in range(self._retries + 1):
            self._bucket.acquire()
            try:
                response = post(self._url, payload)
            except (HTTPError, URLError, OSError, ValueError) as exc:
                # Never let the token reach a message. `exc` could echo the body.
                last_error = _redact(f"{type(exc).__name__}: {exc}", self._token)
                if attempt < self._retries:
                    self._sleep(self._backoff * (2 ** attempt))
                    continue
                raise MarketDataUnavailable(
                    f"Tushare {api_name!r} request failed after "
                    f"{self._retries + 1} attempts: {last_error}. Check network "
                    f"access and that {TUSHARE_TOKEN_ENV} is a valid token.") from None
            break

        if not isinstance(response, dict) or response.get("code") != 0:
            msg = _redact(str(response.get("msg") if isinstance(response, dict)
                              else response), self._token)
            raise MarketDataUnavailable(
                f"Tushare {api_name!r} returned an error: {msg}")

        data = response.get("data") or {}
        resp_fields = data.get("fields") or []
        items = data.get("items") or []
        if not items:
            return None            # a valid but empty answer -> fall through
        as_of = self._as_of_from_items(api_name, resp_fields, items, params)
        return MarketResult(
            api_name=api_name, params=dict(params), fields=list(resp_fields),
            items=[list(r) for r in items],
            provenance=_provenance(
                api_name=api_name, as_of=as_of, mode="live", fetched_at=_now_iso(),
                stale=_is_stale(as_of, category), source_url=self._url,
                category=category))

    @staticmethod
    def _as_of_from_items(api_name: str, fields: Sequence[str],
                          items: Sequence[Sequence[Any]],
                          params: Dict[str, Any]) -> Optional[str]:
        if "date" in fields:
            idx = fields.index("date")
            dates = sorted(str(r[idx]) for r in items if r[idx])
            if dates:
                return _to_iso(dates[-1])
        return params.get("date") and _to_iso(params["date"]) or None


def _to_iso(yyyymmdd: Any) -> str:
    """Tushare dates are 'YYYYMMDD'; normalise to 'YYYY-MM-DD'."""
    text = str(yyyymmdd)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


# --------------------------------------------------------------------------
# core files (the organizers' snapshot)
# --------------------------------------------------------------------------


class CoreFileSource(MarketDataSource):
    """Reads the organizers' snapshot files under <data_dir>/core/.

    Expected layout -- one JSON file per dataset, the same envelope shape Tushare
    returns so parsing is shared:

        <data_dir>/core/cd80_cn_lpr.json            -> {"fields": [...], "items": [[...]]}
        <data_dir>/core/cd80_stock_basic.json
        <data_dir>/core/cd80_stock_fin_indicator.json
        <data_dir>/core/cd80_ci_index_member.json

    `api_name` -> filename is mapped by `DATASET_FILES`; the whole file is
    returned and the caller filters, because these are snapshots, not queries.
    gitignored: a snapshot is data, not source.
    """

    mode = "core"

    #: Tushare api_name -> organizers' core-file basename.
    DATASET_FILES = {
        "shibor_lpr": "cd80_cn_lpr",
        "shibor": "cd80_cn_lpr",
        "stock_basic": "cd80_stock_basic",
        "stock_company": "cd80_stock_basic",
        "fina_indicator": "cd80_stock_fin_indicator",
        "index_member_all": "cd80_ci_index_member",
    }

    def __init__(self, *, data_dir: Optional[str] = None,
                 clock: Callable[[], date] = None) -> None:
        base = data_dir if data_dir is not None else os.environ.get(DATA_DIR_ENV)
        self._dir = Path(base) / CORE_DIR_NAME if base else None
        self._clock = clock or (lambda: datetime.now(timezone.utc).date())

    def fetch(self, api_name: str, params: Dict[str, Any], fields: str,
              *, category: str = "rate") -> Optional[MarketResult]:
        if self._dir is None:
            return None
        basename = self.DATASET_FILES.get(api_name)
        if not basename:
            return None
        path = self._dir / f"{basename}.json"
        if not path.is_file():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        resp_fields = blob.get("fields") or []
        items = blob.get("items") or []
        if not items:
            return None
        as_of = blob.get("as_of") or TushareSource._as_of_from_items(
            api_name, resp_fields, items, params)
        return MarketResult(
            api_name=api_name, params=dict(params), fields=list(resp_fields),
            items=[list(r) for r in items],
            provenance=_provenance(
                api_name=api_name, as_of=as_of, mode="core",
                fetched_at=blob.get("fetched_at", _now_iso()),
                stale=_is_stale(as_of, category, self._clock()),
                source_url=str(path), category=category,
                note="organizers' snapshot"))


# --------------------------------------------------------------------------
# fixtures (offline sample data)
# --------------------------------------------------------------------------


class FixtureSource(MarketDataSource):
    """Raw envelopes under fixtures/market/<api_name>/<sha1(params)[:12]>.json.

    Each file is `{"meta": {...}, "code": 0, "data": {"fields", "items"}}`, a
    handful of rows of labelled sample data. This is what makes every market tool
    testable offline and demoable without a token.
    """

    mode = "fixture"

    def __init__(self, *, root: Optional[str] = None,
                 clock: Callable[[], date] = None) -> None:
        if root is None:
            # <package>/fixtures/market — parents[2] is platform/financial-tools,
            # since this file is fin_tools/market_data/client.py.
            root = str(Path(__file__).resolve().parents[2] / "fixtures" / "market")
        self._root = Path(root)
        self._clock = clock or (lambda: datetime.now(timezone.utc).date())

    def path_for(self, api_name: str, params: Dict[str, Any]) -> Path:
        return self._root / api_name / f"{params_key(api_name, params)}.json"

    def fetch(self, api_name: str, params: Dict[str, Any], fields: str,
              *, category: str = "rate") -> Optional[MarketResult]:
        path = self.path_for(api_name, params)
        if not path.is_file():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        meta = blob.get("meta") or {}
        data = blob.get("data") or {}
        resp_fields = data.get("fields") or []
        items = data.get("items") or []
        if not items:
            return None
        as_of = meta.get("as_of") or TushareSource._as_of_from_items(
            api_name, resp_fields, items, params)
        return MarketResult(
            api_name=api_name, params=dict(params), fields=list(resp_fields),
            items=[list(r) for r in items],
            provenance=_provenance(
                api_name=api_name, as_of=as_of, mode="fixture",
                fetched_at=meta.get("fetched_at", _now_iso()),
                stale=_is_stale(as_of, category, self._clock()),
                source_url=meta.get("source_url", TUSHARE_URL),
                category=category, note="sample data"))


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------


class MarketDataResolver:
    """Picks sources by mode and returns the first that has the series.

    `auto`: live (cached) when a token is set, else core files, else fixtures.
    On any live failure the resolver keeps going down the list, so a Tushare
    outage degrades to the newest cache/core/fixture rather than erroring -- but
    the provenance then reports the mode that actually answered, never 'live'.
    """

    def __init__(self, sources: Sequence[MarketDataSource]) -> None:
        self._sources = list(sources)

    @property
    def sources(self) -> List[MarketDataSource]:
        return list(self._sources)

    def fetch(self, api_name: str, params: Dict[str, Any], fields: str, *,
              category: str = "rate", describe: str = "") -> MarketResult:
        errors: List[str] = []
        for source in self._sources:
            try:
                result = source.fetch(api_name, params, fields, category=category)
            except MarketDataUnavailable as exc:
                errors.append(f"{source.mode}: {exc}")
                continue
            if result is not None:
                return result
        target = describe or f"{api_name} {params}"
        raise MarketDataUnavailable(
            f"no market data for {target} (api {api_name!r}). Tried modes: "
            f"{', '.join(s.mode for s in self._sources)}. To go live set "
            f"{TUSHARE_TOKEN_ENV}; for offline sample data add a fixture under "
            f"fixtures/market/{api_name}/. "
            + ("Last errors: " + "; ".join(errors) if errors else ""))


def build_resolver(*, mode: Optional[str] = None, token: Optional[str] = None,
                   data_dir: Optional[str] = None,
                   fixture_root: Optional[str] = None,
                   post_json: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
                   ) -> MarketDataResolver:
    """Assemble the source chain for a mode.

    Import of `CachedSource` is local so this module has no SQLite-at-import cost
    and so cache.py can import from here without a cycle.
    """
    from .cache import CachedSource

    resolved_mode = (mode or os.environ.get(MODE_ENV) or "auto").lower()
    resolved_token = token if token is not None else os.environ.get(TUSHARE_TOKEN_ENV, "")
    resolved_data_dir = data_dir if data_dir is not None else os.environ.get(DATA_DIR_ENV)

    fixtures = FixtureSource(root=fixture_root)
    core = CoreFileSource(data_dir=resolved_data_dir)

    if resolved_mode == "fixture":
        return MarketDataResolver([fixtures])

    live = TushareSource(token=resolved_token, post_json=post_json)
    cached = CachedSource(live, data_dir=resolved_data_dir)

    if resolved_mode == "live":
        return MarketDataResolver([cached])

    # auto: live-if-token, then core, then fixtures.
    chain: List[MarketDataSource] = []
    if live.configured:
        chain.append(cached)
    chain.extend([core, fixtures])
    return MarketDataResolver(chain)

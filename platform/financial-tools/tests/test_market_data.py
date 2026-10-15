"""Live market data (`market.*`) — every test offline via an injected transport.

The properties under test, in the order they matter:

1. **The token is never observable.** Not in `declare()`, not in a `repr`, not in
   an error message, not in a log line.
2. **The allow-list is closed and an agent cannot widen it.** No tool takes an
   `api_name`; `TushareClient.fetch` refuses anything off the list.
3. **Unreachable is not refused.** A missing token or a timeout surfaces as
   `unavailable`, never as `rejected` — the audit log must not record a decision
   nobody made.
4. **Quote data never becomes money.** No `*_cents` field, and calling a market
   tool leaves `World.balances`/`World.ledger` untouched.
5. **Date resolution is honest.** A point query for an unpublished date returns
   zero rows, so a rate is resolved as the newest publication on-or-before the
   requested date, and `provenance.as_of` says which date that actually was.

The response envelopes below were captured from the real API (verified live), so
field names, float types, YYYYMMDD dates and int `is_open` are the real shapes.
"""
from __future__ import annotations

import json
import socket

import pytest

from fin_tools import (MarketDataError, ServiceUnavailable, TushareClient,
                       UpstreamError, build_world)
from fin_tools.market_data import ALLOWED_APIS, RATE_SERIES, TOKEN_ENV
from fin_tools.service import _wrap

TEST_TOKEN = "test-token-NEVER-in-output"


# --------------------------------------------------------------------------
# envelopes captured from the live API
# --------------------------------------------------------------------------

LPR_ENVELOPE = {"code": 0, "msg": "", "data": {
    "fields": ["date", "1y", "5y"],
    "items": [["20260820", 3.0, 3.5], ["20260720", 3.0, 3.5],
              ["20260622", 3.0, 3.5]]}}

SHIBOR_ENVELOPE = {"code": 0, "msg": "", "data": {
    "fields": ["date", "on", "1w", "2w", "1m", "3m", "6m", "9m", "1y"],
    "items": [["20261014", 1.269, 1.3484, 1.4, 1.4108, 1.43, 1.5, 1.55, 1.6]]}}

DAILY_ENVELOPE = {"code": 0, "msg": "", "data": {
    "fields": ["ts_code", "trade_date", "open", "high", "low", "close",
               "pre_close", "change", "pct_chg", "vol", "amount"],
    "items": [["000001.SZ", "20261014", 11.57, 11.72, 11.48, 11.5, 11.57,
               -0.07, -0.605, 969081.92, 1122283.85223]]}}

CALENDAR_ENVELOPE = {"code": 0, "msg": "", "data": {
    "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
    "items": [["SSE", "20261015", 1, "20261014"],
              ["SSE", "20261014", 1, "20261013"],
              ["SSE", "20261011", 0, "20261009"]]}}

STOCK_ENVELOPE = {"code": 0, "msg": "", "data": {
    "fields": ["ts_code", "symbol", "name", "fullname", "industry",
               "list_date", "list_status"],
    "items": [["600519.SH", "600519", "贵州茅台", "贵州茅台酒股份有限公司",
               "白酒", "20010827", "L"]]}}

ENVELOPES = {
    "shibor_lpr": LPR_ENVELOPE, "shibor": SHIBOR_ENVELOPE,
    "daily": DAILY_ENVELOPE, "trade_cal": CALENDAR_ENVELOPE,
    "stock_basic": STOCK_ENVELOPE,
}


class Recorder:
    """A fake `post_json` that records what was sent and replays an envelope."""

    def __init__(self, envelopes=None, raise_exc=None):
        self.envelopes = ENVELOPES if envelopes is None else envelopes
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def __call__(self, url, payload):
        self.calls.append({"url": url, "api_name": payload["api_name"],
                           "params": dict(payload["params"]),
                           "fields": payload["fields"],
                           "token": payload.get("token")})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.envelopes[payload["api_name"]]


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
    return TEST_TOKEN


@pytest.fixture
def bundle(token):
    return build_world(post_json=Recorder())


def market_names(registry) -> set:
    return {t.name for t in registry.list() if t.name.startswith("market.")}


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def test_four_market_tools_are_registered(bundle):
    assert market_names(bundle.registry) == {
        "market.reference_rate", "market.daily_quotes",
        "market.trading_calendar", "market.company_basic"}


def test_every_market_tool_is_read_only(bundle):
    """A market tool must never be a proposal that mutates state."""
    for name in market_names(bundle.registry):
        assert bundle.registry.get(name).side_effects is False, name


def test_no_tool_accepts_an_api_name(bundle):
    """The allow-list is the security boundary; nothing may widen it at runtime."""
    for spec in bundle.registry.list():
        assert "api_name" not in spec.input_schema, spec.name


def test_existing_tools_are_unaffected(bundle):
    names = {t.name for t in bundle.registry.list()}
    assert {"payments.send", "payments.balance", "email.send",
            "simulation.forecast"} <= names


# --------------------------------------------------------------------------
# the token is never observable
# --------------------------------------------------------------------------

def test_token_does_not_appear_in_any_declared_schema(bundle):
    """declare() is what an agent and the audit gate both consume."""
    blob = json.dumps(bundle.registry.catalog(), ensure_ascii=False)
    assert TEST_TOKEN not in blob


def test_token_is_not_stored_on_the_client(token):
    client = TushareClient(post_json=Recorder())
    assert not hasattr(client, "token")
    assert TEST_TOKEN not in repr(client)
    assert TEST_TOKEN not in json.dumps(client.__dict__, default=str)


def test_there_is_no_set_token_function():
    """A process-global token is a token in a traceback. There is no setter."""
    import fin_tools.market_data as md

    assert not hasattr(md, "set_token")
    assert not hasattr(md.TushareClient, "set_token")


def test_token_is_not_in_any_error_message(token, monkeypatch):
    recorder = Recorder(raise_exc=OSError(f"boom while posting {token}"))
    client = TushareClient(post_json=recorder)
    with pytest.raises(ServiceUnavailable) as exc:
        client.fetch("daily", {"ts_code": "000001.SZ"})
    assert TEST_TOKEN not in str(exc.value)


def test_token_is_redacted_from_an_upstream_error(token):
    recorder = Recorder(envelopes={"daily": {"code": 40101,
                                             "msg": f"bad token {token}",
                                             "data": None}})
    client = TushareClient(post_json=recorder)
    with pytest.raises(MarketDataError) as exc:
        client.fetch("daily", {"ts_code": "000001.SZ"})
    assert TEST_TOKEN not in str(exc.value)
    assert exc.value.code == 40101


def test_token_is_sent_in_the_body_only(token):
    recorder = Recorder()
    client = TushareClient(post_json=recorder)
    client.fetch("daily", {"ts_code": "000001.SZ"})
    sent = recorder.calls[0]
    assert sent["token"] == token           # present in the payload
    assert TEST_TOKEN not in sent["url"]    # never in the URL


# --------------------------------------------------------------------------
# the allow-list
# --------------------------------------------------------------------------

def test_a_non_allowlisted_api_is_refused(token):
    client = TushareClient(post_json=Recorder())
    with pytest.raises(ValueError, match="allow-list"):
        client.fetch("income", {"ts_code": "000001.SZ"})


def test_the_allowlist_is_exactly_the_verified_endpoints():
    assert set(ALLOWED_APIS) == {"trade_cal", "daily", "shibor_lpr", "shibor",
                                 "stock_basic"}


def test_an_unexpected_param_is_refused(token):
    client = TushareClient(post_json=Recorder())
    with pytest.raises(ValueError, match="does not accept param"):
        client.fetch("daily", {"ts_code": "000001.SZ", "api_name": "income"})


def test_an_unknown_rate_series_is_refused_actionably(bundle):
    with pytest.raises(ValueError, match="lpr_1y"):
        bundle.registry.call("market.reference_rate", series="cpi_yoy")


def test_teammate_endpoint_names_are_not_inherited():
    """Their `lpr`/`TRADE_DATE`/`LPR1Y` return Tushare 40101 — verified live."""
    assert "lpr" not in ALLOWED_APIS
    assert ALLOWED_APIS["shibor_lpr"]["fields"] == "date,1y,5y"
    assert ALLOWED_APIS["shibor"]["fields"].startswith("date,on,1w,2w,1m,3m")


# --------------------------------------------------------------------------
# unreachable is not refused
# --------------------------------------------------------------------------

def test_a_missing_token_is_unavailable_not_rejected(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    client = TushareClient(post_json=Recorder())
    with pytest.raises(ServiceUnavailable) as exc:
        client.fetch("daily", {"ts_code": "000001.SZ"})
    assert TOKEN_ENV in str(exc.value)


def test_a_network_failure_is_unavailable(token):
    from urllib.error import URLError

    client = TushareClient(post_json=Recorder(raise_exc=URLError("dns failed")))
    with pytest.raises(ServiceUnavailable):
        client.fetch("daily", {"ts_code": "000001.SZ"})


def test_a_timeout_is_unavailable(token):
    client = TushareClient(post_json=Recorder(raise_exc=socket.timeout()))
    with pytest.raises(ServiceUnavailable):
        client.fetch("daily", {"ts_code": "000001.SZ"})


def test_wrap_marks_unavailable_and_not_rejected():
    """The service.py fix: a timeout must not reach the gate as a rejection."""
    def boom():
        raise ServiceUnavailable("market data unavailable: no token")

    envelope = _wrap("market.reference_rate", boom)
    assert envelope["ok"] is False
    assert envelope["unavailable"] is True
    assert envelope.get("retryable") is True
    assert "rejected" not in envelope, \
        "an outage recorded as a rejection is a false audit entry"


def test_wrap_still_marks_a_real_rejection():
    envelope = _wrap("payments.send",
                     lambda: (_ for _ in ()).throw(ValueError("bad amount")))
    assert envelope["ok"] is False
    assert envelope["rejected"] is True
    assert "unavailable" not in envelope


def test_wrap_marks_an_upstream_refusal_unavailable():
    def boom():
        raise MarketDataError("Tushare daily refused (code 40101)", code=40101)

    envelope = _wrap("market.daily_quotes", boom)
    assert envelope["ok"] is False
    assert envelope["unavailable"] is True
    assert "rejected" not in envelope


def test_both_failure_classes_share_a_base():
    assert issubclass(ServiceUnavailable, UpstreamError)
    assert issubclass(MarketDataError, UpstreamError)


# --------------------------------------------------------------------------
# quote data never becomes money
# --------------------------------------------------------------------------

def test_no_market_output_field_is_named_like_money(token):
    """`*_cents` would imply integer money and invite adding a price to a balance."""
    recorder = Recorder()
    bundle = build_world(post_json=recorder)
    results = [
        bundle.registry.call("market.reference_rate", series="lpr_1y",
                             as_of="2026-10-15"),
        bundle.registry.call("market.daily_quotes", ts_code="000001.SZ"),
        bundle.registry.call("market.trading_calendar", start_date="2026-10-10",
                             end_date="2026-10-15"),
        bundle.registry.call("market.company_basic", ts_code="600519.SH"),
    ]
    for result in results:
        blob = json.dumps(result, ensure_ascii=False)
        assert "_cents" not in blob


def test_calling_every_market_tool_leaves_the_world_untouched(token):
    bundle = build_world(post_json=Recorder())
    before = (dict(bundle.world.balances), dict(bundle.world.ledger))

    bundle.registry.call("market.reference_rate", series="lpr_1y",
                         as_of="2026-10-15")
    bundle.registry.call("market.daily_quotes", ts_code="000001.SZ")
    bundle.registry.call("market.trading_calendar", start_date="2026-10-10",
                         end_date="2026-10-15")
    bundle.registry.call("market.company_basic", ts_code="600519.SH")

    assert (bundle.world.balances, bundle.world.ledger) == before
    assert before == ({}, {})


def test_the_module_cannot_reach_payments():
    """Structural: importing World or Payments here would break the separation."""
    import fin_tools.market_data as md

    source = open(md.__file__, encoding="utf-8").read()
    for forbidden in ("from .payments", "from .contracts import World",
                      "World(", "receive_payment", "send_payment"):
        assert forbidden not in source, f"market_data must not touch {forbidden}"


def test_prices_are_floats_and_documented_as_such(token):
    """The float collision, resolved by classification rather than conversion."""
    bundle = build_world(post_json=Recorder())
    out = bundle.registry.call("market.daily_quotes", ts_code="000001.SZ")
    quote = out["quotes"][0]
    assert quote["close"] == 11.5
    assert isinstance(quote["close"], float)
    assert "CNY per share" in out["units"]["close"]
    assert "lots of 100 shares" in out["units"]["vol_lots"]
    assert "thousands of CNY" in out["units"]["amount_thousand_cny"]


# --------------------------------------------------------------------------
# date resolution
# --------------------------------------------------------------------------

def test_a_rate_resolves_to_the_newest_publication_on_or_before(token):
    """The trap: a point query for an unpublished date returns zero rows."""
    bundle = build_world(post_json=Recorder())
    out = bundle.registry.call("market.reference_rate", series="lpr_1y",
                               as_of="2026-10-15")
    assert out["value_pct"] == 3.0
    # The requested date was 2026-10-15; LPR was last published 2026-08-20.
    assert out["as_of"] == "2026-08-20"
    assert out["requested_as_of"] == "2026-10-15"
    assert out["provenance"]["as_of"] == "2026-08-20"


def test_rate_lookup_sends_a_range_not_a_point_query(token):
    recorder = Recorder()
    build_world(post_json=recorder).registry.call(
        "market.reference_rate", series="lpr_1y", as_of="2026-10-15")
    params = recorder.calls[0]["params"]
    assert "start_date" in params and "end_date" in params
    assert "date" not in params


def test_dates_are_normalised_to_yyyymmdd(token):
    recorder = Recorder()
    build_world(post_json=recorder).registry.call(
        "market.daily_quotes", ts_code="000001.SZ",
        start_date="2026-10-09", end_date="2026-10-15")
    assert recorder.calls[0]["params"]["start_date"] == "20261009"
    assert recorder.calls[0]["params"]["end_date"] == "20261015"


def test_result_dates_are_returned_as_iso(token):
    bundle = build_world(post_json=Recorder())
    cal = bundle.registry.call("market.trading_calendar",
                               start_date="2026-10-10", end_date="2026-10-15")
    assert cal["days"][0]["date"] == "2026-10-11"
    assert cal["days"][0]["pretrade_date"] == "2026-10-09"


def test_row_order_is_not_trusted(token):
    """Upstream came back newest-first; a reversed payload must give the same
    answer, or we are depending on an ordering that is not a contract."""
    reversed_lpr = {"code": 0, "msg": "", "data": {
        "fields": ["date", "1y", "5y"],
        "items": [["20260622", 3.0, 3.5], ["20260820", 2.5, 3.5],
                  ["20260720", 3.0, 3.5]]}}
    bundle = build_world(post_json=Recorder({"shibor_lpr": reversed_lpr}))
    out = bundle.registry.call("market.reference_rate", series="lpr_1y",
                               as_of="2026-10-15")
    assert out["as_of"] == "2026-08-20"
    assert out["value_pct"] == 2.5          # the newest row, not the first


def test_quotes_are_returned_oldest_first(token):
    envelope = {"code": 0, "msg": "", "data": {
        "fields": ["ts_code", "trade_date", "close"],
        "items": [["000001.SZ", "20261014", 11.5],
                  ["000001.SZ", "20261013", 11.57]]}}
    bundle = build_world(post_json=Recorder({"daily": envelope}))
    out = bundle.registry.call("market.daily_quotes", ts_code="000001.SZ")
    assert [q["trade_date"] for q in out["quotes"]] == \
        ["2026-10-13", "2026-10-14"]


def test_a_missing_value_is_skipped_not_returned_as_zero(token):
    """A null rate must not become 0.0 — that is an invented number."""
    envelope = {"code": 0, "msg": "", "data": {
        "fields": ["date", "1y", "5y"],
        "items": [["20260820", None, 3.5], ["20260720", 3.1, 3.5]]}}
    bundle = build_world(post_json=Recorder({"shibor_lpr": envelope}))
    out = bundle.registry.call("market.reference_rate", series="lpr_1y",
                               as_of="2026-10-15")
    assert out["value_pct"] == 3.1
    assert out["as_of"] == "2026-07-20"


def test_no_data_at_all_is_an_error_not_a_guess(token):
    empty = {"code": 0, "msg": "", "data": {"fields": ["date", "1y", "5y"],
                                            "items": []}}
    bundle = build_world(post_json=Recorder({"shibor_lpr": empty}))
    with pytest.raises(MarketDataError, match="no published value"):
        bundle.registry.call("market.reference_rate", series="lpr_1y",
                             as_of="2026-10-15")


def test_an_empty_company_lookup_is_not_a_fuzzy_match(token):
    empty = {"code": 0, "msg": "", "data": {
        "fields": ["ts_code", "symbol", "name", "fullname", "industry",
                   "list_date", "list_status"],
        "items": []}}
    bundle = build_world(post_json=Recorder({"stock_basic": empty}))
    out = bundle.registry.call("market.company_basic", query="不存在的公司XYZ")
    assert out["count"] == 0
    assert out["matches"] == []


# --------------------------------------------------------------------------
# shapes and provenance
# --------------------------------------------------------------------------

def test_is_open_is_a_bool_not_an_int(token):
    bundle = build_world(post_json=Recorder())
    out = bundle.registry.call("market.trading_calendar",
                               start_date="2026-10-10", end_date="2026-10-15")
    assert all(isinstance(d["is_open"], bool) for d in out["days"])
    assert out["open_days"] == ["2026-10-14", "2026-10-15"]


def test_every_result_carries_provenance(token):
    bundle = build_world(post_json=Recorder())
    for result in (
        bundle.registry.call("market.reference_rate", series="lpr_1y",
                             as_of="2026-10-15"),
        bundle.registry.call("market.daily_quotes", ts_code="000001.SZ"),
        bundle.registry.call("market.trading_calendar",
                             start_date="2026-10-10", end_date="2026-10-15"),
        bundle.registry.call("market.company_basic", ts_code="600519.SH"),
    ):
        prov = result["provenance"]
        assert prov["publisher"] == "Tushare Pro"
        assert prov["mode"] == "live"
        assert prov["api_name"] in ALLOWED_APIS
        assert prov["source_url"].startswith("https://")


def test_rows_are_returned_as_dicts_not_lists(token):
    """Matching the package convention: plain dicts, no pandas, no objects."""
    client = TushareClient(post_json=Recorder())
    rows = client.fetch("daily", {"ts_code": "000001.SZ"})
    assert rows == [{"ts_code": "000001.SZ", "trade_date": "20261014",
                     "open": 11.57, "high": 11.72, "low": 11.48, "close": 11.5,
                     "pre_close": 11.57, "change": -0.07, "pct_chg": -0.605,
                     "vol": 969081.92, "amount": 1122283.85223}]
    assert all(isinstance(r, dict) for r in rows)


def test_all_rate_series_resolve(token):
    bundle = build_world(post_json=Recorder())
    lpr = [s for s in RATE_SERIES if s.startswith("lpr")]
    assert bundle.registry.call("market.reference_rate", series="lpr_5y",
                                as_of="2026-10-15")["value_pct"] == 3.5
    shibor = bundle.registry.call("market.reference_rate", series="shibor_1m",
                                  as_of="2026-10-15")
    assert shibor["value_pct"] == 1.4108
    assert len(lpr) == 2


def test_company_lookup_by_name(token):
    recorder = Recorder()
    bundle = build_world(post_json=recorder)
    out = bundle.registry.call("market.company_basic", query="贵州茅台")
    assert out["matches"][0]["fullname"] == "贵州茅台酒股份有限公司"
    assert recorder.calls[0]["params"]["name"] == "贵州茅台"


def test_company_lookup_needs_a_selector(token):
    bundle = build_world(post_json=Recorder())
    with pytest.raises(ValueError, match="ts_code"):
        bundle.registry.call("market.company_basic")


def test_quotes_need_a_ts_code(token):
    bundle = build_world(post_json=Recorder())
    with pytest.raises(ValueError, match="ts_code"):
        bundle.registry.call("market.daily_quotes", ts_code="  ")


def test_no_pandas_or_tushare_sdk_dependency():
    """The decided transport is stdlib urllib."""
    import fin_tools.market_data as md

    source = open(md.__file__, encoding="utf-8").read()
    assert "import pandas" not in source
    assert "import tushare" not in source
    assert "from tushare" not in source
    assert "urlopen" in source

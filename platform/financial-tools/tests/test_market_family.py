"""Task 5: the market-data family (`market.*`).

Four read-only tools over public reference data, each tied to a decision an owner
already faces. Every test is OFFLINE: data comes from `FixtureSource` (labelled
sample data) or an injected fake `post_json`, never a socket or a real token.

What is pinned down:

- **Provenance is honest.** A fixture/core/cache hit reports its own `mode`,
  never `live`. A fallback is never dressed up as a live quote.
- **No invented numbers.** A series no source has raises `MarketDataUnavailable`
  naming the series and the env var to set.
- **The arithmetic is returned.** `market.borrowing_cost_compare` ships each
  option's formula and inputs so the auditor can recompute; the 2/10-net-30
  discount annualises to ~37%.
- **The token never leaks.** It is read from env and redacted from every error.
- **Rate limiting.** The 40/min bucket blocks and the cache honours its TTL.
"""
from __future__ import annotations

import json

import pytest

from fin_tools import build_world
from fin_tools.contracts import ToolRegistry
from fin_tools.knowledge_graph import KnowledgeGraph
from fin_tools.market_data import build_resolver
from fin_tools.market_data.client import (MarketDataUnavailable, TushareSource,
                                          params_key, _TokenBucket, _redact)
from fin_tools.market_data.rates import DISCLAIMER
from fin_tools.market_data import tools as market_tools


@pytest.fixture
def reg():
    """A registry holding only the four market tools, in fixture mode."""
    resolver = build_resolver(mode="fixture")
    graph = KnowledgeGraph()
    registry = ToolRegistry()
    market_tools.register(registry, resolver, graph)
    return registry, graph


# --------------------------------------------------------------------------
# registration + read-only
# --------------------------------------------------------------------------

def test_market_tools_are_registered(reg):
    registry, _ = reg
    names = {t.name for t in registry.list()}
    assert names == {"market.rate", "market.borrowing_cost_compare",
                     "market.company_lookup", "market.collection_period"}


def test_market_tools_are_tagged_market(reg):
    registry, _ = reg
    for spec in registry.list():
        assert "market" in spec.tags, spec.name


def test_market_rate_is_gated_because_it_publishes(reg):
    """It writes a verified graph node, so it is a side effect on shared state."""
    registry, _ = reg
    assert registry.get("market.rate").side_effects is True


def test_the_other_three_are_read_only(reg):
    registry, _ = reg
    for name in ("market.borrowing_cost_compare", "market.company_lookup",
                 "market.collection_period"):
        assert registry.get(name).side_effects is False, name


# --------------------------------------------------------------------------
# market.rate
# --------------------------------------------------------------------------

def test_rate_returns_percent_and_bps_with_provenance(reg):
    registry, _ = reg
    out = registry.call("market.rate", series="lpr_1y")
    assert out["series"] == "lpr_1y"
    assert out["value_pct"] == 2.9
    assert out["value_bps"] == 290
    assert out["as_of"] == "2026-10-20"
    prov = out["provenance"]
    assert prov["publisher"] == "Tushare Pro"
    assert prov["api_name"] == "shibor_lpr"
    assert prov["mode"] == "fixture"        # honest: this came from a fixture
    assert prov["stale"] is False
    assert prov["source_url"]


def test_rate_publishes_a_verified_graph_node(reg):
    registry, graph = reg
    out = registry.call("market.rate", series="lpr_1y")
    node = graph.get(out["node_id"])
    assert node is not None
    assert node["provenance"] == "verified"
    assert node["attrs"]["value_bps"] == 290
    assert node["source"].startswith("Tushare Pro:")


def test_all_four_series_resolve(reg):
    registry, _ = reg
    values = {s: registry.call("market.rate", series=s)["value_pct"]
              for s in ("lpr_1y", "lpr_5y", "shibor_1m", "shibor_3m")}
    assert values == {"lpr_1y": 2.9, "lpr_5y": 3.4, "shibor_1m": 1.92,
                      "shibor_3m": 1.97}


def test_rate_as_of_picks_the_latest_row_on_or_before(reg):
    registry, _ = reg
    # The LPR fixture has rows on 08-20, 09-22 and 10-20 (the 10-20 row is 2.9).
    out = registry.call("market.rate", series="lpr_1y", as_of="2026-09-30")
    assert out["value_pct"] == 3.0          # the 09-22 row, not the 10-20 one
    assert out["as_of"] == "2026-09-22"


def test_unknown_series_is_actionable(reg):
    registry, _ = reg
    with pytest.raises(ValueError, match="unknown rate series"):
        registry.call("market.rate", series="cpi_yoy")   # deliberately out of scope


# --------------------------------------------------------------------------
# no invented numbers
# --------------------------------------------------------------------------

def test_missing_series_raises_not_a_guessed_number():
    """A resolver with only an empty fixture dir answers 'unknown', never 0."""
    resolver = build_resolver(mode="fixture", fixture_root="/nonexistent/market")
    registry = ToolRegistry()
    market_tools.register(registry, resolver, KnowledgeGraph())
    with pytest.raises(MarketDataUnavailable) as exc:
        registry.call("market.rate", series="lpr_1y")
    message = str(exc.value)
    assert "lpr_1y" in message
    assert "TUSHARE_TOKEN" in message       # names the env var to set


# --------------------------------------------------------------------------
# market.borrowing_cost_compare
# --------------------------------------------------------------------------

def _compare(registry, **overrides):
    kwargs = {"amount_cents": 1_000_000, "days": 20, "options": [
        {"kind": "credit_line", "base": "lpr_1y", "spread_bps": 150},
        {"kind": "early_payment_discount", "discount_pct": 2,
         "discount_days": 10, "net_days": 30},
    ]}
    kwargs.update(overrides)
    return registry.call("market.borrowing_cost_compare", **kwargs)


def test_discount_annualises_to_about_37_percent(reg):
    registry, _ = reg
    out = _compare(registry)
    discount = next(o for o in out["options"]
                    if o["kind"] == "early_payment_discount")
    # (2/(100-2)) * (365/(30-10)) * 100 = 37.24%
    assert discount["annualised_pct"] == pytest.approx(37.24, abs=0.05)


def test_credit_line_cost_uses_the_reference_rate(reg):
    registry, _ = reg
    out = _compare(registry)
    line = next(o for o in out["options"] if o["kind"] == "credit_line")
    # 2.9% LPR + 150bps = 4.4%; 1,000,000 cents * 4.4% * 20/365 = 2411 cents
    assert line["annualised_pct"] == pytest.approx(4.4)
    assert line["cost_cents"] == 2411
    assert out["rate_used"]["series"] == "lpr_1y"
    assert out["rate_used"]["value_pct"] == 2.9


def test_cheapest_option_is_selected(reg):
    registry, _ = reg
    out = _compare(registry, options=[
        {"kind": "credit_line", "base": "lpr_1y", "spread_bps": 150},
        {"kind": "chase_customer", "customer_id": "c1", "expected_days": 12},
    ])
    # chasing costs 0 in interest; the credit line costs 2411 cents
    assert out["cheapest"] == "chase_customer"


def test_delay_supplier_with_no_fee_is_free_in_cash_terms(reg):
    registry, _ = reg
    out = _compare(registry, options=[
        {"kind": "delay_supplier", "late_fee_pct": None}])
    assert out["options"][0]["cost_cents"] == 0


def test_every_option_carries_its_formula_and_inputs(reg):
    """The auditor recomputes from these; they must be present and complete."""
    registry, _ = reg
    out = _compare(registry)
    for option in out["options"]:
        assert option["formula"]
        assert option["inputs"]
        assert "amount_cents" in option["inputs"]


def test_disclaimer_is_always_present(reg):
    registry, _ = reg
    out = _compare(registry)
    assert out["disclaimer"] == DISCLAIMER
    assert "not financial advice" in out["disclaimer"]


def test_compare_rejects_a_bad_amount(reg):
    registry, _ = reg
    with pytest.raises(ValueError, match="amount_cents"):
        _compare(registry, amount_cents=0)


def test_compare_rejects_an_unknown_option_kind(reg):
    registry, _ = reg
    with pytest.raises(ValueError, match="unknown option kind"):
        _compare(registry, options=[{"kind": "sell_a_kidney"}])


def test_compare_rejects_an_inverted_discount_window(reg):
    registry, _ = reg
    with pytest.raises(ValueError, match="net_days"):
        _compare(registry, options=[
            {"kind": "early_payment_discount", "discount_pct": 2,
             "discount_days": 30, "net_days": 10}])


# --------------------------------------------------------------------------
# market.company_lookup
# --------------------------------------------------------------------------

def test_company_lookup_by_name(reg):
    registry, _ = reg
    out = registry.call("market.company_lookup", query="茅台")
    assert out["count"] == 1
    assert out["matches"][0]["name"] == "贵州茅台"
    assert out["provenance"]["mode"] == "fixture"


def test_company_lookup_by_ts_code_enriches(reg):
    registry, _ = reg
    out = registry.call("market.company_lookup", query="000001.SZ")
    match = out["matches"][0]
    assert match["com_id"] == "914403001922038216"
    assert match["chairman"] == "谢永林"
    assert match["province"] == "广东"


def test_company_lookup_by_uscc(reg):
    registry, _ = reg
    out = registry.call("market.company_lookup", query="914403001922038216")
    assert out["count"] == 1
    assert out["matches"][0]["com_id"] == "914403001922038216"


def test_company_lookup_miss_is_empty_not_invented(reg):
    registry, _ = reg
    out = registry.call("market.company_lookup", query="不存在的公司XYZ")
    assert out["count"] == 0
    assert out["matches"] == []
    assert out["provenance"]          # still carries where the (empty) answer came from


def test_company_lookup_needs_a_query(reg):
    registry, _ = reg
    with pytest.raises(ValueError, match="non-empty query"):
        registry.call("market.company_lookup", query="   ")


# --------------------------------------------------------------------------
# market.collection_period
# --------------------------------------------------------------------------

def test_collection_period_national_average(reg):
    registry, _ = reg
    out = registry.call("market.collection_period")
    # fixture arturn_days are 30, 10, 20 -> mean 20
    assert out["national_average_days"] == 20.0
    assert out["provenance"]["api_name"] == "fina_indicator"


def test_collection_period_says_it_is_a_prior(reg):
    registry, _ = reg
    out = registry.call("market.collection_period")
    assert "no payment history" in out["basis"]
    assert "not a prediction" in out["basis"]


def test_collection_period_industry_peer_median(reg):
    registry, _ = reg
    out = registry.call("market.collection_period", industry="银行")
    # the 银行 fixture has one member (000001.SZ, arturn 30) -> median 30
    assert out["industry_median_days"] == 30.0
    assert out["industry_sample_size"] == 1
    assert out["national_average_days"] == 20.0


# --------------------------------------------------------------------------
# provenance / resolver / redaction
# --------------------------------------------------------------------------

def test_auto_mode_without_a_token_uses_fixtures(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    resolver = build_resolver(mode="auto")
    modes = [s.mode for s in resolver.sources]
    assert "live" not in modes and "cache" not in modes
    assert modes == ["core", "fixture"]


def test_auto_mode_with_a_token_prefers_the_cached_live_source(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-token-value")
    resolver = build_resolver(mode="auto")
    assert resolver.sources[0].mode == "cache"


def test_live_mode_is_a_single_cached_source():
    resolver = build_resolver(mode="live", token="t")
    assert [s.mode for s in resolver.sources] == ["cache"]


def test_token_is_redacted_from_error_strings():
    text = "request failed for token abc123secret at https://api.tushare.pro"
    assert _redact(text, "abc123secret") == \
        "request failed for token [REDACTED] at https://api.tushare.pro"


def test_live_failure_message_never_contains_the_token():
    """A raising transport must not echo the token into the error."""
    captured = {}

    def bad_post(url, payload):
        captured["payload"] = payload
        raise OSError(f"boom while sending {payload['token']}")

    source = TushareSource(token="SUPERSECRET", post_json=bad_post, retries=0,
                           sleep=lambda s: None)
    with pytest.raises(MarketDataUnavailable) as exc:
        source.fetch("shibor_lpr", {}, "date,1y,5y")
    assert "SUPERSECRET" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value) or "SUPERSECRET" not in str(exc.value)


def test_live_source_parses_a_valid_envelope():
    envelope = {"code": 0, "msg": "",
                "data": {"fields": ["date", "1y", "5y"],
                         "items": [["20261020", 2.9, 3.4]]}}
    source = TushareSource(token="t", post_json=lambda url, payload: envelope,
                           sleep=lambda s: None)
    result = source.fetch("shibor_lpr", {}, "date,1y,5y")
    assert result.first()["1y"] == 2.9
    assert result.provenance["mode"] == "live"
    assert result.provenance["as_of"] == "2026-10-20"


def test_live_source_uses_https_only():
    from fin_tools.market_data.client import TUSHARE_URL
    assert TUSHARE_URL.startswith("https://")


# --------------------------------------------------------------------------
# token bucket + cache TTL
# --------------------------------------------------------------------------

def test_token_bucket_blocks_after_the_limit():
    slept = []
    clock = {"t": 0.0}

    def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds + 61        # jump past the window

    bucket = _TokenBucket(limit=2, window=60.0, clock=lambda: clock["t"],
                          sleep=fake_sleep)
    bucket.acquire()      # 1st
    bucket.acquire()      # 2nd
    bucket.acquire()      # 3rd -> must sleep
    assert slept, "the bucket should have throttled the third call"


def test_token_bucket_does_not_block_under_the_limit():
    slept = []
    bucket = _TokenBucket(limit=5, window=60.0, clock=lambda: 0.0,
                          sleep=slept.append)
    for _ in range(5):
        bucket.acquire()
    assert slept == []


def test_cache_serves_within_ttl_and_reports_cache_mode(tmp_path):
    from fin_tools.market_data.cache import CachedSource

    calls = {"n": 0}
    envelope = {"code": 0, "msg": "",
                "data": {"fields": ["date", "1y", "5y"],
                         "items": [["20261020", 2.9, 3.4]]}}

    def counting_post(url, payload):
        calls["n"] += 1
        return envelope

    inner = TushareSource(token="t", post_json=counting_post, sleep=lambda s: None)
    clock = {"t": 1000.0}
    cache = CachedSource(inner, data_dir=str(tmp_path), clock=lambda: clock["t"])

    first = cache.fetch("shibor_lpr", {}, "date,1y,5y", category="rate")
    second = cache.fetch("shibor_lpr", {}, "date,1y,5y", category="rate")
    assert calls["n"] == 1                     # second was served from cache
    assert first.provenance["mode"] == "live"
    assert second.provenance["mode"] == "cache"


def test_cache_refetches_after_ttl_expires(tmp_path):
    from fin_tools.market_data.cache import CachedSource, TTL_SECONDS

    calls = {"n": 0}
    envelope = {"code": 0, "msg": "",
                "data": {"fields": ["date", "1y", "5y"],
                         "items": [["20261020", 2.9, 3.4]]}}

    def counting_post(url, payload):
        calls["n"] += 1
        return envelope

    inner = TushareSource(token="t", post_json=counting_post, sleep=lambda s: None)
    clock = {"t": 1000.0}
    cache = CachedSource(inner, data_dir=str(tmp_path), clock=lambda: clock["t"])
    cache.fetch("shibor_lpr", {}, "date,1y,5y", category="rate")
    clock["t"] += TTL_SECONDS["rate"] + 1      # expire it
    cache.fetch("shibor_lpr", {}, "date,1y,5y", category="rate")
    assert calls["n"] == 2


def test_cache_serves_stale_row_when_live_fails(tmp_path):
    from fin_tools.market_data.cache import CachedSource

    envelope = {"code": 0, "msg": "",
                "data": {"fields": ["date", "1y", "5y"],
                         "items": [["20261020", 2.9, 3.4]]}}
    state = {"fail": False}

    def flaky_post(url, payload):
        if state["fail"]:
            raise OSError("tushare down")
        return envelope

    inner = TushareSource(token="t", post_json=flaky_post, retries=0,
                          sleep=lambda s: None)
    clock = {"t": 1000.0}
    cache = CachedSource(inner, data_dir=str(tmp_path), clock=lambda: clock["t"])
    cache.fetch("shibor_lpr", {}, "date,1y,5y", category="rate")   # populate

    clock["t"] += 100_000                    # past TTL, so it must go live...
    state["fail"] = True                     # ...which now fails
    stale = cache.fetch("shibor_lpr", {}, "date,1y,5y", category="rate")
    assert stale.provenance["mode"] == "cache"
    assert stale.provenance["stale"] is True
    assert stale.first()["1y"] == 2.9


def test_params_key_is_stable_and_order_independent():
    assert params_key("shibor", {"a": 1, "b": 2}) == params_key("shibor", {"b": 2, "a": 1})
    assert params_key("shibor", {}) != params_key("shibor_lpr", {})


# --------------------------------------------------------------------------
# end-to-end through a real bundle (fixture mode via env)
# --------------------------------------------------------------------------

def test_bundle_market_tools_work_in_fixture_mode(monkeypatch):
    monkeypatch.setenv("HELIOS_MARKET_MODE", "fixture")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    bundle = build_world()
    out = bundle.registry.call("market.rate", series="lpr_5y")
    assert out["value_pct"] == 3.4
    # and the node landed in the bundle's own graph
    assert bundle.graph.get(out["node_id"]) is not None

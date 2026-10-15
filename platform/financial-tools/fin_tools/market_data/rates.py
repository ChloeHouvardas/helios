"""market.rate and market.borrowing_cost_compare.

`market.rate` reads one public reference rate and publishes it as a VERIFIED
knowledge-graph node, so an app's cash-planner can cite a rate whose provenance
the graph attests to rather than one the agent asserted.

`market.borrowing_cost_compare` prices the real options behind "can I make
payroll on the 25th?" against today's LPR/SHIBOR and returns the arithmetic so
the audit gate can recompute every number. It never gives advice -- the
disclaimer is always present and it is read-only.

Both are read-only w.r.t. money. `market.rate` does write a graph node (that is
the point of publishing a verified fact), which is why the tool is gated.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .client import MarketDataResolver, MarketDataUnavailable, _to_iso

#: The four series the tools expose, and how to fetch each. `field` is the
#: column in the Tushare response; `api` is the endpoint.
SERIES: Dict[str, Dict[str, str]] = {
    "lpr_1y":   {"api": "shibor_lpr", "field": "1y",  "category": "rate"},
    "lpr_5y":   {"api": "shibor_lpr", "field": "5y",  "category": "rate"},
    "shibor_1m": {"api": "shibor",    "field": "1m",  "category": "rate"},
    "shibor_3m": {"api": "shibor",    "field": "3m",  "category": "rate"},
}

#: Fields requested per api (the brief's verified column names, lowercase).
API_FIELDS = {
    "shibor_lpr": "date,1y,5y",
    "shibor": "date,on,1w,2w,1m,3m,6m,9m,1y",
}

DISCLAIMER = "Indicative cost from public reference rates; not financial advice."


def _compact(as_of: Optional[str]) -> Optional[str]:
    """'2026-10-15' -> '20261015' (Tushare's date format)."""
    if not as_of:
        return None
    return str(as_of).replace("-", "")


class RateService:
    def __init__(self, resolver: MarketDataResolver, graph: Any) -> None:
        self._resolver = resolver
        self._graph = graph

    def _pick_row(self, rows: List[Dict[str, Any]], field: str,
                  as_of: Optional[str]) -> Optional[Dict[str, Any]]:
        """Latest row on-or-before `as_of`, else the newest row with a value."""
        dated = [r for r in rows if r.get(field) is not None]
        if not dated:
            return None
        dated.sort(key=lambda r: str(r.get("date", "")))
        if as_of:
            target = _compact(as_of) or ""
            upto = [r for r in dated if str(r.get("date", "")) <= target]
            return upto[-1] if upto else None
        return dated[-1]

    def rate(self, *, series: str, as_of: Optional[str] = None,
             publish: bool = True) -> Dict[str, Any]:
        """One reference rate as percent + basis points, with provenance.

        Publishes a VERIFIED graph node `market_rate:<series>@<date>` carrying the
        source and staleness, so the fact is attested rather than agent-asserted.
        """
        spec = SERIES.get(series)
        if spec is None:
            raise ValueError(
                f"unknown rate series {series!r}; expected one of "
                f"{', '.join(sorted(SERIES))}")

        api = spec["api"]
        # Fetch the snapshot with no date param: the fixture/core/live response
        # is a multi-date series, and `as_of` is applied locally by `_pick_row`.
        # Keying the fetch on `as_of` would defeat the cache and miss a snapshot
        # fixture, and would ask the live API for one day it may not return.
        result = self._resolver.fetch(api, {}, API_FIELDS[api],
                                      category=spec["category"],
                                      describe=f"reference rate {series!r}")
        row = self._pick_row(result.rows(), spec["field"], as_of)
        if row is None:
            raise MarketDataUnavailable(
                f"no value for series {series!r}"
                + (f" as of {as_of}" if as_of else "")
                + f" from {api}; the source returned rows without a "
                  f"{spec['field']!r} value on/before that date")

        value_pct = float(row[spec["field"]])
        value_bps = round(value_pct * 100)
        row_date = _to_iso(row.get("date")) or result.provenance.get("as_of")
        provenance = dict(result.provenance)
        provenance["as_of"] = row_date

        node_id = f"market_rate:{series}@{row_date}"
        if publish and self._graph is not None:
            # Verified, with the source: a rate the graph can vouch for.
            self._graph.upsert_verified(
                "market_rate", f"{series}@{row_date}",
                {"series": series, "value_pct": value_pct, "value_bps": value_bps,
                 "stale": provenance.get("stale", False)},
                source=f"{provenance.get('publisher')}:{api}")

        return {"series": series, "as_of": row_date, "value_pct": value_pct,
                "value_bps": value_bps, "provenance": provenance,
                "node_id": node_id}

    # -- borrowing-cost comparison ------------------------------------------

    def borrowing_cost_compare(self, *, amount_cents: int, days: int,
                               options: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Price each funding option and return them ranked, cheapest first.

        Every option carries its own `formula` and `inputs`, so the audit gate
        can recompute the cost from public numbers rather than trust ours.
        `amount_cents` is integer cents; `days` is the horizon the cost covers.
        """
        amount_cents = int(amount_cents)
        days = int(days)
        if amount_cents <= 0:
            raise ValueError(f"amount_cents must be positive, got {amount_cents!r}")
        if days <= 0:
            raise ValueError(f"days must be positive, got {days!r}")
        if not isinstance(options, list) or not options:
            raise ValueError("options must be a non-empty list of option objects")

        priced: List[Dict[str, Any]] = []
        rate_used: Optional[Dict[str, Any]] = None

        for option in options:
            kind = option.get("kind")
            if kind == "credit_line":
                entry, used = self._price_credit_line(amount_cents, days, option)
                if rate_used is None and used is not None:
                    rate_used = used
            elif kind == "early_payment_discount":
                entry = self._price_discount(amount_cents, days, option)
            elif kind == "delay_supplier":
                entry = self._price_delay(amount_cents, days, option)
            elif kind == "chase_customer":
                entry = self._price_chase(days, option)
            else:
                raise ValueError(
                    f"unknown option kind {kind!r}; expected credit_line, "
                    f"early_payment_discount, delay_supplier or chase_customer")
            priced.append(entry)

        # Cheapest by actual money over the horizon; ties broken by annualised
        # rate. Non-financial costs (supplier goodwill) are NOT priced -- see the
        # disclaimer.
        priced.sort(key=lambda o: (o["cost_cents"], o["annualised_pct"]))
        cheapest = priced[0]["kind"] if priced else None

        return {"options": priced, "cheapest": cheapest,
                "rate_used": rate_used, "amount_cents": amount_cents,
                "days": days, "disclaimer": DISCLAIMER}

    def _price_credit_line(self, amount_cents: int, days: int,
                           option: Dict[str, Any]):
        base_series = option.get("base", "lpr_1y")
        spread_bps = float(option.get("spread_bps", 0))
        rate = self.rate(series=base_series)          # publishes the verified node
        base_pct = rate["value_pct"]
        annualised_pct = base_pct + spread_bps / 100.0
        # loan cost = amount x (base+spread) x days/365, with (base+spread) as a
        # decimal fraction: annualised_pct/100.
        cost_cents = round(amount_cents * (annualised_pct / 100.0) * days / 365.0)
        entry = {
            "kind": "credit_line",
            "annualised_pct": round(annualised_pct, 4),
            "cost_cents": int(cost_cents),
            "formula": "cost_cents = amount_cents * (base_pct + spread_bps/100)/100 * days/365",
            "inputs": {"base": base_series, "base_pct": base_pct,
                       "spread_bps": spread_bps, "amount_cents": amount_cents,
                       "days": days, "as_of": rate["as_of"],
                       "provenance": rate["provenance"]},
        }
        used = {"series": base_series, "value_pct": base_pct,
                "as_of": rate["as_of"], "provenance": rate["provenance"]}
        return entry, used

    @staticmethod
    def _price_discount(amount_cents: int, days: int, option: Dict[str, Any]):
        d = float(option.get("discount_pct", 0))
        disc_days = float(option.get("discount_days", 0))
        net_days = float(option.get("net_days", 0))
        if not (0 <= d < 100):
            raise ValueError(f"discount_pct must be in [0,100), got {d!r}")
        if net_days <= disc_days:
            raise ValueError(
                f"net_days ({net_days}) must exceed discount_days ({disc_days})")
        # implied APR of forgoing the discount: (d/(100-d)) * (365/(net-disc))
        ratio = (d / (100.0 - d)) * (365.0 / (net_days - disc_days))
        annualised_pct = ratio * 100.0
        cost_cents = round(amount_cents * ratio * days / 365.0)
        return {
            "kind": "early_payment_discount",
            "annualised_pct": round(annualised_pct, 4),
            "cost_cents": int(cost_cents),
            "formula": "annualised = (d/(100-d)) * (365/(net-disc)); "
                       "cost_cents = amount_cents * annualised/100 * days/365",
            "inputs": {"discount_pct": d, "discount_days": disc_days,
                       "net_days": net_days, "amount_cents": amount_cents,
                       "days": days},
        }

    @staticmethod
    def _price_delay(amount_cents: int, days: int, option: Dict[str, Any]):
        late_fee_pct = option.get("late_fee_pct")
        if late_fee_pct is None:
            # Delaying with no late fee is free in cash terms (but risks the
            # supplier relationship -- not priced here; see disclaimer).
            return {"kind": "delay_supplier", "annualised_pct": 0.0,
                    "cost_cents": 0, "formula": "no late fee; cost_cents = 0",
                    "inputs": {"late_fee_pct": None, "amount_cents": amount_cents,
                               "days": days}}
        late_fee_pct = float(late_fee_pct)
        # A late fee is a one-off charge for the delay window; annualise it over
        # the horizon so it is comparable to the other options.
        cost_cents = round(amount_cents * late_fee_pct / 100.0)
        annualised_pct = (late_fee_pct / 100.0) * (365.0 / days) * 100.0
        return {
            "kind": "delay_supplier",
            "annualised_pct": round(annualised_pct, 4),
            "cost_cents": int(cost_cents),
            "formula": "cost_cents = amount_cents * late_fee_pct/100; "
                       "annualised_pct = late_fee_pct/100 * 365/days * 100",
            "inputs": {"late_fee_pct": late_fee_pct, "amount_cents": amount_cents,
                       "days": days},
        }

    @staticmethod
    def _price_chase(days: int, option: Dict[str, Any]):
        # Chasing a customer for money already owed costs nothing in interest;
        # the expected delay comes from behavior.profile, which the caller passes
        # in as expected_days (the market family owns no customer records).
        expected_days = option.get("expected_days")
        return {
            "kind": "chase_customer",
            "annualised_pct": 0.0,
            "cost_cents": 0,
            "formula": "cost_cents = 0 (chasing money already owed)",
            "inputs": {"customer_id": option.get("customer_id"),
                       "expected_days": expected_days, "horizon_days": days,
                       "note": "expected_days should come from behavior.profile"},
        }

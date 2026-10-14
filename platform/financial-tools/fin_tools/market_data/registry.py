"""market.company_lookup and market.collection_period — registry evidence.

These serve the second and third decision-moments:

  Moment 2b: "supplier announced a new bank account." Does the supplier on file
  match the sender of the change? `company_lookup` returns registry evidence
  (name, USCC, chairman, province, website, email, industry) so the hold has
  something to check the claim against.

  Moment 3: "new customer, no payment history." The forecast should assume the
  national average collection period until it learns, and say so.
  `collection_period` returns that national average (`arturn_days` from
  `fina_indicator`) plus the industry peer median (via `index_member_all`), each
  with provenance and a `basis` that states it is a national/peer average, not a
  prediction about this specific customer.

Endpoints used, nothing more: `stock_basic`, `stock_company`, `fina_indicator`
(only `arturn_days`), `index_member_all`. All read-only. There is deliberately no
credit-score tier, no FX, no macro series.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from .client import MarketDataResolver, MarketDataUnavailable

#: The brief's verified field lists.
STOCK_BASIC_FIELDS = ("ts_code,symbol,name,fullname,industry,list_date,"
                      "list_status,act_name")
STOCK_COMPANY_FIELDS = ("ts_code,com_id,chairman,province,city,website,email,"
                        "main_business")
FINA_FIELDS = "ts_code,end_date,arturn_days"
INDEX_MEMBER_FIELDS = "l1_code,l1_name,con_code,in_date,out_date,is_new"


class RegistryService:
    def __init__(self, resolver: MarketDataResolver) -> None:
        self._resolver = resolver

    # -- company lookup -----------------------------------------------------

    def company_lookup(self, *, query: str) -> Dict[str, Any]:
        """Find listed companies by name, Unified Social Credit Code or ts_code.

        Returns registry facts only. A miss is an empty list with provenance, not
        an error: "no listed company matches" is a real, honest answer, and the
        caller (a payables hold) needs to see it rather than have the tool invent
        a match.
        """
        if not query or not str(query).strip():
            raise ValueError("company_lookup needs a non-empty query "
                             "(a company name, USCC or ts_code)")
        needle = str(query).strip()

        basic = self._resolver.fetch("stock_basic", {"list_status": "L"},
                                     STOCK_BASIC_FIELDS, category="registry",
                                     describe=f"company registry match for {needle!r}")
        matches: List[Dict[str, Any]] = []
        basic_by_code = {r.get("ts_code"): r for r in basic.rows()}

        # A ts_code or a USCC (com_id) is looked up on the company endpoint; a
        # name is matched against stock_basic. Try the direct lookups first.
        direct = self._lookup_direct(needle)
        if direct:
            matches.extend(direct)
        else:
            lower = needle.lower()
            for code, row in basic_by_code.items():
                hay = " ".join(str(row.get(f, "")) for f in
                               ("name", "fullname", "act_name")).lower()
                if lower in hay:
                    matches.append(self._enrich(code, row))

        # De-duplicate by ts_code while preserving order.
        seen: set = set()
        unique: List[Dict[str, Any]] = []
        for match in matches:
            code = match.get("ts_code")
            if code in seen:
                continue
            seen.add(code)
            unique.append(match)

        return {"matches": unique[:50], "count": len(unique),
                "provenance": basic.provenance}

    def _lookup_direct(self, needle: str) -> List[Dict[str, Any]]:
        """Resolve an exact ts_code or USCC (com_id) via stock_company.

        Only attempted when the query LOOKS like a code, so a plain company name
        does not spend a live `stock_company` call before falling through to the
        name match. A ts_code has a dotted exchange suffix; a Unified Social
        Credit Code is an 18-char alphanumeric string.
        """
        is_ts_code = "." in needle and needle.split(".")[-1].upper() in ("SZ", "SH", "BJ")
        is_uscc = len(needle) == 18 and needle.isalnum()
        if not (is_ts_code or is_uscc):
            return []
        params: Dict[str, Any] = ({"ts_code": needle.upper()} if is_ts_code
                                  else {"com_id": needle})
        try:
            company = self._resolver.fetch("stock_company", params,
                                           STOCK_COMPANY_FIELDS, category="registry",
                                           describe=f"company registry for {needle!r}")
        except MarketDataUnavailable:
            return []
        rows = company.rows()
        if not rows:
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            code = row.get("ts_code")
            basic_row = self._basic_row(code) if code else None
            merged = dict(basic_row or {})
            merged.update({k: v for k, v in row.items() if v is not None})
            out.append(self._shape(merged, company.provenance))
        return out

    def _basic_row(self, ts_code: Optional[str]) -> Optional[Dict[str, Any]]:
        if not ts_code:
            return None
        try:
            basic = self._resolver.fetch("stock_basic", {"ts_code": ts_code},
                                         STOCK_BASIC_FIELDS, category="registry",
                                         describe=f"stock_basic for {ts_code!r}")
        except MarketDataUnavailable:
            return None
        return basic.first()

    def _enrich(self, code: str, basic_row: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(basic_row)
        try:
            company = self._resolver.fetch("stock_company", {"ts_code": code},
                                           STOCK_COMPANY_FIELDS, category="registry",
                                           describe=f"company registry for {code!r}")
            first = company.first()
            if first:
                row.update({k: v for k, v in first.items() if v is not None})
        except MarketDataUnavailable:
            pass          # registry detail is best-effort; the basic row still answers
        return self._shape(row, None)

    @staticmethod
    def _shape(row: Dict[str, Any], provenance: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        shaped = {
            "ts_code": row.get("ts_code"),
            "name": row.get("name"),
            "fullname": row.get("fullname"),
            "com_id": row.get("com_id"),
            "chairman": row.get("chairman"),
            "province": row.get("province"),
            "website": row.get("website"),
            "email": row.get("email"),
            "list_status": row.get("list_status"),
            "industry": row.get("industry"),
        }
        if provenance is not None:
            shaped["provenance"] = provenance
        return shaped

    # -- collection period --------------------------------------------------

    def collection_period(self, *, industry: Optional[str] = None,
                          as_of: Optional[str] = None) -> Dict[str, Any]:
        """National average + industry peer median receivables collection period.

        `arturn_days` is accounts-receivable turnover in days -- how long, on
        average, it takes to collect. For a customer with no payment history this
        is the honest prior, and the result says so in `basis`: it is a
        national/peer average, not a forecast about one specific customer.
        """
        national = self._national_average_arturn(as_of)
        result: Dict[str, Any] = {
            "national_average_days": national["value"],
            "provenance": national["provenance"],
            "basis": ("National/peer average receivables collection period. Use "
                      "as the prior for a customer with no payment history; "
                      "replace with behavior.profile once there is history. This "
                      "is an average, not a prediction about a specific customer."),
        }
        if industry:
            peer = self._industry_median_arturn(industry, as_of)
            if peer is not None:
                result["industry"] = industry
                result["industry_median_days"] = peer["value"]
                result["industry_sample_size"] = peer["n"]
                result["industry_provenance"] = peer["provenance"]
        return result

    def _arturn_rows(self, as_of: Optional[str]) -> tuple:
        params: Dict[str, Any] = {}
        if as_of:
            params["end_date"] = str(as_of).replace("-", "")
        result = self._resolver.fetch("fina_indicator", params, FINA_FIELDS,
                                      category="indicator",
                                      describe="receivables turnover days")
        return result.rows(), result.provenance

    def _national_average_arturn(self, as_of: Optional[str]) -> Dict[str, Any]:
        rows, provenance = self._arturn_rows(as_of)
        values = [float(r["arturn_days"]) for r in rows
                  if r.get("arturn_days") is not None]
        if not values:
            raise MarketDataUnavailable(
                "no receivables-turnover (arturn_days) data available from "
                "fina_indicator for the national collection period")
        return {"value": round(statistics.fmean(values), 2),
                "provenance": provenance}

    def _industry_median_arturn(self, industry: str,
                                as_of: Optional[str]) -> Optional[Dict[str, Any]]:
        """Median arturn_days among an industry's Shenwan constituents."""
        try:
            members = self._resolver.fetch(
                "index_member_all", {"l1_name": industry}, INDEX_MEMBER_FIELDS,
                category="registry",
                describe=f"Shenwan industry members for {industry!r}")
        except MarketDataUnavailable:
            return None
        codes = {r.get("con_code") for r in members.rows() if r.get("con_code")}
        if not codes:
            return None
        rows, provenance = self._arturn_rows(as_of)
        values = [float(r["arturn_days"]) for r in rows
                  if r.get("ts_code") in codes and r.get("arturn_days") is not None]
        if not values:
            return None
        return {"value": round(statistics.median(values), 2), "n": len(values),
                "provenance": provenance}

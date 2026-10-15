"""Funding options over the platform's `market.*` family.

What this module owns and what it borrows
-----------------------------------------
Rates and registry evidence come from the platform: `fin_tools.market_data`
serves LPR/SHIBOR and the listed-company registry through one resolver that
answers from fixtures offline (`HELIOS_MARKET_MODE=fixture`, or no token in
`auto` mode), from the organisers' core files, or live from Tushare when
`TUSHARE_TOKEN` is set. Every quote carries the platform's provenance, including
the mode that answered and whether the value is stale. Nothing here invents a
number: an unknown series or an unavailable source raises.

What stays app logic: ranking the three ways to fund one bill (credit line
within the facility limit, delaying the supplier at their late fee, taking an
early-payment discount) and comparing a payment-details change request against
the supplier record on file. Both are decisions about this business's data, not
market data, and both only rank or report -- drawing on the credit line is a
separate gated tool, and registry evidence never changes a verdict.

Money is `Decimal` and integer cents throughout. A platform rate arrives as a
float percentage and is converted with `Decimal(str(value_pct))` once, at the
boundary, so binary rounding never reaches an interest calculation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from book import Book

import config

from fin_tools.market_data import build_resolver
from fin_tools.market_data.client import MarketDataUnavailable
from fin_tools.market_data.rates import SERIES, RateService
from fin_tools.market_data.registry import RegistryService

# Basis-point scale: 1 bp = 0.01%.
_BPS = Decimal("10000")


# --------------------------------------------------------------------------
# Reference rates, from the platform
# --------------------------------------------------------------------------

#: The series this app quotes. Both are served by `market.rate`; the names are
#: the platform's, so a reader can look them up in the tool reference.
RATES: tuple[str, ...] = ("lpr_1y", "shibor_3m")

_resolver = None


def _services() -> tuple[RateService, RegistryService]:
    """One resolver per process, built lazily so tests can set the mode first."""
    global _resolver
    if _resolver is None:
        _resolver = build_resolver()
    # No graph: this app cites the rate on a page; publishing a verified node is
    # the cash planner's job, through the gated `market.rate` tool.
    return RateService(_resolver, None), RegistryService(_resolver)


def reset_sources() -> None:
    """Drop the cached resolver (tests that change `HELIOS_MARKET_MODE`)."""
    global _resolver
    _resolver = None


@dataclass(frozen=True)
class Rate:
    """One quoted rate, with the provenance a reviewer needs to judge it."""

    name: str
    percent: Decimal
    as_of: date
    source: str
    mode: str
    stale: bool
    raw_provenance: Mapping[str, Any] = field(default_factory=dict)

    def provenance(self) -> dict[str, Any]:
        out = dict(self.raw_provenance)
        out.update({"mode": self.mode, "source": self.source,
                    "as_of": self.as_of.isoformat(), "stale": self.stale})
        return out


def rate(name: str, *, as_of: date | None = None) -> Rate:
    """Look up a reference rate on the platform. Raises rather than guessing."""
    if name not in RATES or name not in SERIES:
        raise KeyError(
            f"unknown rate '{name}'; available: {', '.join(RATES)}. "
            "An unknown rate must fail loudly: substituting a default would put a "
            "made-up number into a financing recommendation."
        )
    rates, _ = _services()
    quoted = rates.rate(series=name, as_of=as_of.isoformat() if as_of else None,
                        publish=False)
    prov = dict(quoted.get("provenance") or {})
    published = str(quoted.get("as_of") or prov.get("as_of") or "")[:10]
    return Rate(
        name=name,
        # str() first: Decimal(2.9) would bake the float's binary expansion in.
        percent=Decimal(str(quoted["value_pct"])),
        as_of=date.fromisoformat(published),
        source=f"{prov.get('publisher', 'public reference data')} {prov.get('api_name', '')}".strip(),
        mode=str(prov.get("mode", "unknown")),
        stale=bool(prov.get("stale", True)),
        raw_provenance=prov,
    )


DISCLAIMER = (
    "Informational only, not financial advice. Rates come from public reference "
    "data (see provenance) and may be stale; confirm the current rate before "
    "borrowing."
)


# --------------------------------------------------------------------------
# Costing
# --------------------------------------------------------------------------

def bps_to_percent(bps: int | Decimal) -> Decimal:
    return (Decimal(int(bps)) / _BPS) * Decimal(100)


def _round_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def interest_cost_cents(principal_cents: int, annual_percent: Decimal,
                        days: int) -> int:
    """Simple interest over a number of days, in integer cents.

    Simple rather than compounded because these are short draws — days to a few
    weeks — where compounding would add precision the underlying rate does not
    have. Rounding is HALF_UP and happens once, at the end, so no intermediate
    float ever touches the money.
    """
    if principal_cents <= 0 or days <= 0:
        return 0
    annual = Decimal(int(principal_cents)) * (annual_percent / Decimal(100))
    return _round_cents(annual * Decimal(int(days)) / Decimal(365))


@dataclass
class Option:
    """One way to fund a shortfall, costed and ranked."""

    kind: str
    label: str
    cost_cents: int
    detail: str
    annual_percent: Decimal | None = None
    rate_name: str | None = None
    as_of: str = ""
    source: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    available: bool = True
    unavailable_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {
            "kind": self.kind,
            "label": self.label,
            "cost_cents": self.cost_cents,
            "cost": config.money(self.cost_cents),
            "detail": self.detail,
            "available": self.available,
        }
        if self.annual_percent is not None:
            out["annual_percent"] = str(self.annual_percent)
        if self.rate_name:
            out["rate_name"] = self.rate_name
        if self.as_of:
            out["as_of"] = self.as_of
        if self.source:
            out["source"] = self.source
        if self.provenance:
            out["provenance"] = self.provenance
        if not self.available:
            out["unavailable_reason"] = self.unavailable_reason
        return out


def compare_options(book: Book, bill_id: str, *, days_until_funds: int) -> dict[str, Any]:
    """Rank the ways to fund a bill the operating account cannot cover.

    Three options, costed on the same basis so the ranking means something:

      * `credit_line` — draw the shortfall for `days_until_funds` days at
        LPR 1y plus the configured spread. The draw is capped by
        `config.CREDIT_LINE_LIMIT_CENTS`, and the cap is enforced here rather
        than trusted: a shortfall beyond the facility is reported as unavailable,
        so the agent cannot propose a draw that does not exist.
      * `delay_supplier` — cost the supplier's late fee instead. Cheaper in cash
        terms almost always, and worse in every way that is not cash.
      * `early_payment_discount` — if the supplier offers one, taking it is a
        negative cost: it is money saved rather than spent. That is why it sorts
        first when it applies.

    Returns the ranking plus the provenance of every rate used and the disclaimer,
    so the hold and the financing page can show the owner exactly what number the
    recommendation rests on and how old it is.
    """
    amount = book.amount_cents(bill_id) or 0
    shortfall = book.shortfall_cents(bill_id)
    balance = book.balance_cents()
    supplier_id = book.supplier_id_for(bill_id) or ""
    supplier = book.supplier(supplier_id) or {}
    days = max(1, int(days_until_funds))

    options: list[Option] = []

    # --- credit line -------------------------------------------------------
    lpr = rate("lpr_1y")
    spread = bps_to_percent(config.CREDIT_LINE_SPREAD_BPS)
    annual = lpr.percent + spread
    principal = shortfall if shortfall > 0 else amount
    limit = config.CREDIT_LINE_LIMIT_CENTS
    if principal <= limit:
        options.append(Option(
            kind="credit_line",
            label=f"Draw {config.money(principal)} on the credit line for {days} days",
            cost_cents=interest_cost_cents(principal, annual, days),
            detail=(
                f"LPR 1y {lpr.percent}% + {config.CREDIT_LINE_SPREAD_BPS} bp spread "
                f"= {annual}% annual, for {days} day(s). Facility limit "
                f"{config.money(limit)}."
            ),
            annual_percent=annual,
            rate_name=lpr.name,
            as_of=lpr.as_of.isoformat(),
            source=lpr.source,
            provenance=lpr.provenance(),
        ))
    else:
        # Reported as unavailable rather than silently omitted: the owner should
        # see that the obvious option was considered and did not fit.
        options.append(Option(
            kind="credit_line",
            label="Credit line draw",
            cost_cents=0,
            detail=f"Shortfall {config.money(principal)} exceeds the facility limit "
                   f"{config.money(limit)}.",
            annual_percent=annual,
            rate_name=lpr.name,
            as_of=lpr.as_of.isoformat(),
            source=lpr.source,
            provenance=lpr.provenance(),
            available=False,
            unavailable_reason=(
                f"shortfall {config.money(principal)} exceeds the facility limit "
                f"{config.money(limit)}"
            ),
        ))

    # --- delay the supplier -------------------------------------------------
    late_bps = int(supplier.get("late_penalty_bps") or config.SUPPLIER_LATE_FEE_BPS)
    late_cost = _round_cents(
        Decimal(int(amount)) * bps_to_percent(late_bps) / Decimal(100))
    options.append(Option(
        kind="delay_supplier",
        label=f"Delay {book.supplier_name(supplier_id)} and pay the late fee",
        cost_cents=late_cost,
        detail=(
            f"{late_bps} bp late fee on {config.money(amount)} = "
            f"{config.money(late_cost)}. No cash leaves the account now, but the "
            "supplier relationship and any discount for prompt payment are at risk."
        ),
    ))

    # --- early payment discount --------------------------------------------
    # The discount may be a standing supplier term or a one-off on this bill.
    disc_bps = (supplier.get("early_payment_discount_bps")
                or (book.bill(bill_id) or {}).get("early_payment_discount_bps"))
    if disc_bps:
        saving = _round_cents(
            Decimal(int(amount)) * bps_to_percent(int(disc_bps)) / Decimal(100))
        options.append(Option(
            kind="early_payment_discount",
            label=f"Take the {disc_bps} bp early-payment discount",
            # Negative cost: this option saves money rather than spending it, so
            # it ranks first whenever the account can actually fund the bill.
            cost_cents=-saving,
            detail=(
                f"{disc_bps} bp discount on {config.money(amount)} saves "
                f"{config.money(saving)}. Only available if the bill can be paid "
                f"now — the account holds {config.money(balance)}, so it cannot "
                "without one of the options above."
            ),
            available=False,
            unavailable_reason=(
                f"needs {config.money(amount)} now; the account holds "
                f"{config.money(balance)}"
            ),
        ))

    # Rank: available first, then by cost. A cheap option nobody can use is not
    # the cheapest option.
    ranked = sorted(options, key=lambda o: (not o.available, o.cost_cents))
    cheapest = ranked[0] if ranked and ranked[0].available else None

    return {
        "bill_id": bill_id,
        "amount_cents": amount,
        "balance_cents": balance,
        "shortfall_cents": shortfall,
        "days_until_funds": days,
        "options": [o.as_dict() for o in ranked],
        "cheapest": cheapest.kind if cheapest else None,
        "cheapest_cost_cents": cheapest.cost_cents if cheapest else None,
        "rates_used": [
            {"name": lpr.name, "percent": str(lpr.percent),
             **lpr.provenance()},
        ],
        "stale": any(o.provenance.get("stale") for o in options if o.provenance),
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------
# Registry evidence for the changed-payee hold
# --------------------------------------------------------------------------

def registry_evidence(book: Book, supplier_id: str) -> dict[str, Any]:
    """Compare a payment-details change request against the registry on file.

    This is EVIDENCE ONLY. It never changes a verdict: `app.payee_details_changed`
    denies on the change flag alone, and the owner's re-verification in settings is
    what lifts the denial. A registry mismatch cannot make a denial stronger (it is
    already a denial) and a registry match must not weaken one — a match proves the
    company is who it says it is, not that this particular bank-account change was
    authorised by anyone who matters.

    For a listed supplier the comparison is against the public ticker's registry
    record; for a private one it says plainly that verification is unavailable,
    rather than staying silent and leaving the owner to wonder.
    """
    supplier = book.supplier(supplier_id) or {}
    ticker = str(supplier.get("public_ticker") or "")
    request = book.change_request(supplier_id) or {}

    if not ticker:
        return {
            "available": False,
            "supplier_id": supplier_id,
            "line": "not a listed company; registry verification unavailable",
            "reasons": [],
            "provenance": {"mode": "local-registry", "stale": False},
        }

    reasons: list[str] = []
    registered_site = str(supplier.get("registered_website") or "")
    sender_domain = str(request.get("sender_domain") or "")
    if registered_site and sender_domain and sender_domain != registered_site:
        reasons.append(
            f"registered website is {registered_site}, change came from "
            f"{sender_domain}"
        )

    legal_rep = str(supplier.get("legal_representative") or "")
    signatory = str(request.get("signatory") or "")
    if legal_rep and signatory and signatory != legal_rep:
        reasons.append(
            f"legal representative on file is {legal_rep}, request signed by "
            f"{signatory}"
        )

    account_holder = str(request.get("account_holder_name") or "")
    if account_holder and supplier_id:
        expected = str(supplier.get("name") or "")
        if expected and account_holder != expected:
            reasons.append(
                f"account holder on file is {expected}, request names "
                f"{account_holder}"
            )

    if reasons:
        line = "; ".join(reasons)
    else:
        line = (f"registry check found no mismatch for {ticker}, but the payment "
                "details still changed and re-verification is still required")

    return {
        "available": True,
        "supplier_id": supplier_id,
        "public_ticker": ticker,
        "line": line,
        "reasons": reasons,
        "mismatch": bool(reasons),
        "provenance": {"mode": "local-registry", "ticker": ticker, "stale": False},
        # The platform's registry record for the ticker, attached as evidence the
        # owner can read next to the request. A match or a miss changes nothing
        # above: the denial stands on the change flag alone.
        "registry": _platform_registry(ticker),
    }


def _platform_registry(ticker: str) -> dict[str, Any]:
    """`market.company_lookup` for a ticker, with its provenance; never raises."""
    try:
        _, registry = _services()
        found = registry.company_lookup(query=ticker)
    except (MarketDataUnavailable, ValueError) as exc:
        return {"available": False, "reason": str(exc)[:200]}
    matches = found.get("matches") or []
    first = matches[0] if matches else {}
    return {
        "available": True,
        "count": found.get("count", len(matches)),
        "name": first.get("name"),
        "fullname": first.get("fullname"),
        "website": first.get("website"),
        "chairman": first.get("chairman"),
        "provenance": found.get("provenance"),
    }


__all__ = [
    "DISCLAIMER", "RATES", "Option", "Rate", "bps_to_percent",
    "compare_options", "interest_cost_cents", "rate", "registry_evidence",
    "reset_sources",
]

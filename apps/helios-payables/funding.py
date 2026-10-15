"""Funding options — a LOCAL stand-in for the missing `market.*` tool family.

Why this file exists
--------------------
The session plan calls for `market.rate`, `market.borrowing_cost_compare` and
`market.registry_match`. None of them exist in this checkout: `fin_tools`
registers exactly twelve tools (`email.*`, `graph.*`, `payments.*`,
`simulation.forecast`, `spreadsheet.*`) and there is no `market` package anywhere
in the repo. The plan's own instruction covers this case — build the smallest
version needed inside the app and note it — so this is that smallest version.

It is deliberately small and honest about being small:

  * **No network.** Rates come from `RATES` below, a fixed table. There is no
    `TUSHARE_TOKEN` in the environment and no fixture-backed core file to read, so
    pretending to fetch would be pretending.
  * **Every result carries provenance.** `mode: "offline-fixture"`, the `as_of`
    date the rate belongs to, and `stale: True` whenever that date is not today.
    A rate the app cannot date is worse than no rate, so staleness is always
    explicit rather than inferred by the reader.
  * **It never decides anything.** This module ranks options. Whether to draw on
    the credit line is a gated `pay_bill` call, and the ranking carries a
    disclaimer saying it is information and not financial advice — which is both
    the licence position (no unlicensed investment advice) and the honest one.

If `market.*` lands later, this file is the only thing to delete: `tools.py`
imports `compare_options` and `registry_evidence`, and nothing else does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from book import Book

import config

# Basis-point scale: 1 bp = 0.01%.
_BPS = Decimal("10000")


# --------------------------------------------------------------------------
# Offline rate table
# --------------------------------------------------------------------------

#: Public reference rates, as of the date in `AS_OF`. These are the only numbers
#: in this file that come from outside the app, and they are pinned rather than
#: fetched. `AS_OF` is what makes them checkable: a reader can tell how old the
#: quote is without asking the code.
AS_OF = date(2026, 9, 20)

RATES: dict[str, Decimal] = {
    # China's 1-year Loan Prime Rate, quoted by the PBoC monthly.
    "lpr_1y": Decimal("3.00"),
    # Shanghai Interbank Offered Rate, 3-month tenor.
    "shibor_3m": Decimal("1.85"),
}


@dataclass(frozen=True)
class Rate:
    """One quoted rate, with the provenance a reviewer needs to judge it."""

    name: str
    percent: Decimal
    as_of: date
    source: str
    mode: str = "offline-fixture"

    @property
    def stale(self) -> bool:
        """True unless this quote is dated today.

        Computed rather than stored, so it cannot be forgotten when the fixture
        ages. A stale rate is still usable — it is the best the app has — but the
        UI must say so, because a rate presented as current is a misrepresentation.
        """
        return self.as_of != date.today()

    def provenance(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "stale": self.stale,
        }


_SOURCES = {
    "lpr_1y": "PBoC 1-year Loan Prime Rate (LPR), offline fixture",
    "shibor_3m": "SHIBOR 3-month tenor, offline fixture",
}


def rate(name: str, *, as_of: date | None = None) -> Rate:
    """Look up a reference rate. Raises rather than guessing on an unknown name."""
    if name not in RATES:
        raise KeyError(
            f"unknown rate '{name}'; available: {', '.join(sorted(RATES))}. "
            "An unknown rate must fail loudly: substituting a default would put a "
            "made-up number into a financing recommendation."
        )
    return Rate(
        name=name,
        percent=RATES[name],
        as_of=as_of or AS_OF,
        source=_SOURCES[name],
    )


DISCLAIMER = (
    "Informational only, not financial advice. Rates are pinned offline fixtures "
    "and may be stale; confirm the current rate before borrowing."
)


# --------------------------------------------------------------------------
# Costing
# --------------------------------------------------------------------------

def _bps_to_percent(bps: int | Decimal) -> Decimal:
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
    spread = _bps_to_percent(config.CREDIT_LINE_SPREAD_BPS)
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
        Decimal(int(amount)) * _bps_to_percent(late_bps) / Decimal(100))
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
            Decimal(int(amount)) * _bps_to_percent(int(disc_bps)) / Decimal(100))
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
    }


__all__ = [
    "AS_OF", "DISCLAIMER", "RATES", "Option", "Rate", "compare_options",
    "interest_cost_cents", "rate", "registry_evidence",
]

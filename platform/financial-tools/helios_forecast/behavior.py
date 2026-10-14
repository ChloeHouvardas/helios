"""Behavioural timing models: learn per-customer payment behaviour from
verified payment records, with partial pooling toward segment priors.

This is the P1 layer from specs/forecasting-simulation.md §3.5/§8: the
statistically principled version of the customer → segment → global fallback
ladder. Per-customer estimates are *shrunk* toward the segment mean with
strength proportional to evidence (a discrete analogue of empirical-Bayes
partial pooling; see specs/forecasting-research-claims.md C6), so:

- a customer with many payments keeps mostly their own pattern,
- a customer with few payments borrows the segment pattern,
- a brand-new customer *is* the segment pattern (cold start, built in).

Everything is a pure function over JSON-compatible inputs; outputs are the
``offset_days`` / ``promise_kept`` shapes the projection engine consumes.

Record (dict form)::

    {"customer_id": "cust-1", "segment": "retail",
     "due": "2026-09-30", "paid": "2026-10-07"}     # paid - due = +7 days late

Promise record (dict form)::

    {"customer_id": "cust-1", "segment": "retail", "kept": true}
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .model import ForecastError, _as_date


@dataclass(frozen=True)
class PaymentRecord:
    customer_id: str
    segment: str
    due: date
    paid: date

    @property
    def offset_days(self) -> int:
        return (self.paid - self.due).days


@dataclass(frozen=True)
class PromiseRecord:
    customer_id: str
    segment: str
    kept: bool


def parse_payment_record(raw: object) -> PaymentRecord:
    if not isinstance(raw, dict):
        raise ForecastError(f"payment record: expected an object, got {raw!r}")
    for key in ("customer_id", "segment", "due", "paid"):
        if key not in raw:
            raise ForecastError(f"payment record: missing required field {key!r}")
    rec = PaymentRecord(
        customer_id=str(raw["customer_id"]),
        segment=str(raw["segment"]),
        due=_as_date(raw["due"], "payment record due"),
        paid=_as_date(raw["paid"], "payment record paid"),
    )
    if rec.paid < rec.due:
        # Early payment is legitimate; paid before the invoice existed is not.
        raise ForecastError(
            f"payment record for {rec.customer_id!r}: paid date is before due date"
        )
    return rec


def parse_promise_record(raw: object) -> PromiseRecord:
    if not isinstance(raw, dict):
        raise ForecastError(f"promise record: expected an object, got {raw!r}")
    for key in ("customer_id", "segment", "kept"):
        if key not in raw:
            raise ForecastError(f"promise record: missing required field {key!r}")
    if not isinstance(raw["kept"], bool):
        raise ForecastError(f"promise record 'kept': expected true/false, got {raw['kept']!r}")
    return PromiseRecord(
        customer_id=str(raw["customer_id"]),
        segment=str(raw["segment"]),
        kept=raw["kept"],
    )


def _discrete(offsets: list[int]) -> dict[int, float]:
    """Empirical distribution over whole-day offsets: day -> probability."""
    if not offsets:
        return {}
    counts: dict[int, int] = {}
    for d in offsets:
        counts[d] = counts.get(d, 0) + 1
    return {d: c / len(offsets) for d, c in sorted(counts.items())}


def _offset_days_json(dist: dict[int, float]) -> list:
    return [[d, w] for d, w in sorted(dist.items()) if w > 0]


def segment_offset_profiles(records: list[PaymentRecord]) -> dict[str, dict]:
    """Pooled offset distributions per segment, plus a "global" profile."""
    segments: dict[str, list[int]] = {"global": []}
    for r in records:
        segments.setdefault(r.segment, []).append(r.offset_days)
        segments["global"].append(r.offset_days)
    return {
        seg: {"offset_days": _offset_days_json(_discrete(offs)), "n": len(offs)}
        for seg, offs in segments.items()
        if offs
    }


def _segment_dist(seg_profiles: dict[str, dict], segment: str) -> dict[int, float]:
    raw = seg_profiles.get(segment) or seg_profiles.get("global") or {}
    return {d: w for d, w in raw.get("offset_days", [])}


def customer_offset_profiles(
    records: list[PaymentRecord], prior_strength: float = 5.0
) -> dict[str, dict]:
    """Per-customer offset distributions, shrunk toward the segment profile.

    ``w(day) = (n * p_customer(day) + k * p_segment(day)) / (n + k)`` — with
    n payments observed and prior strength k (in pseudo-observations).
    """
    if prior_strength <= 0:
        raise ForecastError(f"prior_strength must be positive, got {prior_strength}")
    seg_profiles = segment_offset_profiles(records)
    by_customer: dict[str, list[int]] = {}
    customer_segment: dict[str, str] = {}
    for r in records:
        by_customer.setdefault(r.customer_id, []).append(r.offset_days)
        customer_segment[r.customer_id] = r.segment

    out: dict[str, dict] = {}
    for cust, offsets in by_customer.items():
        cust_dist = _discrete(offsets)
        seg_dist = _segment_dist(seg_profiles, customer_segment[cust])
        n = len(offsets)
        days = set(cust_dist) | set(seg_dist)
        pooled = {
            d: (n * cust_dist.get(d, 0.0) + prior_strength * seg_dist.get(d, 0.0))
            / (n + prior_strength)
            for d in sorted(days)
        }
        out[cust] = {
            "offset_days": _offset_days_json(pooled),
            "n": n,
            "segment": customer_segment[cust],
        }
    return out


def offset_profile_for(
    records: list[PaymentRecord],
    customer_id: str,
    segment: str,
    prior_strength: float = 5.0,
) -> dict:
    """The full fallback ladder for one customer: customer → segment → global → conservative default.

    Returns a profile dict with "offset_days" ready for parse_flow, plus
    "n" (evidence) and "level" (which rung of the ladder answered).
    """
    seg_profiles = segment_offset_profiles(records)
    customers = customer_offset_profiles(records, prior_strength)
    if customer_id in customers:
        return {**customers[customer_id], "level": "customer"}
    if segment in seg_profiles:
        return {**seg_profiles[segment], "segment": segment, "level": "segment"}
    if "global" in seg_profiles:
        return {**seg_profiles["global"], "segment": segment, "level": "global"}
    # Cold start with no history anywhere: assume on-time or 30 days late.
    return {
        "offset_days": [[0, 0.5], [30, 0.5]],
        "n": 0,
        "segment": segment,
        "level": "default",
    }


def promise_profiles(
    records: list[PromiseRecord], prior_strength: float = 2.0, default_rate: float = 0.5
) -> dict[str, dict]:
    """Per-customer promise-keeping rates via Beta-Binomial shrinkage.

    ``p = (kept + k * segment_rate) / (n + k)`` — the conjugate prior's
    posterior mean, computed in closed form (deterministic, no sampling).
    """
    if prior_strength <= 0:
        raise ForecastError(f"prior_strength must be positive, got {prior_strength}")
    by_customer: dict[str, list[bool]] = {}
    customer_segment: dict[str, str] = {}
    seg_totals: dict[str, list[int]] = {"global": [0, 0]}  # [kept, total]
    for r in records:
        by_customer.setdefault(r.customer_id, []).append(r.kept)
        customer_segment[r.customer_id] = r.segment
        seg = seg_totals.setdefault(r.segment, [0, 0])
        seg[0] += 1 if r.kept else 0
        seg[1] += 1
        seg_totals["global"][0] += 1 if r.kept else 0
        seg_totals["global"][1] += 1

    def seg_rate(segment: str) -> float:
        kept, total = seg_totals.get(segment) or seg_totals["global"]
        return default_rate if total == 0 else kept / total

    out: dict[str, dict] = {}
    for cust, keeps in by_customer.items():
        n = len(keeps)
        kept = sum(1 for k in keeps if k)
        rate = (kept + prior_strength * seg_rate(customer_segment[cust])) / (n + prior_strength)
        out[cust] = {
            "promise_kept": rate,
            "n": n,
            "kept": kept,
            "segment": customer_segment[cust],
        }
    return out


def promise_rate_for(
    records: list[PromiseRecord],
    customer_id: str,
    segment: str,
    prior_strength: float = 2.0,
    default_rate: float = 0.5,
) -> dict:
    """Fallback ladder for one customer's promise credibility (Collect story)."""
    customers = promise_profiles(records, prior_strength, default_rate)
    if customer_id in customers:
        return {**customers[customer_id], "level": "customer"}
    kept, total = 0, 0
    for r in records:
        if r.segment == segment:
            kept += 1 if r.kept else 0
            total += 1
    if total:
        return {"promise_kept": kept / total, "n": total, "kept": kept,
                "segment": segment, "level": "segment"}
    kept_g = sum(1 for r in records if r.kept)
    if records:
        return {"promise_kept": kept_g / len(records), "n": len(records), "kept": kept_g,
                "segment": segment, "level": "global"}
    return {"promise_kept": default_rate, "n": 0, "kept": 0,
            "segment": segment, "level": "default"}

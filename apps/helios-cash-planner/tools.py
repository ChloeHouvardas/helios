"""The tools Helios Cash Planner's agent may actually do.

Seven reads and five gated side effects. Reads run directly; every side effect
becomes a proposal the auditor adjudicates against the FORECAST, which the
agent cannot write. Three structural controls live here rather than in rules:

* a warning is published for a week by id; its shortfall is read from the
  projection and the agent's number is only accepted if it does not exceed it;
* a recommendation records exactly one action for one week and never executes
  it — chasing, rescheduling and drawing are other apps' or the owner's acts;
* the digest's recipient is configuration, never an argument.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from agent_toolkit.contracts import ToolDefinition
from fin_tools.market_data import build_resolver
from fin_tools.market_data.rates import DISCLAIMER, RateService

from book import Book

import config

CASH_POSITION = "cash_position"
PROJECT_CASH = "project_cash"
SIMULATE_CASH = "simulate_cash"
WHAT_IF = "what_if"
CUSTOMER_BEHAVIOR = "customer_behavior"
COMPARE_FUNDING = "compare_funding"
REFERENCE_RATE = "reference_rate"
PUBLISH_SHORTFALL_WARNING = "publish_shortfall_warning"
RECOMMEND_FOLLOW_UP = "recommend_follow_up"
RECOMMEND_RESCHEDULE = "recommend_reschedule"
RECOMMEND_CREDIT_DRAW = "recommend_credit_draw"
SEND_OWNER_DIGEST = "send_owner_digest"

TOOL_NAMES: tuple[str, ...] = (
    CASH_POSITION, PROJECT_CASH, SIMULATE_CASH, WHAT_IF, CUSTOMER_BEHAVIOR,
    COMPARE_FUNDING, REFERENCE_RATE, PUBLISH_SHORTFALL_WARNING, RECOMMEND_FOLLOW_UP,
    RECOMMEND_RESCHEDULE, RECOMMEND_CREDIT_DRAW, SEND_OWNER_DIGEST,
)

_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_ANY = {"type": "object"}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


_WEEK = {"type": "string", "pattern": "^W([1-9]|1[0-3])$",
         "description": "A horizon week id, W1..W13 (W1 starts today)."}
_REASON = {"type": "string", "minLength": config.FOLLOW_UP_MIN_REASON_CHARS}


class _Rates:
    """One market resolver per process; no graph (this app cites, it does not attest)."""

    def __init__(self) -> None:
        self._service: RateService | None = None

    def service(self) -> RateService:
        if self._service is None:
            self._service = RateService(build_resolver(), None)
        return self._service


_RATES = _Rates()


def _week_row(book: Book, week_id: str) -> dict[str, Any]:
    row = book.week(week_id)
    if row is None:
        raise ValueError(f"unknown week '{week_id}'; expected one of {', '.join(book.week_ids())}")
    return row


# --------------------------------------------------------------------------
# read-only
# --------------------------------------------------------------------------

def make_cash_position(book: Book) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        short = book.first_short_week()
        return {
            "today": book.today.isoformat(),
            "balance_cents": book.balance_cents(),
            "currency": config.ACCOUNT_CURRENCY,
            "open_receivables_cents": sum(int(r["amount_cents"]) for r in book.open_receivables()),
            "open_payables_cents": sum(int(p["amount_cents"]) for p in book.open_payables()),
            "credit_line_headroom_cents": book.credit_line_headroom_cents(),
            "first_short_week": short["week"] if short else None,
            "first_short_week_starts": short["start"] if short else None,
        }

    return ToolDefinition(
        name=CASH_POSITION,
        description=("Today's balance, what is owed to and by the business over the "
                     "horizon, the credit headroom, and the first week the forecast "
                     "marks short. Read-only. Call it first."),
        input_schema=_EMPTY, output_schema=_ANY, side_effect=False, handler=handler)


def make_project_cash(book: Book) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        proj = book.projection()
        return {
            "start": book.today.isoformat(), "days": config.HORIZON_DAYS,
            "weeks": book.weeks(),
            "min_balance_cents": proj["min_balance_cents"],
            "min_balance_date": proj["min_balance_date"],
            "first_shortfall": proj["first_shortfall"],
            "final_balance_cents": proj["final_balance_cents"],
            "inputs_hash": proj["inputs_hash"],
            "cash_buffer_cents": config.CASH_BUFFER_CENTS,
        }

    return ToolDefinition(
        name=PROJECT_CASH,
        description=("The deterministic thirteen-week projection, one row per week with "
                     "minimum and closing balance and whether the week is SHORT (below "
                     "the cash buffer). Inflows are timed by how each customer actually "
                     "pays. Read-only. The auditor reads the same projection, so the "
                     "weeks it marks short are the only weeks you may warn about."),
        input_schema=_EMPTY, output_schema=_ANY, side_effect=False, handler=handler)


def make_simulate_cash(book: Book) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        sim = book.simulation()
        return {
            "n_paths": sim["n_paths"], "seed": sim["seed"],
            "probability_below_buffer": sim["probability_below"],
            "first_shortfall_probability": sim.get("first_shortfall_probability"),
            "first_shortfall_median_date": sim.get("first_shortfall_median_date"),
            "min_balance_percentiles": sim.get("min_balance_percentiles"),
            "inputs_hash": sim["inputs_hash"],
        }

    return ToolDefinition(
        name=SIMULATE_CASH,
        description=("Seeded Monte Carlo over the same inputs: the probability the "
                     "balance dips below the buffer and when. Read-only, deterministic "
                     "for the same seed."),
        input_schema=_EMPTY, output_schema=_ANY, side_effect=False, handler=handler)


_WHAT_IF_INPUT = _obj({
    "kind": {"type": "string", "enum": ["customer_pays_late", "move_bill", "drop_inflow"]},
    "target_id": {"type": "string", "minLength": 1},
    "days": {"type": "integer", "minimum": 0, "maximum": 120},
}, ["kind", "target_id"])


def make_what_if(book: Book) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(args["kind"])
        target = str(args["target_id"])
        days = int(args.get("days", 14))
        if kind == "customer_pays_late":
            if book.receivable(target) is None:
                raise ValueError(f"unknown receivable '{target}'")
            delta = {"operations": [{"op": "shift_date", "flow_id": target, "days": days}]}
        elif kind == "move_bill":
            if book.payable(target) is None:
                raise ValueError(f"unknown payable '{target}'")
            delta = {"operations": [{"op": "shift_date", "flow_id": target, "days": days}]}
        else:
            if book.receivable(target) is None:
                raise ValueError(f"unknown receivable '{target}'")
            delta = {"operations": [{"op": "remove_flow", "flow_id": target}]}
        result = book.what_if(delta)
        return {"kind": kind, "target_id": target, "days": days, **result}

    return ToolDefinition(
        name=WHAT_IF,
        description=("Answer a what-if in plain terms: a customer paying N days late, a "
                     "bill moved by N days, or an inflow not arriving. Returns the "
                     "baseline and scenario minimum balance and first shortfall. "
                     "Read-only; it changes nothing."),
        input_schema=_WHAT_IF_INPUT, output_schema=_ANY, side_effect=False, handler=handler)


def make_customer_behavior(book: Book) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        customer_id = str(args["customer_id"])
        record = book.customer(customer_id)
        if record is None:
            raise ValueError(f"unknown customer '{customer_id}'; expected one of "
                             f"{', '.join(book.customer_ids())}")
        profile = book.behaviour(customer_id)
        # The dispute flag is withheld: it is a fact the auditor resolves, and a
        # control the agent can read is a control the agent can argue with.
        return {"customer_id": customer_id, "name": record.get("name"),
                "segment": record.get("segment"), "offset_days": profile["offset_days"],
                "evidence_level": profile["level"], "observations": profile["n"]}

    return ToolDefinition(
        name=CUSTOMER_BEHAVIOR,
        description=("How a customer actually pays: the learned days-late distribution "
                     "and the evidence level behind it (customer, segment, global, or "
                     "default when there is no history). Read-only. Does not report "
                     "whether the customer is in dispute; the auditor resolves that."),
        input_schema=_obj({"customer_id": {"type": "string", "minLength": 1}}, ["customer_id"]),
        output_schema=_ANY, side_effect=False, handler=handler)


_COMPARE_INPUT = _obj({
    "amount_cents": {"type": "integer", "minimum": 1},
    "days": {"type": "integer", "minimum": 1, "maximum": 180},
    "customer_id": {"type": "string"},
}, ["amount_cents", "days"])


def make_compare_funding(book: Book) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        amount = int(args["amount_cents"])
        days = int(args["days"])
        options: list[dict[str, Any]] = [
            {"kind": "credit_line", "base": config.CREDIT_LINE_BASE_SERIES,
             "spread_bps": config.CREDIT_LINE_SPREAD_BPS},
            {"kind": "delay_supplier", "late_fee_pct": 0.5},
        ]
        customer_id = str(args.get("customer_id") or "")
        if customer_id and book.customer(customer_id) is not None:
            profile = book.behaviour(customer_id)
            expected = sum(d * w for d, w in profile["offset_days"])
            options.append({"kind": "chase_customer", "customer_id": customer_id,
                            "expected_days": round(expected, 1)})
        compared = _RATES.service().borrowing_cost_compare(amount_cents=amount, days=days,
                                                          options=options)
        compared["credit_line_headroom_cents"] = book.credit_line_headroom_cents()
        return compared

    return ToolDefinition(
        name=COMPARE_FUNDING,
        description=("Price the ways to cover a gap for N days: draw the credit line at "
                     "the public reference rate plus the spread, delay a supplier, or "
                     "chase a customer whose payment timing is known. Each option carries "
                     "its formula, inputs and provenance so the arithmetic can be "
                     "recomputed. Read-only; it recommends nothing. " + DISCLAIMER),
        input_schema=_COMPARE_INPUT, output_schema=_ANY, side_effect=False, handler=handler)


def make_reference_rate(book: Book) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        series = str(args.get("series") or config.CREDIT_LINE_BASE_SERIES)
        quoted = _RATES.service().rate(series=series, publish=False)
        return {"series": series, "value_pct": str(Decimal(str(quoted["value_pct"]))),
                "value_bps": quoted["value_bps"], "as_of": quoted["as_of"],
                "provenance": quoted["provenance"], "disclaimer": DISCLAIMER}

    return ToolDefinition(
        name=REFERENCE_RATE,
        description=("One public reference rate (LPR or SHIBOR) with its provenance: "
                     "publisher, as-of date, the mode that answered and whether it is "
                     "stale. Read-only. Cite it, never round it into advice."),
        input_schema=_obj({"series": {"type": "string",
                                      "enum": ["lpr_1y", "lpr_5y", "shibor_1m", "shibor_3m"]}}, []),
        output_schema=_ANY, side_effect=False, handler=handler)


# --------------------------------------------------------------------------
# gated
# --------------------------------------------------------------------------

_WARNING_INPUT = _obj({
    "subject_id": _WEEK,
    "shortfall_cents": {"type": "integer", "minimum": 1},
}, ["subject_id", "shortfall_cents"])


def make_publish_shortfall_warning(book: Book, bundle: Any) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        week_id = str(args["subject_id"])
        row = _week_row(book, week_id)
        if not row["short"]:
            raise ValueError(f"{week_id} is not short in the projection; nothing to warn about")
        # The number that is published is the PROJECTED shortfall, never the
        # agent's. The rule already holds a larger claim; a smaller one would
        # understate the gap, so the projection wins either way.
        shortfall = int(row["shortfall_cents"])
        rate = _RATES.service().rate(series=config.CREDIT_LINE_BASE_SERIES, publish=False)
        node_id = None
        if bundle.graph is not None:
            node = bundle.graph.upsert_verified(
                "cashplan", week_id,
                {"week": week_id, "start": row["start"], "end": row["end"],
                 "shortfall_cents": shortfall,
                 "projected_min_balance_cents": int(row["min_balance_cents"]),
                 "payroll_in_week": bool(row["payroll"]),
                 "reference_rate": {"series": rate["series"], "value_pct": rate["value_pct"],
                                    "as_of": rate["as_of"],
                                    "mode": rate["provenance"].get("mode")},
                 "event": "shortfall_warning"},
                source=config.GRAPH_SOURCE)
            node_id = node["node_id"]
        record = book.record_warning(week_id, shortfall_cents=shortfall,
                                     projected_min_balance_cents=int(row["min_balance_cents"]),
                                     graph_node_id=node_id)
        return {**record, "published": True}

    return ToolDefinition(
        name=PUBLISH_SHORTFALL_WARNING,
        description=("Publish a shortfall warning for ONE horizon week as a VERIFIED "
                     "knowledge-graph node (cashplan:<week>), so Payables and the owner "
                     "see it. Side effect. Only a week the projection marks short can "
                     "be warned about, and the published amount is the projected gap; "
                     "a claimed shortfall above the projection is held."),
        input_schema=_WARNING_INPUT, output_schema=_ANY, side_effect=True, handler=handler)


_FOLLOW_UP_INPUT = _obj({
    "subject_id": _WEEK,
    "invoice_id": {"type": "string", "minLength": 1},
    "reason": _REASON,
}, ["subject_id", "invoice_id", "reason"])


def make_recommend_follow_up(book: Book, bundle: Any) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        week_id = str(args["subject_id"])
        invoice_id = str(args["invoice_id"])
        _week_row(book, week_id)
        rec = book.receivable(invoice_id)
        if rec is None:
            raise ValueError(f"unknown receivable '{invoice_id}'")
        reason = str(args["reason"]).strip()
        node_id = _observe(bundle, "follow_up", f"{week_id}:{invoice_id}",
                           {"week": week_id, "invoice_id": invoice_id,
                            "customer_id": rec.get("customer_id"), "reason": reason})
        record = book.record_recommendation(week_id=week_id, kind="follow_up",
                                            target_id=invoice_id, reason=reason,
                                            amount_cents=int(rec["amount_cents"]),
                                            graph_node_id=node_id)
        return record

    return ToolDefinition(
        name=RECOMMEND_FOLLOW_UP,
        description=("Recommend chasing ONE receivable to close a short week. Records "
                     "the recommendation as a graph OBSERVATION for Collect and the "
                     "owner; it sends nothing itself. Side effect. Refused for a "
                     "customer in dispute and for a week that is not short."),
        input_schema=_FOLLOW_UP_INPUT, output_schema=_ANY, side_effect=True, handler=handler)


_RESCHEDULE_INPUT = _obj({
    "subject_id": _WEEK,
    "bill_id": {"type": "string", "minLength": 1},
    "new_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    "reason": _REASON,
}, ["subject_id", "bill_id", "new_date", "reason"])


def make_recommend_reschedule(book: Book, bundle: Any) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        week_id = str(args["subject_id"])
        bill_id = str(args["bill_id"])
        _week_row(book, week_id)
        pay = book.payable(bill_id)
        if pay is None:
            raise ValueError(f"unknown payable '{bill_id}'")
        new_date = date.fromisoformat(str(args["new_date"]))
        if new_date <= date.fromisoformat(str(pay["due_date"])):
            raise ValueError("new_date must be after the bill's current due date")
        reason = str(args["reason"]).strip()
        node_id = _observe(bundle, "reschedule", f"{week_id}:{bill_id}",
                           {"week": week_id, "bill_id": bill_id,
                            "new_date": new_date.isoformat(), "reason": reason})
        return book.record_recommendation(week_id=week_id, kind="reschedule", target_id=bill_id,
                                          reason=reason, amount_cents=int(pay["amount_cents"]),
                                          new_date=new_date.isoformat(), graph_node_id=node_id)

    return ToolDefinition(
        name=RECOMMEND_RESCHEDULE,
        description=("Recommend moving ONE supplier bill to a later date to clear a "
                     "short week. Records an OBSERVATION for Payables and the owner; "
                     "Payables moves the bill under its own gate. Side effect. Payroll "
                     "and other non-reschedulable bills are refused."),
        input_schema=_RESCHEDULE_INPUT, output_schema=_ANY, side_effect=True, handler=handler)


_DRAW_INPUT = _obj({
    "subject_id": _WEEK,
    "amount_cents": {"type": "integer", "minimum": 1},
    "reason": _REASON,
}, ["subject_id", "amount_cents", "reason"])


def make_recommend_credit_draw(book: Book, bundle: Any) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        week_id = str(args["subject_id"])
        _week_row(book, week_id)
        amount = int(args["amount_cents"])
        reason = str(args["reason"]).strip()
        rate = _RATES.service().rate(series=config.CREDIT_LINE_BASE_SERIES, publish=False)
        annual = Decimal(str(rate["value_pct"])) + Decimal(config.CREDIT_LINE_SPREAD_BPS) / Decimal(100)
        interest = int((Decimal(amount) * annual / Decimal(100) * Decimal(30) / Decimal(365))
                       .quantize(Decimal(1)))
        node_id = _observe(bundle, "credit_draw", week_id,
                           {"week": week_id, "amount_cents": amount, "reason": reason,
                            "rate_series": rate["series"], "rate_pct": str(rate["value_pct"]),
                            "rate_as_of": rate["as_of"], "interest_30d_cents": interest})
        return book.record_recommendation(
            week_id=week_id, kind="credit_draw", target_id=config.OPERATING_ACCOUNT,
            reason=reason, amount_cents=amount, rate_name=rate["series"],
            rate_as_of=rate["as_of"], rate_pct=str(rate["value_pct"]),
            rate_mode=rate["provenance"].get("mode"), rate_stale=rate["provenance"].get("stale"),
            interest_30d_cents=interest, graph_node_id=node_id, disclaimer=DISCLAIMER)

    return ToolDefinition(
        name=RECOMMEND_CREDIT_DRAW,
        description=("Recommend drawing on the credit line for a short week, costed at "
                     "the public reference rate plus the spread with provenance. Side "
                     "effect. ALWAYS held for the owner: a draw is borrowing. Capped "
                     "at the facility headroom, which the auditor reads itself. It "
                     "draws nothing; the owner draws."),
        input_schema=_DRAW_INPUT, output_schema=_ANY, side_effect=True, handler=handler)


_DIGEST_INPUT = _obj({
    "subject_id": {"type": "string", "enum": ["owner"]},
    "subject": {"type": "string", "minLength": 4},
    "body": _REASON,
}, ["subject_id", "subject", "body"])


def make_send_owner_digest(book: Book, bundle: Any) -> ToolDefinition:
    def handler(args: Mapping[str, Any]) -> dict[str, Any]:
        subject = str(args["subject"])
        body = str(args["body"]).strip()
        sent = bundle.email.send(to=config.OWNER_EMAIL, subject=subject, body=body,
                                 template="escalate_to_owner")
        book.record_digest()
        return {"message_id": str(sent.get("message_id") or ""), "to": config.OWNER_EMAIL,
                "subject": subject}

    return ToolDefinition(
        name=SEND_OWNER_DIGEST,
        description=("Email the owner the weekly cash digest. The recipient is "
                     "configuration, never an argument. Side effect; at most one per "
                     "rolling week."),
        input_schema=_DIGEST_INPUT, output_schema=_ANY, side_effect=True, handler=handler)


def _observe(bundle: Any, kind: str, key: str, attrs: dict[str, Any]) -> str | None:
    """A recommendation is an inference: an observation, never a verified record."""
    if bundle.graph is None:
        return None
    node = bundle.graph.add_observation(f"recommendation.{kind}", key, attrs,
                                        observer=config.GRAPH_OBSERVER)
    return node["node_id"]


def build_tools(book: Book, bundle: Any) -> list[ToolDefinition]:
    return [
        make_cash_position(book), make_project_cash(book), make_simulate_cash(book),
        make_what_if(book), make_customer_behavior(book), make_compare_funding(book),
        make_reference_rate(book),
        make_publish_shortfall_warning(book, bundle), make_recommend_follow_up(book, bundle),
        make_recommend_reschedule(book, bundle), make_recommend_credit_draw(book, bundle),
        make_send_owner_digest(book, bundle),
    ]

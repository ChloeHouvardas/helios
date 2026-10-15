"""The cash book: accounts, customers, receivables, payables, and the projection.

What this file owns
-------------------
The data the forecast is built from, and the forecast itself. Receivables and
customers live in the platform's `Store` (its `invoices` / `customers` slots), the
same way the other flagships keep their books, so `build_integration()` gets a
store it understands. Payables and accounts are this app's own records.

The projection is computed here, not by the agent. That is the point of the
app: every rule about a week reads `week_facts()`, which is this book's own
arithmetic over the platform's forecast engine. The agent may NAME a week; it
never gets to STATE whether that week is short, by how much, or what the
balance will be. `facts.py` hands exactly these values to the auditor.

Relative dates
--------------
Fixtures use `_days_ago` (negative for the future) so the demo is evergreen:
the forecast always starts today and payroll always lands on the next 25th.
`PAYROLL` carries `due_day_of_month` and is expanded into one payable per month
inside the horizon.

Cross-app input
---------------
`apply_graph_nodes()` and `apply_event()` let verified `invoice` / `bill` nodes
from the shared graph, and tenant events, replace the fixture dates. The app
runs standalone without either; when Collect agrees an extension it simply
re-projects with the new date.
"""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent_toolkit.store import Store
from fin_tools.forecast import (behavior_profile, forecast_project, forecast_scenario,
                                forecast_simulate)

import config


def _load_json(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


class Book:
    """Everything the planner knows, over one platform store."""

    def __init__(self, store: Store, *, accounts: Mapping[str, Mapping[str, Any]],
                 payables: Mapping[str, Mapping[str, Any]]) -> None:
        self.store = store
        self._accounts = {str(k): dict(v) for k, v in accounts.items()}
        self._payables = {str(k): dict(v) for k, v in payables.items()}
        self._balances = {k: int(v.get("balance_cents", 0)) for k, v in self._accounts.items()}
        self._drawn_cents = int(self._accounts.get(config.OPERATING_ACCOUNT, {})
                                .get("credit_line_drawn_cents", 0))
        self._warnings: dict[str, dict[str, Any]] = {}
        self._recommendations: dict[str, dict[str, Any]] = {}
        self._digests: list[date] = []
        self._events: list[dict[str, Any]] = []
        self._overrides: dict[str, dict[str, Any]] = {}
        self._projection_cache: dict[str, Any] | None = None

    @classmethod
    def load(cls, data_dir: Path | str | None = None, *, today: date | None = None) -> "Book":
        root = Path(data_dir) if data_dir else config.DATA_DIR
        customers = _load_json(root / "customers.json")
        receivables = _load_json(root / "receivables.json")
        accounts = _load_json(root / "accounts.json")
        payables = _load_json(root / "payables.json")
        store = Store(customers=customers, invoices=receivables, today=today)
        resolved_payables = {
            key: store._resolve_relative_dates(dict(record))  # noqa: SLF001
            for key, record in payables.items()
        }
        book = cls(store, accounts=accounts, payables=resolved_payables)
        book._expand_recurring()
        return book

    # -- identity ------------------------------------------------------------

    @property
    def today(self) -> date:
        return self.store.today

    @property
    def horizon_end(self) -> date:
        return self.today + timedelta(days=config.HORIZON_DAYS - 1)

    def week_ids(self) -> list[str]:
        return [f"W{n}" for n in range(1, config.WEEKS + 1)]

    def week_bounds(self, week_id: str) -> tuple[date, date] | None:
        """The first and last day of a horizon week (`W1` starts today)."""
        if not week_id or not week_id.startswith("W"):
            return None
        try:
            n = int(week_id[1:])
        except ValueError:
            return None
        if not 1 <= n <= config.WEEKS:
            return None
        start = self.today + timedelta(days=(n - 1) * 7)
        return start, start + timedelta(days=6)

    def week_for(self, when: date) -> str | None:
        offset = (when - self.today).days
        if offset < 0 or offset >= config.HORIZON_DAYS:
            return None
        return f"W{offset // 7 + 1}"

    # -- accounts --------------------------------------------------------------

    def balance_cents(self, account_id: str | None = None) -> int:
        return self._balances.get(account_id or config.OPERATING_ACCOUNT, 0)

    def set_balance_cents(self, account_id: str, cents: int) -> None:
        self._balances[account_id] = int(cents)
        self._invalidate()

    @property
    def credit_line_drawn_cents(self) -> int:
        return self._drawn_cents

    def credit_line_headroom_cents(self) -> int:
        return max(0, config.CREDIT_LINE_LIMIT_CENTS - self._drawn_cents)

    # -- customers and receivables -----------------------------------------------

    def customer(self, customer_id: str) -> dict[str, Any] | None:
        return self.store.get_customer(customer_id)

    def customer_ids(self) -> list[str]:
        return sorted(self.store._customers)  # noqa: SLF001

    def customer_disputed(self, customer_id: str) -> bool | None:
        record = self.customer(customer_id)
        if record is None:
            return None
        return bool(record.get("disputed"))

    def receivable(self, invoice_id: str) -> dict[str, Any] | None:
        record = self.store.get_invoice(invoice_id)
        if record is None:
            return None
        record.update(self._overrides.get(invoice_id, {}))
        return record

    def receivable_ids(self) -> list[str]:
        return sorted(self.store._invoices)  # noqa: SLF001

    def open_receivables(self) -> list[dict[str, Any]]:
        out = [self.receivable(i) for i in self.receivable_ids()]
        return [r for r in out if r and r.get("status") == "open"]

    def payment_records(self) -> list[dict[str, Any]]:
        """`{customer_id, segment, due, paid}` rows for behavior.profile."""
        rows: list[dict[str, Any]] = []
        for cid in self.customer_ids():
            record = self.customer(cid) or {}
            for pair in record.get("payment_history") or []:
                rows.append({"customer_id": cid, "segment": record.get("segment", "default"),
                             "due": pair["due"], "paid": pair["paid"]})
        return rows

    def behaviour(self, customer_id: str) -> dict[str, Any]:
        """How this customer actually pays: an offset_days distribution + its level."""
        record = self.customer(customer_id) or {}
        return behavior_profile(records=self.payment_records(), customer_id=customer_id,
                                segment=str(record.get("segment", "default")),
                                prior_strength=4.0)

    # -- payables ---------------------------------------------------------------

    def payable(self, bill_id: str) -> dict[str, Any] | None:
        record = self._payables.get(bill_id)
        if record is None:
            return None
        out = dict(record)
        out.update(self._overrides.get(bill_id, {}))
        return out

    def payable_ids(self) -> list[str]:
        return sorted(self._payables)

    def open_payables(self) -> list[dict[str, Any]]:
        out = [self.payable(i) for i in self.payable_ids()]
        return [p for p in out if p and p.get("status") == "scheduled"]

    def _expand_recurring(self) -> None:
        """Turn `due_day_of_month` into one dated payable per month in the horizon."""
        for key in list(self._payables):
            record = self._payables[key]
            day = record.get("due_day_of_month")
            if not day:
                continue
            del self._payables[key]
            cursor = self.today
            for _ in range(4):
                last = calendar.monthrange(cursor.year, cursor.month)[1]
                due = cursor.replace(day=min(int(day), last))
                if due >= self.today and due <= self.horizon_end:
                    bill_id = f"{key}-{due.strftime('%Y-%m')}"
                    self._payables[bill_id] = {
                        **{k: v for k, v in record.items() if k != "due_day_of_month"},
                        "id": bill_id, "due_date": due.isoformat(),
                    }
                year, month = (cursor.year + (cursor.month // 12), cursor.month % 12 + 1)
                cursor = date(year, month, 1)

    # -- cross-app input ----------------------------------------------------------

    def apply_event(self, event: Mapping[str, Any]) -> bool:
        """Fold a tenant event into the book; True when the forecast changed."""
        name = str(event.get("event", ""))
        data = event.get("data") or {}
        self._events.append({"event": name, "data": dict(data), "app": event.get("app")})
        if name == "invoice.extension_agreed" and data.get("invoice") in self.store._invoices:  # noqa: SLF001
            self._overrides.setdefault(str(data["invoice"]), {})["due_date"] = str(data["new_due"])
        elif name == "invoice.payment_applied" and data.get("invoice") in self.store._invoices:  # noqa: SLF001
            self._overrides.setdefault(str(data["invoice"]), {})["status"] = "paid"
        elif name == "bill.rescheduled" and data.get("bill") in self._payables:
            self._overrides.setdefault(str(data["bill"]), {})["due_date"] = str(data["new_date"])
        elif name == "bill.paid" and data.get("bill") in self._payables:
            self._overrides.setdefault(str(data["bill"]), {})["status"] = "paid"
        else:
            return False
        self._invalidate()
        return True

    def apply_graph_nodes(self, nodes: Iterable[Mapping[str, Any]]) -> int:
        """Prefer verified `invoice` / `bill` nodes over the fixture dates."""
        applied = 0
        for node in nodes:
            if node.get("provenance") != "verified":
                continue
            attrs = node.get("attrs") or {}
            kind, key = node.get("kind"), str(node.get("key"))
            if kind == "invoice" and key in self.store._invoices:  # noqa: SLF001
                for field in ("due_date", "status"):
                    if attrs.get(field):
                        self._overrides.setdefault(key, {})[field] = attrs[field]
                applied += 1
            elif kind == "bill" and key in self._payables:
                for src, dst in (("scheduled_date", "due_date"), ("status", "status")):
                    if attrs.get(src):
                        self._overrides.setdefault(key, {})[dst] = attrs[src]
                applied += 1
        if applied:
            self._invalidate()
        return applied

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    # -- the forecast -------------------------------------------------------------

    def opening(self) -> list[dict[str, Any]]:
        return [{"account": config.OPERATING_ACCOUNT,
                 "balance_cents": self.balance_cents(config.OPERATING_ACCOUNT)}]

    def flows(self) -> list[dict[str, Any]]:
        """Inflows timed by how each customer actually pays; outflows on their dates."""
        flows: list[dict[str, Any]] = []
        for rec in self.open_receivables():
            profile = self.behaviour(str(rec["customer_id"]))
            flows.append({
                "id": rec["id"], "account": config.OPERATING_ACCOUNT, "direction": "inflow",
                "amount_cents": int(rec["amount_cents"]),
                "date": {"due": _iso(rec["due_date"]), "offset_days": profile["offset_days"]},
                "customer_id": rec["customer_id"], "source": rec.get("source"),
            })
        for pay in self.open_payables():
            flows.append({
                "id": pay["id"], "account": config.OPERATING_ACCOUNT, "direction": "outflow",
                "amount_cents": int(pay["amount_cents"]), "date": _iso(pay["due_date"]),
                "source": pay.get("source"),
            })
        return flows

    def _invalidate(self) -> None:
        self._projection_cache = None

    def projection(self) -> dict[str, Any]:
        """The deterministic 13-week projection, cached until the book changes."""
        if self._projection_cache is None:
            self._projection_cache = forecast_project(
                opening=self.opening(), flows=self.flows(),
                start=self.today.isoformat(), days=config.HORIZON_DAYS)
        return self._projection_cache

    def weeks(self) -> list[dict[str, Any]]:
        """One row per horizon week: bounds, min/closing balance, short or not."""
        proj = self.projection()
        totals = proj["total_cents"]
        rows = []
        for n, week_id in enumerate(self.week_ids()):
            start, end = self.week_bounds(week_id)  # type: ignore[misc]
            slice_ = totals[n * 7:(n + 1) * 7]
            if not slice_:
                continue
            low = min(slice_)
            short = low < config.CASH_BUFFER_CENTS
            rows.append({
                "week": week_id, "start": start.isoformat(), "end": end.isoformat(),
                "min_balance_cents": low, "closing_balance_cents": slice_[-1],
                "inflows_cents": sum(proj["inflows_cents"][n * 7:(n + 1) * 7]),
                "outflows_cents": sum(proj["outflows_cents"][n * 7:(n + 1) * 7]),
                "short": short,
                "shortfall_cents": max(0, config.CASH_BUFFER_CENTS - low) if short else 0,
                "payroll": any(p["kind"] == "payroll" and start <= date.fromisoformat(_iso(p["due_date"])) <= end
                               for p in self.open_payables()),
            })
        return rows

    def week(self, week_id: str) -> dict[str, Any] | None:
        for row in self.weeks():
            if row["week"] == week_id:
                return row
        return None

    def first_short_week(self) -> dict[str, Any] | None:
        return next((w for w in self.weeks() if w["short"]), None)

    def simulation(self) -> dict[str, Any]:
        return forecast_simulate(
            opening=self.opening(), flows=self.flows(), start=self.today.isoformat(),
            days=config.HORIZON_DAYS, n_paths=config.SIMULATION_PATHS,
            seed=config.SIMULATION_SEED, threshold_cents=config.CASH_BUFFER_CENTS)

    def what_if(self, delta: Mapping[str, Any]) -> dict[str, Any]:
        return forecast_scenario(opening=self.opening(), flows=self.flows(), delta=dict(delta),
                                 start=self.today.isoformat(), days=config.HORIZON_DAYS)

    # -- what the gated tools write -----------------------------------------------

    def record_warning(self, week_id: str, *, shortfall_cents: int,
                       projected_min_balance_cents: int, graph_node_id: str | None) -> dict[str, Any]:
        record = {
            "id": f"WARN-{week_id}", "week": week_id, "shortfall_cents": int(shortfall_cents),
            "projected_min_balance_cents": int(projected_min_balance_cents),
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "graph_node_id": graph_node_id,
        }
        self._warnings[week_id] = record
        return dict(record)

    def warnings(self) -> list[dict[str, Any]]:
        return [dict(w) for w in self._warnings.values()]

    def record_recommendation(self, *, week_id: str, kind: str, target_id: str,
                              reason: str, amount_cents: int | None = None,
                              status: str = "recorded", **extra: Any) -> dict[str, Any]:
        rec_id = f"REC-{len(self._recommendations) + 1:03d}"
        record = {"id": rec_id, "week": week_id, "kind": kind, "target_id": target_id,
                  "amount_cents": amount_cents, "reason": reason, "status": status, **extra}
        self._recommendations[rec_id] = record
        return dict(record)

    def recommendations(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._recommendations.values()]

    def record_digest(self, when: date | None = None) -> None:
        self._digests.append(when or self.today)

    def digest_count(self, within_days: int) -> int:
        cutoff = self.today - timedelta(days=int(within_days))
        return sum(1 for d in self._digests if d >= cutoff)

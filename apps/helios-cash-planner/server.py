"""Helios Cash Planner — the web app, served by FastAPI over Jinja2 pages.

The deployment contract (`platform/deployment/README.md`): an ASGI `app`, and
`GET /health` that is constant and builds nothing. The engine's smoke test runs
this container with no Redis, no audit sidecar, no model key and no data
volume, so the planner is built lazily on the first real page.

Cross-app input: when `HELIOS_REDIS_URL` is set the planner subscribes to the
tenant event bus and folds `invoice.*` / `bill.*` events into the book, then
re-projects. The app runs identically without a bus.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402

#: Runs and the audit chain live under HELIOS_DATA_DIR when deployed (a named
#: volume shared with the audit sidecar) and under ./runs when run by hand.
DATA_DIR = Path(os.environ.get("HELIOS_DATA_DIR") or (HERE / "runs"))

from app import (  # noqa: E402
    App,
    ScriptedOwner,
    build_app,
    run_scenario,
    scenarios,
)

app = FastAPI(title=config.APP_NAME, description="AI cash-flow agent on Helios")
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))

NAV = (
    {"id": "dashboard", "label": "Dashboard", "href": "/"},
    {"id": "forecast", "label": "Forecast", "href": "/forecast"},
    {"id": "scenarios", "label": "What if", "href": "/scenarios"},
    {"id": "funding", "label": "Funding", "href": "/funding"},
    {"id": "holds", "label": "Holds", "href": "/holds"},
    {"id": "settings", "label": "Settings", "href": "/settings"},
)

STATUS_CLASS = {"done": "paid", "refused": "refused", "declined_by_owner": "refused",
                "held": "held", "error": "refused", "read_only": "unpaid"}
STATUS_MEANING = {
    "done": "The warning or recommendation was recorded, under audit.",
    "refused": "A rule DENIED it. The owner was never asked.",
    "declined_by_owner": "A rule held it and the owner said no.",
    "held": "Paused for the owner; nothing recorded yet.",
    "error": "Something broke; never reported as success.",
}


class State:
    """Process-level state. The planner is built lazily; see /health."""

    def __init__(self) -> None:
        self._app: App | None = None
        self.outcomes: list[Any] = []
        self.owner_decisions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._subscribed = False
        self.last_run = ""

    @property
    def app(self) -> App:
        with self._lock:
            if self._app is None:
                self._app = build_app(run_dir=DATA_DIR)
                for event in self.events:
                    self._app.book.apply_event(event)
                self._subscribe()
            return self._app

    @property
    def built(self) -> bool:
        return self._app is not None

    def _subscribe(self) -> None:
        """Listen to the tenant bus when one is configured. Never raises."""
        if self._subscribed or not os.environ.get("HELIOS_REDIS_URL"):
            return
        self._subscribed = True
        try:
            from helios_sdk import subscribe_events
        except ImportError:
            return

        def handler(event: dict[str, Any]) -> None:
            self.record_event(event)

        subscribe_events(handler)

    def record_event(self, event: dict[str, Any]) -> bool:
        """Fold one tenant event in; the next page re-projects. Returns whether it mattered."""
        self.events.append(dict(event))
        with self._lock:
            if self._app is None:
                return True
            return self._app.book.apply_event(event)

    def reset(self) -> None:
        with self._lock:
            if self._app is not None:
                self._app.close()
                self._app = None

    def run_all(self) -> None:
        from datetime import datetime

        self.outcomes = []
        self.owner_decisions = []
        for scenario in scenarios():
            fresh = build_app(run_dir=DATA_DIR)
            for event in self.events:
                fresh.book.apply_event(event)
            try:
                owner = ScriptedOwner(verbose=False, decline_all=scenario.owner_declines)
                outcome = run_scenario(fresh, scenario, owner)
                self.outcomes.append(outcome)
                for decision in owner.decisions:
                    self.owner_decisions.append({**decision, "scenario": scenario.id})
            finally:
                fresh.close()
        self.reset()
        self.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def close(self) -> None:
        with self._lock:
            if self._app is not None:
                self._app.close()
                self._app = None


STATE = State()


def _prefix(request: Request) -> str:
    return request.headers.get("x-forwarded-prefix", "").rstrip("/")


def _ctx(request: Request, active: str) -> dict[str, Any]:
    book = STATE.app.book
    return {"request": request, "active": active, "nav": NAV, "prefix": _prefix(request),
            "subtitle": f"{config.APP_NAME} — thirteen weeks from {book.today.isoformat()}",
            "config": config.describe(), "status_class": STATUS_CLASS,
            "status_meaning": STATUS_MEANING, "money": config.money}


@app.get("/health")
def health() -> dict[str, Any]:
    """Constant liveness for the deploy smoke test. Builds nothing."""
    return {"status": "ok", "app": config.APP_ID, "built": STATE.built}


@app.get("/diagnostics")
def diagnostics() -> dict[str, Any]:
    instance = STATE.app
    short = instance.book.first_short_week()
    return {"app": config.APP_ID, "built": True, "today": instance.book.today.isoformat(),
            "rules": len(instance.audit_manifest.rules), "tools": len(instance.audit_manifest.tools),
            "first_short_week": short["week"] if short else None,
            "events_applied": len(STATE.events), "audit_log": str(instance.audit_log_path)}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    book = STATE.app.book
    weeks = book.weeks()
    short = book.first_short_week()
    sim = book.simulation()
    counts: dict[str, int] = {}
    for o in STATE.outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    ctx = _ctx(request, "dashboard")
    ctx.update({"weeks": weeks, "short": short, "sim": sim,
                "balance": config.money(book.balance_cents()),
                "receivables": config.money(sum(int(r["amount_cents"]) for r in book.open_receivables())),
                "payables": config.money(sum(int(p["amount_cents"]) for p in book.open_payables())),
                "headroom": config.money(book.credit_line_headroom_cents()),
                "warnings": book.warnings(), "recommendations": book.recommendations(),
                "counts": sorted(counts.items()), "outcomes": STATE.outcomes,
                "events": STATE.events[-10:], "last_run": STATE.last_run})
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.post("/run")
def run(request: Request) -> RedirectResponse:
    STATE.run_all()
    return RedirectResponse(url=f"{_prefix(request)}/holds", status_code=303)


@app.get("/forecast", response_class=HTMLResponse)
def forecast(request: Request) -> HTMLResponse:
    book = STATE.app.book
    ctx = _ctx(request, "forecast")
    ctx.update({"weeks": book.weeks(), "flows": book.flows(),
                "receivables": book.open_receivables(), "payables": book.open_payables(),
                "behaviours": {cid: book.behaviour(cid) for cid in book.customer_ids()},
                "customers": {cid: book.customer(cid) for cid in book.customer_ids()}})
    return templates.TemplateResponse(request, "forecast.html", ctx)


@app.get("/scenarios", response_class=HTMLResponse)
def scenarios_page(request: Request, kind: str = "", target_id: str = "", days: int = 14) -> HTMLResponse:
    book = STATE.app.book
    ctx = _ctx(request, "scenarios")
    result = None
    error = ""
    if kind and target_id:
        try:
            tool = next(t for t in STATE.app.integration.tools if t.name == "what_if")
            result = tool.handler({"kind": kind, "target_id": target_id, "days": days})
        except (ValueError, StopIteration) as exc:
            error = str(exc)
    ctx.update({"receivables": book.open_receivables(), "payables": book.open_payables(),
                "customers": {cid: book.customer(cid) for cid in book.customer_ids()},
                "result": result, "error": error, "kind": kind, "target_id": target_id, "days": days})
    return templates.TemplateResponse(request, "scenarios.html", ctx)


@app.get("/funding", response_class=HTMLResponse)
def funding(request: Request) -> HTMLResponse:
    book = STATE.app.book
    short = book.first_short_week()
    amount = int(short["shortfall_cents"]) if short else config.CASH_BUFFER_CENTS
    compare = next(t for t in STATE.app.integration.tools if t.name == "compare_funding")
    rate_tool = next(t for t in STATE.app.integration.tools if t.name == "reference_rate")
    ctx = _ctx(request, "funding")
    try:
        comparison = compare.handler({"amount_cents": amount, "days": 30, "customer_id": "CUST-001"})
        rate = rate_tool.handler({"series": config.CREDIT_LINE_BASE_SERIES})
        error = ""
    except Exception as exc:  # noqa: BLE001 - a page must say why, not 500
        comparison, rate, error = None, None, str(exc)
    ctx.update({"short": short, "amount": config.money(amount), "comparison": comparison,
                "rate": rate, "error": error})
    return templates.TemplateResponse(request, "funding.html", ctx)


@app.get("/holds", response_class=HTMLResponse)
def holds(request: Request) -> HTMLResponse:
    ctx = _ctx(request, "holds")
    pending = []
    for o in STATE.outcomes:
        for h in o.holds:
            pending.append({"scenario": o.id, "hold_id": h.get("hold_id"), "tool": h.get("tool"),
                            "question": h.get("owner_question") or h.get("reason") or ""})
    refused = [{"scenario": o.id, "control": o.control, **r}
               for o in STATE.outcomes for r in o.refused if r.get("rule") != "owner.rejected_hold"]
    ctx.update({"pending": pending, "decided": STATE.owner_decisions, "refused": refused,
                "outcomes": STATE.outcomes})
    return templates.TemplateResponse(request, "holds.html", ctx)


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> HTMLResponse:
    ctx = _ctx(request, "settings")
    ctx.update({"rules": [{"id": r.id, "description": r.description,
                           "tools": list(r.tools)} for r in STATE.app.audit_manifest.rules]})
    return templates.TemplateResponse(request, "settings.html", ctx)


@app.post("/events")
async def events_in(request: Request) -> JSONResponse:
    """Accept a tenant event over HTTP (the bus delivers the same shape)."""
    try:
        event = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"body must be JSON: {exc}") from exc
    if not isinstance(event, dict) or "event" not in event:
        raise HTTPException(422, "an event is {event, data, app}")
    changed = STATE.record_event(event)
    return JSONResponse({"accepted": True, "forecast_changed": changed, "events": len(STATE.events)})


@app.get("/events")
def events_out() -> dict[str, Any]:
    return {"events": STATE.events[-50:]}


@app.on_event("shutdown")
def _shutdown() -> None:
    STATE.close()

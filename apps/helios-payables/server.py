"""FastAPI web UI for helios-payables.

    .venv/bin/python -m uvicorn server:app --port 8000

Serves the five `ui.pages` declared in `helios-app.yaml` as Jinja2 templates:
dashboard (summary), bills (table), bill (detail), holds (approval_queue),
settings (settings). The CLI in `app.py` renders the same data, so the two cannot
drift — both read the same `Book`, the same auditor and the same scenario list.

What the web owner does, stated plainly
---------------------------------------
A held action pauses the agent loop, and resuming it needs the live runtime. The
CLI does that with `--interactive`; here the runs use a `ScriptedOwner` whose policy
you choose on the dashboard (approve everything, or decline everything). That is
deliberate rather than a shortcut: the two policies are what demonstrate the control.

  * **Approve everything** and BILL-2002 is still refused — because
    `app.payee_details_changed` DENIES, and a denial never becomes a queue item.
  * **Decline everything** and BILL-2010 pays nothing — the same threshold, the
    other direction.

The one genuinely interactive checkpoint is re-verifying a supplier in `/settings`,
which is a POST that mutates the book and lifts that refusal on the next run.

`GET /health` exists for the deployment contract and reports whether the auditor and
the book are live, not merely that the process answered.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
from app import (  # noqa: E402
    App,
    ScriptedOwner,
    build_app,
    redact_text,
    registry_evidence_for,
    run_scenario,
    scenarios,
)

app = FastAPI(title=config.APP_NAME, description="AI accounts-payable agent on Helios")
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))

NAV = (
    {"id": "dashboard", "label": "Dashboard", "href": "/"},
    {"id": "bills", "label": "Bills", "href": "/bills"},
    {"id": "holds", "label": "Holds", "href": "/holds"},
    {"id": "settings", "label": "Settings", "href": "/settings"},
)

STATUS_CLASS = {
    "paid": "paid", "done": "paid",
    "held": "held", "held_unresolved": "held",
    "refused": "refused", "declined_by_owner": "refused",
    "rejected_by_ledger": "refused", "error": "refused",
    "unpaid": "unpaid", "rescheduled": "rescheduled",
}

STATUS_MEANING = {
    "done": "The work landed: money moved or the date moved, under audit.",
    "refused": "A rule DENIED it. The owner was never asked, so approving "
               "everything could not have allowed it.",
    "declined_by_owner": "A rule held it and the owner said no.",
    "rejected_by_ledger": "The gate allowed it and the LEDGER refused it — a "
                          "credential or idempotency problem, not a policy one.",
    "held": "Paused for the owner; nothing executed yet.",
    "error": "Something broke. A run that errors is never reported as success.",
}


class State:
    """Process-level UI state.

    One `App` per run, kept open so the pages can read the book it mutated. Each
    scenario still runs against a FRESH instance (see `run_all`), because a shared
    book would let one bill's payment change another's facts.
    """

    def __init__(self) -> None:
        self.app: App = build_app(run_dir=HERE / "runs")
        self.outcomes: list[Any] = []
        self.owner_decisions: list[dict[str, Any]] = []
        self.owner_declines = False
        self.last_run: str = ""
        #: Suppliers the owner has re-verified. Every scenario runs against a FRESH
        #: app built from the fixtures, so a re-verification recorded only in the
        #: display app's book would vanish on the next run — and the settings page
        #: would appear to do nothing. Re-applied to each fresh instance so the
        #: checkpoint survives, which is what makes "re-verify lifts the denial"
        #: demonstrable from the UI and not only from a test.
        self.reverified: set[str] = set()

    def _apply_reverified(self, instance: App) -> None:
        for supplier_id in sorted(self.reverified):
            try:
                instance.book.reverify_supplier(supplier_id)
            except KeyError:
                continue

    def reset(self) -> None:
        """Rebuild the display app WITHOUT discarding the recorded outcomes.

        The outcomes are the evidence a page renders, so clearing them here would
        make every run look like it produced nothing. Only the book is reset, so
        the pages show the current fixtures rather than the accumulated state of
        fifteen separate runs.
        """
        self.app.close()
        self.app = build_app(run_dir=HERE / "runs")
        self._apply_reverified(self.app)

    def run_all(self) -> None:
        """Run every scenario, each against its own fresh app."""
        from datetime import datetime

        self.outcomes = []
        self.owner_decisions = []
        for scenario in scenarios():
            fresh = build_app(run_dir=HERE / "runs")
            self._apply_reverified(fresh)
            try:
                decline = self.owner_declines or scenario.owner_declines
                owner = ScriptedOwner(verbose=False, decline_all=decline)
                self.outcomes.append(run_scenario(fresh, scenario, owner))
                for decision in owner.decisions:
                    decision = dict(decision)
                    decision["bill_id"] = scenario.bill_id
                    self.owner_decisions.append(decision)
            finally:
                fresh.close()
        self.reset()
        self.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def close(self) -> None:
        self.app.close()


STATE = State()


def _ctx(request: Request, active: str, subtitle: str = "") -> dict[str, Any]:
    return {
        "request": request, "active": active, "nav": NAV,
        "subtitle": subtitle or f"{config.APP_NAME} — as of {STATE.app.book.today.isoformat()}",
        "config": config.describe(),
        "status_class": STATUS_CLASS,
        "status_meaning": STATUS_MEANING,
    }


# --------------------------------------------------------------------------
# Health — the deployment contract
# --------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness that means something: the auditor and the book are both usable."""
    try:
        loaded = bool(STATE.app.audit_manifest.rules)
        bills = len(STATE.app.book.bill_ids())
    except Exception as exc:  # a broken app must not report healthy
        return {"status": "unhealthy", "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok" if (loaded and bills) else "unhealthy",
        "app_id": config.APP_ID,
        "rules_loaded": len(STATE.app.audit_manifest.rules),
        "tools_declared": len(STATE.app.audit_manifest.tools),
        "bills": bills,
        "suppliers": len(STATE.app.book.supplier_ids()),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    book = STATE.app.book
    summary = book.summary()
    counts: dict[str, int] = {}
    for outcome in STATE.outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request, "dashboard") | {
        "summary": summary,
        "due_soon_money": config.money(summary["due_soon_cents"], summary["currency"]),
        "paid_money": config.money(summary["paid_cents"], summary["currency"]),
        "balance_money": config.money(summary["balance_cents"], summary["currency"]),
        "credit_money": config.money(book.credit_line_headroom_cents(),
                                     summary["currency"]),
        "counts": sorted(counts.items(), key=lambda kv: -kv[1]),
        "outcomes": STATE.outcomes,
        "last_run": STATE.last_run,
        "owner_declines": STATE.owner_declines,
    })


@app.post("/run")
def run(scope: str = Form("all"), owner_policy: str = Form("approve")) -> RedirectResponse:
    """Run the scenarios. `owner_policy` is the owner's stance on holds."""
    STATE.owner_declines = owner_policy == "decline"
    STATE.run_all()
    return RedirectResponse(url="/holds", status_code=303)


@app.get("/bills", response_class=HTMLResponse)
def bills(request: Request) -> HTMLResponse:
    rows = []
    for bill_id in STATE.app.book.bill_ids():
        row = STATE.app.book.snapshot_bill(bill_id)
        rows.append(row | {
            "money": config.money(row.get("amount_cents"), str(row.get("currency") or "")),
            "status_class": STATUS_CLASS.get(str(row.get("status")), "unpaid"),
        })
    return templates.TemplateResponse(request, "bills.html", _ctx(request, "bills") | {"rows": rows})


@app.get("/bills/{bill_id}", response_class=HTMLResponse)
def bill_detail(request: Request, bill_id: str) -> HTMLResponse:
    book = STATE.app.book
    if book.bill(bill_id) is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown bill '{bill_id}'. Known: "
                                   f"{', '.join(book.bill_ids())}")
    row = book.snapshot_bill(bill_id)
    supplier_id = str(row.get("supplier_id") or "")
    supplier = book.snapshot_supplier(supplier_id, include_fraud_flag=False)
    return templates.TemplateResponse(request, "bill.html", _ctx(request, "bills") | {
        "row": row | {
            "money": config.money(row.get("amount_cents"), str(row.get("currency") or "")),
            "status_class": STATUS_CLASS.get(str(row.get("status")), "unpaid"),
        },
        "supplier": supplier,
        "avg_money": config.money(book.avg_bill_cents(supplier_id)),
        "ceiling_money": config.money(book.amount_ceiling_cents(supplier_id)),
        "evidence": registry_evidence_for(STATE.app, bill_id),
    })


@app.get("/holds", response_class=HTMLResponse)
def holds(request: Request) -> HTMLResponse:
    """The `approval_queue` pattern: what you were asked, and what was refused
    without asking you.

    The question shown is the auditor's own `owner_question`, redacted so a
    single-use payment credential is not rendered onto a page — `audit_agent`'s
    `REDACTED_KEYS` does not cover `auth_code`, so the app masks it itself.
    """
    decided = []
    for decision in STATE.owner_decisions:
        outcome = next((o for o in STATE.outcomes
                        if o.bill_id == decision.get("bill_id")), None)
        funding = None
        if outcome is not None and outcome.funding_evidence:
            ev = outcome.funding_evidence
            funding = ev | {
                "drawn_money": config.money(ev.get("drawn_cents")),
                "interest_money": config.money(ev.get("interest_cents")),
            }
        decided.append(decision | {
            "question": redact_text(decision.get("question")),
            "funding": funding,
            "evidence": (outcome.registry_evidence if outcome else None),
        })

    refused = []
    for outcome in STATE.outcomes:
        if outcome.status not in {"refused", "declined_by_owner"} or outcome.owner_decisions:
            continue
        items = outcome.refused or [{}]
        for item in items:
            refused.append({
                "bill_id": outcome.bill_id,
                "control": outcome.control,
                "rule": item.get("rule"),
                "reason": redact_text(item.get("reason")),
                "evidence": outcome.registry_evidence,
            })

    return templates.TemplateResponse(request, "holds.html", _ctx(request, "holds") | {
        "pending": [],          # web runs use a scripted owner; see the module docstring
        "decided": decided,
        "refused": refused,
        "outcomes": STATE.outcomes,
        "last_run": STATE.last_run,
        "owner_declines": STATE.owner_declines,
    })


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> HTMLResponse:
    book = STATE.app.book
    rows = []
    for supplier_id in book.supplier_ids():
        days = book.details_changed_days_ago(supplier_id)
        if days is None:
            continue
        supplier = book.supplier(supplier_id) or {}
        rows.append({
            "supplier_id": supplier_id,
            "name": supplier.get("name", supplier_id),
            "days": days,
            "denied": bool(book.payment_details_changed(supplier_id)),
            "reverified": bool(supplier.get("reverified")),
            "reverified_at": supplier.get("reverified_at"),
        })
    return templates.TemplateResponse(request, "settings.html", _ctx(request, "settings") | {
        "suppliers": rows,
        "settings": sorted(config.describe().items()),
        "window": config.PAYEE_CHANGE_DENY_WINDOW_DAYS,
        "manifest_summary": STATE.app.audit_manifest.summary(),
    })


@app.post("/settings/reverify")
def reverify(supplier_id: str = Form(...)) -> RedirectResponse:
    """The only action that lifts a payee-change refusal."""
    from app import reverify_supplier

    try:
        reverify_supplier(STATE.app, supplier_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Remembered so the next run's fresh app instances carry it too.
    STATE.reverified.add(supplier_id)
    return RedirectResponse(url="/settings", status_code=303)


@app.on_event("shutdown")
def _shutdown() -> None:
    STATE.close()

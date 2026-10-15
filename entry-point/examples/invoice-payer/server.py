"""invoice-payer as a deployable Helios app: FastAPI + Jinja2/HTMX.

The deployment contract (`helios.yaml` entry: `server:app`) around the existing
CLI example. Same two load-bearing properties as helios-collect:

**/health never builds the app.** The smoke test runs this container with no
Redis, no audit sidecar and no model key, polling `/health` for up to 45 s.

**All URLs carry the mount prefix.** nginx strips `/apps/<slug>`; the engine
sends `X-Forwarded-Prefix` and every link uses it, because relative links break
at nested routes.

How the approval queue works here
---------------------------------
`ScriptedPayerAgent.run()` resolves holds inline through its owner, so a plain
scripted run would never leave anything for a human to see. `WebOwner` instead
ABORTS the run when a hold appears, leaving the proposal pending in the auditor.
The queue then lists it, and Approve calls `runtime.execute_held(...)` with the
exact held arguments -- an approval applies to that action and no other, so the
agent cannot swap in a different amount afterwards. Decline executes nothing.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HELIOS_DATA_DIR") or (HERE / "runs"))

app = FastAPI(title="invoice-payer", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(HERE / "templates"))

_lock = threading.Lock()
_runtime: Any = None
#: hold_id -> the exact proposal that was held. execute_held() reuses these
#: arguments verbatim; an approval must not be transferable to a different call.
_held_proposals: dict[str, dict] = {}
#: invoice_id -> last outcome, for the dashboard.
_outcomes: dict[str, dict] = {}


class HoldInterrupt(Exception):
    """Raised by WebOwner to stop a run at a hold and leave it pending."""

    def __init__(self, hold_id: str, proposal: dict):
        self.hold_id = hold_id
        self.proposal = proposal
        super().__init__(hold_id)


class WebOwner:
    """An owner that defers to the web queue instead of deciding inline."""

    def __init__(self, book: Any) -> None:
        self.book = book
        self.decisions: list[dict] = []

    def confirm_intent(self, request: dict[str, Any]) -> bool:
        # The payment intent is confirmed, exactly as the scripted owner does:
        # the interesting control in this example is the auditor's refusal of
        # INV-1003 even after the owner CONFIRMS a payment to a supplier whose
        # banking details just changed. Confirming here keeps that demo intact.
        self.decisions.append({"intent": request, "confirmed": True})
        return True

    def decide_hold(self, hold: Any) -> bool:
        raise HoldInterrupt(str(hold.get("hold_id")), dict(hold))


def get_runtime() -> Any:
    """Lazily assemble the app. Never called by /health."""
    global _runtime
    if _runtime is not None:
        return _runtime
    with _lock:
        if _runtime is not None:
            return _runtime
        try:
            import app as cli_app
        except Exception as exc:
            raise HTTPException(500, {
                "stage": "app_import",
                "error": f"could not import the invoice-payer app: "
                         f"{type(exc).__name__}: {exc}",
            }) from exc
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            runtime = cli_app.build_app(run_dir=DATA_DIR)
            cli_app._add_supplier_lookup(runtime.book)
        except Exception as exc:
            raise HTTPException(500, {
                "stage": "app_build",
                "error": f"could not assemble the app: {type(exc).__name__}: {exc}",
            }) from exc
        _runtime = runtime
        return runtime


def _prefix(request: Request) -> str:
    raw = (request.headers.get("x-forwarded-prefix") or "").strip()
    if not raw or raw == "/":
        return ""
    return raw.rstrip("/")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "app": "invoice-payer", "built": _runtime is not None}


@app.get("/style.css")
def style_css() -> FileResponse:
    return FileResponse(HERE / "templates" / "style.css", media_type="text/css")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    rt = get_runtime()
    invoices = []
    for inv_id, inv in rt.book.invoices.items():
        sup = rt.book.suppliers.get(inv["supplier_id"], {})
        outcome = _outcomes.get(inv_id)
        invoices.append({
            "invoice_id": inv_id, "reference": inv.get("reference", ""),
            "amount": inv.get("amount"), "currency": inv.get("currency"),
            "supplier": sup.get("name", inv["supplier_id"]),
            "status": (outcome or {}).get("status", "not run"),
            "reason": (outcome or {}).get("reason", ""),
        })
    import app as cli_app
    balance = rt.read("payments.balance", account=cli_app.OPERATING_ACCOUNT)
    return templates.TemplateResponse(request, "dashboard.html", {
        "prefix": _prefix(request),
        "invoices": invoices,
        "balance_cents": balance.get("balance_cents"),
        "holds_open": len(_open_holds(rt)),
        "app_id": rt.app_id, "task_type": rt.task_type,
    })


def _open_holds(rt: Any) -> list[dict]:
    """Pending holds, with the auditor's own owner_question."""
    out = []
    try:
        pending = rt.pending_holds()
    except Exception:
        return out
    for h in pending:
        rec = h if isinstance(h, dict) else getattr(h, "__dict__", {})
        if str(rec.get("status") or "pending") != "pending":
            continue
        hold_id = str(rec.get("hold_id") or "")
        if not hold_id:
            continue
        out.append({
            "hold_id": hold_id,
            "tool": rec.get("tool") or "?",
            "owner_question": rec.get("owner_question") or rec.get("reason") or "",
            "reason": rec.get("reason") or "",
            "arguments": rec.get("arguments") or {},
            "resumable": hold_id in _held_proposals,
        })
    return out


@app.get("/holds", response_class=HTMLResponse)
def holds_page(request: Request):
    rt = get_runtime()
    holds = _open_holds(rt)
    return templates.TemplateResponse(request, "holds.html", {
        "prefix": _prefix(request), "holds": holds, "holds_open": len(holds),
    })


@app.post("/holds/{hold_id}/approve")
def approve(hold_id: str, request: Request):
    return _decide(hold_id, True, request)


@app.post("/holds/{hold_id}/decline")
def decline(hold_id: str, request: Request):
    return _decide(hold_id, False, request)


def _decide(hold_id: str, approve: bool, request: Request) -> RedirectResponse:
    rt = get_runtime()
    proposal = _held_proposals.get(hold_id)
    if proposal is None:
        raise HTTPException(404, {
            "stage": "hold",
            "error": f"hold '{hold_id}' is unknown or was already resumed",
            "hint": "Reload the queue. A resumed hold is single-use, and holds "
                    "live in memory unless the data volume is mounted.",
        })
    try:
        rt.resolve_hold(hold_id, approve=approve)
    except Exception as exc:
        raise HTTPException(500, {
            "stage": "resume",
            "error": f"could not record the decision for '{hold_id}': "
                     f"{type(exc).__name__}: {exc}",
        }) from exc

    if approve:
        # execute_held reuses the EXACT held arguments, so an approval cannot be
        # redirected at a different amount or payee.
        result = rt.execute_held(proposal)
        _outcomes[str(proposal.get("invoice_id") or "")] = {
            "status": "paid_after_approval",
            "reason": f"owner approved hold {hold_id}",
            "state": (result or {}).get("state"),
        }
    else:
        _outcomes[str(proposal.get("invoice_id") or "")] = {
            "status": "declined_by_owner",
            "reason": proposal.get("reason", "the owner declined"),
        }
    _held_proposals.pop(hold_id, None)
    return RedirectResponse(url=f"{_prefix(request)}/holds", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    rt = get_runtime()
    invoices = []
    for inv_id, inv in rt.book.invoices.items():
        sup = rt.book.suppliers.get(inv["supplier_id"], {})
        invoices.append({"invoice_id": inv_id, "reference": inv.get("reference", ""),
                         "amount": inv.get("amount"), "currency": inv.get("currency"),
                         "supplier": sup.get("name", "")})
    return templates.TemplateResponse(request, "runs.html", {
        "prefix": _prefix(request), "invoices": invoices,
        "outcomes": _outcomes,
    })


@app.post("/runs")
def run_one(request: Request, invoice_id: str = Form(""), task: str = Form("")):
    """Run one invoice through the gated loop.

    Scripted, deliberately: the point is to prove the GATE works independently
    of what any model does. Substituting a real Qwen-Agent loop means replacing
    ScriptedPayerAgent.run with a model call over runtime.tool_specs(); the gate
    underneath does not change.
    """
    rt = get_runtime()
    import app as cli_app

    if not invoice_id:
        raise HTTPException(422, {
            "stage": "run", "error": "invoice_id is required",
            "problems": ["invoice_id was empty",
                         "known invoice_ids: " + ", ".join(sorted(rt.book.invoices))],
        })
    if invoice_id not in rt.book.invoices:
        raise HTTPException(422, {
            "stage": "run",
            "error": f"unknown invoice '{invoice_id}'",
            "problems": [f"invoice_id '{invoice_id}' is not in this app's data",
                         "known invoice_ids: " + ", ".join(sorted(rt.book.invoices))],
        })

    owner = WebOwner(rt.book)
    agent = cli_app.ScriptedPayerAgent(rt, owner=owner)
    try:
        outcome = agent.run(invoice_id)
    except HoldInterrupt as held:
        _held_proposals[held.hold_id] = held.proposal
        _outcomes[invoice_id] = {
            "status": "held", "reason": "waiting for the owner in the approval queue",
        }
        return RedirectResponse(url=f"{_prefix(request)}/holds", status_code=303)

    _outcomes[invoice_id] = {
        "status": outcome.get("status", "?"), "reason": outcome.get("reason", ""),
    }
    return RedirectResponse(url=f"{_prefix(request)}/runs/{invoice_id}", status_code=303)


@app.get("/runs/{invoice_id}", response_class=HTMLResponse)
def run_detail(request: Request, invoice_id: str):
    rt = get_runtime()
    if invoice_id not in rt.book.invoices:
        raise HTTPException(404, f"no such invoice: {invoice_id}")
    entries = [e for e in rt.log.entries()
               if e.get("invoice_id") == invoice_id or invoice_id in json.dumps(e, default=str)]
    return templates.TemplateResponse(request, "run_detail.html", {
        "prefix": _prefix(request), "run_id": invoice_id, "entries": entries,
        "outcome": _outcomes.get(invoice_id, {}),
    })


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    rt = get_runtime()
    path = rt.log.run_dir / "audit.jsonl"
    ok, problems = True, []
    try:
        from audit_agent.audit_log import AuditLog
        ok, problems = AuditLog(path).verify_chain()
    except Exception as exc:
        ok, problems = False, [f"{type(exc).__name__}: {exc}"]
    return templates.TemplateResponse(request, "audit.html", {
        "prefix": _prefix(request), "intact": ok, "problems": problems,
        "path": str(path),
    })

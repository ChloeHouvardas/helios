"""helios-collect as a deployable Helios app: FastAPI + Jinja2/HTMX.

This file is the deployment contract (`helios.yaml` entry: `server:app`) around
the existing CLI app. It adds a web UI; it does not change the CLI, the
scenarios, or any control.

Two properties are load-bearing and non-obvious:

**/health never touches the app.** The deploy smoke test runs this container on
an isolated network with no Redis, no audit sidecar and no model key, and polls
`/health` for up to 45 seconds. If /health needed any of those, every deploy
would fail. So the app is built LAZILY, on first use of a real page, and /health
is a constant.

**All URLs are relative.** nginx strips the `/apps/<slug>` prefix before
proxying, so an absolute link to `/holds` would escape the prefix and 404. Every
link and form action here is relative (`holds`, `runs`, `.`), which resolves
against whatever prefix the browser is already under.

The approval queue is the page that matters: without it a held agent pauses
invisibly. `/holds` shows the auditor's OWN `owner_question` -- never a summary
the agent wrote about itself -- and Approve/Decline call `runtime.resume(...)`,
which executes exactly the held action and nothing else.
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

# Runs and SQLite live under HELIOS_DATA_DIR (the engine mounts a named volume
# there, so holds and audit chains survive a redeploy). Locally it defaults to
# ./runs, matching the CLI.
DATA_DIR = Path(os.environ.get("HELIOS_DATA_DIR") or (HERE / "runs"))
RUNS_DIR = DATA_DIR

app = FastAPI(title="helios-collect", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(HERE / "templates"))

# One app instance per process, built on first use and guarded by a lock: two
# concurrent first-requests must not each assemble an auditor and a fact source.
_lock = threading.Lock()
_app: Any = None
_app_error: str = ""


def get_app() -> Any:
    """Lazily assemble the CLI app. Raises HTTPException with a usable message."""
    global _app, _app_error
    if _app is not None:
        return _app
    with _lock:
        if _app is not None:
            return _app
        try:
            import app as cli_app
        except Exception as exc:
            _app_error = f"{type(exc).__name__}: {exc}"
            raise HTTPException(500, {
                "stage": "app_import",
                "error": f"could not import the helios-collect app: {_app_error}",
                "hint": "Check the container logs; the platform packages must be "
                        "installed in the image.",
            }) from exc
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _app = cli_app.build_app(run_dir=RUNS_DIR)
        except Exception as exc:
            _app_error = f"{type(exc).__name__}: {exc}"
            raise HTTPException(500, {
                "stage": "app_build",
                "error": f"could not assemble the app: {_app_error}",
            }) from exc
        return _app


def _prefix(request: Request) -> str:
    """The URL prefix this app is mounted under, without a trailing slash.

    nginx strips `/apps/<slug>` before proxying, so the app always sees "/".
    Relative links are not a workaround: from `/runs/run-x` a relative `../holds`
    resolves to `/runs/holds`, which does not exist. The engine sends
    `X-Forwarded-Prefix`; using it lets every template write one absolute-in-prefix
    URL that is correct both behind nginx and when run locally at "/".
    """
    raw = request.headers.get("x-forwarded-prefix") or ""
    raw = raw.strip()
    if not raw or raw == "/":
        return ""
    return raw.rstrip("/")


@app.get("/style.css")
def style_css() -> FileResponse:
    return FileResponse(HERE / "templates" / "style.css", media_type="text/css")


def _model_driver_or_none() -> Any:
    """A real ModelDriver when a key is configured, else None (scripted runs)."""
    if not (os.environ.get("HELIOS_LLM_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")):
        return None
    try:
        from agent_integration.model_driver import ModelDriver
        return ModelDriver()
    except Exception:
        return None


# --------------------------------------------------------------------------
# health: constant, no dependencies, answers inside the smoke-test window
# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "app": "helios-collect", "built": _app is not None}


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    a = get_app()
    import app as cli_app

    outcomes = [_load_outcome(p) for p in _recent_runs(limit=12)]
    snapshot = a.book.snapshot()
    unpaid = [r for r in snapshot["invoices"]
              if str(r.get("status")).lower() not in {"paid", "settled", "void"}]
    overdue = [r for r in unpaid if (r.get("days_overdue") or 0) > 0]
    total_cents = sum(int(a.book.amount_due_cents(r["invoice_id"]) or 0) for r in unpaid)

    return templates.TemplateResponse(request, "dashboard.html", {
        "prefix": _prefix(request),
        "unpaid_count": len(unpaid),
        "overdue_count": len(overdue),
        "total_due": f"{total_cents / 100:.2f}",
        "task_types": a.task_types(),
        "outcomes": [o for o in outcomes if o],
        "holds_open": len(_open_holds(a)),
    })


@app.get("/holds", response_class=HTMLResponse)
def holds_page(request: Request):
    a = get_app()
    return templates.TemplateResponse(request, "holds.html", {
        "prefix": _prefix(request),
        "holds": _open_holds(a),
        "holds_open": len(_open_holds(a)),
    })


def _open_holds(a: Any) -> list[dict]:
    """Pending holds from the bridge, with the auditor's own owner_question.

    A hold is only resumable while the bridge still remembers the exact action,
    so the queue is read from the bridge rather than from any stored list.
    """
    out: list[dict] = []
    try:
        bridge = a.integration.bridge
        pending = bridge.client.pending_holds()
    except Exception:
        return out
    for h in pending:
        rec = h if isinstance(h, dict) else getattr(h, "__dict__", {})
        hold_id = str(rec.get("hold_id") or rec.get("id") or "")
        if not hold_id:
            continue
        status = str(rec.get("status") or "")
        if status and status not in {"pending", "held", "open", ""}:
            continue
        held = None
        try:
            held = a.runtime.bridge.held_action(hold_id)
        except Exception:
            held = None
        out.append({
            "hold_id": hold_id,
            "tool": rec.get("tool") or (held or {}).get("tool") or "?",
            "owner_question": rec.get("owner_question") or rec.get("reason") or "",
            "reason": rec.get("reason") or "",
            "rule": rec.get("rule") or rec.get("rule_id") or "",
            "arguments": (held or {}).get("arguments") or rec.get("arguments") or {},
            "resumable": held is not None,
        })
    return out


@app.post("/holds/{hold_id}/approve")
def approve_hold(hold_id: str, request: Request):
    return _decide(hold_id, True, request)


@app.post("/holds/{hold_id}/decline")
def decline_hold(hold_id: str, request: Request):
    return _decide(hold_id, False, request)


def _decide(hold_id: str, approve: bool, request: Request) -> RedirectResponse:
    """Record the owner's decision and resume the held run.

    resume() executes exactly the held action and nothing else; it does not
    re-ask the auditor, because the fact that triggered the hold has not
    changed. Declining executes nothing and the reason goes back to the model.
    """
    a = get_app()
    held = a.runtime.bridge.held_action(hold_id)
    if held is None:
        raise HTTPException(404, {
            "stage": "hold",
            "error": f"hold '{hold_id}' is unknown or was already resumed",
            "hint": "Reload the queue; a resumed hold is single-use.",
        })
    run_id = _run_id_for_hold(a, hold_id)
    try:
        import app as cli_app
        a.runtime.resume(
            driver=cli_app.ScriptedDriver(
                [], final_text="Resumed after the owner's decision."),
            run_id=run_id, hold_id=hold_id, decision=approve,
            decided_by=a.principal, task="owner decision on a held action",
            task_type=_task_type_for_run(a, run_id),
        )
    except Exception as exc:
        raise HTTPException(500, {
            "stage": "resume",
            "error": f"could not resume hold '{hold_id}': {type(exc).__name__}: {exc}",
        }) from exc
    # Absolute-in-prefix, not relative: the POST target is
    # /holds/{id}/approve, so a relative "../holds" would resolve to
    # /holds/holds. Under nginx the prefix must be carried too, since the app
    # itself only ever sees "/".
    return RedirectResponse(url=f"{_prefix(request)}/holds", status_code=303)


def _run_id_for_hold(a: Any, hold_id: str) -> str:
    """Find the run a hold belongs to, so resume() writes to the right log."""
    for run_dir in sorted((RUNS_DIR).glob("run-*"), reverse=True):
        if not run_dir.is_dir():
            continue
        log = run_dir / "action-log.jsonl"
        if not log.is_file():
            continue
        try:
            for line in log.read_text(encoding="utf-8").splitlines():
                if hold_id in line:
                    return run_dir.name
        except OSError:
            continue
    return ""


def _task_type_for_run(a: Any, run_id: str) -> str:
    log = RUNS_DIR / run_id / "action-log.jsonl"
    if not log.is_file():
        return ""
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            if '"task_type"' in line:
                rec = json.loads(line)
                if rec.get("task_type"):
                    return str(rec["task_type"])
    except (OSError, ValueError):
        pass
    return ""


@app.get("/runs", response_class=HTMLResponse)
def runs_form(request: Request):
    a = get_app()
    import app as cli_app
    return templates.TemplateResponse(request, "runs.html", {
        "prefix": _prefix(request),
        "scenarios": [
            {"invoice_id": s.invoice_id, "task_type": s.task_type,
             "control": s.control, "expects": s.expects, "note": s.note}
            for s in cli_app.scenarios()
        ],
        "task_types": a.task_types(),
        "model_available": _model_driver_or_none() is not None,
        "recent": _recent_runs(limit=10),
    })


@app.post("/runs")
def run_one(request: Request, task: str = Form(""), task_type: str = Form(""),
            invoice_id: str = Form(""), driver: str = Form("scripted")):
    """Run one scenario through the gated loop.

    Scripted by default: the point is to prove the GATE works independently of
    what any model does. `driver=model` uses the real ModelDriver when a key is
    present -- and the gate underneath is identical, which is the separability
    claim the architecture defence rests on.
    """
    a = get_app()
    import app as cli_app

    scenario = None
    if invoice_id:
        scenario = next((s for s in cli_app.scenarios()
                         if s.invoice_id == invoice_id), None)
    if scenario is None and task_type:
        scenario = next((s for s in cli_app.scenarios()
                         if s.task_type == task_type), None)
    if scenario is None:
        raise HTTPException(422, {
            "stage": "run",
            "error": "no scenario matches the request",
            "problems": [
                f"invoice_id '{invoice_id}' does not match a known scenario"
                if invoice_id else "invoice_id was empty",
                f"task_type '{task_type}' does not match a known scenario"
                if task_type else "task_type was empty",
                "known invoice_ids: "
                + ", ".join(s.invoice_id for s in cli_app.scenarios()),
                "known task_types: " + ", ".join(a.task_types()),
            ],
        })

    if driver == "model":
        model_driver = _model_driver_or_none()
        if model_driver is None:
            raise HTTPException(422, {
                "stage": "run",
                "error": "driver=model was requested but no model key is configured",
                "hint": "Set HELIOS_LLM_API_KEY (or DASHSCOPE_API_KEY) in the "
                        "engine environment and declare it under `env:` in "
                        "helios.yaml, or use the scripted driver.",
            })
        real_driver = model_driver
    else:
        real_driver = cli_app.ScriptedDriver(scenario.calls)

    owner = cli_app.ScriptedOwner()
    task_text = task or (
        f"{scenario.task_type} for invoice {scenario.invoice_id}")

    result = a.runtime.run(
        driver=real_driver, task=task_text, task_type=scenario.task_type,
        # Request every tool the app has; the auditor grants only what this
        # task's allowlist permits, so an ungranted tool is invisible to the
        # agent rather than rejected after the fact.
        requested_tools=list(cli_app.app_tools.TOOL_NAMES),
    )
    return RedirectResponse(url=f"{_prefix(request)}/runs/{result.run_id}",
                            status_code=303)


def _recent_runs(limit: int = 12) -> list[Path]:
    if not RUNS_DIR.is_dir():
        return []
    runs = sorted((p for p in RUNS_DIR.glob("run-*") if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[:limit]


def _load_outcome(run_dir: Path) -> dict:
    """Summarise a run from its action log, for the dashboard.

    The log's record type is `kind`, not `event`: run_started, authorization,
    tool_call, proposal, verdict, hold, resumed, action_result, run_finished.
    `executed` is true only on action_result, which is the entry that proves a
    side effect actually happened -- counting tool_call entries would report an
    attempted action as a completed one.
    """
    log = run_dir / "action-log.jsonl"
    if not log.is_file():
        return {}
    kinds: list[str] = []
    task_type = ""
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("task_type"):
                task_type = str(rec["task_type"])
            if rec.get("kind"):
                kinds.append(str(rec["kind"]))
    except (OSError, ValueError):
        return {}
    denied = kinds.count("verdict") - kinds.count("action_result")
    return {
        "run_id": run_dir.name,
        "task_type": task_type,
        "executed": kinds.count("action_result"),
        "denied": max(denied, 0),
        "held": kinds.count("hold"),
        "steps": len(kinds),
    }


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    run_dir = RUNS_DIR / run_id
    if not run_dir.is_dir():
        raise HTTPException(404, f"no such run: {run_id}")
    log = run_dir / "action-log.jsonl"
    entries: list[dict] = []
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    entries.append({"raw": line})
    return templates.TemplateResponse(request, "run_detail.html", {
        "prefix": _prefix(request), "run_id": run_id, "entries": entries,
    })


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    """Chain verification of the auditor's own hash-chained evidence."""
    a = get_app()
    ok, problems = True, []
    path = a.audit_log_path
    try:
        from audit_agent.audit_log import AuditLog
        ok, problems = AuditLog(path).verify_chain()
    except Exception as exc:
        ok, problems = False, [f"{type(exc).__name__}: {exc}"]
    return templates.TemplateResponse(request, "audit.html", {
        "prefix": _prefix(request),
        "intact": ok, "problems": problems, "path": str(path),
    })

"""Web UI tests for helios-collect as a deployed app (Task 7).

The four properties these tests exist to prove:

1. `/health` answers 200 with NO environment at all -- no key, no Redis, no
   auditor. The deploy smoke test runs the container on an isolated network and
   polls `/health` for up to 45 s, so a health check that needed any of those
   would fail every deploy.
2. The dashboard renders from the app's real book.
3. A scenario the auditor HOLDS shows up in the approval queue, carrying the
   auditor's own `owner_question` -- not a summary the agent wrote about itself.
4. Approving executes EXACTLY the held action (asserted against the action log);
   declining executes nothing.

The existing scenario tests in test_controls.py cover the CLI and are untouched.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient over a server whose runs land in a temp dir.

    HELIOS_DATA_DIR is honoured by server.py at import time, so it must be set
    before the module is imported. Each test gets a fresh module and a fresh
    directory: the app instance is a module-level singleton, and holds are
    per-process state.
    """
    import importlib
    import sys

    monkeypatch.setenv("HELIOS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HELIOS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    for name in ("server", "app", "book", "tools", "facts", "config"):
        sys.modules.pop(name, None)
    server = importlib.import_module("server")
    # Point the app's own runs directory at the temp dir too, so the CLI-shaped
    # default (HERE/runs) is not polluted by tests.
    server.DATA_DIR = tmp_path
    server.RUNS_DIR = tmp_path
    with TestClient(server.app) as c:
        c.server = server
        yield c


def _log_entries(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _executed(entries: list[dict]) -> list[dict]:
    """Entries proving a side effect actually happened.

    `executed` is set on the `action_result` record only. Counting `tool_call`
    entries would report an attempted (and possibly denied) action as completed.
    """
    return [e for e in entries if e.get("kind") == "action_result" and e.get("executed")]


def _run_app(client, invoice_id: str):
    """Drive one scenario through the gate and return (app, RunResult, Scenario)."""
    server = client.server
    import app as cli_app_mod
    a = server.get_app()
    scenario = next(s for s in cli_app_mod.scenarios() if s.invoice_id == invoice_id)
    result = a.runtime.run(
        driver=cli_app_mod.ScriptedDriver(scenario.calls),
        task=f"{scenario.task_type} for invoice {scenario.invoice_id} "
             f"{scenario.note[:20] or 'a receivables task needing an agent'}",
        task_type=scenario.task_type,
        requested_tools=list(cli_app_mod.app_tools.TOOL_NAMES),
    )
    return a, result, scenario


# --------------------------------------------------------------------------
# 1. health must not need anything
# --------------------------------------------------------------------------
def test_health_ok_with_no_environment(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["app"] == "helios-collect"
    # Proves /health did not build the app: no auditor, no fact source, no book.
    assert body["built"] is False


def test_health_does_not_build_the_app(client):
    client.get("/health")
    assert client.server._app is None


# --------------------------------------------------------------------------
# 2. pages render
# --------------------------------------------------------------------------
def test_dashboard_renders_real_book(client):
    r = client.get("/")
    assert r.status_code == 200
    # The book's own numbers, not placeholders.
    assert "Unpaid invoices" in r.text
    assert "collect.reminder" in r.text


def test_css_is_served(client):
    r = client.get("/style.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


def test_audit_page_verifies_chain(client):
    r = client.get("/audit")
    assert r.status_code == 200
    assert "intact" in r.text


def test_runs_page_lists_scenarios(client):
    r = client.get("/runs")
    assert r.status_code == 200
    assert "INV-1006" in r.text and "INV-1004" in r.text


def test_unknown_run_404s(client):
    assert client.get("/runs/run-not-real").status_code == 404


# --------------------------------------------------------------------------
# 3. prefix handling (nginx strips /apps/<slug>)
# --------------------------------------------------------------------------
def test_prefix_is_applied_to_links(client):
    r = client.get("/holds", headers={"X-Forwarded-Prefix": "/apps/helios-collect-t1"})
    assert r.status_code == 200
    assert "/apps/helios-collect-t1/holds" in r.text
    assert "/apps/helios-collect-t1/style.css" in r.text


def test_prefix_absent_locally(client):
    r = client.get("/holds")
    assert r.status_code == 200
    assert 'href="/holds"' in r.text


# --------------------------------------------------------------------------
# 4. the approval queue: the page that matters
# --------------------------------------------------------------------------
def test_hold_appears_in_queue_with_the_auditors_own_question(client):
    a, result, scenario = _run_app(client, "INV-1004")  # 45-day extension -> hold
    assert result.status == "held"
    assert len(result.holds) == 1
    hold = result.holds[0]

    r = client.get("/holds")
    assert r.status_code == 200
    assert hold["hold_id"] in r.text
    assert "send_email" in r.text
    # The auditor's question, verbatim -- never a summary the agent wrote.
    # Jinja autoescapes, so compare against the escaped rendering.
    from markupsafe import escape
    assert str(escape(hold["owner_question"][:60])) in r.text
    assert f"/holds/{hold['hold_id']}/approve" in r.text
    assert f"/holds/{hold['hold_id']}/decline" in r.text


def test_declining_executes_nothing(client):
    a, result, scenario = _run_app(client, "INV-1004")
    hold_id = result.holds[0]["hold_id"]
    log_path = Path(result.action_log_path)
    before = _executed(_log_entries(log_path))

    r = client.post(f"/holds/{hold_id}/decline", follow_redirects=False)
    assert r.status_code == 303

    entries = _log_entries(log_path)
    assert len(_executed(entries)) == len(before), \
        "declining must not execute anything"
    assert any(e.get("kind") == "resumed" and e.get("approved") is False
               for e in entries)
    # A resumed hold is single-use.
    assert a.runtime.bridge.held_action(hold_id) is None


def test_approving_executes_exactly_the_held_action(client):
    a, result, scenario = _run_app(client, "INV-1004")
    hold_id = result.holds[0]["hold_id"]
    held = a.runtime.bridge.held_action(hold_id)
    assert held is not None
    held_tool = held["tool"]
    held_args = dict(held["arguments"])

    log_path = Path(result.action_log_path)
    executed_before = _executed(_log_entries(log_path))

    r = client.post(f"/holds/{hold_id}/approve", follow_redirects=False)
    assert r.status_code == 303

    after = _log_entries(log_path)
    new = _executed(after)[len(executed_before):]

    # EXACTLY one action executed: the held one, with the held arguments.
    assert len(new) == 1, f"expected exactly one executed action, got {len(new)}"
    assert new[0].get("tool") == held_tool
    resumed = [e for e in after if e.get("kind") == "resumed"]
    assert resumed and resumed[-1].get("approved") is True
    assert resumed[-1].get("tool") == held_tool
    assert resumed[-1].get("arguments") == held_args
    # And it is single-use: approving twice must not execute it again.
    assert a.runtime.bridge.held_action(hold_id) is None


def test_approving_twice_does_not_execute_twice(client):
    a, result, _ = _run_app(client, "INV-1004")
    hold_id = result.holds[0]["hold_id"]
    assert client.post(f"/holds/{hold_id}/approve", follow_redirects=False).status_code == 303
    r = client.post(f"/holds/{hold_id}/approve", follow_redirects=False)
    assert r.status_code == 404


# --------------------------------------------------------------------------
# 5. a refusal never reaches the queue
# --------------------------------------------------------------------------
def test_denied_action_never_appears_in_the_queue(client):
    """INV-1006 is disputed: denied in code, so the owner is never consulted.

    This is the control that must not depend on a human noticing -- a busy owner
    rubber-stamping the queue still cannot make it happen.

    Asserted on the absence of any hold CARD, not on the absence of the string
    "INV-1006": the empty-queue help text names that scenario on purpose, so a
    substring check would pass for the wrong reason or fail for an irrelevant
    one.
    """
    a, result, _ = _run_app(client, "INV-1006")
    assert result.holds == [], "a denied action must not become a hold"

    r = client.get("/holds")
    assert r.status_code == 200
    assert 'class="hold"' not in r.text, "the queue must show no actionable hold"
    assert "/approve" not in r.text and "/decline" not in r.text
    assert "Nothing is held" in r.text


# --------------------------------------------------------------------------
# 6. POST /runs drives a scenario
# --------------------------------------------------------------------------
def test_post_runs_executes_a_scenario(client):
    r = client.post("/runs", data={"invoice_id": "INV-1001", "driver": "scripted"},
                    follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/runs/run-"), location
    detail = client.get(location)
    assert detail.status_code == 200
    assert "run_started" in detail.text or "tool_call" in detail.text


def test_post_runs_rejects_unknown_scenario_with_actionable_error(client):
    r = client.post("/runs", data={"invoice_id": "INV-9999", "driver": "scripted"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["stage"] == "run"
    # The error must name what to write instead, for a model to act on.
    assert any("known invoice_ids" in p for p in detail["problems"])


def test_model_driver_refused_without_a_key(client):
    """Asking for the real model with no key must fail loudly, not silently
    fall back to the script -- that would report a model run that never happened."""
    r = client.post("/runs", data={"invoice_id": "INV-1001", "driver": "model"})
    assert r.status_code == 422
    assert r.json()["detail"]["stage"] == "run"
    assert "no model key" in r.json()["detail"]["error"]


def test_data_dir_is_honoured(client, tmp_path):
    """Runs and SQLite must land under HELIOS_DATA_DIR, the mounted volume."""
    client.post("/runs", data={"invoice_id": "INV-1001", "driver": "scripted"})
    assert list(tmp_path.glob("run-*")), "no run directory was created under the data dir"

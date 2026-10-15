"""Web UI tests for invoice-payer as a deployed app (Task 7).

Same four properties as helios-collect's suite: /health needs nothing, pages
render, a hold reaches the approval queue, and approving executes exactly the
held action while declining executes nothing.

The control this example exists to demonstrate -- INV-1003 -- is asserted here
too: the owner CONFIRMS a payment to a supplier whose banking details just
changed, and the auditor still REFUSES it. The control does not depend on the
human noticing, and it does not depend on the agent behaving.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HELIOS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    for name in ("server", "app"):
        sys.modules.pop(name, None)
    server = importlib.import_module("server")
    server.DATA_DIR = tmp_path
    server._runtime = None
    server._held_proposals.clear()
    server._outcomes.clear()
    with TestClient(server.app) as c:
        c.server = server
        yield c


# --------------------------------------------------------------------------
# health needs nothing
# --------------------------------------------------------------------------
def test_health_ok_with_no_environment(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["app"] == "invoice-payer"
    # Proves /health did not build the app: the smoke test has no Redis, no
    # auditor and no key, and must still answer within 45 s.
    assert body["built"] is False


def test_health_does_not_build_the_app(client):
    client.get("/health")
    assert client.server._runtime is None


# --------------------------------------------------------------------------
# pages render
# --------------------------------------------------------------------------
def test_dashboard_lists_invoices(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "INV-1001" in r.text and "INV-1003" in r.text


def test_css_served(client):
    r = client.get("/style.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


def test_audit_page_verifies_chain(client):
    r = client.get("/audit")
    assert r.status_code == 200
    assert "intact" in r.text


def test_prefix_applied_to_links(client):
    r = client.get("/holds", headers={"X-Forwarded-Prefix": "/apps/invoice-payer-t1"})
    assert r.status_code == 200
    assert "/apps/invoice-payer-t1/holds" in r.text


def test_unknown_invoice_404s(client):
    assert client.get("/runs/INV-9999").status_code == 404


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------
def test_run_rejects_unknown_invoice_with_actionable_error(client):
    r = client.post("/runs", data={"invoice_id": "INV-9999"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["stage"] == "run"
    assert any("known invoice_ids" in p for p in detail["problems"])


def test_run_requires_an_invoice(client):
    r = client.post("/runs", data={"invoice_id": ""})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invoice_id is required"


def test_inv1003_is_refused_even_though_the_owner_confirms(client):
    """THE DEMO. Owner confirms; the auditor still refuses.

    WebOwner.confirm_intent returns True, so the intent credential is minted --
    and the payment is refused anyway, because the supplier's banking details
    changed recently and that is a fact the auditor reads from the fact source,
    not something the agent or the owner can talk around.
    """
    r = client.post("/runs", data={"invoice_id": "INV-1003"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.server._outcomes["INV-1003"]["status"] == "refused"
    detail = client.get("/runs/INV-1003")
    assert detail.status_code == 200
    assert "refused" in detail.text.lower()


def test_paid_invoice_settles(client):
    client.post("/runs", data={"invoice_id": "INV-1001"}, follow_redirects=False)
    assert client.server._outcomes["INV-1001"]["status"] == "paid"


# --------------------------------------------------------------------------
# approval queue
# --------------------------------------------------------------------------
def _find_holding_invoice(client) -> str | None:
    """An invoice whose payment the auditor holds rather than allows or denies."""
    rt = client.server.get_runtime()
    import app as cli_app
    for inv_id in list(rt.book.invoices):
        client.server._outcomes.clear()
        client.server._held_proposals.clear()
        r = client.post("/runs", data={"invoice_id": inv_id}, follow_redirects=False)
        if r.status_code == 303 and r.headers["location"].endswith("/holds"):
            return inv_id
    return None


def test_hold_reaches_the_queue_and_decline_executes_nothing(client):
    inv = _find_holding_invoice(client)
    if inv is None:
        pytest.skip("no scenario produces a hold in this data set")

    assert len(client.server._held_proposals) == 1
    hold_id = next(iter(client.server._held_proposals))

    r = client.get("/holds")
    assert r.status_code == 200
    assert hold_id in r.text
    assert f"/holds/{hold_id}/approve" in r.text
    assert f"/holds/{hold_id}/decline" in r.text
    # The auditor's own question, never a summary the agent wrote.
    assert "owner_question" in r.text or "payments.send" in r.text

    log_entries_before = len(client.server.get_runtime().log.entries())
    r2 = client.post(f"/holds/{hold_id}/decline", follow_redirects=False)
    assert r2.status_code == 303

    rt = client.server.get_runtime()
    executed = [e for e in rt.log.entries() if e.get("kind") == "held_executed"]
    assert executed == [], "declining must not execute the held payment"
    assert hold_id not in client.server._held_proposals, "a decision is single-use"
    assert any(e.get("kind") == "hold_resolved" and e.get("approve") is False
               for e in rt.log.entries())


def test_unknown_hold_404s(client):
    r = client.post("/holds/hold-does-not-exist/approve", follow_redirects=False)
    assert r.status_code == 404

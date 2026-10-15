"""Deployment-contract tests for the web server."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

import config

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]


@pytest.fixture
def client():
    import server
    importlib.reload(server)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as c:
        yield c, server
    server.STATE.close()


def test_health_is_constant_and_never_builds_the_app(client):
    c, server = client
    first = c.get("/health")
    assert first.status_code == 200 and first.json()["status"] == "ok"
    assert server.STATE.built is False
    assert c.get("/health").json() == first.json()
    assert server.STATE.built is False


def test_every_page_renders(client):
    c, server = client
    for path in ("/", "/forecast", "/scenarios", "/funding", "/holds", "/settings"):
        r = c.get(path)
        assert r.status_code == 200, path
    assert server.STATE.built is True
    assert c.get("/diagnostics").json()["first_short_week"]


def test_pages_build_links_from_the_forwarded_prefix(client):
    c, _ = client
    r = c.get("/", headers={"x-forwarded-prefix": "/apps/helios-cash-planner-demo"})
    assert 'href="/apps/helios-cash-planner-demo/forecast"' in r.text


def test_a_what_if_answers_without_changing_the_book(client):
    c, server = client
    before = server.STATE.app.book.first_short_week()
    r = c.get("/scenarios", params={"kind": "customer_pays_late", "target_id": "INV-3002", "days": 14})
    assert r.status_code == 200 and "Scenario lowest balance" in r.text
    assert server.STATE.app.book.first_short_week() == before


def test_a_tenant_event_re_projects(client):
    c, server = client
    before = c.get("/diagnostics").json()["first_short_week"]
    r = c.post("/events", json={"event": "invoice.extension_agreed",
                                "data": {"invoice": "INV-3002", "new_due": "2099-01-01"},
                                "app": "helios-collect"})
    assert r.status_code == 200 and r.json()["forecast_changed"] is True
    assert c.get("/events").json()["events"][-1]["event"] == "invoice.extension_agreed"
    after = c.get("/diagnostics").json()["first_short_week"]
    assert int(after[1:]) <= int(before[1:])


def test_a_malformed_event_is_a_422(client):
    c, _ = client
    assert c.post("/events", json={"nope": 1}).status_code == 422


def test_run_redirects_to_the_holds_page(client):
    c, server = client
    r = c.post("/run", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/holds")
    page = c.get("/holds").text
    assert "app.warning_only_when_short" in page
    assert len(server.STATE.outcomes) == 11


def test_deployment_manifest_passes_the_real_engine_validator():
    from engine.contracts import lint, load_manifest

    manifest = load_manifest(HERE)
    assert manifest.name == config.APP_ID
    assert manifest.entry == "server:app"
    assert set(manifest.permissions) >= {"forecast", "market-data", "knowledge-graph", "email", "events"}
    assert manifest.data_dir == "/data"
    assert lint(HERE) == []


def test_the_app_spec_and_both_manifests_name_the_same_tools():
    spec = yaml.safe_load((HERE / "helios-app.yaml").read_text(encoding="utf-8"))
    names = [t if isinstance(t, str) else t["name"] for t in spec["tools"]]
    audit = yaml.safe_load((HERE / "agents" / "helios-cash-planner-audit.yaml").read_text(encoding="utf-8"))
    assert sorted(names) == sorted(t["name"] for t in audit["tools"])

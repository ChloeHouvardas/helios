"""Deployment-contract tests for the web server.

These exist because two properties of `server.py` are load-bearing for `helios
deploy` and neither is obvious from reading the app:

1. **`GET /health` must be constant and must not build the app.** The engine's smoke
   test runs the container on an isolated network with no Redis, no audit sidecar, no
   model key and no data volume, polling `/health` up to 45 times at 1s intervals
   (`platform/deployment/engine/orchestrator.py`). An app built at import time, or
   built inside `/health`, fails that check and the deploy fails at the
   `smoke_test` stage. This app's first version did exactly that and would not have
   deployed.

2. **The app is built lazily, on first use of a real page.** So `/diagnostics` — the
   endpoint that does report whether the auditor compiled its rules — must be a
   different route from the liveness probe.

`fastapi.testclient` needs no network, so these run inside the offline guard in
conftest.py like everything else.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
DEPLOYMENT = REPO.parent / "p2-deploy" / "platform" / "deployment"

for _p in (
    REPO / "platform" / "audit-agent-builder" / "src",
    REPO / "platform" / "action-agent-builder" / "src",
    REPO / "platform" / "agent-integration" / "src",
    REPO / "platform" / "financial-tools",
    HERE,
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import config  # noqa: E402


@pytest.fixture
def client():
    """A TestClient over a freshly imported server, so STATE starts unbuilt."""
    import server
    importlib.reload(server)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as c:
        yield c, server
    server.STATE.close()


def test_importing_the_server_does_not_build_the_app(client):
    """The smoke test imports the module before any request arrives."""
    _, server = client
    assert server.STATE.built is False


def test_health_is_constant_and_never_builds_the_app(client):
    """The property that decides whether this app is deployable at all."""
    c, server = client
    first = c.get("/health")
    assert first.status_code == 200
    assert first.json()["status"] == "ok"
    assert server.STATE.built is False, (
        "/health must not build the app: the deploy smoke test runs with no Redis, "
        "no audit sidecar, no model key and no data volume")
    # Constant across calls, so a 45x1s poll sees the same answer every time.
    assert c.get("/health").json() == first.json()
    assert server.STATE.built is False


def test_diagnostics_reports_the_real_readiness(client):
    """The honest readiness check, deliberately kept off the liveness path."""
    c, server = client
    payload = c.get("/diagnostics").json()
    assert payload["status"] == "ok"
    assert payload["rules_loaded"] == 17
    assert payload["tools_declared"] == 10
    assert payload["bills"] == 15
    assert payload["suppliers"] == 8
    assert server.STATE.built is True


def test_pages_build_the_app_lazily_and_render(client):
    c, server = client
    assert server.STATE.built is False
    for path in ("/", "/bills", "/holds", "/settings"):
        assert c.get(path).status_code == 200, path
    assert server.STATE.built is True


def test_unknown_bill_is_a_404_with_an_actionable_detail(client):
    """Error messages name the problem and what to do instead."""
    c, _ = client
    r = c.get("/bills/BILL-NOPE")
    assert r.status_code == 404
    assert "BILL-NOPE" in r.json()["detail"]


def test_run_then_reverify_lifts_the_payee_denial(client):
    """The settings checkpoint survives the lazy rebuild.

    Each scenario builds a fresh app from the fixtures, so re-verification has to be
    re-applied to every instance — otherwise the settings page would appear to do
    nothing, which is the bug this guards.
    """
    c, _ = client
    c.post("/run", data={"scope": "all", "owner_policy": "approve"})
    assert "BILL-2002" in c.get("/holds").text

    # TestClient follows redirects, so the 303 lands as 200 on /settings.
    posted = c.post("/settings/reverify", data={"supplier_id": "SUP-002"})
    assert posted.status_code == 200
    assert posted.url.path == "/settings", "the POST must redirect back to settings"
    assert "re-verified" in posted.text, "the page must show the new state"

    c.post("/run", data={"scope": "all", "owner_policy": "approve"})
    assert "BILL-2002" not in c.get("/holds").text


def test_reverify_unknown_supplier_is_a_404(client):
    c, _ = client
    assert c.post("/settings/reverify",
                  data={"supplier_id": "SUP-NOPE"}).status_code == 404


def test_reverify_uses_a_redirect_not_a_render(client):
    """POST-then-redirect, so a reload cannot re-submit the re-verification.

    Asserted on the raw response, because TestClient follows redirects and would
    otherwise hide the status code that makes this safe.
    """
    c, _ = client
    raw = c.post("/settings/reverify", data={"supplier_id": "SUP-002"},
                 follow_redirects=False)
    assert raw.status_code == 303
    assert raw.headers["location"] == "/settings"


# --------------------------------------------------------------------------
# The manifest, checked against the real engine when it is available
# --------------------------------------------------------------------------

def test_deployment_manifest_shape():
    """`helios.yaml` conforms to the engine's Manifest schema.

    The engine rejects unknown fields outright, so a manifest written from memory
    rather than from `contracts.py` fails at the `manifest` stage with no build ever
    attempted. Checked structurally here, and against the real validator when the
    p2-deploy worktree is present.
    """
    raw = yaml.safe_load((HERE / "helios.yaml").read_text(encoding="utf-8"))

    known = {"name", "tenant", "entry", "port", "permissions", "env",
             "audit_manifest", "data_dir"}
    assert set(raw) - known == set(), (
        f"unknown field(s) the engine would reject: {sorted(set(raw) - known)}")

    assert raw["name"] == config.APP_ID
    assert raw["entry"] == "server:app"
    assert isinstance(raw["port"], int) and not isinstance(raw["port"], bool)
    assert 1 <= raw["port"] <= 65535
    # Must be absolute: a relative path resolves against the container WORKDIR and
    # silently lands somewhere else.
    assert raw["data_dir"].startswith("/")
    # The reviewed policy has to be named, or the sidecar falls back to deny-all.
    assert (HERE / raw["audit_manifest"]).exists()
    # Env entries are NAMES only; a value here would be a committed secret.
    for name in raw["env"]:
        assert isinstance(name, str) and "=" not in name, name

    allowed = {"payments", "email", "knowledge-graph", "events", "forecast",
               "market-data", "spreadsheets", "collect"}
    unknown_perms = [p for p in raw["permissions"] if p not in allowed]
    assert not unknown_perms, (
        f"permissions the engine would refuse: {unknown_perms}")


@pytest.mark.skipif(not DEPLOYMENT.exists(),
                    reason="the p2-deploy worktree is not present")
def test_deployment_manifest_passes_the_real_engine_validator():
    """Validated by the engine itself, not by a copy of its rules.

    Skipped when the deployment worktree is absent, so this suite stays runnable
    from a merged checkout where the engine lives at platform/deployment.
    """
    sys.path.insert(0, str(DEPLOYMENT))
    from engine.contracts import ContractError, load_manifest

    manifest = load_manifest(HERE)          # raises ContractError with every problem
    assert manifest.name == config.APP_ID
    assert manifest.entry == "server:app"
    assert manifest.audit_manifest == "agents/helios-payables-audit.yaml"

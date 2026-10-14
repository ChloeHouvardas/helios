"""Service hardening: bearer auth, /health, and durable world state.

The deployment engine runs this service in a container that must survive
restarts and must not be callable by anyone who can reach the port. Three
properties are asserted here:

1. **Bearer auth when configured.** With `HELIOS_EXECUTION_SECRET` (or an
   explicit secret) every route except `GET /health` needs the token. Without it
   the service keeps its historical open behaviour, so existing callers and the
   other service tests are unaffected.
2. **The token is not the signing secret.** A client holding the bearer token can
   reach the tools but still cannot forge an intent auth code — that material
   stays server-side. Authenticating the caller does not authorize an action.
3. **Persistence.** A `World` snapshot saved to SQLite is restored into a fresh
   bundle, so a restart does not forget payments or verified graph facts.

Offline: the server runs on localhost with an injected fake transport-free setup;
no external network, no real secrets.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from fin_tools import (EXECUTION_SECRET_ENV, HttpToolClient, SqlitePersistence,
                       ToolServer, build_world, free_port)

EXEC_SECRET = "container-execution-secret"


@pytest.fixture
def authed_server():
    """A server that enforces a bearer token."""
    bundle = build_world()
    srv = ToolServer(bundle, port=free_port(), secret=b"server-only-secret",
                     execution_secret=EXEC_SECRET).start()
    yield srv, bundle
    srv.stop()


def _raw_get(url: str, headers: dict | None = None):
    req = Request(url, method="GET", headers=headers or {})
    with urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode())


# --------------------------------------------------------------------------
# /health is open
# --------------------------------------------------------------------------

def test_health_is_reachable_without_a_token(authed_server):
    srv, _ = authed_server
    status, body = _raw_get(f"{srv.base_url}/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["role"] == "helios-financial-tools"


def test_health_leaks_no_secret(authed_server):
    srv, _ = authed_server
    _, body = _raw_get(f"{srv.base_url}/health")
    assert EXEC_SECRET not in json.dumps(body)


# --------------------------------------------------------------------------
# bearer auth
# --------------------------------------------------------------------------

def test_tools_requires_a_token_when_configured(authed_server):
    srv, _ = authed_server
    with pytest.raises(HTTPError) as exc:
        _raw_get(f"{srv.base_url}/tools")
    assert exc.value.code == 401


def test_call_requires_a_token(authed_server):
    srv, _ = authed_server
    req = Request(f"{srv.base_url}/call",
                  data=json.dumps({"tool": "payments.balance",
                                   "args": {"account": "x"}}).encode(),
                  method="POST", headers={"Content-Type": "application/json"})
    with pytest.raises(HTTPError) as exc:
        with urlopen(req, timeout=5) as r:
            r.read()
    assert exc.value.code == 401


def test_a_wrong_token_is_rejected(authed_server):
    srv, _ = authed_server
    client = HttpToolClient(srv.base_url, timeout_seconds=5,
                            execution_secret="not-the-secret")
    with pytest.raises(PermissionError):
        client.catalog()


def test_the_right_token_is_accepted(authed_server):
    srv, _ = authed_server
    client = HttpToolClient(srv.base_url, timeout_seconds=5,
                            execution_secret=EXEC_SECRET)
    names = {t["name"] for t in client.catalog()["tools"]}
    assert "payments.send" in names


def test_authed_call_works_end_to_end(authed_server):
    srv, bundle = authed_server
    client = HttpToolClient(srv.base_url, timeout_seconds=5,
                            execution_secret=EXEC_SECRET)
    bundle.payments.receive_payment(payer="c", payee="us", amount_cents=700,
                                    currency="CNY")
    out = client.call("payments.balance", account="us")
    assert out["result"] == {"balance_cents": 700}


def test_execution_secret_is_read_from_the_environment(monkeypatch):
    """The deployment path: the secret comes from the environment only."""
    monkeypatch.setenv(EXECUTION_SECRET_ENV, EXEC_SECRET)
    bundle = build_world()
    srv = ToolServer(bundle, port=free_port()).start()   # no explicit secret
    try:
        # without a token -> 401
        with pytest.raises(HTTPError) as exc:
            _raw_get(f"{srv.base_url}/tools")
        assert exc.value.code == 401
        # with the env token -> 200
        client = HttpToolClient(srv.base_url, timeout_seconds=5,
                                execution_secret=EXEC_SECRET)
        assert client.catalog()["tools"]
    finally:
        srv.stop()


# --------------------------------------------------------------------------
# auth does not authorize: the bearer token is not the signing secret
# --------------------------------------------------------------------------

def test_a_valid_bearer_token_cannot_forge_an_auth_code(authed_server):
    srv, _ = authed_server
    client = HttpToolClient(srv.base_url, timeout_seconds=5,
                            execution_secret=EXEC_SECRET)
    # Authenticated caller, but a made-up auth code: the payment is refused,
    # because authenticating the HTTP caller does not authorize the money move.
    out = client.call("payments.send", payer="ops", payee="supplier",
                      amount_cents=5000, currency="CNY", auth_code="0" * 64)
    assert out["result"]["state"] == "rejected"


def test_owner_confirmation_still_settles_over_an_authed_connection(authed_server):
    srv, bundle = authed_server
    client = HttpToolClient(srv.base_url, timeout_seconds=5,
                            execution_secret=EXEC_SECRET)
    intent = bundle.intents.capture_intent("chloe", 5000, "CNY", "supplier", "p")
    code = bundle.intents.confirm(intent.intent_id, by="chloe").code
    out = client.call("payments.send", payer="ops", payee="supplier",
                      amount_cents=5000, currency="CNY", auth_code=code)
    assert out["result"]["state"] == "settled"


# --------------------------------------------------------------------------
# open mode is unchanged (no execution secret configured)
# --------------------------------------------------------------------------

def test_open_mode_when_no_secret_configured(monkeypatch):
    monkeypatch.delenv(EXECUTION_SECRET_ENV, raising=False)
    bundle = build_world()
    srv = ToolServer(bundle, port=free_port()).start()
    try:
        client = HttpToolClient(srv.base_url, timeout_seconds=5)
        assert client.catalog()["tools"]          # no token needed
        out = client.call("payments.balance", account="nobody")
        assert out["result"] == {"balance_cents": 0}
    finally:
        srv.stop()


# --------------------------------------------------------------------------
# kg/nodes route (the deployment core exposes this)
# --------------------------------------------------------------------------

def test_kg_nodes_lists_published_verified_facts(authed_server):
    srv, bundle = authed_server
    bundle.graph.upsert_verified("market_rate", "lpr_1y@2026-10-20",
                                 {"value_bps": 290}, source="Tushare Pro:shibor_lpr")
    client_secret = EXEC_SECRET
    req = Request(f"{srv.base_url}/kg/nodes", method="GET",
                  headers={"Authorization": f"Bearer {client_secret}"})
    with urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read().decode())
    ids = {n["node_id"] for n in body["nodes"]}
    assert "market_rate:lpr_1y@2026-10-20" in ids


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def test_world_state_survives_a_restart(tmp_path):
    store_dir = str(tmp_path)
    persistence = SqlitePersistence(data_dir=store_dir)

    first = build_world()
    first.payments.receive_payment(payer="c", payee="us", amount_cents=12_345,
                                   currency="CNY", reference="INV-9")
    first.graph.upsert_verified("invoice", "INV-9", {"amount_cents": 12_345},
                                source="test")
    persistence.save(first)
    persistence.close()

    # A fresh bundle + a fresh persistence handle over the same file.
    reopened = SqlitePersistence(data_dir=store_dir)
    second = build_world()
    assert second.payments.balance("us") == 0        # before restore
    found = reopened.restore(second)
    reopened.close()

    assert found is True
    assert second.payments.balance("us") == 12_345
    assert second.world.ledger                       # the payment record is back
    assert second.graph.get("invoice:INV-9") is not None


def test_restore_on_an_empty_store_is_a_noop(tmp_path):
    persistence = SqlitePersistence(data_dir=str(tmp_path))
    bundle = build_world()
    assert persistence.restore(bundle) is False       # nothing saved yet
    persistence.close()


def test_persistence_is_disabled_without_a_data_dir(monkeypatch):
    monkeypatch.delenv("HELIOS_DATA_DIR", raising=False)
    persistence = SqlitePersistence()
    assert persistence.enabled is False
    assert persistence.path is None
    bundle = build_world()
    persistence.save(bundle)        # must not raise
    assert persistence.restore(bundle) is False


def test_outbox_survives_a_restart(tmp_path):
    persistence = SqlitePersistence(data_dir=str(tmp_path))
    first = build_world(tenant="acme")
    first.email.send(to="ap@acme.example", subject="Hi", body="There")
    assert len(first.world.outbox) == 1
    persistence.save(first)
    persistence.close()

    reopened = SqlitePersistence(data_dir=str(tmp_path))
    second = build_world(tenant="acme")
    reopened.restore(second)
    reopened.close()
    assert len(second.world.outbox) == 1
    assert second.world.outbox[0]["to"] == "ap@acme.example"

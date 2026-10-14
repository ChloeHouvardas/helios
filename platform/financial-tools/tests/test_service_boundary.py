"""Phase 0: the service boundary's guarantees, asserted individually.

The architectural claims under test (specs/financial-tools-service.md):
  §3 — signing material never crosses to a client
  §4.1 — an app cannot grant itself permission to move money
  §4.3 — an unreachable service fails closed
  §4.4 — no settlement without a validated authorization, across a network
"""
import json
from urllib.request import Request, urlopen

import pytest

from fin_tools import (HttpToolClient, InProcessToolClient, ServiceUnavailable,
                       ToolServer, build_world, free_port)


@pytest.fixture
def server():
    bundle = build_world()
    srv = ToolServer(bundle, port=free_port(),
                     secret=b"server-only-secret").start()
    yield srv, bundle
    srv.stop()


def client(srv):
    return HttpToolClient(srv.base_url, timeout_seconds=5.0)


# -- same interface either way --------------------------------------------

def test_both_clients_expose_the_same_surface(server):
    srv, bundle = server
    for c in (HttpToolClient(srv.base_url), InProcessToolClient(bundle)):
        cat = c.catalog()
        assert cat["tools"], "catalog must not be empty"
        names = {t["name"] for t in cat["tools"]}
        assert "payments.send" in names


def test_catalog_identical_across_transports(server):
    srv, bundle = server
    remote = {t["name"] for t in client(srv).catalog()["tools"]}
    local = {t["name"] for t in InProcessToolClient(bundle).catalog()["tools"]}
    assert remote == local


# -- §3: trust material stays server-side ---------------------------------

def test_client_cannot_obtain_the_signing_secret(server):
    srv, bundle = server
    assert bundle.intents.secret == b"server-only-secret"
    # No endpoint returns it, and no tool exposes it.
    with urlopen(Request(f"{srv.base_url}/tools"), timeout=5) as r:
        payload = r.read().decode()
    assert "server-only-secret" not in payload
    assert not any("secret" in t["name"] for t in json.loads(payload)["tools"])


# -- §4.1 / §4.4: cannot self-authorize, across a network ------------------

def test_payment_without_authorization_is_rejected_over_http(server):
    srv, _ = server
    out = client(srv).call("payments.send", payer="ops", payee="supplier",
                           amount_cents=5000, currency="CNY")
    assert out["ok"] is True           # the call succeeded
    assert out["result"]["state"] == "rejected"   # the payment did not


def test_client_cannot_forge_an_authorization(server):
    """The client has no secret, so any code it invents must fail."""
    srv, _ = server
    c = client(srv)
    forged = "0" * 64
    out = c.call("payments.send", payer="ops", payee="supplier",
                 amount_cents=5000, currency="CNY", auth_code=forged)
    assert out["result"]["state"] == "rejected"
    assert c.call("payments.balance", account="supplier")["result"] == \
        {"balance_cents": 0}


def test_agent_cannot_capture_or_confirm_its_own_intent(server):
    """The human confirmation is not a tool the agent can call.

    If an agent could capture and confirm its own intent, it could mint its own
    authorization and §4.1 would be false. So neither operation is published.
    """
    srv, _ = server
    c = client(srv)
    names = {t["name"] for t in c.catalog()["tools"]}
    assert not any(n.startswith("intents.") for n in names)
    assert not any("confirm" in n for n in names)

    # And attempting it fails closed rather than silently doing nothing.
    out = c.call("intents.capture", principal="agent", amount_cents=5000,
                 currency="CNY", payee="supplier", purpose="self-granted")
    assert out["ok"] is False and out["rejected"] is True


def test_owner_confirmation_path_stays_server_side(server):
    """The owner acts through the service, not through the agent's client."""
    srv, bundle = server
    intent = bundle.intents.capture_intent("chloe", 5000, "CNY", "supplier", "p")
    code = bundle.intents.confirm(intent.intent_id, by="chloe").code
    out = client(srv).call("payments.send", payer="ops", payee="supplier",
                           amount_cents=5000, currency="CNY", auth_code=code)
    assert out["result"]["state"] == "settled"
    assert out["result"]["intent_id"] == intent.intent_id


def test_tampered_amount_rejected_over_http(server):
    srv, bundle = server
    c = client(srv)
    # Issue a real code server-side (as the owner would), then tamper on the wire.
    intent = bundle.intents.capture_intent("chloe", 5000, "CNY", "supplier", "p")
    code = bundle.intents.confirm(intent.intent_id, by="chloe").code
    out = c.call("payments.send", payer="ops", payee="supplier",
                 amount_cents=5001, currency="CNY", auth_code=code)
    assert out["result"]["state"] == "rejected"
    assert "amount differs" in out["result"]["reject_reason"]


def test_authorization_is_single_use_over_http(server):
    srv, bundle = server
    c = client(srv)
    intent = bundle.intents.capture_intent("chloe", 5000, "CNY", "supplier", "p")
    code = bundle.intents.confirm(intent.intent_id, by="chloe").code
    first = c.call("payments.send", payer="ops", payee="supplier",
                   amount_cents=5000, currency="CNY", auth_code=code)
    assert first["result"]["state"] == "settled"
    replay = c.call("payments.send", payer="ops", payee="supplier",
                    amount_cents=5000, currency="CNY", auth_code=code,
                    idempotency_key="different-key")
    assert replay["result"]["state"] == "rejected"
    assert c.call("payments.balance", account="supplier")["result"] == \
        {"balance_cents": 5000}   # paid once, not twice


# -- §4.3: fail closed ------------------------------------------------------

def test_unreachable_service_raises_rather_than_proceeding():
    dead = HttpToolClient(f"http://127.0.0.1:{free_port()}", timeout_seconds=1.0)
    with pytest.raises(ServiceUnavailable):
        dead.call("payments.send", payer="ops", payee="s", amount_cents=1,
                  currency="CNY")


def test_unreachable_service_raises_on_catalog_too():
    dead = HttpToolClient(f"http://127.0.0.1:{free_port()}", timeout_seconds=1.0)
    with pytest.raises(ServiceUnavailable):
        dead.catalog()


# -- envelope: 'tool said no' is not 'wire broke' ---------------------------

def test_rejection_is_reported_as_ok_call_with_rejected_result(server):
    srv, _ = server
    out = client(srv).call("payments.send", payer="ops", payee="s",
                           amount_cents=1, currency="CNY")
    assert out["ok"] is True and "rejected" not in out


def test_validation_failure_is_reported_in_envelope(server):
    srv, _ = server
    out = client(srv).call("payments.send", payer="ops", payee="s",
                           amount_cents=-1, currency="CNY")
    assert out["ok"] is False and out["rejected"] is True


def test_unknown_tool_does_not_crash_the_server(server):
    srv, _ = server
    c = client(srv)
    out = c.call("nope.does_not_exist")
    assert out["ok"] is False
    assert c.catalog()["tools"]          # still serving


def test_malformed_request_is_a_400(server):
    srv, _ = server
    req = Request(f"{srv.base_url}/call", data=b"{not json", method="POST")
    with pytest.raises(Exception):
        with urlopen(req, timeout=5) as r:
            r.read()

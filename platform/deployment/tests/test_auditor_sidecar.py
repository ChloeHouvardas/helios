"""Offline tests for the audit sidecar's manifest-driven policy (Task 5).

The sidecar is the platform's last gate before a real side effect, so its
refusals are the point of these tests: an over-cap payment, a forbidden value,
a missing required argument and a duplicate must all be denied, and only a
clean, in-policy, non-duplicate action may be forwarded to core.

The module reads its config (token, manifest path, data dir) at import time, so
each test imports a fresh copy with env pointed at a temp manifest and a temp
log dir. The audit_agent hash-chained log writer is exercised for real -- the
sidecar must reuse it, not reimplement the chain.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

# A minimal but valid audit manifest exercising every fact-free control:
# required argument, forbidden value, constant ceiling, and a fact-bound rule
# that the sidecar must SKIP (delegating it to the in-app gate) rather than
# fail closed on.
MANIFEST_YAML = """
manifest_version: "1"
app_id: test-app
name: Test App Audit Agent
tools:
  - name: payments
    side_effect: true
    input_schema:
      type: object
      properties:
        amount: {type: number}
        currency: {type: string}
        payee: {type: string}
tasks:
  test.pay:
    allowed_tools: [payments]
    tool_overrides:
      payments:
        max_calls: 3
rules:
  - id: app.amount_cap
    tools: [payments]
    limits:
      - argument: amount
        max: 5000
        on_exceed: deny
  - id: app.no_test_payee
    tools: [payments]
    forbidden_values:
      payee: ["BLOCKED-PAYEE"]
  - id: app.needs_payee
    tools: [payments]
    required_arguments: [payee]
  - id: app.needs_fact
    tools: [payments]
    deny_if_fact_true: [payment_details_changed]
settings:
  allow_unsigned_grants: false
"""


def _load_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                  manifest_text: str = MANIFEST_YAML):
    """Import images/auditor/server.py with env pointed at temp paths."""
    manifest = tmp_path / "audit.yaml"
    manifest.write_text(manifest_text)
    data = tmp_path / "data"
    data.mkdir()

    monkeypatch.setenv("EXPECTED_ACTION_TOKEN", "tok-secret")
    monkeypatch.setenv("HELIOS_EXECUTION_SECRET", "exec-secret")
    monkeypatch.setenv("HELIOS_CORE_URL", "http://core.invalid:9000")
    monkeypatch.setenv("ALLOWED_TOOLS", "payments,email")
    monkeypatch.setenv("HELIOS_APP_NAME", "test-app")
    monkeypatch.setenv("HELIOS_TENANT", "t1")
    monkeypatch.setenv("HELIOS_AUDIT_MANIFEST_PATH", str(manifest))
    monkeypatch.setenv("HELIOS_DATA_DIR", str(data))

    path = Path(__file__).resolve().parents[1] / "images" / "auditor" / "server.py"
    spec = importlib.util.spec_from_file_location("auditor_server", path)
    mod = importlib.util.module_from_spec(spec)
    # Must be registered BEFORE exec_module: the module defines a @dataclass, and
    # dataclasses._is_type resolves annotations through
    # sys.modules[cls.__module__].__dict__. An unregistered module makes that
    # lookup return None and every field annotation raises AttributeError.
    import sys
    sys.modules["auditor_server"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_manifest_policy_is_extracted(tmp_path, monkeypatch):
    mod = _load_sidecar(tmp_path, monkeypatch)
    pol = mod.POLICY
    assert pol.loaded is True
    assert "payments" in pol.tool_names
    # constant ceiling parsed as Decimal, not float (decision B.6)
    assert pol.ceilings["payments"]["amount"] == 5000
    from decimal import Decimal
    assert isinstance(pol.ceilings["payments"]["amount"], Decimal)
    # required + forbidden extracted
    assert "payee" in pol.required_arguments["payments"]
    assert pol.forbidden_values["payments"]["payee"] == ["BLOCKED-PAYEE"]
    # max_calls from tool_overrides
    assert pol.max_calls["payments"] == 3
    # the fact-bound rule is delegated, not enforced here
    assert "app.needs_fact" in pol.skipped_fact_rules


def test_clean_action_passes_evaluate(tmp_path, monkeypatch):
    mod = _load_sidecar(tmp_path, monkeypatch)
    findings = mod.evaluate("payments", {"amount": "100.00", "payee": "ACME", "currency": "USD"})
    assert findings == []


def test_over_cap_denied(tmp_path, monkeypatch):
    mod = _load_sidecar(tmp_path, monkeypatch)
    findings = mod.evaluate("payments", {"amount": "9999", "payee": "ACME"})
    assert any(f.rule_id == "sidecar.exceeds_limit" for f in findings)
    assert any("9999" in f.message and "5000" in f.message for f in findings)


def test_cap_boundary_is_inclusive(tmp_path, monkeypatch):
    """Exactly at the cap is allowed; one cent over is not."""
    mod = _load_sidecar(tmp_path, monkeypatch)
    assert mod.evaluate("payments", {"amount": "5000", "payee": "ACME"}) == []
    assert mod.evaluate("payments", {"amount": "5000.01", "payee": "ACME"})


def test_forbidden_value_denied(tmp_path, monkeypatch):
    mod = _load_sidecar(tmp_path, monkeypatch)
    findings = mod.evaluate("payments", {"amount": "10", "payee": "BLOCKED-PAYEE"})
    assert any(f.rule_id == "sidecar.forbidden_value" for f in findings)


def test_missing_required_argument_denied(tmp_path, monkeypatch):
    mod = _load_sidecar(tmp_path, monkeypatch)
    findings = mod.evaluate("payments", {"amount": "10"})  # no payee
    assert any(f.rule_id == "sidecar.required_argument" for f in findings)


def test_unparseable_amount_fails_closed(tmp_path, monkeypatch):
    """An amount that is not a number must not be read as zero."""
    mod = _load_sidecar(tmp_path, monkeypatch)
    findings = mod.evaluate("payments", {"amount": "lots", "payee": "ACME"})
    assert any(f.rule_id == "sidecar.unparseable_amount" for f in findings)


def test_all_problems_reported_at_once(tmp_path, monkeypatch):
    """Over cap AND forbidden AND missing arg: every problem, one pass."""
    mod = _load_sidecar(tmp_path, monkeypatch)
    findings = mod.evaluate("payments", {"amount": "99999"})  # over cap + no payee
    rule_ids = {f.rule_id for f in findings}
    assert "sidecar.exceeds_limit" in rule_ids
    assert "sidecar.required_argument" in rule_ids


def test_default_payment_cap_when_manifest_silent(tmp_path, monkeypatch):
    """A manifest with no cap still gets the platform default on `amount`."""
    mod = _load_sidecar(tmp_path, monkeypatch, manifest_text="""
manifest_version: "1"
app_id: t
name: T
tools:
  - name: payments
    side_effect: true
    input_schema: {type: object, properties: {amount: {type: number}}}
tasks:
  t.pay: {allowed_tools: [payments]}
""")
    # default cap is 10000
    assert mod.evaluate("payments", {"amount": "5000"}) == []
    assert mod.evaluate("payments", {"amount": "99999"})


def test_sidecar_chain_log_verifies(tmp_path, monkeypatch):
    """Decisions land in a hash-chained log written by audit_agent's own writer."""
    mod = _load_sidecar(tmp_path, monkeypatch)
    # A denial writes to the chain.
    result = mod._deny("payments", {"amount": "9"}, [], reason="test denial")
    assert result["status"] == "denied"
    ok, problems = mod.LOG.verify_chain()
    assert ok, problems
    assert mod.LOG.sequence >= 1


def test_health_reports_delegated_rules(tmp_path, monkeypatch):
    """/health must disclose what the sidecar does NOT check."""
    mod = _load_sidecar(tmp_path, monkeypatch)
    h = mod.health()
    assert h["manifest"]["loaded"] is True
    assert "app.needs_fact" in h["manifest"]["delegated_to_in_app_gate"]
    assert h["audit_chain"]["intact"] is True


def test_execute_rejects_bad_token(tmp_path, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    mod = _load_sidecar(tmp_path, monkeypatch)

    class FakeReq:
        async def json(self):
            return {"action": "payments", "payload": {"amount": "1", "payee": "X"}}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.execute(FakeReq(), authorization="Bearer wrong"))
    assert exc.value.status_code == 401

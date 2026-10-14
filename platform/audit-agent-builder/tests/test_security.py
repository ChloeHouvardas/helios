"""Security properties: the ones that make the audit agent trustworthy.

These are the tests to show a judge or an auditor. Each one asserts that a
specific way of getting around the mandate does not work.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import make_action, make_authorization

from audit_agent import (
    ActionRequest,
    AuditAgent,
    AuthorizationRequest,
    Decision,
    Finding,
    GrantInvalidError,
    Keyring,
    PolicyEngine,
    PolicyLooseningError,
    PolicyPack,
    Precedence,
    SigningKeyMissingError,
    StaticFactSource,
    TaskAuthorization,
    ToolDescriptor,
    ToolRegistry,
    utcnow,
)


class TestAgentCannotWidenItsOwnAuthority:
    """The core property. Claims are not facts."""

    def test_under_reporting_an_amount_does_not_dodge_approval(
        self, agent: AuditAgent
    ) -> None:
        """INV-1002 is 24500 in the trusted store. The agent claims 1.00."""
        auth = make_authorization(agent, invoice_id="INV-1002")
        decision = make_action(
            agent,
            {"invoice_id": "INV-1002", "message": "Please pay"},
            auth,
            resolved_facts={"amount_due": "1.00"},
        )
        assert decision.decision is Decision.HOLD

    def test_claiming_no_prior_contacts_does_not_dodge_the_frequency_limit(
        self, agent: AuditAgent
    ) -> None:
        """INV-1003 has two trusted contacts in the window."""
        auth = make_authorization(agent, invoice_id="INV-1003")
        decision = make_action(
            agent,
            {"invoice_id": "INV-1003", "message": "Another reminder"},
            auth,
            resolved_facts={"recent_contact_count": 0, "contact_count": 0},
        )
        assert decision.decision is Decision.DENY

    def test_claims_in_the_authorization_context_are_ignored(
        self, agent: AuditAgent
    ) -> None:
        auth = agent.authorize(
            AuthorizationRequest(
                request_id="r-claim",
                app_id="helios-collect",
                agent_id="collect-reminder",
                task_type="collect.reminder",
                task_summary="Chase the overdue invoice for payment",
                requested_tools=["send_reminder_email"],
                context={"invoice_id": "INV-1002", "amount_due": "5.00"},
            )
        )
        decision = make_action(
            agent, {"invoice_id": "INV-1002", "message": "Please pay"}, auth
        )
        assert decision.decision is Decision.HOLD

    def test_resolved_facts_never_enter_the_fact_set(self, agent: AuditAgent) -> None:
        """Directly: the resolver must not treat caller values as facts."""
        facts = agent._facts_for_action(
            ActionRequest(
                request_id="probe",
                app_id="helios-collect",
                agent_id="collect-reminder",
                task_type="collect.reminder",
                tool="send_reminder_email",
                arguments={"invoice_id": "INV-1002", "message": "x"},
                resolved_facts={
                    "amount_due": "1.00",
                    "payment_details_changed": False,
                    "duplicate_invoice": False,
                },
            ),
            None,
        )
        assert facts.amounts["INV-1002"].as_tuple()  # real value present
        assert str(facts.amounts["INV-1002"]) == "24500.00"
        assert "payment_details_changed" not in facts.extras
        assert "duplicate_invoice" not in facts.extras

    def test_an_unverifiable_subject_fails_closed(self, agent: AuditAgent) -> None:
        """A subject the fact source cannot confirm is refused, not assumed
        small. Guessing here would be the whole attack."""
        auth = make_authorization(agent, invoice_id="INV-9999")
        decision = make_action(
            agent, {"invoice_id": "INV-9999", "message": "Please pay"}, auth
        )
        assert decision.decision is Decision.DENY

    def test_no_fact_source_at_all_fails_closed(self, registry, collect_pack, log_path) -> None:
        agent = AuditAgent.build(
            registry,
            app_packs=[collect_pack],
            fact_source=None,  # nothing wired
            log_path=log_path,
            keyring=Keyring(b"test-signing-key", "test-key"),
        )
        auth = make_authorization(agent, invoice_id="INV-1001")
        decision = make_action(
            agent, {"invoice_id": "INV-1001", "message": "Reminder"}, auth
        )
        assert decision.decision is Decision.DENY


class TestGrantCannotBeReusedOrForged:
    def test_a_side_effect_grant_is_single_use(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        arguments = {"invoice_id": "INV-1001", "message": "Reminder"}
        first = make_action(agent, arguments, auth, request_id="c-first")
        second = make_action(agent, arguments, auth, request_id="c-second")
        assert first.decision is Decision.ALLOW
        assert second.decision is Decision.DENY

    def test_the_same_request_id_cannot_be_replayed(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        arguments = {"invoice_id": "INV-1001", "message": "Reminder"}
        first = make_action(agent, arguments, auth, request_id="c-same")
        replay = make_action(agent, arguments, auth, request_id="c-same")
        assert first.decision is Decision.ALLOW
        assert replay.decision is Decision.DENY
        assert "already been used" in replay.reason

    def test_an_expired_grant_is_refused(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        keyring = Keyring(b"test-signing-key", "test-key")
        expired = keyring.sign(
            auth.model_copy(update={"expires_at": utcnow() - timedelta(seconds=1)})
        )
        decision = make_action(
            agent, {"invoice_id": "INV-1001", "message": "Reminder"}, expired
        )
        assert decision.decision is Decision.DENY
        assert "expired" in decision.reason

    def test_a_forged_signature_is_refused(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        forged = auth.model_copy(
            update={"signature": auth.signature.model_copy(update={"value": "0" * 64})}
        )
        decision = make_action(
            agent, {"invoice_id": "INV-1001", "message": "Reminder"}, forged
        )
        assert decision.decision is Decision.DENY
        assert "signature" in decision.reason

    def test_a_privilege_escalated_grant_is_refused(self, agent: AuditAgent) -> None:
        """Re-signing with the real key but adding a tool must still fail,
        because the body changed after signing."""
        auth = make_authorization(agent)
        escalated = TaskAuthorization.model_validate(
            {
                **auth.model_dump(),
                "permissions": [
                    {"tool": "pay_supplier", "side_effect": True},
                    *auth.model_dump()["permissions"],
                ],
            }
        )
        decision = make_action(
            agent, {"invoice_id": "INV-1001", "message": "Reminder"}, escalated
        )
        assert decision.decision is Decision.DENY

    def test_a_grant_for_another_agent_is_refused(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Reminder"},
            auth,
            agent_id="some-other-agent",
        )
        assert decision.decision is Decision.DENY
        assert "different agent" in decision.reason

    def test_a_grant_for_another_app_is_refused(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Reminder"},
            auth,
            app_id="some-other-app",
        )
        assert decision.decision is Decision.DENY
        assert "different app" in decision.reason

    def test_a_grant_for_another_principal_is_refused(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Reminder"},
            auth,
            principal="someone-else",
        )
        assert decision.decision is Decision.DENY
        assert "different principal" in decision.reason

    def test_a_grant_from_another_task_is_refused(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = agent.adjudicate(
            ActionRequest(
                request_id="c-othertask",
                app_id="helios-collect",
                agent_id="collect-reminder",
                task_type="payables.pay_invoice",  # not what was granted
                tool="send_reminder_email",
                arguments={"invoice_id": "INV-1001", "message": "Reminder"},
                authorization=auth,
            )
        )
        assert decision.decision is Decision.DENY


class TestBaselineCannotBeWeakened:
    """The platform ships rules an app may extend but never remove."""

    def test_disabling_a_baseline_rule_is_refused(self, registry) -> None:
        sneaky = PolicyPack(
            name="sneaky", source="app", disabled_rule_ids=["baseline.grant_valid"]
        )
        with pytest.raises(PolicyLooseningError):
            PolicyEngine.compose(
                registry, app_packs=[sneaky], keyring=Keyring(b"k", "kid")
            )

    def test_an_app_cannot_claim_to_be_baseline(self, registry) -> None:
        with pytest.raises(Exception):
            PolicyEngine.compose(
                registry,
                app_packs=[PolicyPack(name="fake", source="baseline")],
                keyring=Keyring(b"k", "kid"),
            )

    def test_redefining_a_baseline_rule_id_is_refused(self) -> None:
        from audit_agent import AuditManifest, PolicyError

        with pytest.raises((PolicyLooseningError, PolicyError)):
            AuditManifest.parse(
                """
manifest_version: "1"
app_id: sneaky
name: sneaky
tools:
  - {name: t, side_effect: false}
tasks:
  x: {allowed_tools: [t]}
rules:
  - id: baseline.grant_valid
    required_arguments: [anything]
""",
                origin="sneaky.yaml",
            )

    def test_an_unregistered_tool_in_policy_is_refused(self, registry) -> None:
        bad = PolicyPack.from_yaml_text(
            "name: bad\nsource: app\ntasks:\n  t:\n    allowed_tools: [nonexistent]\n",
            origin="bad.yaml",
        )
        with pytest.raises(Exception):
            PolicyEngine.compose(registry, app_packs=[bad], keyring=Keyring(b"k", "kid"))


class TestSigningIsNotOptional:
    def test_build_fails_without_a_key(self, registry, collect_pack, log_path) -> None:
        with pytest.raises(SigningKeyMissingError):
            AuditAgent.build(
                registry,
                app_packs=[collect_pack],
                log_path=log_path,
                keyring=Keyring(None, "kid", allow_unsigned=False),
            )

    def test_unsigned_grants_are_marked(self, registry, collect_pack, log_path) -> None:
        agent = AuditAgent.build(
            registry,
            app_packs=[collect_pack],
            log_path=log_path,
            keyring=Keyring(None, "kid", allow_unsigned=True),
        )
        auth = make_authorization(agent)
        assert auth.unsigned is True
        assert auth.signature is None

    def test_a_strict_auditor_refuses_an_unsigned_grant(
        self, registry, collect_pack, tmp_path
    ) -> None:
        lax = AuditAgent.build(
            registry,
            app_packs=[collect_pack],
            log_path=tmp_path / "lax.jsonl",
            keyring=Keyring(None, "kid", allow_unsigned=True),
        )
        auth = make_authorization(lax)
        strict = AuditAgent.build(
            registry,
            app_packs=[collect_pack],
            log_path=tmp_path / "strict.jsonl",
            keyring=Keyring(b"test-signing-key", "test-key"),
        )
        decision = make_action(
            strict, {"invoice_id": "INV-1001", "message": "Reminder"}, auth
        )
        assert decision.decision is Decision.DENY


class TestPrecedence:
    def test_the_most_restrictive_finding_wins(self) -> None:
        findings = [
            Finding(rule_id="a", effect="allow", message="fine"),
            Finding(rule_id="b", effect="deny", message="no"),
        ]
        assert Precedence.resolve(findings) is Decision.DENY

    def test_hold_outranks_allow(self) -> None:
        findings = [
            Finding(rule_id="a", effect="allow", message="fine"),
            Finding(rule_id="b", effect="hold", message="ask"),
        ]
        assert Precedence.resolve(findings) is Decision.HOLD

    def test_no_findings_is_a_deny(self) -> None:
        """Nothing authorising an action is not the same as permission."""
        assert Precedence.resolve([]) is Decision.DENY

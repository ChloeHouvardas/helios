"""Adjudication: whether one concrete call may proceed.

Covers the mandate from `specs/build1.md` — the same three outcomes a Build 1
reviewer will look for: a normal reminder is approved, an out-of-mandate
request is held for the owner, and something outside policy is rejected with
a reason the agent can act on.
"""

from __future__ import annotations

from conftest import make_action, make_authorization

from audit_agent import AuditAgent, Decision


class TestApprove:
    def test_normal_reminder_is_allowed(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent, {"invoice_id": "INV-1001", "message": "Friendly reminder"}, auth
        )
        assert decision.decision is Decision.ALLOW
        assert decision.allowed is True

    def test_extension_within_the_cap_is_allowed(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Reminder", "extension_days": 7},
            auth,
        )
        assert decision.decision is Decision.ALLOW

    def test_read_only_call_needs_no_grant(self, agent: AuditAgent) -> None:
        """Read-only tools run directly, per specs/decisions.md."""
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001"},
            None,
            tool="lookup_invoice",
        )
        assert decision.decision is Decision.ALLOW

    def test_allowed_decision_explains_itself(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent, {"invoice_id": "INV-1001", "message": "Reminder"}, auth
        )
        assert decision.reason
        assert any(f.rule_id == "baseline.within_grant" for f in decision.findings)


class TestHold:
    def test_long_extension_is_held(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Take 60 days", "extension_days": 60},
            auth,
        )
        assert decision.decision is Decision.HOLD
        assert decision.hold_id

    def test_large_invoice_is_held(self, agent: AuditAgent) -> None:
        """INV-1002 is 24500 in the trusted store, above the 10000 threshold."""
        auth = make_authorization(agent, invoice_id="INV-1002")
        decision = make_action(
            agent, {"invoice_id": "INV-1002", "message": "Please pay"}, auth
        )
        assert decision.decision is Decision.HOLD

    def test_hold_carries_a_question_for_the_owner(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Take 60 days", "extension_days": 60},
            auth,
        )
        assert decision.owner_question
        assert "send_reminder_email" in decision.owner_question
        assert "extension_days=60" in decision.owner_question

    def test_hold_is_listed_and_resolvable(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Take 60 days", "extension_days": 60},
            auth,
        )
        assert len(agent.pending_holds()) == 1
        record = agent.resolve_hold(
            decision.hold_id, approve=True, decided_by="owner@helios-collect"
        )
        assert record.status == "approved"
        assert record.decided_by == "owner@helios-collect"
        assert agent.pending_holds() == []

    def test_a_hold_can_be_rejected(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Take 60 days", "extension_days": 60},
            auth,
        )
        record = agent.resolve_hold(
            decision.hold_id, approve=False, decided_by="owner@helios-collect"
        )
        assert record.status == "rejected"

    def test_a_hold_keeps_the_exact_action(self, agent: AuditAgent) -> None:
        """An approval applies to the action that was held, not to a class of
        actions. The record must still carry the original arguments."""
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Take 60 days", "extension_days": 60},
            auth,
        )
        record = agent.get_hold(decision.hold_id)
        assert record.arguments["extension_days"] == 60
        assert record.tool == "send_reminder_email"

    def test_a_hold_cannot_be_decided_twice(self, agent: AuditAgent) -> None:
        import pytest

        from audit_agent import GrantInvalidError

        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Take 60 days", "extension_days": 60},
            auth,
        )
        agent.resolve_hold(decision.hold_id, approve=True, decided_by="owner")
        with pytest.raises(GrantInvalidError):
            agent.resolve_hold(decision.hold_id, approve=False, decided_by="owner")


class TestReject:
    def test_contact_frequency_is_denied(self, agent: AuditAgent) -> None:
        """INV-1003 already has two contacts inside the 7-day window."""
        auth = make_authorization(agent, invoice_id="INV-1003")
        decision = make_action(
            agent, {"invoice_id": "INV-1003", "message": "Third time this week"}, auth
        )
        assert decision.decision is Decision.DENY
        assert "twice" in decision.reason

    def test_malformed_arguments_are_denied(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Reminder", "extension_days": -5},
            auth,
        )
        assert decision.decision is Decision.DENY
        assert "schema validation" in decision.reason

    def test_unregistered_tool_is_denied(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"supplier_id": "SUP-1", "amount": "100.00"},
            auth,
            tool="pay_supplier",
        )
        assert decision.decision is Decision.DENY

    def test_side_effect_without_a_grant_is_denied(self, agent: AuditAgent) -> None:
        decision = make_action(
            agent, {"invoice_id": "INV-1001", "message": "Reminder"}, None
        )
        assert decision.decision is Decision.DENY
        assert "no authorization" in decision.reason or "not granted" in decision.reason

    def test_rejection_carries_an_actionable_reason(self, agent: AuditAgent) -> None:
        """The agent has to be able to adjust, so the reason cannot be empty."""
        auth = make_authorization(agent)
        decision = make_action(
            agent,
            {"invoice_id": "INV-1001", "message": "Reminder", "extension_days": -5},
            auth,
        )
        assert decision.reason
        assert decision.findings

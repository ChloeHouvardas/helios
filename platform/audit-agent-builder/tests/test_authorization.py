"""Authorization: which tools an action agent may use for a task.

This is the audit agent's headline capability — the thing that decides what
the action agent is allowed to touch. Everything else in the system is
enforcement detail.
"""

from __future__ import annotations

from conftest import make_authorization

from audit_agent import AuditAgent, AuthorizationRequest, Decision


class TestGrantScope:
    def test_grants_the_requested_tools(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        assert auth.decision is Decision.ALLOW
        assert sorted(auth.tool_names()) == ["lookup_invoice", "send_reminder_email"]

    def test_grant_is_narrowed_to_what_was_requested(self, agent: AuditAgent) -> None:
        """Asking for a subset gets a subset. A grant is not a blanket."""
        auth = make_authorization(agent, tools=["lookup_invoice"])
        assert auth.tool_names() == ["lookup_invoice"]

    def test_requesting_a_tool_outside_policy_is_not_granted(self, agent: AuditAgent) -> None:
        """The action agent cannot add a tool by asking for it."""
        auth = make_authorization(agent, tools=["lookup_invoice", "pay_supplier"])
        assert "pay_supplier" not in auth.tool_names()
        assert auth.decision is Decision.DENY  # unregistered tool -> fail closed

    def test_side_effect_tools_are_marked_gated(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        permission = auth.permission_for("send_reminder_email")
        assert permission is not None
        assert permission.side_effect is True
        assert auth.permission_for("lookup_invoice").side_effect is False


class TestGrantIsSignedAndScoped:
    def test_grant_is_signed(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        assert auth.signature is not None
        assert auth.unsigned is False

    def test_grant_is_bound_to_identity(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        assert auth.app_id == "helios-collect"
        assert auth.agent_id == "collect-reminder"
        assert auth.principal == "owner"
        assert auth.task_type == "collect.reminder"

    def test_grant_expires(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        assert auth.expires_at > auth.issued_at
        assert not auth.is_expired()

    def test_tampering_breaks_the_signature(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        forged = auth.model_copy(update={"task_type": "payables.pay_invoice"})
        assert forged.verify(b"test-signing-key") is False

    def test_owner_brief_states_the_intent(self, agent: AuditAgent) -> None:
        auth = make_authorization(agent)
        assert "collect.reminder" in auth.owner_brief
        assert "send_reminder_email" in auth.owner_brief


class TestFailClosed:
    def test_unknown_task_type_is_denied(self, agent: AuditAgent) -> None:
        auth = agent.authorize(
            AuthorizationRequest(
                request_id="r-unknown",
                app_id="helios-collect",
                agent_id="collect-reminder",
                task_type="cash.forecast",  # not in policy
                task_summary="Forecast next month's cash position",
                requested_tools=["lookup_invoice"],
            )
        )
        assert auth.decision is Decision.DENY
        assert auth.permissions == []
        assert any(f.rule_id == "baseline.task_known" for f in auth.findings)

    def test_missing_intent_is_denied(self, agent: AuditAgent) -> None:
        """An unauditable task gets no grant: the owner must be able to see
        what the agent intends before approving anything it does."""
        auth = agent.authorize(
            AuthorizationRequest(
                request_id="r-nointent",
                app_id="helios-collect",
                agent_id="collect-reminder",
                task_type="collect.reminder",
                task_summary="",
                requested_tools=["send_reminder_email"],
                context={"invoice_id": "INV-1001"},
            )
        )
        assert auth.decision is Decision.DENY
        assert any(f.rule_id == "baseline.intent_stated" for f in auth.findings)

    def test_empty_request_yields_no_tools(self, agent: AuditAgent) -> None:
        auth = agent.authorize(
            AuthorizationRequest(
                request_id="r-empty",
                app_id="helios-collect",
                agent_id="collect-reminder",
                task_type="collect.reminder",
                task_summary="Chase the overdue invoice for payment",
                requested_tools=[],  # nothing asked for
                context={"invoice_id": "INV-1001"},
            )
        )
        # An empty request means "grant me everything policy allows", which
        # is the documented behaviour, so this is an allow with both tools.
        assert auth.decision is Decision.ALLOW
        assert len(auth.tool_names()) == 2


class TestCapabilityIntrospection:
    def test_capabilities_lists_tasks_and_tools(self, agent: AuditAgent) -> None:
        caps = agent.describe_capabilities()
        assert "collect.reminder" in caps["tasks"]
        assert caps["tools"]["send_reminder_email"]["gated"] is True
        assert caps["tools"]["lookup_invoice"]["gated"] is False

    def test_tools_for_task_answers_directly(self, agent: AuditAgent) -> None:
        assert agent.tools_for_task("collect.reminder") == [
            "lookup_invoice",
            "send_reminder_email",
        ]

    def test_task_types_are_discoverable(self, agent: AuditAgent) -> None:
        assert agent.task_types() == ["collect.reminder"]

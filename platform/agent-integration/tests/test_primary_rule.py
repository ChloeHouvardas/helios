"""The verdict names the rule that DECIDED, not merely the most severe finding."""
from __future__ import annotations

from audit_agent.contracts import ActionDecision, Decision, Finding, Severity

from agent_integration.bridge import AuditBridge


def _finding(rule_id: str, effect: str, severity: Severity) -> Finding:
    return Finding(rule_id=rule_id, effect=effect, severity=severity,
                   message=rule_id, source="app", details={})


def _decision(decision: Decision, findings: list[Finding]) -> ActionDecision:
    return ActionDecision(decision=decision, findings=findings, reason="x",
                          request_id="r1", app_id="a", agent_id="g",
                          tool="pay_bill", task_type="t.pay")


def test_a_deny_names_the_denying_rule_even_when_a_critical_hold_also_fired():
    decision = _decision(Decision.DENY, [
        _finding("app.always_hold", "hold", Severity.CRITICAL),
        _finding("app.over_facility", "deny", Severity.WARN),
    ])
    assert AuditBridge._primary_rule(decision) == "app.over_facility"


def test_a_hold_names_the_most_severe_holding_rule():
    decision = _decision(Decision.HOLD, [
        _finding("app.large_payment", "hold", Severity.WARN),
        _finding("app.payroll_at_risk", "require_approval", Severity.CRITICAL),
        _finding("baseline.ok", "allow", Severity.INFO),
    ])
    assert AuditBridge._primary_rule(decision) == "app.payroll_at_risk"


def test_no_findings_falls_back_to_the_decision_name():
    assert AuditBridge._primary_rule(_decision(Decision.DENY, [])) == "deny"

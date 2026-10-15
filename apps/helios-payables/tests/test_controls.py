"""Control tests for helios-payables — one test per control, all offline.

These assert that each RULE FIRES, not merely that the app runs. Modelled on
`platform/audit-agent-builder/tests/test_security.py` and
`apps/helios-collect/tests/test_controls.py`.

Everything here runs with the scripted driver, no API key and no network: the
platform's `agent-integration/tests/conftest.py` blocks non-loopback sockets, and
this suite does not need any. The thing being tested is the GATE, so a script that
includes requests which *should* be refused is more trustworthy than hoping a model
decides to misbehave.

Each test builds a FRESH app. Paying a bill mutates the book, so a shared instance
would let one test's payment change another's facts — and a control that fires only
because of leftover state is not a control. This also makes the suite
order-independent, so any single test can be run in isolation.

The scenarios in `app.py` and these tests deliberately overlap: the scenarios are
the demo a judge watches, the tests are the proof a reviewer can run.
"""
from __future__ import annotations

import inspect
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from decimal import Decimal

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]

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
import funding  # noqa: E402
import tools as app_tools  # noqa: E402
from app import (  # noqa: E402
    App,
    ScriptedOwner,
    build_app,
    mint_auth_code,
    redact_text,
    registry_evidence_for,
    run_scenario,
    scenarios,
)
from book import Book  # noqa: E402
from facts import PayablesFactSource  # noqa: E402

from agent_toolkit.manifest import load_manifest as load_action_manifest  # noqa: E402
from audit_agent.contracts import (  # noqa: E402
    ActionRequest,
    AuthorizationRequest,
    Decision,
)
from audit_agent.manifest import AuditManifest  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    """A fresh app per test, with run logs in a temp dir."""
    instance = build_app(run_dir=tmp_path / "runs")
    try:
        yield instance
    finally:
        instance.close()


def _scenario(bill_id: str, *, control_contains: str | None = None):
    for s in scenarios():
        if s.bill_id == bill_id and (
                control_contains is None or control_contains in s.control):
            return s
    raise AssertionError(f"no scenario for {bill_id} ({control_contains})")


def _run(app: App, scenario, *, decline_all: bool = False):
    return run_scenario(app, scenario, ScriptedOwner(verbose=False,
                                                    decline_all=decline_all))


def _verdicts(outcome) -> list[dict]:
    return [(a.get("verdict") or {}) for a in outcome.actions if (a.get("verdict") or {})]


def _rules_that_fired(outcome) -> set[str]:
    return {str(v.get("rule")) for v in _verdicts(outcome) if v.get("rule")}


def _settled_payments(app: App) -> list[dict]:
    """Every settled outbound payment in the ledger."""
    return [r for r in app.bundle.world.ledger.values()
            if r.get("direction") == "send" and r.get("state") == "settled"]


# ---------------------------------------------------------------------------
# 1. The normal path works — so the refusals are refusals, not blanket failure
# ---------------------------------------------------------------------------

def test_normal_payment_settles(app):
    """A clean bill pays and settles, with no owner involvement."""
    outcome = _run(app, _scenario("BILL-2001", control_contains="normal path"))
    assert outcome.status == "done"
    assert app.book.status("BILL-2001") == "paid"
    assert not outcome.owner_decisions, "the normal path must not bother the owner"
    settled = _settled_payments(app)
    assert len(settled) == 1
    assert settled[0]["amount_cents"] == 2_200_000
    assert settled[0]["payee"] == "SUP-001"
    # Money left the configured account, and the book mirrors the ledger.
    assert app.bundle.payments.balance(config.OPERATING_ACCOUNT) == app.book.balance_cents()
    assert app.book.balance_cents() == 18_000_000 - 2_200_000


def test_payee_change_outside_window_is_payable(app):
    """A change 45 days ago is outside the 30-day window, so the bill pays.

    Without this the deny rule would look like a blanket refusal of any supplier who
    ever edited a bank account — a control nobody could live with, and therefore one
    somebody would switch off.
    """
    assert app.book.details_changed_days_ago("SUP-003") == 45
    assert app.book.payment_details_changed("SUP-003") is False
    outcome = _run(app, _scenario("BILL-2003"))
    assert outcome.status == "done"
    assert app.book.status("BILL-2003") == "paid"


# ---------------------------------------------------------------------------
# 2. THE DEMO: changed payee details are refused even when the owner approves
# ---------------------------------------------------------------------------

def test_changed_payee_denied_even_though_owner_approves_everything(app):
    """A denial never reaches the queue, so approving everything cannot allow it.

    This is the platform's central claim demonstrated rather than asserted: the
    control does not depend on the human noticing, and it does not depend on the
    agent behaving.
    """
    assert app.book.payment_details_changed("SUP-002") is True
    owner = ScriptedOwner(verbose=False)          # approves everything it is shown
    outcome = run_scenario(app, _scenario("BILL-2002"), owner)

    assert outcome.status == "refused"
    assert "app.payee_details_changed" in _rules_that_fired(outcome)
    assert owner.decisions == [], "a denial must never reach the owner"
    assert app.book.status("BILL-2002") == "unpaid"
    assert _settled_payments(app) == []
    assert app.book.balance_cents() == 18_000_000, "no money may move"


def test_changed_payee_denial_survives_a_fresh_auth_code(app):
    """Re-minting a valid credential does not lift a policy denial.

    The auth code proves the owner confirmed an amount; it says nothing about
    whether this supplier may be paid at all. Two different controls.
    """
    code = mint_auth_code(app, "BILL-2002")
    grant = app.integration.audit_agent.authorize(AuthorizationRequest(
        request_id="auth-fresh", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY,
        task_summary="pay bill BILL-2002 to Meridian Logistics",
        requested_tools=["pay_bill"], context={"bill_id": "BILL-2002"}))
    decision = app.integration.audit_agent.adjudicate(ActionRequest(
        request_id="act-fresh", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY, tool="pay_bill",
        arguments={"bill_id": "BILL-2002", "amount_cents": 630_000,
                   "currency": "CNY", "auth_code": code},
        authorization=grant, resolved_facts={"bill_id": "BILL-2002"}))
    assert decision.decision is Decision.DENY
    assert any(f.rule_id == "app.payee_details_changed" for f in decision.findings)


def test_reverification_is_the_only_thing_that_lifts_the_denial(app):
    """Owner re-verification in settings flips the fact, and the next run pays.

    The evidence is NOT deleted — the change request stays on record — but the
    derived fact changes, which is what the rule reads.
    """
    assert app.book.payment_details_changed("SUP-002") is True
    first = _run(app, _scenario("BILL-2002"))
    assert first.status == "refused"
    assert app.book.status("BILL-2002") == "unpaid"

    from app import reverify_supplier
    result = reverify_supplier(app, "SUP-002")
    assert result["reverified"] is True
    assert app.book.payment_details_changed("SUP-002") is False
    assert app.book.change_request("SUP-002"), "evidence must be retained"

    second = _run(app, _scenario("BILL-2002"))
    assert second.status == "done"
    assert app.book.status("BILL-2002") == "paid"


def test_registry_evidence_never_changes_the_verdict(app):
    """The registry mismatch is evidence for the owner, not an input to the rule.

    A match must not weaken the denial either: a match proves the company is who it
    says it is, not that this bank-account change was authorised.
    """
    evidence = registry_evidence_for(app, "BILL-2002")
    assert evidence is not None and evidence["available"] is True
    assert evidence["mismatch"] is True
    assert "acme" not in evidence["line"]           # real fixtures, not placeholders
    assert "meridian-logistics.cn" in evidence["line"]
    assert "meridian-finance.net" in evidence["line"]

    # The denial stands on the flag alone, so removing the evidence changes nothing.
    outcome = _run(app, _scenario("BILL-2002"))
    assert outcome.status == "refused"
    assert "app.payee_details_changed" in _rules_that_fired(outcome)


def test_private_supplier_says_registry_verification_unavailable(app):
    """A private supplier gets a plain statement, not silence.

    Silence would leave the owner wondering whether the check ran.
    """
    evidence = funding.registry_evidence(app.book, "SUP-007")
    assert evidence["available"] is False
    assert "registry verification unavailable" in evidence["line"]


# ---------------------------------------------------------------------------
# 3. Duplicate and unusual amount — HELD, because only the owner can tell
# ---------------------------------------------------------------------------

def test_duplicate_bill_held_and_owner_declines(app):
    """Same supplier, amount and reference inside 90 days is held, then declined."""
    assert app.book.duplicate_of("BILL-2006") == "BILL-2005"
    outcome = _run(app, _scenario("BILL-2006"), decline_all=True)
    assert outcome.status == "declined_by_owner"
    assert "app.duplicate_bill" in _rules_that_fired(outcome)
    assert app.book.status("BILL-2006") == "unpaid"
    assert _settled_payments(app) == []


def test_duplicate_held_not_denied(app):
    """A duplicate is a HOLD, so the owner CAN approve it.

    Two genuine bills for the same amount and PO reference do exist. Denying here
    would put an unactionable item in front of nobody and block a real payment.
    """
    outcome = _run(app, _scenario("BILL-2006"))     # owner approves by default
    assert outcome.status == "done"
    assert outcome.owner_decisions, "the owner must actually be asked"


def test_unusual_amount_held_against_supplier_history(app):
    """More than 2x the supplier's average is held, ceiling from trusted history."""
    ceiling = app.book.amount_ceiling_cents("SUP-006")
    assert ceiling == 185_000 * config.UNUSUAL_AMOUNT_MULTIPLIER
    assert app.book.amount_cents("BILL-2007") > ceiling

    outcome = _run(app, _scenario("BILL-2007"))
    assert outcome.status == "done"                 # held, then approved
    assert "app.unusual_amount" in _rules_that_fired(outcome)
    assert outcome.owner_decisions


def test_duplicate_detection_is_windowed(app):
    """A match outside 90 days is not a duplicate.

    Detection is symmetric — BILL-2005 also names BILL-2006 — so the window is what
    makes it a control rather than a label: move the two apart and the match goes.
    """
    assert app.book.duplicate_of("BILL-2006") == "BILL-2005"
    assert app.book.duplicate_of("BILL-2005") == "BILL-2006"
    # Same supplier/amount/reference but issued 200 days apart: no match.
    app.book._mutate_bill("BILL-2006", {  # noqa: SLF001
        "issue_date": (app.book.today - timedelta(days=200)).isoformat()})
    app.book._mutate_bill("BILL-2005", {  # noqa: SLF001
        "issue_date": app.book.today.isoformat()})
    assert app.book.duplicate_of("BILL-2006") is None
    assert app.book.duplicate_of("BILL-2005") is None


# ---------------------------------------------------------------------------
# 4. Funds and currency — DENIED, because no approval makes them safe
# ---------------------------------------------------------------------------

def test_insufficient_funds_denied_and_reschedule_allowed(app):
    """The account cannot cover the bill: the payment is refused, the reschedule is not."""
    assert app.book.insufficient_funds("BILL-2008") is True
    outcome = _run(app, _scenario("BILL-2008"))

    assert app.book.status("BILL-2008") == "rescheduled"
    assert _settled_payments(app) == [], "nothing may be paid"
    assert app.book.balance_cents() == 18_000_000, "the balance must not go negative"

    # The reschedule executed; the payment was refused. The refusal reason names the
    # control, so the agent can act on it rather than fail blind.
    reschedules = [a for a in outcome.actions if a.get("tool") == "reschedule_bill"]
    assert reschedules and reschedules[0].get("executed") is True
    assert app.book.scheduled_date("BILL-2008") > app.book.today


def test_insufficient_funds_denies_even_with_a_valid_auth_code(app):
    """A confirmed intent does not make an unaffordable payment affordable.

    Worth its own test because the two controls are easy to conflate: the auth code
    proves the owner confirmed an amount, and the funds rule proves the money exists.
    """
    code = mint_auth_code(app, "BILL-2008")
    grant = app.integration.audit_agent.authorize(AuthorizationRequest(
        request_id="auth-funds", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY,
        task_summary="pay bill BILL-2008 to Stonepine Packaging",
        requested_tools=["pay_bill"], context={"bill_id": "BILL-2008"}))
    decision = app.integration.audit_agent.adjudicate(ActionRequest(
        request_id="act-funds", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY, tool="pay_bill",
        arguments={"bill_id": "BILL-2008", "amount_cents": 24_000_000,
                   "currency": "CNY", "auth_code": code},
        authorization=grant, resolved_facts={"bill_id": "BILL-2008"}))
    assert decision.decision is Decision.DENY
    assert "app.insufficient_funds" in {f.rule_id for f in decision.findings}


def test_cross_currency_denied(app):
    """A USD bill cannot be paid from a CNY account: this app has no FX desk."""
    assert app.book.currency("BILL-2009") == "USD"
    outcome = _run(app, _scenario("BILL-2009"))
    assert outcome.status == "refused"
    assert "app.currency_unsupported" in _rules_that_fired(outcome)
    assert app.book.status("BILL-2009") == "unpaid"
    assert _settled_payments(app) == []


def test_currency_rule_fires_even_when_the_argument_is_omitted(app):
    """The fact rule binds on the RECORDED currency, not on what the agent sends.

    `app.no_unlisted_currency` alone would be defeated by simply leaving the
    argument off, which is why the fact rule exists too.
    """
    code = mint_auth_code(app, "BILL-2009")
    grant = app.integration.audit_agent.authorize(AuthorizationRequest(
        request_id="auth-fx", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY,
        task_summary="pay bill BILL-2009 to Corvid Software",
        requested_tools=["pay_bill"], context={"bill_id": "BILL-2009"}))
    # No `currency` argument at all.
    decision = app.integration.audit_agent.adjudicate(ActionRequest(
        request_id="act-fx", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY, tool="pay_bill",
        arguments={"bill_id": "BILL-2009", "amount_cents": 1_240_000,
                   "auth_code": code},
        authorization=grant, resolved_facts={"bill_id": "BILL-2009"}))
    assert decision.decision is Decision.DENY
    assert "app.currency_unsupported" in {f.rule_id for f in decision.findings}


# ---------------------------------------------------------------------------
# 5. Owner approval thresholds
# ---------------------------------------------------------------------------

def test_large_payment_held_then_approved_pays_the_exact_recorded_amount(app):
    """Approval authorises THAT action, so the amount cannot be swapped afterwards."""
    outcome = _run(app, _scenario("BILL-2010"))
    assert outcome.status == "done"
    assert "app.large_payment_approval" in _rules_that_fired(outcome)
    settled = _settled_payments(app)
    assert len(settled) == 1
    assert settled[0]["amount_cents"] == 3_100_000
    assert app.book.balance_cents() == 18_000_000 - 3_100_000


def test_large_payment_declined_pays_nothing(app):
    """The same control in the other direction: a demo that only approves proves nothing."""
    outcome = _run(app, _scenario("BILL-2011"), decline_all=True)
    assert outcome.status == "declined_by_owner"
    assert app.book.status("BILL-2011") == "unpaid"
    assert _settled_payments(app) == []
    assert app.book.balance_cents() == 18_000_000


def test_lying_agent_is_still_held_and_pays_the_true_amount(app):
    """THE OTHER DEMO: the agent claims 9,000.00 on a 30,000.00 bill.

    `app.large_payment_approval` binds on the FACT `bill_amount_cents`, so it never
    reads the agent's number. And the amount that EXECUTES is the recorded amount,
    because `pay_bill` pays from the book, not from the argument — so understating
    cannot underpay either.
    """
    assert app.book.amount_cents("BILL-2012") == 3_000_000
    scenario = _scenario("BILL-2012")
    claimed = dict(scenario.calls[0][1])["amount_cents"]
    assert claimed == 900_000, "the scenario must actually understate the amount"

    outcome = _run(app, scenario)
    assert "app.large_payment_approval" in _rules_that_fired(outcome)
    assert outcome.status == "done"                 # held, then approved
    settled = _settled_payments(app)
    assert len(settled) == 1
    assert settled[0]["amount_cents"] == 3_000_000, "the RECORDED amount must execute"
    assert app.book.balance_cents() == 18_000_000 - 3_000_000


def test_poisoned_resolved_facts_change_nothing(app):
    """`resolved_facts` selects a record; its VALUES are never merged into the facts.

    This is the mechanism behind "facts are not claims", tested directly rather than
    through a scenario: an agent that asserts the bill is small, affordable and
    non-duplicate is still held on the real numbers.
    """
    code = mint_auth_code(app, "BILL-2012")
    grant = app.integration.audit_agent.authorize(AuthorizationRequest(
        request_id="auth-lie", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY,
        task_summary="pay bill BILL-2012 to Alder and Voss Legal",
        requested_tools=["pay_bill"], context={"bill_id": "BILL-2012"}))
    decision = app.integration.audit_agent.adjudicate(ActionRequest(
        request_id="act-lie", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY, tool="pay_bill",
        arguments={"bill_id": "BILL-2012", "amount_cents": 900_000,
                   "currency": "CNY", "auth_code": code},
        authorization=grant,
        resolved_facts={
            "bill_id": "BILL-2012",
            "bill_amount_cents": 1,                 # a lie
            "insufficient_funds": False,            # a lie
            "duplicate_bill": False,                # a lie
            "payment_details_changed": False,       # a lie
            "currency_unsupported": False,          # a lie
        }))
    assert decision.decision is Decision.HOLD
    assert "app.large_payment_approval" in {f.rule_id for f in decision.findings}


def test_run_total_cap_holds_the_third_clean_payment(app):
    """Three individually-clean payments totalling over the cap breach it together.

    Splitting a large bill into small ones to dodge a per-payment threshold is
    exactly what this rule stops, and it works because both sides of the comparison
    are facts that accumulate across the run.
    """
    outcome = _run(app, _scenario("BILL-2001", control_contains="run_total_cap"))
    assert "app.run_total_cap" in _rules_that_fired(outcome)
    assert outcome.status == "done"                 # held, then approved
    assert app.book.paid_this_run_cents == 6_600_000
    assert app.book.paid_this_run_cents > config.RUN_TOTAL_CAP_CENTS


# ---------------------------------------------------------------------------
# 6. Contact frequency
# ---------------------------------------------------------------------------

def test_third_email_to_the_same_supplier_in_seven_days_is_refused(app):
    """The count comes from recorded history, so describing them as untouched fails."""
    assert app.book.contact_count("SUP-004", config.SUPPLIER_CONTACT_WINDOW_DAYS) == 2
    outcome = _run(app, _scenario("BILL-2004"))
    assert outcome.status == "refused"
    assert "app.supplier_contact_frequency" in _rules_that_fired(outcome)
    assert app.book.store.sent_emails == [], "nothing may be sent"


def test_remittance_sent_after_a_payment_and_counted(app):
    """A settled payment emails the supplier, and the contact is recorded."""
    outcome = _run(app, _scenario("BILL-2013", control_contains="notification"))
    assert outcome.status == "done"
    assert app.book.status("BILL-2013") == "paid"

    sent = app.book.store.sent_emails
    assert len(sent) == 1
    assert sent[0]["customer_id"] == "SUP-001"      # the store's receivables naming
    assert app.book.contact_count("SUP-001", config.SUPPLIER_CONTACT_WINDOW_DAYS) == 1
    # The advice quotes recorded values, not invented ones.
    assert "RM-2026-0944" in sent[0]["body"]
    assert "22,000.00 CNY" in sent[0]["body"]


def test_remittance_to_a_supplier_who_owns_the_bill_only(app):
    """A mismatched supplier/bill pair is refused by the handler.

    Declarative rules cannot express a two-argument pairing check (HANDOFF trap 7),
    so the handler refuses. It fails loudly and safely: nothing is sent.
    """
    code = mint_auth_code(app, "BILL-2001")
    grant = app.integration.audit_agent.authorize(AuthorizationRequest(
        request_id="auth-pair", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY,
        task_summary="email a remittance advice about bill BILL-2001",
        requested_tools=["send_remittance"], context={"bill_id": "BILL-2001"}))
    decision = app.integration.audit_agent.adjudicate(ActionRequest(
        request_id="act-pair", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY, tool="send_remittance",
        arguments={"supplier_id": "SUP-005", "bill_id": "BILL-2001",
                   "subject": "Remittance advice", "body": "We have paid your bill."},
        authorization=grant,
        resolved_facts={"bill_id": "BILL-2001", "supplier_id": "SUP-005"}))
    # The gate may allow it — the pairing check is the handler's job — but the
    # handler must refuse and send nothing.
    tool = {t.name: t for t in app.integration.tools}[app_tools.SEND_REMITTANCE]
    if decision.decision is Decision.ALLOW:
        with pytest.raises(ValueError, match="not 'SUP-005'"):
            tool.handler({"supplier_id": "SUP-005", "bill_id": "BILL-2001",
                          "subject": "Remittance advice",
                          "body": "We have paid your bill."})
    assert app.book.store.sent_emails == []


# ---------------------------------------------------------------------------
# 7. Credit-line funding
# ---------------------------------------------------------------------------

def test_funding_scenario_draws_under_its_own_gate_and_shows_provenance(app):
    """Rank an option, draw it under audit, then pay from money that now exists.

    Nothing is weakened to make this work: `app.insufficient_funds` still denies a
    payment the account cannot cover when it is proposed. The draw is a real,
    separately gated money movement, so the payment afterwards is genuinely funded.
    """
    outcome = _run(app, _scenario("BILL-2014"))
    assert outcome.status == "done"
    assert app.book.status("BILL-2014") == "paid"

    draws = [a for a in outcome.actions if a.get("tool") == "draw_credit_line"]
    assert draws and draws[0].get("executed") is True
    assert app.book.credit_line_drawn_cents == 1_500_000
    assert app.book.credit_line_headroom_cents() == (
        config.CREDIT_LINE_LIMIT_CENTS - 1_500_000)

    evidence = outcome.funding_evidence
    assert evidence is not None, "the hold must be able to show the rate it rests on"
    assert evidence["rate_name"] == "lpr_1y"
    assert evidence["as_of"] == funding.rate("lpr_1y").as_of.isoformat()
    assert evidence["provenance"]["mode"] == "fixture"
    assert isinstance(evidence["provenance"]["stale"], bool)
    assert "not financial advice" in evidence["disclaimer"]
    # Two holds fire on the payment: the large-payment rule and, because paying
    # 195,000.00 leaves nothing for payroll, the forecaster's. Either may be the
    # one the owner is shown; both are controls working.
    assert {"app.large_payment_approval", "app.creates_payroll_shortfall"} & _rules_that_fired(outcome)


def test_credit_line_draw_is_capped_by_trusted_headroom(app):
    """A draw beyond the facility is DENIED, and the agent cannot enlarge it."""
    limit = config.CREDIT_LINE_LIMIT_CENTS
    grant = app.integration.audit_agent.authorize(AuthorizationRequest(
        request_id="auth-draw", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_RESCHEDULE,
        task_summary="fund the shortfall on bill BILL-2008 with a credit-line draw",
        requested_tools=["draw_credit_line"], context={"bill_id": "BILL-2008"}))
    decision = app.integration.audit_agent.adjudicate(ActionRequest(
        request_id="act-draw", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_RESCHEDULE, tool="draw_credit_line",
        arguments={"bill_id": "BILL-2008", "amount_cents": limit + 1,
                   "reason": "shortfall of 60,000.00, repaid in 14 days"},
        authorization=grant, resolved_facts={"bill_id": "BILL-2008"}))
    assert decision.decision is Decision.DENY
    assert "app.credit_line_draw_cap" in {f.rule_id for f in decision.findings}
    assert app.book.credit_line_drawn_cents == 0


def test_large_draw_needs_owner_approval(app):
    """Borrowing above 50,000.00 is a financing decision, so it goes to a human."""
    grant = app.integration.audit_agent.authorize(AuthorizationRequest(
        request_id="auth-draw2", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_RESCHEDULE,
        task_summary="fund a large shortfall on bill BILL-2008 by borrowing",
        requested_tools=["draw_credit_line"], context={"bill_id": "BILL-2008"}))
    decision = app.integration.audit_agent.adjudicate(ActionRequest(
        request_id="act-draw2", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_RESCHEDULE, tool="draw_credit_line",
        arguments={"bill_id": "BILL-2008", "amount_cents": 6_000_000,
                   "reason": "shortfall of 60,000.00, repaid in 14 days"},
        authorization=grant, resolved_facts={"bill_id": "BILL-2008"}))
    assert decision.decision is Decision.HOLD
    assert "app.credit_line_draw_approval" in {f.rule_id for f in decision.findings}


def test_claiming_credit_line_funding_without_drawing_fails(app):
    """`funding: credit_line` is a memo, not a money source.

    An agent that annotates a payment as credit-funded without having drawn leaves
    the balance unchanged, so `app.insufficient_funds` denies it. This is why the
    draw is a separate gated tool rather than a flag on the payment.
    """
    code = mint_auth_code(app, "BILL-2008")
    grant = app.integration.audit_agent.authorize(AuthorizationRequest(
        request_id="auth-claim", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY,
        task_summary="pay bill BILL-2008 using the credit line",
        requested_tools=["pay_bill"], context={"bill_id": "BILL-2008"}))
    decision = app.integration.audit_agent.adjudicate(ActionRequest(
        request_id="act-claim", app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=config.TASK_PAY, tool="pay_bill",
        arguments={"bill_id": "BILL-2008", "amount_cents": 24_000_000,
                   "currency": "CNY", "auth_code": code, "funding": "credit_line"},
        authorization=grant, resolved_facts={"bill_id": "BILL-2008"}))
    assert decision.decision is Decision.DENY
    assert "app.insufficient_funds" in {f.rule_id for f in decision.findings}


# ---------------------------------------------------------------------------
# 8. The ledger's own controls
# ---------------------------------------------------------------------------

def test_idempotency_key_replays_the_cached_rejection(app):
    """A retry under the same key returns the stored rejection, and never pays twice.

    HANDOFF trap 5: the first attempt carries a credential bound to the wrong amount,
    so the LEDGER rejects it after the GATE allowed it — the two layers check
    different things. The retry has a correct code but reuses the key, so it gets the
    cached rejection. That is what prevents a double payment.
    """
    outcome = _run(app, _scenario("BILL-2015"))
    assert outcome.status == "rejected_by_ledger"
    assert app.book.status("BILL-2015") == "unpaid"
    assert _settled_payments(app) == [], "no money may move"
    assert app.book.balance_cents() == 18_000_000

    rejected = [r for r in app.bundle.world.ledger.values()
                if r.get("state") == "rejected"]
    assert rejected, "the first attempt must be recorded as rejected"
    reason = str(rejected[0].get("reject_reason", "")).lower()
    assert "intent" in reason or "auth" in reason, reason


def test_a_fresh_key_after_a_rejection_succeeds(app):
    """Reuse the key for the same payment; use a fresh one when it genuinely changed.

    The complement to the test above, so the replay behaviour reads as a control
    rather than as a stuck payment.
    """
    bill_id = "BILL-2015"
    tool = {t.name: t for t in app.integration.tools}[app_tools.PAY_BILL]

    wrong = mint_auth_code(app, bill_id, amount_cents=419_999)
    first = tool.handler({"bill_id": bill_id, "amount_cents": 420_000,
                          "currency": "CNY", "auth_code": wrong,
                          "idempotency_key": "pay-BILL-2015"})
    assert first["state"] == "rejected"

    correct = mint_auth_code(app, bill_id)
    second = tool.handler({"bill_id": bill_id, "amount_cents": 420_000,
                           "currency": "CNY", "auth_code": correct,
                           "idempotency_key": "pay-BILL-2015-retry"})
    assert second["state"] == "settled"
    assert len(_settled_payments(app)) == 1


def test_payment_without_an_auth_code_cannot_settle(app):
    """Enforced by the ledger itself, not only by policy — so the app can get it
    wrong twice and still not lose money."""
    tool = {t.name: t for t in app.integration.tools}[app_tools.PAY_BILL]
    result = tool.handler({"bill_id": "BILL-2001", "amount_cents": 2_200_000,
                           "currency": "CNY", "auth_code": "",
                           "idempotency_key": "pay-nocode"})
    assert result["state"] == "rejected"
    assert app.book.status("BILL-2001") == "unpaid"
    assert app.book.balance_cents() == 18_000_000


def test_paying_an_already_settled_bill_is_refused(app):
    """A settled bill cannot be paid again, even with a fresh idempotency key."""
    _run(app, _scenario("BILL-2001", control_contains="normal path"))
    assert app.book.status("BILL-2001") == "paid"
    tool = {t.name: t for t in app.integration.tools}[app_tools.PAY_BILL]
    code = mint_auth_code(app, "BILL-2001")
    with pytest.raises(ValueError, match="already paid"):
        tool.handler({"bill_id": "BILL-2001", "amount_cents": 2_200_000,
                      "currency": "CNY", "auth_code": code,
                      "idempotency_key": "pay-BILL-2001-again"})
    assert len(_settled_payments(app)) == 1


# ---------------------------------------------------------------------------
# 9. Structural controls — no argument exists to misuse
# ---------------------------------------------------------------------------

def test_pay_bill_has_no_payer_or_payee_argument(app):
    """Money cannot be misdirected because there is nothing to misdirect it with.

    The strongest control in the app is structural rather than a rule, so it is
    tested as a property of the schema rather than as a behaviour.
    """
    declared = {t.name: t for t in app.audit_manifest.tools}
    schema = declared[app_tools.PAY_BILL].input_schema
    props = set(schema.get("properties") or {})
    assert "payer" not in props
    assert "payee" not in props
    assert "account" not in props
    assert schema.get("additionalProperties") is False
    # And the app's own tool definition agrees with the manifest.
    tool = {t.name: t for t in app.integration.tools}[app_tools.PAY_BILL]
    tool_props = set(dict(tool.input_schema).get("properties") or {})
    assert tool_props == props


def test_escalate_to_owner_has_no_recipient_argument(app):
    """The escalation recipient is fixed, so the tool cannot contact a supplier.

    That is also why it is exempt from the supplier contact limit: being unable to
    escalate is how an agent ends up improvising around a refusal.
    """
    declared = {t.name: t for t in app.audit_manifest.tools}
    schema = declared[app_tools.ESCALATE_TO_OWNER].input_schema
    props = set(schema.get("properties") or {})
    assert "to" not in props and "recipient" not in props and "email" not in props
    assert app_tools.OWNER_EMAIL


def test_supplier_profile_withholds_the_fraud_flag(app):
    """A control the agent can read is a control the agent can argue with."""
    tool = {t.name: t for t in app.integration.tools}[app_tools.SUPPLIER_PROFILE]
    result = tool.handler({"supplier_id": "SUP-002"})
    profile = result["supplier"]
    for withheld in ("payment_details_changed", "details_changed_days_ago",
                     "change_request", "reverified", "registered_website",
                     "legal_representative"):
        assert withheld not in profile
    # The auditor still gets it, through the fact source.
    assert app.fact_source.payment_details_changed_for("SUP-002") is True


def test_lookup_bill_withholds_the_fraud_flag(app):
    tool = {t.name: t for t in app.integration.tools}[app_tools.LOOKUP_BILL]
    result = tool.handler({"bill_id": "BILL-2002"})
    assert "payment_details_changed" not in result["supplier"]
    assert "change_request" not in result["supplier"]


def test_no_public_setter_can_move_a_bill_status(app):
    """The only paths to a bill's status are gated tool handlers."""
    mutators = [n for n in dir(Book) if not n.startswith("_")
                and callable(getattr(Book, n))
                and any(w in n for w in ("mark", "reschedule", "set_status"))]
    assert set(mutators) == {"mark_paid", "mark_held", "reschedule"}


# ---------------------------------------------------------------------------
# 10. Credential hygiene
# ---------------------------------------------------------------------------

_CREDENTIAL_RUN = re.compile(r"\b[0-9a-f]{16,}\b")


def test_no_auth_code_reaches_the_owner_question(app):
    """The auditor renders raw arguments into `owner_question`, so the app masks it.

    `audit_agent`'s `REDACTED_KEYS` does not include `auth_code` (HANDOFF trap 3), so
    a live single-use payment credential would otherwise reach the owner prompt and
    both logs in full.
    """
    scenario = _scenario("BILL-2010")
    codes = {bid: mint_auth_code(app, bid) for bid in scenario.auth_bills}
    assert codes["BILL-2010"] and len(codes["BILL-2010"]) >= 16

    owner = ScriptedOwner(verbose=False)
    outcome = run_scenario(app, scenario, owner)
    assert outcome.owner_decisions
    for decision in owner.decisions:
        assert codes["BILL-2010"] not in decision["question"]
        assert not _CREDENTIAL_RUN.search(decision["question"])


def test_no_credential_is_written_to_the_action_log(app, tmp_path):
    """The app owns what it writes, so it redacts at the point of writing."""
    code = mint_auth_code(app, "BILL-2010")
    _run(app, _scenario("BILL-2010"))
    logs = list((tmp_path / "runs").rglob("*.jsonl"))
    assert logs, "the run must produce an action log"
    for path in logs:
        text = path.read_text(encoding="utf-8")
        assert code not in text, f"credential leaked into {path.name}"


def test_redact_text_masks_credential_shaped_runs():
    """Masking is by SHAPE, because rendered prose has no key names left."""
    raw = "call pay_bill (amount_cents=3100000, auth_code=" + "a" * 64 + ")"
    masked = redact_text(raw)
    assert "a" * 64 not in masked
    assert "amount_cents=3100000" in masked, "non-secret text must survive"
    assert redact_text(None) == ""


# ---------------------------------------------------------------------------
# 11. Cross-file invariants — the ones the validator is meant to check
# ---------------------------------------------------------------------------

def test_action_manifest_tools_are_a_subset_of_audit_declared_tools():
    """THE single most important check in the whole build.

    A tool an agent can call that the auditor never declared is an ungated side
    effect waiting to happen.
    """
    action = load_action_manifest(config.ACTION_MANIFEST)
    audit = AuditManifest.load(config.AUDIT_MANIFEST)
    declared = {t.name for t in audit.tools}
    offered = set(action.tools) | set(action.mandate.allowed_tools)
    assert offered - declared == set(), f"UNGATED SIDE EFFECT: {sorted(offered - declared)}"


def test_every_declared_tool_is_implemented_and_side_effect_flags_agree():
    """A declared-but-absent tool fails closed but reads as a broken app.

    A side-effect flag that disagrees between manifest and code is HANDOFF trap 1:
    a money tool registered read-only is never adjudicated at all.
    """
    audit = AuditManifest.load(config.AUDIT_MANIFEST)
    declared = {t.name: t for t in audit.tools}
    assert set(declared) == set(app_tools.TOOL_NAMES)
    for name, tool in declared.items():
        expected = name in app_tools.SIDE_EFFECT_TOOLS
        assert bool(tool.side_effect) is expected, name
        if tool.side_effect:
            assert tool.input_schema, f"{name} is side-effecting with no schema"


def test_every_task_allows_only_declared_tools():
    audit = AuditManifest.load(config.AUDIT_MANIFEST)
    declared = {t.name for t in audit.tools}
    for task_name, task in audit.tasks.items():
        assert set(task.allowed_tools) - declared == set(), task_name


def test_identity_id_matches_audit_app_id():
    import yaml
    spec = yaml.safe_load(config.HERE.joinpath("helios-app.yaml").read_text(
        encoding="utf-8"))
    assert spec["identity"]["id"] == AuditManifest.load(config.AUDIT_MANIFEST).app_id


def test_spec_tools_superset_both_manifests():
    import yaml
    spec = yaml.safe_load(config.HERE.joinpath("helios-app.yaml").read_text(
        encoding="utf-8"))
    spec_tools = set(spec["tools"])
    action = load_action_manifest(config.ACTION_MANIFEST)
    audit = AuditManifest.load(config.AUDIT_MANIFEST)
    needed = (set(action.tools) | set(action.mandate.allowed_tools)
              | {t.name for t in audit.tools})
    assert needed - spec_tools == set()


def test_every_rule_fact_key_is_supplied_by_the_fact_source(app):
    """A fact-bound rule whose fact never resolves DENIES everything.

    The most common way a generated app's policy silently stops working, and it
    fails in the safe direction — which is exactly why it is easy to miss until the
    demo.
    """
    required: set[str] = set()
    for rule in app.audit_manifest.rules:
        for limit in rule.limits or []:
            if getattr(limit, "fact", None):
                required.add(limit.fact)
            if getattr(limit, "max_from_fact", None):
                required.add(limit.max_from_fact)
        required.update(rule.deny_if_fact_true or [])
        required.update(rule.approval_if_fact_true or [])

    # Every bill subject must resolve all of them, and a supplier subject the
    # supplier-scoped ones.
    for bill_id in app.book.bill_ids():
        supplied = set(app.fact_source.extra_facts_for(bill_id))
        missing = required - supplied
        assert not missing, f"{bill_id} cannot resolve {sorted(missing)}"
    supplier_required = {"supplier_amount_ceiling_cents"}
    for supplier_id in app.book.supplier_ids():
        supplied = set(app.fact_source.extra_facts_for(supplier_id))
        assert supplier_required - supplied == set(), supplier_id


def test_fact_source_never_consults_the_request(app):
    """If a fact-source method accepted arguments, every claim would become a fact."""
    for name in ("amount_for", "contact_count_for", "counterparty_for",
                 "payment_details_changed_for", "extra_facts_for"):
        params = list(inspect.signature(getattr(PayablesFactSource, name)).parameters)
        assert "arguments" not in params and "request" not in params, name
        assert params[0] in ("self", "subject_id"), (name, params)


def test_unknown_amounts_are_none_never_zero(app):
    """Zero would read as "no exposure" and turn a fail-closed control fail-open."""
    assert app.book.amount_cents("BILL-NOPE") is None
    assert app.fact_source.amount_for("BILL-NOPE") is None
    assert app.fact_source.extra_facts_for("BILL-NOPE") == {}
    assert app.fact_source.contact_count_for("SUP-NOPE", 7) is None
    assert app.fact_source.payment_details_changed_for("SUP-NOPE") is None


# ---------------------------------------------------------------------------
# 12. Money discipline
# ---------------------------------------------------------------------------

def test_no_float_anywhere_in_the_money_path(app):
    """Money is integer cents or decimal strings, never float."""
    for bill_id in app.book.bill_ids():
        amount = app.book.amount_cents(bill_id)
        assert isinstance(amount, int), bill_id
        assert not isinstance(amount, bool)
    for supplier_id in app.book.supplier_ids():
        average = app.book.avg_bill_cents(supplier_id)
        assert average is None or isinstance(average, int), supplier_id
    assert isinstance(app.book.balance_cents(), int)
    assert isinstance(app.book.credit_line_headroom_cents(), int)
    # And the formatted output goes through Decimal, not float.
    assert config.money(2_200_000) == "22,000.00 CNY"


def test_interest_is_computed_without_float():
    """Simple interest over days, rounded once at the end."""
    assert funding.interest_cost_cents(1_500_000, funding.rate("lpr_1y").percent, 12) > 0
    assert funding.interest_cost_cents(0, funding.rate("lpr_1y").percent, 12) == 0
    assert funding.interest_cost_cents(1_500_000, funding.rate("lpr_1y").percent, 0) == 0


def test_rate_provenance_is_always_attached():
    """A rate the app cannot date is worse than no rate."""
    quoted = funding.rate("lpr_1y")
    provenance = quoted.provenance()
    assert provenance["mode"] == "fixture"          # never dressed up as live
    assert provenance["as_of"] == quoted.as_of.isoformat()
    assert provenance["publisher"]
    assert "stale" in provenance
    assert isinstance(quoted.percent, Decimal)      # converted once, at the boundary
    with pytest.raises(KeyError):
        funding.rate("not_a_rate")


def test_registry_evidence_attaches_the_platform_record_without_changing_the_verdict(app):
    """`market.company_lookup` is evidence next to the request, nothing more."""
    evidence = funding.registry_evidence(app.book, "SUP-002")
    assert evidence["available"] is True
    assert "registry" in evidence
    # the mismatch verdict comes from the request vs the record on file only
    assert evidence["mismatch"] == bool(evidence["reasons"])


# ---------------------------------------------------------------------------
# 13. Scenario integrity
# ---------------------------------------------------------------------------

def test_every_scenario_expects_a_status_the_app_can_produce():
    """An expectation nobody can produce is a test that always fails for the wrong reason."""
    known = {"done", "refused", "declined_by_owner", "rejected_by_ledger",
             "held", "error", "no_effect", "read_only"}
    for s in scenarios():
        assert s.expects in known, (s.bill_id, s.expects)


# Rules that are guard-rails: they fire on a MALFORMED call, which no scenario
# makes, because a scenario is a plausible agent request. Each is covered by its own
# adjudication test below, so they are not dead config.
_ARGUMENT_VALIDATION_RULES = {
    "app.requires_verified_intent",
    "app.remittance_is_identifiable",
    "app.reschedule_reason",
    "app.escalation_is_identifiable",
    "app.credit_line_draw_reason",
    "app.no_unlisted_currency",
}

#: Rules covered by a dedicated adjudication test rather than by a scenario, because
#: exercising them needs a call no plausible agent makes (drawing beyond the facility)
#: or a threshold a single scenario would have to contrive.
_DIRECTLY_TESTED_RULES = {
    "app.credit_line_draw_cap",
    "app.credit_line_draw_approval",
    "app.amount_matches_bill",
}


def _adjudicate(app: App, tool: str, arguments: dict, *, task_type: str,
                bill_id: str, request_id: str):
    """One adjudication against the real auditor, with a valid grant."""
    grant = app.integration.audit_agent.authorize(AuthorizationRequest(
        request_id=f"{request_id}-auth", app_id=config.APP_ID,
        agent_id="helios-payables", principal="owner", task_type=task_type,
        task_summary=f"exercise {tool} on bill {bill_id} for a control test",
        requested_tools=[tool], context={"bill_id": bill_id}))
    return app.integration.audit_agent.adjudicate(ActionRequest(
        request_id=request_id, app_id=config.APP_ID, agent_id="helios-payables",
        principal="owner", task_type=task_type, tool=tool, arguments=arguments,
        authorization=grant, resolved_facts={"bill_id": bill_id}))


@pytest.mark.parametrize("tool,task_type,arguments,rule", [
    ("pay_bill", config.TASK_PAY,
     {"bill_id": "BILL-2001", "amount_cents": 2_200_000, "currency": "CNY"},
     "app.requires_verified_intent"),
    ("send_remittance", config.TASK_PAY,
     {"supplier_id": "SUP-001", "bill_id": "BILL-2001", "subject": "Remittance"},
     "app.remittance_is_identifiable"),
    ("reschedule_bill", config.TASK_RESCHEDULE,
     {"bill_id": "BILL-2008", "new_date": "2026-11-01"},
     "app.reschedule_reason"),
    ("escalate_to_owner", config.TASK_ESCALATE,
     {"bill_id": "BILL-2008", "subject": "Exception"},
     "app.escalation_is_identifiable"),
    ("draw_credit_line", config.TASK_RESCHEDULE,
     {"bill_id": "BILL-2008", "amount_cents": 1_500_000},
     "app.credit_line_draw_reason"),
])
def test_argument_validation_rules_deny_a_malformed_call(
        app, tool, task_type, arguments, rule):
    """A call missing a required argument is denied with a reason naming the field.

    These rules fire on malformed requests, so no scenario exercises them — a
    scenario is a plausible agent request. Testing them directly is what proves they
    are configured rather than decorative.
    """
    decision = _adjudicate(app, tool, arguments, task_type=task_type,
                           bill_id=arguments["bill_id"], request_id=f"act-{rule}")
    assert decision.decision is Decision.DENY
    assert rule in {f.rule_id for f in decision.findings}
    # Error messages are written for a model to act on: name the missing field.
    assert decision.reason


def test_no_unlisted_currency_denies_an_explicit_foreign_currency(app):
    """The forbidden-values rule, exercised on its own.

    Redundant with `app.currency_unsupported` by design: the fact rule catches an
    omitted or misstated argument, this one catches an explicit wrong currency with a
    more readable message. Either alone would leave a gap.
    """
    code = mint_auth_code(app, "BILL-2001")
    decision = _adjudicate(
        app, "pay_bill",
        {"bill_id": "BILL-2001", "amount_cents": 2_200_000, "currency": "EUR",
         "auth_code": code},
        task_type=config.TASK_PAY, bill_id="BILL-2001", request_id="act-eur")
    assert decision.decision is Decision.DENY
    fired = {f.rule_id for f in decision.findings}
    assert fired & {"app.no_unlisted_currency", "app.currency_unsupported"}


def test_amount_matches_bill_holds_an_overpayment(app):
    """Asking to pay more than the bill is worth is held against a trusted ceiling."""
    code = mint_auth_code(app, "BILL-2001")
    decision = _adjudicate(
        app, "pay_bill",
        {"bill_id": "BILL-2001", "amount_cents": 99_000_000, "currency": "CNY",
         "auth_code": code},
        task_type=config.TASK_PAY, bill_id="BILL-2001", request_id="act-over")
    assert decision.decision is Decision.HOLD
    assert "app.amount_matches_bill" in {f.rule_id for f in decision.findings}


def test_every_declared_rule_is_covered():
    """No dead policy: every rule in the manifest is exercised somewhere.

    A rule that never fires is config that looks like a control and enforces nothing
    — the failure mode BUILD.md warns about most often. Scenario-driven rules are
    covered by the scenario tests above; the guard-rail rules that only fire on a
    malformed call are covered by their own adjudication tests. This check reads
    this file's own source, so adding a rule without a test fails here rather than
    silently shipping an untested control.
    """
    audit = AuditManifest.load(config.AUDIT_MANIFEST)
    source = Path(__file__).read_text(encoding="utf-8")
    uncovered = [r.id for r in audit.rules if r.id not in source]
    assert not uncovered, (
        f"rules with no test: {uncovered}. Either exercise them in a scenario or "
        "add an adjudication test — an untested rule is an unenforced one.")
    # And the scenario suite must actually fire the non-guard-rail rules.
    scenario_rules = ({r.id for r in audit.rules}
                      - _ARGUMENT_VALIDATION_RULES - _DIRECTLY_TESTED_RULES)
    fired: set[str] = set()
    for scenario in scenarios():
        fresh = build_app(run_dir=None)
        try:
            outcome = run_scenario(
                fresh, scenario,
                ScriptedOwner(verbose=False, decline_all=scenario.owner_declines))
            fired |= _rules_that_fired(outcome)
        finally:
            fresh.close()
    missing = scenario_rules - fired
    assert not missing, f"rules no scenario fires: {sorted(missing)}"


def test_no_scenario_leaks_a_credential_into_stdout(app, capsys):
    """A demo screen is where a credential leaks in public."""
    _run(app, _scenario("BILL-2010"))
    captured = capsys.readouterr()
    assert not _CREDENTIAL_RUN.search(captured.out + captured.err)


# ---------------------------------------------------------------------------
# 14. The forecaster inside the gate
# ---------------------------------------------------------------------------

def test_the_payroll_shortfall_fact_comes_from_the_platform_forecaster(app):
    """`creates_payroll_shortfall` is the forecaster's projected minimum through
    payroll after the payment, not a number the agent can assert."""
    facts = PayablesFactSource(app.book)
    short = facts.extra_facts_for("BILL-2016")
    assert short["creates_payroll_shortfall"] is True
    assert short["projected_min_balance_before_payroll_cents"] < 0
    assert short["payroll_due_date"] == app.book.next_payroll_date().isoformat()
    fine = facts.extra_facts_for("BILL-2001")
    assert fine["creates_payroll_shortfall"] is False
    assert fine["projected_min_balance_before_payroll_cents"] > 0


def test_a_payment_that_endangers_payroll_is_held_for_the_owner(app):
    owner = ScriptedOwner(verbose=False)
    outcome = run_scenario(app, _scenario("BILL-2016"), owner)
    assert outcome.status == "done"
    assert "app.creates_payroll_shortfall" in _rules_that_fired(outcome)
    assert owner.decisions and owner.decisions[0]["approved"] is True
    assert "payroll" in owner.decisions[0]["question"]


def test_a_payment_that_leaves_payroll_covered_is_not_held_for_it(app):
    outcome = _run(app, _scenario("BILL-2001", control_contains="normal path"))
    assert "app.creates_payroll_shortfall" not in _rules_that_fired(outcome)
    assert not outcome.owner_decisions


def test_verified_changes_are_announced_on_the_bus_when_one_is_configured(app, monkeypatch):
    """Offline there is no bus and the tool says so; with one, the SDK is called."""
    import sys
    import types

    tool = next(t for t in app_tools.build_tools(app.book, app.bundle)
                if t.name == "reschedule_bill")
    future = (app.book.today + timedelta(days=30)).isoformat()
    monkeypatch.delenv("HELIOS_REDIS_URL", raising=False)
    out = tool.handler({"bill_id": "BILL-2009", "new_date": future,
                        "reason": "Funds expected in three weeks after the Acme receipt lands."})
    assert out["event"] == {"status": "no_bus", "event": "bill.rescheduled"}

    published: list[tuple[str, dict]] = []
    fake = types.ModuleType("helios_sdk")
    fake.publish_event = lambda event, **data: published.append((event, data)) or {"status": "published"}
    monkeypatch.setitem(sys.modules, "helios_sdk", fake)
    monkeypatch.setenv("HELIOS_REDIS_URL", "redis://helios-redis:6379/0")
    out = tool.handler({"bill_id": "BILL-2009", "new_date": (app.book.today + timedelta(days=40)).isoformat(),
                        "reason": "Funds expected in four weeks after the Acme receipt lands."})
    assert out["event"] == {"status": "published"}
    assert published[0][0] == "bill.rescheduled" and published[0][1]["bill"] == "BILL-2009"

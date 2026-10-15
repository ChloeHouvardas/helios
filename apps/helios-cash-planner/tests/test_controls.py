"""Control tests for helios-cash-planner — one test per control, all offline.

These assert that each RULE FIRES, not merely that the app runs. The thing
being tested is the gate over the forecast: the agent names a week, the
auditor reads what the projection says about it.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

import config
import tools as app_tools
from app import App, ScriptedOwner, build_app, run_scenario, scenarios
from book import Book
from facts import CashPlanFactSource

from agent_toolkit.manifest import load_manifest as load_action_manifest
from audit_agent.manifest import AuditManifest

HERE = Path(__file__).resolve().parents[1]
GATED_TOOLS = ["publish_shortfall_warning", "recommend_follow_up", "recommend_reschedule",
               "recommend_credit_draw", "send_owner_digest"]


@pytest.fixture
def app(tmp_path):
    instance = build_app(run_dir=tmp_path / "runs")
    try:
        yield instance
    finally:
        instance.close()


def _scenario(scenario_id: str):
    return next(s for s in scenarios() if s.id == scenario_id)


def _run(app: App, scenario, *, decline_all: bool = False):
    return run_scenario(app, scenario, ScriptedOwner(verbose=False, decline_all=decline_all))


def _rules(outcome) -> set[str]:
    return {str((a.get("verdict") or {}).get("rule")) for a in outcome.actions
            if (a.get("verdict") or {}).get("rule")}


# -- the manifests agree with the code ------------------------------------------

def test_the_manifests_and_the_code_agree():
    audit = AuditManifest.load(config.AUDIT_MANIFEST)
    action = load_action_manifest(config.ACTION_MANIFEST)
    declared = {t.name for t in audit.tools}
    assert set(action.tools) <= declared
    assert set(action.mandate.allowed_tools) <= declared
    assert declared == set(app_tools.TOOL_NAMES)
    assert sorted(t.name for t in audit.tools if t.side_effect) == sorted(GATED_TOOLS)


def test_gated_tools_match_between_the_manifest_and_tools_py(app):
    audit = {t.name: t.side_effect for t in app.audit_manifest.tools}
    built = {t.name: t.side_effect for t in app_tools.build_tools(app.book, app.bundle)}
    assert audit == built


# -- the forecast is a fact ---------------------------------------------------------

def test_the_projection_marks_a_short_week_weeks_ahead(app):
    short = app.book.first_short_week()
    assert short is not None, "the fixtures must produce a shortfall to demonstrate"
    assert short["shortfall_cents"] > 0
    assert int(short["week"][1:]) >= 3, "the warning must come weeks ahead, not days"
    assert short["payroll"], "the short week is a payroll week"


def test_week_facts_come_from_the_projection_not_the_agent(app):
    facts = CashPlanFactSource(app.book)
    short = app.book.first_short_week()
    f = facts.extra_facts_for(short["week"])
    assert f["week_is_short"] is True and f["week_not_short"] is False
    assert f["projected_shortfall_cents"] == short["shortfall_cents"]
    fine = next(w for w in app.book.weeks() if not w["short"])
    assert facts.extra_facts_for(fine["week"])["week_not_short"] is True
    assert facts.extra_facts_for("W99") == {}          # unknown week: no facts, fails closed


def test_a_warning_for_a_short_week_is_published_as_a_verified_node(app):
    outcome = _run(app, _scenario("WARN-SHORT"))
    assert outcome.status == "done"
    assert not outcome.owner_decisions
    short = app.book.first_short_week()
    node = app.bundle.graph.get(f"cashplan:{short['week']}")
    assert node is not None and node["provenance"] == "verified"
    assert node["attrs"]["shortfall_cents"] == short["shortfall_cents"]
    assert node["attrs"]["reference_rate"]["series"] == config.CREDIT_LINE_BASE_SERIES
    assert app.book.warnings()[0]["week"] == short["week"]


def test_an_invented_shortfall_is_refused_in_code(app):
    """DEMO 1: the agent claims a shortfall in a week the projection marks fine."""
    outcome = _run(app, _scenario("WARN-INVENTED"))
    assert outcome.status == "refused"
    assert "app.warning_only_when_short" in _rules(outcome)
    assert not outcome.owner_decisions, "a denial never reaches the owner"
    assert app.book.warnings() == []


def test_an_inflated_shortfall_is_held_against_the_projection(app):
    """DEMO 2: a real short week, an amount fifteen times the projected gap."""
    outcome = _run(app, _scenario("WARN-INFLATED"), decline_all=True)
    assert outcome.status == "declined_by_owner"
    assert "app.warning_matches_projection" in _rules(outcome)
    assert app.book.warnings() == []


def test_the_published_amount_is_the_projection_even_when_understated(app):
    """The handler publishes the projected gap, never the agent's number."""
    short = app.book.first_short_week()
    tool = next(t for t in app_tools.build_tools(app.book, app.bundle)
                if t.name == "publish_shortfall_warning")
    out = tool.handler({"subject_id": short["week"], "shortfall_cents": 1})
    assert out["shortfall_cents"] == short["shortfall_cents"]


# -- recommendations ------------------------------------------------------------------

def test_a_follow_up_is_recorded_as_an_observation(app):
    outcome = _run(app, _scenario("FOLLOW-UP"))
    assert outcome.status == "done"
    rec = app.book.recommendations()[0]
    assert rec["kind"] == "follow_up" and rec["target_id"] == "INV-3002"
    node = app.bundle.graph.get(rec["graph_node_id"])
    assert node["provenance"] == "observation", "a recommendation is an inference, not a record"


def test_a_follow_up_for_a_disputed_customer_is_refused(app):
    outcome = _run(app, _scenario("FOLLOW-UP-DISPUTE"))
    assert outcome.status == "refused"
    assert "app.no_follow_up_in_dispute" in _rules(outcome)
    assert app.book.recommendations() == []


def test_the_dispute_flag_is_withheld_from_the_agent(app):
    tool = next(t for t in app_tools.build_tools(app.book, app.bundle)
                if t.name == "customer_behavior")
    out = tool.handler({"customer_id": "CUST-003"})
    assert "disputed" not in json.dumps(out)


def test_a_supplier_bill_may_be_recommended_for_rescheduling(app):
    outcome = _run(app, _scenario("RESCHEDULE"))
    assert outcome.status == "done"
    assert app.book.recommendations()[0]["kind"] == "reschedule"


def test_payroll_is_never_moved(app):
    outcome = _run(app, _scenario("RESCHEDULE-PAYROLL"))
    assert outcome.status == "refused"
    assert "app.payroll_is_never_moved" in _rules(outcome)
    assert not outcome.owner_decisions


def test_a_credit_draw_always_goes_to_the_owner_and_carries_the_rate(app):
    """DEMO 3: borrowing is the owner's call, shown with the rate and its provenance."""
    outcome = _run(app, _scenario("DRAW"))
    assert outcome.status == "done"
    assert "app.credit_draw_needs_owner" in _rules(outcome)
    assert outcome.owner_decisions and outcome.owner_decisions[0]["approved"] is True
    rec = app.book.recommendations()[0]
    assert rec["kind"] == "credit_draw"
    assert rec["rate_name"] == config.CREDIT_LINE_BASE_SERIES
    assert rec["rate_mode"] == "fixture"            # never dressed up as live
    assert rec["interest_30d_cents"] > 0
    assert "not financial advice" in rec["disclaimer"]


def test_a_declined_draw_records_nothing(app):
    outcome = _run(app, _scenario("DRAW-DECLINED"), decline_all=True)
    assert outcome.status == "declined_by_owner"
    assert app.book.recommendations() == []


def test_a_draw_above_the_facility_is_refused_not_held(app):
    outcome = _run(app, _scenario("DRAW-OVER-FACILITY"))
    assert outcome.status == "refused"
    assert not outcome.owner_decisions, "nothing to approve above the facility"
    assert app.book.recommendations() == []


def test_a_recommendation_for_a_fine_week_is_refused(app):
    fine = next(w for w in app.book.weeks() if not w["short"])
    from app import Scenario
    scenario = Scenario(id="X", task_type=config.TASK_PLAN, control="app.recommendation_only_for_short_week",
                        expects="refused",
                        calls=(("recommend_follow_up", {"subject_id": fine["week"], "invoice_id": "INV-3002",
                                                        "reason": "There is nothing wrong but chase them anyway."}),))
    outcome = _run(app, scenario)
    assert outcome.status == "refused"
    assert "app.recommendation_only_for_short_week" in _rules(outcome)


# -- the digest ------------------------------------------------------------------------

def test_the_second_digest_in_a_week_is_refused(app):
    outcome = _run(app, _scenario("DIGEST-TWICE"))
    assert outcome.status == "refused"
    assert "app.one_digest_per_week" in _rules(outcome)
    sent = [m for m in app.bundle.world.outbox if m.get("to") == config.OWNER_EMAIL]
    assert len(sent) == 1, "the first digest went out, the second did not"


# -- cross-app input ----------------------------------------------------------------------

def test_an_extension_event_moves_the_inflow_and_deepens_the_shortfall(app):
    before = app.book.first_short_week()
    rec = app.book.receivable("INV-3002")
    new_due = (app.book.today + timedelta(days=16 + 14)).isoformat()
    changed = app.book.apply_event({"event": "invoice.extension_agreed",
                                    "data": {"invoice": "INV-3002", "new_due": new_due},
                                    "app": "helios-collect"})
    assert changed is True
    assert app.book.receivable("INV-3002")["due_date"] == new_due
    after = app.book.first_short_week()
    assert after["shortfall_cents"] > before["shortfall_cents"] or \
        int(after["week"][1:]) < int(before["week"][1:])
    assert rec["due_date"] != new_due


def test_a_verified_graph_node_overrides_the_fixture_date(app):
    new_due = (app.book.today + timedelta(days=50)).isoformat()
    applied = app.book.apply_graph_nodes([
        {"kind": "invoice", "key": "INV-3001", "provenance": "verified", "attrs": {"due_date": new_due}},
        {"kind": "invoice", "key": "INV-3001", "provenance": "observation", "attrs": {"due_date": "1999-01-01"}},
    ])
    assert applied == 1
    assert app.book.receivable("INV-3001")["due_date"] == new_due


def test_an_unrelated_event_changes_nothing(app):
    assert app.book.apply_event({"event": "something.else", "data": {}}) is False


# -- every scenario matches its expectation -------------------------------------------------

@pytest.mark.parametrize("scenario_id", [s.id for s in scenarios()])
def test_every_scenario_matches_its_expectation(tmp_path, scenario_id):
    scenario = _scenario(scenario_id)
    app = build_app(run_dir=tmp_path / "runs")
    try:
        outcome = _run(app, scenario, decline_all=scenario.owner_declines)
        assert outcome.status == scenario.expects, (scenario_id, outcome.detail)
        assert Path(outcome.action_log_path).exists()
    finally:
        app.close()

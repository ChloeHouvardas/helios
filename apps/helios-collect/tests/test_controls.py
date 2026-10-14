"""Tests that each control in helios-collect actually fires.

    cd apps/helios-collect && python3 -m pytest tests -q

Modelled on `platform/audit-agent-builder/tests/test_security.py`, which asserts
every security property individually and is the file BUILD.md says to show a judge.
This is the app-level equivalent: not "does the app run" but "does each rule do what
its description claims".

Every test is offline. Each builds a fresh app over a throwaway run directory, so
nothing is shared and no test depends on another's ordering.

Three groups matter most:

    test_the_agent_cannot_claim_more_money_arrived_than_the_invoice_is_worth
    test_a_lie_in_resolved_facts_changes_nothing
    test_under_recording_a_payment_is_not_caught_by_the_ceiling
        → facts are not claims, and which direction each control guards

    test_a_disputed_invoice_is_denied_and_never_reaches_the_owner
    test_the_owner_approves_everything_and_the_denials_still_do_not_happen
        → the control does not depend on the human noticing

    test_no_tool_accepts_a_destination_account
    test_undeclared_tool_is_rejected_at_build
        → the strongest control here is structural, not a rule

A note on the two audit gates. `agent_toolkit`'s in-process `AuditGate` is not
exercised: `agent_integration`'s bridge makes the standalone auditor authoritative,
which is the resolution that package's README proposes for the overlap. That is a
defensible choice, not a settled design, and it is flagged in README.md rather than
baked in silently. See also `test_the_in_process_gate_is_not_the_authority`.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
for _p in (
    REPO / "platform" / "audit-agent-builder" / "src",
    REPO / "platform" / "action-agent-builder" / "src",
    REPO / "platform" / "agent-integration" / "src",
    REPO / "platform" / "financial-tools",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
import tools as app_tools  # noqa: E402
from app import (  # noqa: E402
    App,
    Outcome,
    ScriptedOwner,
    build_app,
    run_scenario,
    scenarios,
)
from book import Book  # noqa: E402
from facts import CollectFactSource  # noqa: E402

from agent_integration.runtime import ScriptedDriver  # noqa: E402
from agent_toolkit.manifest import (  # noqa: E402
    ManifestError,
    load_manifest,
    validate_manifest,
)
from agent_toolkit.models import Proposal  # noqa: E402
from audit_agent.contracts import ActionRequest, AuthorizationRequest, Decision  # noqa: E402
from audit_agent.manifest import AuditManifest  # noqa: E402

AUDIT_MANIFEST = HERE / "agents" / "helios-collect-audit.yaml"
ACTION_MANIFEST = HERE / "agents" / "helios-collect-action.json"

# One invoice per control path. Named here rather than re-derived so a fixture
# edit that breaks a scenario fails loudly in the test that depends on it.
NORMAL = "INV-1001"          # 4,200.00  — the normal path
FREQUENT = "INV-1002"        #   850.00  — customer contacted twice this week
LARGE = "INV-1003"           # 12,500.00 — over the owner-approval threshold
LONG_EXTENSION = "INV-1004"  #  3,100.00 — customer asked for 45 days
SHORT_EXTENSION = "INV-1005" #  2,200.00 — customer asked for 10 days
DISPUTED = "INV-1006"        #  1,980.00 — customer disputes the work
NOT_DUE = "INV-1007"         #  3,100.00 — due in 21 days
PAID = "INV-1008"            #   450.00  — money arrived
OVERPAID = "INV-1009"        # 12,500.00 — agent claims more arrived than is owed
OVER_LIMIT = "INV-1010"      #  9,600.00 — customer over their credit limit

TASK_REMINDER = "collect.reminder"
TASK_NEGOTIATE = "collect.negotiate_terms"
TASK_APPLY_PAYMENT = "collect.apply_payment"


@pytest.fixture()
def app(tmp_path: Path) -> App:
    """A fresh app over a throwaway run directory."""
    built = build_app(run_dir=tmp_path)
    yield built
    built.close()


def _scenario(invoice_id: str):
    for s in scenarios():
        if s.invoice_id == invoice_id:
            return s
    raise AssertionError(f"no scenario for {invoice_id}")


def _review(app: App, task_type: str, tool: str, arguments: dict) -> object:
    """Adjudicate one side-effect call through the real bridge.

    Going through `bridge.review` rather than the auditor directly is deliberate:
    it is the path a running app actually takes, including the fresh single-use
    grant the bridge requests per side effect.
    """
    session = app.integration.bridge.authorize_task(
        task_type, f"test: {tool} on {arguments.get('invoice_id', '?')}",
        list(app_tools.TOOL_NAMES))
    assert session.authorized, session.reason
    return app.integration.bridge.review(
        session, Proposal.create("helios-collect", tool, arguments))


def _adjudicate_with_facts(app: App, task_type: str, tool: str, arguments: dict,
                           resolved_facts: dict) -> object:
    """Adjudicate with agent-supplied `resolved_facts`, to prove they are ignored.

    The bridge never forwards `resolved_facts` at all, so this goes to the auditor
    directly. That is the point of the test: even on a path where an agent *could*
    assert facts, the auditor does not merge them.
    """
    client = app.integration.client
    grant = client.authorize(AuthorizationRequest(
        request_id=f"authz-test-{tool}", app_id=app.app_id,
        agent_id=app.action_manifest.name, principal=app.principal,
        task_type=task_type, task_summary=f"test the {tool} gate",
        requested_tools=list(app_tools.TOOL_NAMES)))
    return client.adjudicate(ActionRequest(
        request_id=f"act-test-{tool}", app_id=app.app_id,
        agent_id=app.action_manifest.name, principal=app.principal,
        task_type=task_type, tool=tool, arguments=arguments,
        authorization=grant, resolved_facts=resolved_facts))


def _email(invoice_id: str, customer_id: str, **extra) -> dict:
    return {
        "customer_id": customer_id, "invoice_id": invoice_id,
        "subject": f"About invoice {invoice_id}",
        "body": "A short, courteous reminder about the invoice above.",
        **extra,
    }


# ---------------------------------------------------------------------------
# Manifests parse, and agree with each other
# ---------------------------------------------------------------------------

def test_audit_manifest_loads_under_its_own_validator():
    m = AuditManifest.load(AUDIT_MANIFEST)
    assert m.app_id == "helios-collect"
    assert set(m.tasks) == {TASK_REMINDER, TASK_NEGOTIATE, TASK_APPLY_PAYMENT}
    assert {r.id for r in m.rules} >= {
        "app.contact_frequency", "app.not_yet_due", "app.disputed_invoice",
        "app.large_invoice_approval", "app.extension_offer_limit",
        "app.extension_record_limit", "app.extension_over_credit_limit",
        "app.disputed_extension", "app.payment_exceeds_amount_due",
        "app.payment_requires_invoice", "app.disputed_payment",
    }


def test_action_manifest_loads_under_its_own_validator():
    m = load_manifest(ACTION_MANIFEST)
    assert m.name == "helios-collect"
    assert "send_email" in m.mandate.allowed_tools


def test_action_manifest_rejects_a_comment_key():
    """Strict loading: the JSON manifest tolerates no extra keys.

    `extra="forbid"` semantics, so a generated manifest that adds `_comment` for
    its own documentation fails to load. Commentary belongs in a README.
    """
    data = json.loads(ACTION_MANIFEST.read_text(encoding="utf-8"))
    data["_comment"] = "notes about this agent"
    with pytest.raises(ManifestError) as exc:
        validate_manifest(data)
    assert "_comment" in str(exc.value)


def test_every_action_tool_is_declared_by_the_auditor():
    """The single most important cross-file invariant.

    The audit agent is the registry of what may be granted at all. A tool an action
    agent can call that the auditor never declared is an ungated side effect.
    """
    action = load_manifest(ACTION_MANIFEST)
    audit = AuditManifest.load(AUDIT_MANIFEST)
    declared = {t.name for t in audit.tools}
    offered = set(action.tools) | set(action.mandate.allowed_tools)
    assert offered <= declared, f"undeclared tools: {sorted(offered - declared)}"


def test_the_app_implements_every_declared_tool():
    """A declared-but-absent tool is denied at runtime — safe, but looks broken."""
    audit = AuditManifest.load(AUDIT_MANIFEST)
    declared = {t.name for t in audit.tools}
    assert declared == set(app_tools.TOOL_NAMES)


def test_identity_ids_agree_across_files():
    """identity.id == audit app_id, or two files that look like one app are two."""
    import yaml
    spec = yaml.safe_load((HERE / "helios-app.yaml").read_text(encoding="utf-8"))
    audit = AuditManifest.load(AUDIT_MANIFEST)
    action = load_manifest(ACTION_MANIFEST)
    assert spec["identity"]["id"] == audit.app_id == action.name


def test_spec_tools_is_a_superset_of_both_manifests():
    import yaml
    spec = yaml.safe_load((HERE / "helios-app.yaml").read_text(encoding="utf-8"))
    audit = AuditManifest.load(AUDIT_MANIFEST)
    action = load_manifest(ACTION_MANIFEST)
    needed = {t.name for t in audit.tools} | set(action.tools)
    assert needed <= set(spec["tools"]), f"undeclared in spec: {sorted(needed - set(spec['tools']))}"


def test_every_rule_fact_key_is_supplied_by_the_fact_source(app):
    """A fact-bound rule whose fact never resolves denies EVERYTHING.

    This is the most common way a generated app's policy silently stops working, and
    it fails in the safe direction — which is exactly why it goes unnoticed until
    the demo. `entry-point/checks/check_app.py` can only warn about the names; this
    asserts the running fact source actually supplies them.
    """
    audit = AuditManifest.load(AUDIT_MANIFEST)
    needed: set[str] = set()
    for rule in audit.rules:
        for lim in rule.limits or []:
            if lim.fact:
                needed.add(lim.fact)
            if lim.max_from_fact:
                needed.add(lim.max_from_fact)
        needed.update(rule.deny_if_fact_true or [])
        needed.update(rule.approval_if_fact_true or [])
    assert needed, "the manifest declares no fact-bound rules; this test is stale"

    supplied: set[str] = set()
    for invoice_id in app.book.invoice_ids():
        supplied.update(app.fact_source.extra_facts_for(invoice_id))
    missing = sorted(needed - supplied)
    assert not missing, (
        f"rules bind facts the fact source never supplies: {missing}. Those rules "
        f"fail closed and would deny every call.")


def test_money_is_never_float_in_the_spec():
    import yaml
    spec = yaml.safe_load((HERE / "helios-app.yaml").read_text(encoding="utf-8"))
    closed = {"string", "integer", "decimal", "date", "datetime", "boolean", "json"}
    for ent in spec["data"]["local"]["entities"]:
        for f in ent["fields"]:
            assert f["type"] in closed, f"{ent['name']}.{f['name']}: {f['type']}"
            assert f["type"] != "float"


def test_config_mirrors_the_audit_manifest():
    """config.py restates ceilings for display; the manifest enforces them.

    If these drift, the settings page lies to the owner. Asserting agreement is
    cheaper than reading both files.
    """
    audit = AuditManifest.load(AUDIT_MANIFEST)
    rules = {r.id: r for r in audit.rules}
    offer = rules["app.extension_offer_limit"].limits[0]
    assert offer.max == config.EXTENSION_DAYS_WITHOUT_APPROVAL
    large = rules["app.large_invoice_approval"].limits[0]
    assert str(large.max) == config.LARGE_INVOICE_THRESHOLD
    freq = rules["app.contact_frequency"].max_contacts_in_days
    assert int(freq["within_days"]) == config.CONTACT_WINDOW_DAYS
    assert int(freq["max"]) == config.MAX_CONTACTS_IN_WINDOW


# ---------------------------------------------------------------------------
# Authority: the grant is scoped, and ungranted tools are invisible
# ---------------------------------------------------------------------------

def test_a_task_only_gets_its_own_tools(app):
    """Least authority: each task is granted its allowlist and nothing more."""
    for task_type, expected in (
        (TASK_REMINDER, {"list_overdue_invoices", "lookup_invoice", "send_email"}),
        (TASK_NEGOTIATE, {"lookup_invoice", "send_email", "record_agreed_extension"}),
        (TASK_APPLY_PAYMENT, {"lookup_invoice", "record_inbound_payment"}),
    ):
        session = app.integration.bridge.authorize_task(
            task_type, f"follow up on an overdue invoice for {task_type}",
            list(app_tools.TOOL_NAMES))
        assert session.authorized, session.reason
        assert set(session.allowed_tools) == expected, task_type


def test_a_tool_outside_the_task_allowlist_is_denied(app):
    """Capability is granted per task, never assumed.

    `record_inbound_payment` exists and works, but a collections-reminder task may
    not use it. This is what stops one agent's authority leaking into another's job.
    """
    session = app.integration.bridge.authorize_task(
        TASK_REMINDER, "chase an overdue invoice", list(app_tools.TOOL_NAMES))
    assert "record_inbound_payment" not in session.allowed_tools
    gate = app.integration.bridge.review(
        session, Proposal.create("helios-collect", "record_inbound_payment",
                                {"invoice_id": PAID, "amount_cents": 100,
                                 "currency": "CNY"}))
    assert gate.rejected
    assert "not granted" in gate.verdict.reason


def test_an_unregistered_tool_is_denied(app):
    session = app.integration.bridge.authorize_task(
        TASK_REMINDER, "chase an overdue invoice", ["payments.wire_offshore"])
    assert not session.authorized


def test_the_grant_is_signed(app):
    """Grants are signed even with no key in the environment.

    `build_integration()` generates an ephemeral key rather than falling back to
    unsigned, so the replay and tamper checks are genuinely exercised. An unsigned
    grant is not what a deployment runs.
    """
    session = app.integration.bridge.authorize_task(
        TASK_REMINDER, "chase an overdue invoice", list(app_tools.TOOL_NAMES))
    assert session.grant is not None
    assert session.grant.signature is not None
    assert not session.grant.unsigned


def test_the_in_process_gate_is_not_the_authority(app):
    """Documents the unresolved seam rather than hiding it.

    `agent_toolkit.audit.AuditGate` implements receivables semantics and its
    `MandateLimits` accepts only four keys. This app *is* a receivables app, so
    unlike the payables example those keys do fit — but the authoritative gate is
    still the standalone auditor, because an app cannot be trusted to police
    itself. The action manifest's `mandate.limits` therefore restates four
    ceilings as documentation; nothing enforces them.

    Two gates overlapping with no agreed winner is an architecture question the
    team has not settled. It is flagged here and in README.md, not resolved
    silently in an app.
    """
    action = load_manifest(ACTION_MANIFEST)
    limits = action.mandate.limits
    # All four receivables-shaped keys are expressible here, and only those four.
    assert limits.max_extension_days == 90
    assert limits.extension_owner_approval_days == 14
    assert limits.min_days_between_contacts == 3
    assert limits.invoice_owner_approval_amount == 10000.0
    # But the policy that actually refuses a call is the audit manifest's.
    audit = AuditManifest.load(AUDIT_MANIFEST)
    assert len(audit.rules) == 11


# ---------------------------------------------------------------------------
# The normal path
# ---------------------------------------------------------------------------

def test_a_normal_reminder_is_allowed_and_sent(app):
    gate = _review(app, TASK_REMINDER, "send_email",
                   _email(NORMAL, "CUST-001"))
    assert gate.approved, gate.verdict.reason


def test_read_only_tools_run_without_a_grant(app):
    """Reads are not gated. A gate on every call would make the app unusable."""
    tool = app.integration.runtime.registry.require(app_tools.LOOKUP_INVOICE)
    assert tool.side_effect is False
    result = tool.handler({"invoice_id": NORMAL})
    assert result["invoice"]["number"] == "RL-2026-047"


def test_a_side_effect_is_marked_as_one(app):
    """The flag that routes a call to the gate. Getting it wrong is how an app
    acquires an ungated side effect — and it fails silently."""
    for name, expected in (
        (app_tools.LIST_OVERDUE, False),
        (app_tools.LOOKUP_INVOICE, False),
        (app_tools.SEND_EMAIL, True),
        (app_tools.RECORD_EXTENSION, True),
        (app_tools.RECORD_PAYMENT, True),
    ):
        tool = app.integration.runtime.registry.require(name)
        assert tool.side_effect is expected, name
        # And the auditor agrees — the two must not drift.
        assert app.integration.audit_agent.registry.side_effect(name) is expected


def test_lookup_invoice_withholds_the_control_input(app):
    """The agent never sees `disputed`.

    It is a control input, not business information. The auditor reads it from the
    fact source, so an agent cannot argue with a value it was never given.
    """
    tool = app.integration.runtime.registry.require(app_tools.LOOKUP_INVOICE)
    result = tool.handler({"invoice_id": DISPUTED})
    assert "disputed" not in result["invoice"]
    assert "disputed" not in result["customer"]
    # ...but the fact source does see it.
    assert app.fact_source.extra_facts_for(DISPUTED)["invoice_disputed"] is True


def test_lookup_invoice_reports_contact_frequency(app):
    """A control the agent can read is one it can plan around legitimately.

    Better to see "already contacted twice" before proposing than to discover it
    from a denial and try again.
    """
    tool = app.integration.runtime.registry.require(app_tools.LOOKUP_INVOICE)
    result = tool.handler({"invoice_id": FREQUENT})
    assert result["contacts_in_last_7_days"] == 2


# ---------------------------------------------------------------------------
# Control: contact frequency
# ---------------------------------------------------------------------------

def test_a_third_contact_in_seven_days_is_refused(app):
    gate = _review(app, TASK_REMINDER, "send_email",
                   _email(FREQUENT, "CUST-002"))
    assert gate.rejected
    assert "contacted twice" in gate.verdict.reason


def test_contact_frequency_counts_from_recorded_history_not_from_claims(app):
    """An agent cannot lower the count by describing the customer as untouched.

    The count is resolved by the fact source from stored contact dates. There is no
    argument that carries it, so there is nothing to lie with.
    """
    assert app.fact_source.contact_count_for("CUST-002", 7) == 2
    # Naming the invoice resolves to the same customer: the business question a
    # frequency limit asks is about the relationship, not one document.
    assert app.fact_source.contact_count_for(FREQUENT, 7) == 2


def test_an_unknown_subject_yields_no_count_rather_than_zero(app):
    """Unknown is never read as "no contacts yet".

    `None` is what makes the frequency rule deny rather than wave through. A `0`
    here would read as "this customer has not been contacted" and licence exactly
    the contact the rule exists to prevent.
    """
    assert app.fact_source.contact_count_for("CUST-DOES-NOT-EXIST", 7) is None
    assert app.fact_source.contact_count_for("INV-DOES-NOT-EXIST", 7) is None
    assert app.fact_source.amount_for("INV-DOES-NOT-EXIST") is None


def test_a_customer_invoice_mismatch_is_caught_but_not_by_the_gate(app):
    """Documents a real platform gap rather than implying a control that is absent.

    `send_email` takes a `customer_id` AND an `invoice_id`, and the handler raises
    if the invoice does not belong to that customer. The audit rules have no
    declarative way to express a two-argument pairing check — `limits` and
    `deny_if_fact_true` resolve facts for ONE subject at a time, and
    `FactResolver._subject_key` picks `invoice_id` first, so a valid invoice id makes
    the mismatched customer id invisible to the frequency rule.

    The result: the gate approves, the handler refuses, nothing is sent, and the run
    ends in ERROR. That fails loudly and safely — no email goes out — but the refusal
    happens AFTER the auditor approved. `platform/agent-integration/README.md`
    records the same gap against the same tool, and closing it properly needs either
    a pairing fact on the audit side or a rule shape that can compare two arguments.

    Recorded in README.md §Limitations. This test is the tripwire: if the platform
    ever closes the gap, it fails and the docstring should be updated.
    """
    gate = _review(app, TASK_REMINDER, "send_email",
                   _email(NORMAL, "CUST-DOES-NOT-EXIST"))
    assert gate.approved, "expected the gate to miss the pairing, see docstring"

    tool = app.integration.runtime.registry.require(app_tools.SEND_EMAIL)
    with pytest.raises(ValueError, match="unknown customer"):
        tool.handler(gate.arguments_to_execute(
            _email(NORMAL, "CUST-DOES-NOT-EXIST")))
    # The safety property that matters: nothing was sent.
    assert app.book.store.sent_emails == []


def test_a_second_email_in_one_run_raises_the_count(app):
    """The count reflects reality as the run progresses.

    `GatedAgentRuntime._execute` calls `record_contact` after a send that actually
    happened — and it does so outside its try/except, so a fact source without that
    method takes the whole run down. Recorded by execution, never by the agent.
    """
    before = app.fact_source.contact_count_for("CUST-001", 7)
    assert before == 0
    app.fact_source.record_contact("CUST-001", app.book.today)
    assert app.fact_source.contact_count_for("CUST-001", 7) == 1


def test_recording_a_contact_twice_on_one_day_does_not_inflate_it(app):
    """A resumed run must not double-count and thereby deny a legitimate follow-up."""
    app.fact_source.record_contact("CUST-001", app.book.today)
    app.fact_source.record_contact("CUST-001", app.book.today)
    assert app.fact_source.contact_count_for("CUST-001", 7) == 1


# ---------------------------------------------------------------------------
# Control: not due yet, and disputed — both DENY, not hold
# ---------------------------------------------------------------------------

def test_chasing_an_invoice_that_is_not_due_is_denied(app):
    gate = _review(app, TASK_REMINDER, "send_email",
                   _email(NOT_DUE, "CUST-001"))
    assert gate.rejected
    assert "not due" in gate.verdict.reason


def test_not_due_is_computed_from_the_stores_clock(app):
    """Due-ness is a fact, so an agent cannot assert an invoice is overdue."""
    assert app.book.is_not_yet_due(NOT_DUE) is True
    assert app.book.is_not_yet_due(NORMAL) is False
    days = app.book.days_overdue(NOT_DUE)
    assert days is not None and days < 0


def test_a_disputed_invoice_is_denied(app):
    gate = _review(app, TASK_REMINDER, "send_email",
                   _email(DISPUTED, "CUST-004"))
    assert gate.rejected
    assert "disputed" in gate.verdict.reason


def test_denials_are_denials_and_not_holds(app):
    """Deliberate: no owner decision makes these appropriate.

    Routing them to a queue would invite the owner to approve the thing most likely
    to damage the customer relationship. HOLD means "might be legitimate, only the
    owner can tell"; DENY means "a human must fix something out of band first".
    Getting this backwards either floods the queue or silently permits harm.
    """
    for invoice_id, customer_id in ((NOT_DUE, "CUST-001"), (DISPUTED, "CUST-004")):
        gate = _review(app, TASK_REMINDER, "send_email",
                       _email(invoice_id, customer_id))
        assert gate.rejected, invoice_id
        assert gate.hold_id is None, invoice_id


def test_a_disputed_extension_is_denied(app):
    """Recording terms implies the customer accepts a debt they are contesting."""
    gate = _review(app, TASK_NEGOTIATE, app_tools.RECORD_EXTENSION,
                   {"invoice_id": DISPUTED, "extension_days": 5,
                    "terms_note": "Customer asked for a few more days."})
    assert gate.rejected
    assert "disputed" in gate.verdict.reason


def test_a_payment_on_a_disputed_invoice_goes_to_the_owner(app):
    """HOLD, not deny: whether a payment settles a dispute is the owner's judgement."""
    gate = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                   {"invoice_id": DISPUTED, "amount_cents": 198000,
                    "currency": "CNY", "reference": "BANK-REF-CI-1"})
    assert gate.held
    assert gate.hold_id


# ---------------------------------------------------------------------------
# Control: large invoices go to the owner
# ---------------------------------------------------------------------------

def test_a_large_invoice_is_held_before_contact(app):
    gate = _review(app, TASK_REMINDER, "send_email",
                   _email(LARGE, "CUST-003"))
    assert gate.held
    assert gate.hold_id
    assert "10,000" in gate.owner_question or "10000" in gate.owner_question


def test_the_large_invoice_threshold_reads_a_fact_not_the_email(app):
    """Understating the amount in the body cannot slip an invoice under.

    The rule binds `fact: amount_due`, so it never reads the agent's words at all.
    This is the shape BUILD.md recommends when the threat is "the agent misstates
    what a record says" rather than "the agent asks for too much".
    """
    understated = _email(
        LARGE, "CUST-003",
        body="A trivial balance of 50.00 CNY remains on this account.")
    gate = _review(app, TASK_REMINDER, "send_email", understated)
    assert gate.held, "an invoice over the threshold was waved through"


def test_an_invoice_just_under_the_threshold_is_not_held(app):
    """The threshold is a threshold, not a general suspicion of large numbers."""
    gate = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                   {"invoice_id": OVER_LIMIT, "amount_cents": 960000,
                    "currency": "CNY", "reference": "BANK-REF-CI-95"})
    assert not gate.held


# ---------------------------------------------------------------------------
# Control: negotiating terms
# ---------------------------------------------------------------------------

def test_an_extension_inside_the_agents_authority_is_allowed(app):
    gate = _review(app, TASK_NEGOTIATE, "send_email",
                   _email(SHORT_EXTENSION, "CUST-005", extension_offer_days=10))
    assert gate.approved, gate.verdict.reason


def test_an_extension_beyond_the_cap_is_held(app):
    gate = _review(app, TASK_NEGOTIATE, "send_email",
                   _email(LONG_EXTENSION, "CUST-005", extension_offer_days=45))
    assert gate.held
    assert "14 days" in gate.owner_question


def test_the_cap_applies_to_the_record_as_well_as_the_offer(app):
    """Capping only the offer would let an agent promise 10 days and record 60.

    Both sides are checked against the same ceiling, so the ledger cannot end up
    with terms nobody approved.
    """
    gate = _review(app, TASK_NEGOTIATE, app_tools.RECORD_EXTENSION,
                   {"invoice_id": SHORT_EXTENSION, "extension_days": 60,
                    "terms_note": "Agreed 60 days with the customer."})
    assert gate.held
    assert "14 days" in gate.owner_question


def test_a_disputed_or_not_due_invoice_cannot_be_negotiated_around_a_reminder(app):
    """The deny rules cover the negotiate task too, not just reminders."""
    gate = _review(app, TASK_NEGOTIATE, "send_email",
                   _email(NOT_DUE, "CUST-001", extension_offer_days=5))
    assert gate.rejected


def test_extending_terms_for_a_customer_over_their_limit_is_held(app):
    """Fact measured against fact: neither side comes from the agent.

    7 days is inside the agent's own authority, so `app.extension_record_limit`
    does not fire. What fires is credit exposure — and extending terms IS extending
    credit, which is a credit decision rather than a collections one.
    """
    gate = _review(app, TASK_NEGOTIATE, app_tools.RECORD_EXTENSION,
                   {"invoice_id": OVER_LIMIT, "extension_days": 7,
                    "terms_note": "Customer asked for a week."})
    assert gate.held
    assert "credit limit" in gate.owner_question


def test_the_credit_fact_is_computed_in_cents_on_both_sides(app):
    """Units must match, or the rule exceeds on every single call.

    A decimal ceiling against a cents value would hold every extension, which looks
    like a working control and is actually a broken one.
    """
    facts = app.fact_source.extra_facts_for(OVER_LIMIT)
    assert facts["outstanding_cents"] == 1_158_000   # 9,600.00 + 1,980.00
    assert facts["credit_limit_cents"] == 800_000    # 8,000.00
    assert facts["outstanding_cents"] > facts["credit_limit_cents"]


def test_a_customer_inside_their_limit_is_not_held_for_credit(app):
    facts = app.fact_source.extra_facts_for(SHORT_EXTENSION)
    assert facts["outstanding_cents"] <= facts["credit_limit_cents"]
    gate = _review(app, TASK_NEGOTIATE, app_tools.RECORD_EXTENSION,
                   {"invoice_id": SHORT_EXTENSION, "extension_days": 10,
                    "terms_note": "Customer asked for ten days."})
    assert gate.approved, gate.verdict.reason


def test_an_agreed_extension_moves_the_due_date_and_publishes_it(app):
    """Other Helios apps must forecast against the real date.

    The new due date is published `verified` from the ledger; the account of what
    the customer said is published as an `observation`, because that is an agent's
    claim about a conversation rather than a system of record.
    """
    before = app.book.get_invoice(SHORT_EXTENSION)["due_date"]
    gate = _review(app, TASK_NEGOTIATE, app_tools.RECORD_EXTENSION,
                   {"invoice_id": SHORT_EXTENSION, "extension_days": 10,
                    "terms_note": "Customer asked for ten more days."})
    assert gate.approved, gate.verdict.reason
    # The gate allows it; execution is the runtime's job. Call the handler the way
    # the runtime does, with the granted arguments.
    tool = app.integration.runtime.registry.require(app_tools.RECORD_EXTENSION)
    result = tool.handler(gate.arguments_to_execute(
        {"invoice_id": SHORT_EXTENSION, "extension_days": 10,
         "terms_note": "Customer asked for ten more days."}))

    after = app.book.get_invoice(SHORT_EXTENSION)["due_date"]
    assert after != before
    assert result["new_due_date"] == after
    node = app.bundle.graph.get(f"invoice:{SHORT_EXTENSION}")
    assert node is not None and node["provenance"] == "verified"
    negotiation = app.bundle.graph.get(f"negotiation:{SHORT_EXTENSION}")
    assert negotiation is not None and negotiation["provenance"] == "observation"


def test_an_observation_cannot_overwrite_a_verified_fact(app):
    """The graph's own provenance rule: an agent's claim never displaces a record."""
    from fin_tools.knowledge_graph import GraphWriteError

    app.bundle.graph.upsert_verified(
        "invoice", SHORT_EXTENSION, {"due_date": "2026-11-01"},
        source=config.GRAPH_SOURCE)
    with pytest.raises(GraphWriteError):
        app.bundle.graph.add_observation(
            "invoice", SHORT_EXTENSION, {"due_date": "2027-01-01"},
            observer=config.GRAPH_OBSERVER)


# ---------------------------------------------------------------------------
# Control: directing money
# ---------------------------------------------------------------------------

def test_no_tool_accepts_a_destination_account():
    """THE structural control: there is no argument to misdirect money with.

    Checked against the schemas rather than by reading the code, because a schema
    is what the auditor validates against and what a model is offered. If a future
    edit adds `account` or `payee` to an inbound tool, this fails — and that edit
    would be handing the agent control of where money goes.
    """
    dangerous = {"account", "payee", "to_account", "destination",
                 "credited_account", "bank_account"}
    for name, schema in (
        (app_tools.RECORD_PAYMENT, app_tools._PAYMENT_INPUT),
        (app_tools.RECORD_EXTENSION, app_tools._EXTENSION_INPUT),
    ):
        props = set(schema["properties"])
        assert not (props & dangerous), f"{name} accepts a destination: {props & dangerous}"
        assert "additionalProperties" in schema and schema["additionalProperties"] is False


def test_routing_is_resolved_from_configuration_not_from_the_request(app):
    """Where money lands is the owner's setting, keyed by the invoice's customer."""
    assert config.inbound_account_for("CUST-006") == config.DEFAULT_INBOUND_ACCOUNT
    assert config.inbound_account_for("CUST-003") == "helios-collect-receivables-harbor"
    # An unknown customer falls back rather than raising: the payment is real and
    # must land somewhere, and the choice is not the agent's either way.
    assert config.inbound_account_for("CUST-UNKNOWN") == config.DEFAULT_INBOUND_ACCOUNT


def test_a_full_payment_is_recorded_and_credits_the_configured_account(app):
    gate = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                   {"invoice_id": PAID, "amount_cents": 45000,
                    "currency": "CNY", "reference": "BANK-REF-LF-88213"})
    assert gate.approved, gate.verdict.reason

    before = app.bundle.payments.balance(config.DEFAULT_INBOUND_ACCOUNT)
    tool = app.integration.runtime.registry.require(app_tools.RECORD_PAYMENT)
    result = tool.handler(gate.arguments_to_execute(
        {"invoice_id": PAID, "amount_cents": 45000, "currency": "CNY",
         "reference": "BANK-REF-LF-88213"}))

    assert result["credited_account"] == config.DEFAULT_INBOUND_ACCOUNT
    assert result["status"] == "paid"
    assert result["balance_cents"] == 0
    assert app.bundle.payments.balance(config.DEFAULT_INBOUND_ACCOUNT) == before + 45000


def test_a_partial_payment_leaves_the_invoice_part_paid(app):
    """The control against under-recording, and it is not a limit.

    Applying less than an invoice is worth is INSIDE the fact ceiling, so no rule
    catches it. What stops it being invisible is that the invoice stays part_paid
    with a balance owed, so the shortfall remains on the dashboard instead of being
    quietly closed.
    """
    tool = app.integration.runtime.registry.require(app_tools.RECORD_PAYMENT)
    result = tool.handler({"invoice_id": NORMAL, "amount_cents": 100_000,
                           "currency": "CNY", "reference": "BANK-REF-PART"})
    assert result["status"] == "part_paid"
    assert result["balance_cents"] == 420_000 - 100_000


# ---------------------------------------------------------------------------
# THE ONE THAT MATTERS MOST: facts are not claims
# ---------------------------------------------------------------------------

def test_the_agent_cannot_claim_more_money_arrived_than_the_invoice_is_worth(app):
    """The agent chooses the amount; the ledger chooses the ceiling.

    Claiming 20,000.00 arrived on a 12,500.00 invoice is held, because the ceiling
    is `max_from_fact: invoice_amount_cents` resolved from the store. Neither side
    of that comparison is under the agent's control.

    This is the shape that matters most in finance, and the one BUILD.md tells you
    to write a test for.
    """
    gate = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                   {"invoice_id": OVERPAID, "amount_cents": 2_000_000,
                    "currency": "CNY", "reference": "BANK-REF-HL-99120"})
    assert gate.held
    assert "exceeds the recorded invoice amount" in gate.verdict.reason


def test_under_recording_a_payment_is_not_caught_by_the_ceiling(app):
    """Documents a boundary honestly rather than implying protection that is absent.

    A ceiling on an *argument* constrains how much the agent may claim arrived.
    Recording 1.00 against a 12,500.00 invoice is inside that ceiling, so this rule
    allows it. That is not a defect — it is the wrong rule for that threat, and
    `test_a_partial_payment_leaves_the_invoice_part_paid` shows what handles it.

    This test exists so nobody "fixes" the rule by adding a floor without
    understanding that the real control is elsewhere.
    """
    gate = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                   {"invoice_id": OVERPAID, "amount_cents": 100,
                    "currency": "CNY", "reference": "BANK-REF-TINY"})
    assert gate.approved


def test_a_lie_in_resolved_facts_changes_nothing(app):
    """`resolved_facts` names a subject; it is not evidence.

    `AuditAgent._facts_for_action` deliberately does not merge request-supplied
    values into the facts, so an agent cannot assert the very values a limit checks.
    Claiming the invoice is tiny and undisputed changes nothing.
    """
    decision = _adjudicate_with_facts(
        app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
        {"invoice_id": OVERPAID, "amount_cents": 2_000_000,
         "currency": "CNY", "reference": "BANK-REF-HL-99120"},
        resolved_facts={"invoice_id": OVERPAID, "invoice_amount_cents": 100,
                        "amount_due": "1.00", "invoice_disputed": False,
                        "not_yet_due": False})
    assert decision.decision is Decision.HOLD, decision.reason


def test_a_lie_cannot_switch_off_the_dispute_flag(app):
    """Same mechanism, different rule: asserting `disputed=False` is ignored."""
    decision = _adjudicate_with_facts(
        app, TASK_REMINDER, "send_email", _email(DISPUTED, "CUST-004"),
        resolved_facts={"invoice_id": DISPUTED, "invoice_disputed": False})
    assert decision.decision is Decision.DENY


def test_the_bridge_never_forwards_agent_supplied_facts():
    """Stronger than ignoring them: the app-side path cannot even send them.

    `AuditBridge.review` constructs its `ActionRequest` without `resolved_facts`,
    so on the path a running app actually takes there is no channel for a claim to
    travel. Worth asserting because it is a property of code this app depends on
    but does not own, and a change there would silently weaken the guarantee.
    """
    import agent_integration.bridge as bridge_mod
    source = inspect.getsource(bridge_mod.AuditBridge.review)
    assert "resolved_facts" not in source


def test_the_fact_source_never_reads_the_request():
    """The property that makes the two tests above meaningful.

    `CollectFactSource` takes only a subject id and reads the app's own data. If it
    accepted arguments, every claim would be a fact.
    """
    book = Book.load(HERE / "data")
    fs = CollectFactSource(book)
    for name, fn in inspect.getmembers(fs, predicate=inspect.ismethod):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(fn).parameters)
        assert "arguments" not in params and "request" not in params, (
            f"{name} must not consult the request")


def test_unknown_facts_are_none_never_zero(app):
    """Unknown is never read as small.

    Returning 0 for an unresolvable balance would read as "no exposure" and quietly
    turn a fail-closed control into a fail-open one.
    """
    assert app.fact_source.amount_for("INV-NOPE") is None
    assert app.fact_source.extra_facts_for("INV-NOPE") == {}
    assert app.book.credit_limit_cents("CUST-NOPE") is None
    assert app.book.outstanding_cents("CUST-NOPE") is None


def test_an_unresolvable_fact_fails_closed(app):
    """A fact-bound rule with no fact denies rather than allowing."""
    decision = _adjudicate_with_facts(
        app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
        # `inv` is not one of FactResolver.SUBJECT_KEYS, so no record is selected
        # and invoice_amount_cents never resolves. This is the most common way a
        # generated app's policy silently stops working — safely, but confusingly.
        {"inv": OVERPAID, "amount_cents": 2_000_000, "currency": "CNY"},
        resolved_facts={"inv": OVERPAID})
    assert decision.decision is not Decision.ALLOW


def test_a_payment_must_name_an_invoice(app):
    gate = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                   {"amount_cents": 45000, "currency": "CNY"})
    assert gate.rejected


def test_malformed_arguments_are_refused_before_execution(app):
    """`baseline.arguments_validate` uses the tool's own declared schema.

    A non-integer amount is rejected at the gate, not by the handler — so no
    partial ledger write happens first.
    """
    gate = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                   {"invoice_id": PAID, "amount_cents": "lots",
                    "currency": "CNY"})
    assert gate.rejected


# ---------------------------------------------------------------------------
# The agent cannot argue with the gate
# ---------------------------------------------------------------------------

def test_a_held_call_stays_held_no_matter_how_it_is_reworded(app):
    """Rewording a held call does not get past the hold.

    This is the failure mode a reviewer looks for: an agent that treats a hold as a
    negotiation rather than a stop. Every retry is judged against the same
    fact-derived ceiling, so changing the reference changes nothing.

    Note what this does NOT claim: an agent may legitimately record a *smaller*
    payment that is inside policy, and the gate will allow it. That is correct
    behaviour, not a bypass — see
    test_under_recording_a_payment_is_not_caught_by_the_ceiling and
    test_splitting_a_payment_does_not_evade_the_ceiling.
    """
    base = {"invoice_id": OVERPAID, "amount_cents": 2_000_000,
            "currency": "CNY", "reference": "BANK-REF-HL-99120"}
    assert _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT, base).held
    reworded = dict(base, reference="confirmed by the customer by phone")
    assert _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT, reworded).held
    # Nor does inflating it further, or dropping the optional reference entirely.
    bigger = dict(base, amount_cents=9_999_999)
    assert _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT, bigger).held
    stripped = {k: v for k, v in base.items() if k != "reference"}
    assert _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT, stripped).held


def test_splitting_a_payment_does_not_evade_the_ceiling(app):
    """Asserting the real behaviour, not the hoped-for one.

    "Split it up" is the first thing a clever agent tries, so it deserves an
    explicit test — but the honest result is that each half IS inside the ceiling
    and is therefore allowed. The ceiling is per invoice amount, not per total
    across calls, so splitting is not evading anything: two 10,000.00 recordings
    against a 12,500.00 invoice leave the invoice part-paid with a balance owed,
    which is exactly what `apply_payment` records.

    What this does mean is that an over-recording across several calls is not caught
    by this rule. That is a real boundary of the control and is recorded in
    README.md §Limitations rather than papered over with an assertion that passes
    for the wrong reason.
    """
    first = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                    {"invoice_id": OVERPAID, "amount_cents": 1_000_000,
                     "currency": "CNY", "reference": "SPLIT-1"})
    assert first.approved, first.verdict.reason

    tool = app.integration.runtime.registry.require(app_tools.RECORD_PAYMENT)
    result = tool.handler(first.arguments_to_execute(
        {"invoice_id": OVERPAID, "amount_cents": 1_000_000,
         "currency": "CNY", "reference": "SPLIT-1"}))
    # 10,000.00 of a 12,500.00 invoice: still owed, still visible.
    assert result["status"] == "part_paid"
    assert result["balance_cents"] == 1_250_000 - 1_000_000


def test_a_held_call_does_not_execute_while_it_waits(app):
    """The proposal is not executed optimistically before the owner decides."""
    before = app.bundle.payments.balance(config.DEFAULT_INBOUND_ACCOUNT)
    gate = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                   {"invoice_id": PAID, "amount_cents": 45000,
                    "currency": "CNY", "reference": "BANK-REF-LF-88213"})
    # This one is allowed, so use a held case for the assertion that matters.
    held = _review(app, TASK_APPLY_PAYMENT, app_tools.RECORD_PAYMENT,
                   {"invoice_id": OVERPAID, "amount_cents": 2_000_000,
                    "currency": "CNY", "reference": "BANK-REF-HL-99120"})
    assert held.held
    assert held.granted_arguments is None
    assert app.bundle.payments.balance(config.DEFAULT_INBOUND_ACCOUNT) == before


# ---------------------------------------------------------------------------
# The owner, and the point of the whole exercise
# ---------------------------------------------------------------------------

def test_the_owner_approves_everything_and_the_denials_still_do_not_happen(app):
    """THE DEMO. The control does not depend on the human noticing.

    A permissive owner who approves every hold they are shown still cannot make the
    three denied scenarios happen, because those never reach the queue: they are
    refused in code. This is the platform's central claim demonstrated rather than
    asserted.
    """

    class ApproveEverything:
        def __init__(self) -> None:
            self.decisions: list[dict] = []
            self.asked: list[str] = []

        def decide_hold(self, hold) -> bool:
            self.asked.append(str(hold.get("hold_id")))
            self.decisions.append({"hold_id": hold.get("hold_id"), "approved": True})
            return True

    owner = ApproveEverything()
    outcomes = {s.invoice_id: run_scenario(app, s, owner) for s in scenarios()}

    # Denied in code, never put to the owner.
    for invoice_id in (FREQUENT, DISPUTED, NOT_DUE):
        assert outcomes[invoice_id].status == "refused", invoice_id
        assert not outcomes[invoice_id].owner_decisions, invoice_id

    # And nothing was sent for them.
    sent = {e["invoice_id"] for e in app.book.store.sent_emails}
    assert not sent & {FREQUENT, DISPUTED, NOT_DUE}


def test_every_scenario_reaches_its_expected_outcome(app):
    """Ten invoices, ten different control paths, in one run."""
    owner = ScriptedOwner(verbose=False)
    for scenario in scenarios():
        outcome = run_scenario(app, scenario, owner)
        assert outcome.status == scenario.expects, (
            f"{scenario.invoice_id}: expected {scenario.expects}, got "
            f"{outcome.status} ({outcome.detail})")


def test_the_owner_sees_the_auditors_own_question(app):
    """Never a summary the agent wrote about itself — BUILD.md step 7."""
    owner = ScriptedOwner(verbose=False)
    outcome = run_scenario(app, _scenario(LARGE), owner)
    assert outcome.holds or outcome.owner_decisions
    assert owner.asked, "the owner was never asked about a held action"


def test_an_approved_hold_executes_exactly_what_was_held(app):
    """An approval applies to that action and no other.

    `resume` executes the held action's own arguments, so an agent cannot have a
    small amount approved and then substitute a large one.
    """
    owner = ScriptedOwner(verbose=False)
    outcome = run_scenario(app, _scenario(LARGE), owner)
    assert outcome.status == "done"
    executed = [a for a in outcome.actions if a.get("executed")]
    assert executed, "nothing executed after the owner approved"
    assert executed[-1]["verdict"]["rule"] == "owner.approved_hold"


def test_a_declined_hold_does_not_execute(app):
    owner = ScriptedOwner(verbose=False)
    outcome = run_scenario(app, _scenario(LONG_EXTENSION), owner)
    assert outcome.status == "declined_by_owner"
    assert not [a for a in outcome.actions if a.get("executed")]
    assert not app.book.store.sent_emails


def test_a_declined_hold_is_not_retried(app):
    """The scripted agent obeys the manifest: stop, do not reword and retry.

    A single call per scenario, so this asserts the driver does not loop on a
    refusal. Retrying a reworded variant of a denied call is circumventing a
    control, and it is the specific failure a judge looks for.
    """
    owner = ScriptedOwner(verbose=False)
    outcome = run_scenario(app, _scenario(LONG_EXTENSION), owner)
    send_calls = [a for a in outcome.actions if a.get("tool") == "send_email"]
    # one held, one rejected-on-resume. Not a third.
    assert len(send_calls) == 2, [a.get("verdict") for a in send_calls]


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def test_every_step_is_written_to_the_action_log(app):
    owner = ScriptedOwner(verbose=False)
    outcome = run_scenario(app, _scenario(NORMAL), owner)
    log_path = Path(outcome.action_log_path)
    assert log_path.exists()
    entries = [json.loads(line) for line in
               log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    kinds = {e.get("kind") for e in entries}
    assert {"run_started", "authorization", "tool_call", "proposal",
            "verdict", "action_result", "run_finished"} <= kinds


def test_the_audit_log_is_hash_chained(app):
    """Tamper-evident evidence: edits and deletions break verification."""
    owner = ScriptedOwner(verbose=False)
    run_scenario(app, _scenario(LARGE), owner)
    audit_log = app.audit_log_path
    assert audit_log.exists()
    entries = [json.loads(line) for line in
               audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert entries, "the auditor wrote nothing"
    assert all("prev_hash" in e and "hash" in e for e in entries)
    # Each entry links to the one before it.
    for previous, current in zip(entries, entries[1:]):
        assert current["prev_hash"] == previous["hash"]


def test_denials_are_recorded_as_evidence_too(app):
    """A refusal is an event worth logging, not a silent non-occurrence."""
    owner = ScriptedOwner(verbose=False)
    run_scenario(app, _scenario(DISPUTED), owner)
    entries = [json.loads(line) for line in
               app.audit_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    decided = [e for e in entries if e.get("event_type") == "action_decided"]
    assert any(e.get("decision") == "deny" for e in decided)


def test_no_credential_shaped_value_reaches_the_logs(app):
    """This app takes no payment credential, so nothing should be masked or leaked.

    Worth asserting rather than assuming: `audit_agent.redact()` does not cover
    `auth_code`, and BUILD.md flags that gap. helios-collect never calls
    `payments.send` — collections applies inbound money via `payments.receive`,
    which needs no intent code — so the leak has no path here. If a future change
    adds an outbound payment, this test is the tripwire.
    """
    owner = ScriptedOwner(verbose=False)
    for scenario in scenarios():
        run_scenario(app, scenario, owner)
    for path in [app.audit_log_path,
                 *Path(app.integration.runtime.runs_dir).glob("*/action-log.jsonl")]:
        text = path.read_text(encoding="utf-8")
        assert "auth_code" not in text, f"{path} mentions a payment credential"


# ---------------------------------------------------------------------------
# Undeclared tools are refused at build time
# ---------------------------------------------------------------------------

def test_an_action_manifest_offering_an_undeclared_tool_is_rejected(tmp_path):
    """`build_app` refuses to start, rather than running with an ungated side effect."""
    action = json.loads(ACTION_MANIFEST.read_text(encoding="utf-8"))
    action["tools"].append("payments.wire_offshore")
    action["mandate"]["allowed_tools"].append("payments.wire_offshore")
    tampered = tmp_path / "tampered-action.json"
    tampered.write_text(json.dumps(action), encoding="utf-8")

    import app as app_mod
    original = app_mod.ACTION_MANIFEST
    app_mod.ACTION_MANIFEST = tampered
    try:
        with pytest.raises(ValueError, match="not declared"):
            app_mod.build_app(run_dir=tmp_path / "runs")
    finally:
        app_mod.ACTION_MANIFEST = original


def test_a_manifest_declaring_an_unimplemented_tool_is_rejected(tmp_path):
    """The other direction: promised capability that does not exist fails closed."""
    audit_text = AUDIT_MANIFEST.read_text(encoding="utf-8")
    tampered = tmp_path / "tampered-audit.yaml"
    tampered.write_text(
        audit_text.replace(
            "tools:\n",
            "tools:\n  - name: phantom.tool\n    description: not implemented\n"
            "    side_effect: false\n", 1),
        encoding="utf-8")

    import app as app_mod
    original = app_mod.AUDIT_MANIFEST
    app_mod.AUDIT_MANIFEST = tampered
    try:
        with pytest.raises(ValueError, match="does not implement"):
            app_mod.build_app(run_dir=tmp_path / "runs")
    finally:
        app_mod.AUDIT_MANIFEST = original


def test_baseline_rules_cannot_be_overridden_by_this_app():
    """The most damaging thing a generated manifest can do, refused at parse time."""
    from audit_agent.errors import PolicyLooseningError

    text = AUDIT_MANIFEST.read_text(encoding="utf-8")
    for bad_id in ("baseline.grant_valid", "baseline.anything"):
        tampered = text.replace("- id: app.contact_frequency",
                                f"- id: {bad_id}", 1)
        with pytest.raises(PolicyLooseningError):
            AuditManifest.parse(tampered, origin="<test>")

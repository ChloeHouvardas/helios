"""Tests for the seam between the two builders.

Every assertion here was derived from the real API of both packages, not from
what either README implies. No network, no API key, no external services.

The fixtures matter, so they are stated once:

    today            2026-10-14 (Store.from_fixtures resolves it)
    customers        CUST-001, CUST-002, CUST-003
    invoices         INV-1001 (CUST-001,  4200.00)
                     INV-1002 (CUST-002,   850.00)
                     INV-1003 (CUST-003, 12500.00)
                     INV-1004 (CUST-001,  3100.00)
    last_contacted   CUST-001 21 days ago (outside the 7-day window -> 0)
                     CUST-002  1 day ago  (inside the window -> 1)
                     CUST-003  never

Mandate defaults: extensions over 14 days hold, invoices over 10000.00 hold,
at most 2 contacts per customer in any 7-day window.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_integration import (
    COMPLETED,
    ERROR,
    HELD,
    REFUSED,
    AuditBridge,
    DEFAULT_MANDATE,
    HeliosStoreFactSource,
    Integration,
    ScriptedDriver,
    ToolCall,
    ToolContractView,
    build_audit_manifest,
    build_integration,
    build_runtime_for,
    manifest_to_yaml,
    tool_views,
    validate_generated,
)
from agent_toolkit.action_log import LOG_FILENAME
from agent_toolkit.contracts import ToolDefinition
from agent_toolkit.models import Proposal
from agent_toolkit.store import Store
from audit_agent import AuditManifest, ToolDescriptor, validate_manifest_text

from agent_integration.wiring import DEFAULT_MANIFEST_PATH

TASK = "collect.reminder"
#: The auditor requires a task summary long enough to actually audit.
SUMMARY = "follow up on the overdue invoice with a courteous reminder"


@pytest.fixture
def workspace():
    runs = tempfile.mkdtemp(prefix="helios-it-runs-")
    data = tempfile.mkdtemp(prefix="helios-it-data-")
    yield Path(runs), Path(data)
    shutil.rmtree(runs, ignore_errors=True)
    shutil.rmtree(data, ignore_errors=True)


@pytest.fixture
def integration(workspace):
    runs, data = workspace
    built = build_integration(
        app_id="helios-collect-test",
        agent_id="collect-reminder-test",
        runs_dir=str(runs),
        data_dir=str(data),
        allow_unsigned=True,
    )
    yield built
    built.close()


def _names(integration: Integration) -> list[str]:
    return [tool.name for tool in integration.tools]


def _send(**overrides: Any) -> ToolCall:
    """A normal, approvable reminder: INV-1001 / CUST-001, in every limit."""
    arguments: dict[str, Any] = {
        "customer_id": "CUST-001",
        "invoice_id": "INV-1001",
        "subject": "Invoice RL-2026-047 is overdue",
        "body": "A courteous reminder that this invoice was due 21 days ago.",
    }
    arguments.update(overrides)
    return ToolCall(name="send_email", arguments=arguments)


def _large_invoice_call() -> ToolCall:
    """INV-1003 is 12500.00, above the 10000.00 owner threshold."""
    return _send(
        customer_id="CUST-003",
        invoice_id="INV-1003",
        subject="Invoice HL-2026-078 is overdue",
        body="This invoice is past due; please advise on payment.",
    )


def _session(integration: Integration, summary: str = SUMMARY, tools=None):
    return integration.bridge.authorize_task(
        TASK, summary, _names(integration) if tools is None else tools
    )


def _review(integration: Integration, call: ToolCall, summary: str = SUMMARY):
    return integration.bridge.review(
        _session(integration, summary),
        Proposal.create("collect-reminder-test", call.name, call.arguments),
    )


# ---------------------------------------------------------------------------
# Adapters: the gap that fails silently
# ---------------------------------------------------------------------------


def test_gated_tool_adapts_with_side_effect_preserved(integration: Integration) -> None:
    """The regression that matters most.

    ``ToolDescriptor.from_contract()`` reads ``has_side_effect`` (policy.py:92).
    The action side's ``ToolDefinition`` names the field ``side_effect``.
    Duck-typing them together does not raise — the missing attribute defaults
    to ``False``, so ``send_email`` registers as read-only and is never
    adjudicated. The app would email customers with no audit at all while
    every log looked correctly wired.
    """
    send_email = next(t for t in integration.tools if t.name == "send_email")
    assert send_email.side_effect is True
    assert not hasattr(send_email, "has_side_effect")

    # What the auditor infers with no adapter.
    naive = ToolDescriptor.from_contract(send_email)
    assert naive.side_effect is False, "the silent-failure premise has changed"
    assert naive.input_schema is None

    # And with the adapter.
    adapted = ToolDescriptor.from_contract(ToolContractView(send_email))
    assert adapted.side_effect is True
    assert adapted.input_schema is not None


def test_adapter_exposes_input_schema_as_callable(integration: Integration) -> None:
    """``from_contract`` only reads a *callable* ``input_schema`` (policy.py:84).
    The action side's is a plain Mapping, so without the adapter the auditor
    gets no schema and malformed arguments reach execution."""
    send_email = next(t for t in integration.tools if t.name == "send_email")
    assert not callable(send_email.input_schema)

    view = ToolContractView(send_email)
    assert callable(view.input_schema)
    schema = view.input_schema()
    assert sorted(schema["properties"]) == [
        "body", "customer_id", "extension_offer_days", "invoice_id", "subject",
    ]


def test_adapter_schema_is_a_copy(integration: Integration) -> None:
    """The auditor must not be able to mutate the action side's schema."""
    send_email = next(t for t in integration.tools if t.name == "send_email")
    view = ToolContractView(send_email)
    first = view.input_schema()
    first["properties"]["smuggled"] = {"type": "string"}
    assert "smuggled" not in view.input_schema()["properties"]
    assert "smuggled" not in send_email.input_schema["properties"]


def test_view_exposes_both_side_effect_spellings(integration: Integration) -> None:
    send_email = next(t for t in integration.tools if t.name == "send_email")
    view = ToolContractView(send_email)
    assert view.has_side_effect is True  # the auditor's name
    assert view.side_effect is True  # the action side's name
    assert view.tool is send_email


def test_read_only_tool_stays_read_only(integration: Integration) -> None:
    lookup = next(t for t in integration.tools if t.name == "lookup_invoice")
    assert lookup.side_effect is False
    descriptor = ToolDescriptor.from_contract(ToolContractView(lookup))
    assert descriptor.side_effect is False


def test_every_action_tool_adapts_and_registers(integration: Integration) -> None:
    for view in tool_views(integration.tools):
        descriptor = ToolDescriptor.from_contract(view)
        assert descriptor.name == view.name
        assert descriptor.side_effect == view.tool.side_effect

    registry = integration.audit_agent.registry
    for name in _names(integration):
        assert registry.has(name), f"{name} missing from the audit registry"
        assert registry.get(name).side_effect is (name == "send_email")


def test_capabilities_report_send_email_as_gated(integration: Integration) -> None:
    caps = integration.capabilities()
    assert caps["tools"]["send_email"]["side_effect"] is True
    assert caps["tools"]["send_email"]["gated"] is True
    assert caps["tools"]["lookup_invoice"]["gated"] is False


# ---------------------------------------------------------------------------
# Trusted facts: the trust boundary
# ---------------------------------------------------------------------------


def test_fact_source_reads_amounts_from_the_store(integration: Integration) -> None:
    """Amounts come from the ledger, never from what the agent claims."""
    source = integration.fact_source
    assert source.amount_for("INV-1001").to_integral_value() == 4200
    assert source.amount_for("INV-1003").to_integral_value() == 12500


def test_fact_source_returns_none_for_unknown_subjects(integration: Integration) -> None:
    """``None`` means unverifiable, and the auditor then refuses. Returning 0
    here would quietly turn a fail-closed control into a fail-open one."""
    source = integration.fact_source
    assert source.amount_for("INV-9999") is None
    assert source.contact_count_for("INV-9999", 7) is None
    assert source.counterparty_for("INV-9999") is None


def test_fact_source_resolves_counterparty(integration: Integration) -> None:
    source = integration.fact_source
    assert source.counterparty_for("INV-1003") == "Harbor Logistics Co"
    # Asked about a customer directly, it answers about that customer.
    assert source.counterparty_for("CUST-001") == "Ridgeline Construction Ltd"


def test_contact_counts_come_from_the_fixture_window() -> None:
    source = HeliosStoreFactSource(Store.from_fixtures(), contact_window_days=7)
    # 21 days ago is outside the window; 1 day ago is inside it.
    assert source.contact_count_for("CUST-001", 7) == 0
    assert source.contact_count_for("CUST-002", 7) == 1
    assert source.contact_count_for("CUST-003", 7) == 0


def test_contact_count_resolves_an_invoice_to_its_customer() -> None:
    source = HeliosStoreFactSource(Store.from_fixtures())
    assert source.contact_count_for("INV-1002", 7) == 1


def test_record_contact_increments_the_count() -> None:
    source = HeliosStoreFactSource(Store.from_fixtures())
    assert source.contact_count_for("CUST-001", 7) == 0
    source.record_contact("CUST-001")
    assert source.contact_count_for("CUST-001", 7) == 1


def test_the_send_email_tool_declares_the_contact_marker() -> None:
    """Defect 3, contract side: the flag lives on the tool, not in the runtime.

    `make_send_email` is the platform tool that contacts a customer, so it is
    the one that must declare `records_contact`. `lookup_invoice` reads only,
    so it must not — a read that inflated a contact count would deny legitimate
    follow-ups.
    """
    from agent_toolkit.tools import make_lookup_invoice, make_send_email

    store = Store.from_fixtures()
    assert make_send_email(store).records_contact is True
    assert make_lookup_invoice(store).records_contact is False


def test_records_contact_defaults_to_false() -> None:
    """An opt-in flag: silence must mean "does not contact anyone"."""
    tool = ToolDefinition(
        name="quiet_tool",
        description="does something that contacts nobody",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
        side_effect=True,
        handler=lambda args: {"ok": True},
    )
    assert tool.records_contact is False


def test_records_contact_must_be_a_boolean() -> None:
    """Fail closed on a malformed contract, like every other flag here."""
    with pytest.raises(ValueError, match="records_contact must be a boolean"):
        ToolDefinition(
            name="bad_tool",
            description="a tool with a non-boolean marker",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object", "properties": {}},
            side_effect=True,
            handler=lambda args: {"ok": True},
            records_contact="yes",
        )


def test_contact_recording_follows_the_marker_not_the_tool_name(
    workspace,
) -> None:
    """Defect 3, runtime side: a contact tool under any name is counted.

    The runtime used to hard-code `name == "send_email"`, so a tool that
    contacts a customer under any other name — a `fin_tools` `email.send`, the
    teammate email tool — silently skipped the contact count. The
    contact-frequency rule would then under-count and let an agent message the
    same customer past the cap. The runtime now reads
    `ToolDefinition.records_contact`.

    This registers a side-effect tool named `notify_customer` carrying the
    marker, runs it, and asserts the fact source was told a contact happened.
    Before the runtime was generalised this failed: the name did not match, so
    nothing was recorded.
    """
    runs, data = workspace

    notify = ToolDefinition(
        name="notify_customer",
        description="notify a customer about their invoice; this contacts them",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["customer_id", "invoice_id"],
            "properties": {
                "customer_id": {"type": "string", "minLength": 1},
                "invoice_id": {"type": "string", "minLength": 1},
            },
        },
        output_schema={
            "type": "object",
            "properties": {"sent": {"type": "boolean"}},
        },
        side_effect=True,
        handler=lambda args: {"sent": True, "customer_id": args["customer_id"]},
        records_contact=True,
    )

    built = build_integration(
        app_id="marker-test",
        agent_id="marker-agent",
        task_type="collect.reminder",
        tools=[notify],
        runs_dir=str(runs),
        data_dir=str(data),
        allow_unsigned=True,
        # Self-declared so the custom tool is granted; the point of this test
        # is the runtime's contact recording, not manifest governance.
        manifest_path=None,
        # Suppress the two DEFAULT_MANDATE rules that reference arguments or
        # facts this tool does not have (the manifest validator rightly rejects
        # a limit on an argument the tool never declares). None overrides the
        # default, which is how _mandate_rules skips a rule.
        mandate={
            "extension_owner_approval_days": None,
            "invoice_owner_approval_amount": None,
        },
    )
    try:
        recorded: list[tuple[Any, Any]] = []
        original = built.fact_source.record_contact

        def spy(customer_id, when=None):
            recorded.append((customer_id, when))
            return original(customer_id, when)

        built.fact_source.record_contact = spy

        result = built.runtime.run(
            driver=ScriptedDriver(
                [ToolCall(name="notify_customer",
                          arguments={"customer_id": "CUST-001",
                                     "invoice_id": "INV-1001"})],
                final_text="done",
            ),
            task=SUMMARY,
            task_type="collect.reminder",
        )
        assert result.summary()["side_effects_executed"] == 1, result.detail
        # The contact was recorded, under a tool name that is not send_email.
        assert [cid for cid, _ in recorded] == ["CUST-001"]
        assert built.fact_source.contact_count_for("CUST-001", 7) == 1
    finally:
        built.close()


def test_a_side_effect_without_the_marker_records_no_contact(
    workspace,
) -> None:
    """The converse: marking is opt-in, not applied to every side effect.

    A tool that changes state without contacting anyone (recording a payment,
    say) must not inflate a contact count, or a customer who paid would look
    like a customer who was nagged.
    """
    runs, data = workspace

    quiet = ToolDefinition(
        name="record_note",
        description="attach an internal note; contacts nobody",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["customer_id", "invoice_id"],
            "properties": {
                "customer_id": {"type": "string", "minLength": 1},
                "invoice_id": {"type": "string", "minLength": 1},
            },
        },
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        side_effect=True,
        handler=lambda args: {"ok": True},
        # records_contact intentionally omitted -> False
    )

    built = build_integration(
        app_id="no-marker-test",
        agent_id="no-marker-agent",
        task_type="collect.reminder",
        tools=[quiet],
        runs_dir=str(runs),
        data_dir=str(data),
        allow_unsigned=True,
        manifest_path=None,
        mandate={
            "extension_owner_approval_days": None,
            "invoice_owner_approval_amount": None,
        },
    )
    try:
        recorded: list[Any] = []
        original = built.fact_source.record_contact

        def spy(customer_id, when=None):
            recorded.append(customer_id)
            return original(customer_id, when)

        built.fact_source.record_contact = spy

        result = built.runtime.run(
            driver=ScriptedDriver(
                [ToolCall(name="record_note",
                          arguments={"customer_id": "CUST-001",
                                     "invoice_id": "INV-1001"})],
                final_text="done",
            ),
            task=SUMMARY,
            task_type="collect.reminder",
        )
        assert result.summary()["side_effects_executed"] == 1, result.detail
        assert recorded == []
        assert built.fact_source.contact_count_for("CUST-001", 7) == 0
    finally:
        built.close()


def test_payment_details_changed_is_honestly_unanswered(
    integration: Integration,
) -> None:
    """The collections fixtures do not model this, so the source says so
    rather than inventing an answer."""
    assert integration.fact_source.payment_details_changed_for("INV-1001") is None


def test_extra_facts_expose_amount_and_days_overdue(
    integration: Integration,
) -> None:
    facts = integration.fact_source.extra_facts_for("INV-1001")
    assert facts["amount_due"].to_integral_value() == 4200
    assert facts["invoice_amount"].to_integral_value() == 4200
    assert facts["currency"] == "CNY"
    assert facts["days_overdue"] == 21  # due 2026-09-23, today 2026-10-14
    assert integration.fact_source.extra_facts_for("INV-9999") == {}


# ---------------------------------------------------------------------------
# Manifest generation: the two sides must not drift
# ---------------------------------------------------------------------------


def test_generated_manifest_uses_the_real_tool_names(integration: Integration) -> None:
    """``audit-agent-builder/manifests/collect-reminder.yaml`` declares
    ``send_reminder_email(invoice_id, message, extension_days)`` — a tool the
    action side does not have. Wired naively, every call is denied as
    unregistered. Deriving from the real contract is what stops that."""
    manifest = build_audit_manifest(integration.tools)
    assert "send_reminder_email" not in {t["name"] for t in manifest["tools"]}
    assert {t["name"] for t in manifest["tools"]} == {"lookup_invoice", "send_email"}


def test_generated_manifest_marks_side_effects_correctly(
    integration: Integration,
) -> None:
    manifest = build_audit_manifest(integration.tools)
    by_name = {t["name"]: t for t in manifest["tools"]}
    assert by_name["send_email"]["side_effect"] is True
    assert by_name["lookup_invoice"]["side_effect"] is False


def test_generated_manifest_carries_the_real_schemas(
    integration: Integration,
) -> None:
    manifest = build_audit_manifest(integration.tools)
    by_name = {t["name"]: t for t in manifest["tools"]}
    assert "extension_offer_days" in by_name["send_email"]["input_schema"]["properties"]


def test_generated_manifest_validates_and_parses(integration: Integration) -> None:
    manifest = build_audit_manifest(integration.tools)
    text = manifest_to_yaml(manifest)

    ok, problems = validate_generated(manifest)
    assert ok, problems
    ok, problems = validate_manifest_text(text)
    assert ok, problems

    parsed = AuditManifest.parse(text)
    assert parsed.app_id == manifest["app_id"]
    assert {t.name for t in parsed.tools} == {"lookup_invoice", "send_email"}
    assert TASK in parsed.tasks


def test_generated_rules_use_the_action_sides_argument_names(
    integration: Integration,
) -> None:
    """A limit on an argument the tool never declares is rejected by the
    validator — which is exactly why this is generated, not hand-typed."""
    manifest = build_audit_manifest(integration.tools)
    rules = {r["id"]: r for r in manifest["rules"]}
    assert rules["app.extension_limit"]["limits"][0]["argument"] == (
        "extension_offer_days"
    )
    # The invoice cap is a FACT, so an under-reporting agent cannot slip under.
    assert rules["app.large_invoice_approval"]["limits"][0]["fact"] == "amount_due"


def test_mandate_overrides_reach_the_manifest(integration: Integration) -> None:
    manifest = build_audit_manifest(
        integration.tools,
        mandate={"extension_owner_approval_days": 3, "max_contacts_in_window": 5},
    )
    rules = {r["id"]: r for r in manifest["rules"]}
    assert rules["app.extension_limit"]["limits"][0]["max"] == 3
    assert rules["app.contact_frequency"]["max_contacts_in_days"]["max"] == 5


def test_default_mandate_matches_build1(integration: Integration) -> None:
    assert DEFAULT_MANDATE["extension_owner_approval_days"] == 14
    assert DEFAULT_MANDATE["invoice_owner_approval_amount"] == "10000.00"
    assert DEFAULT_MANDATE["max_contacts_in_window"] == 2
    assert DEFAULT_MANDATE["contact_window_days"] == 7


def test_generated_manifest_is_unsigned_by_default(integration: Integration) -> None:
    """A real deployment must fail at startup rather than quietly run
    unsigned, so the flag defaults to False."""
    manifest = build_audit_manifest(integration.tools)
    assert manifest["settings"]["allow_unsigned_grants"] is False
    assert manifest["settings"]["advisory"] == ["heuristic"]


# ---------------------------------------------------------------------------
# Bridge, interaction 1: which tools may this agent use
# ---------------------------------------------------------------------------


def test_authorize_grants_the_declared_tools(integration: Integration) -> None:
    session = _session(integration)
    assert session.authorized is True
    assert set(session.allowed_tools) == {"lookup_invoice", "send_email"}
    assert session.denied_tools == ()
    assert session.grant is not None


def test_authorize_is_scoped_to_the_requested_tools(
    integration: Integration,
) -> None:
    """The grant follows what was asked for, so a read-only agent cannot be
    handed a side-effect tool it never requested."""
    session = _session(integration, tools=["lookup_invoice"])
    assert session.authorized is True
    assert session.allowed_tools == ("lookup_invoice",)


def test_authorize_refuses_an_unregistered_tool(integration: Integration) -> None:
    session = _session(integration, tools=["lookup_invoice", "drain_bank_account"])
    assert session.authorized is False
    assert session.allowed_tools == ()
    assert "not registered" in session.reason


def test_authorize_refuses_an_unknown_task_type(integration: Integration) -> None:
    session = integration.bridge.authorize_task(
        "collect.nonexistent", SUMMARY, _names(integration)
    )
    assert session.authorized is False
    assert session.allowed_tools == ()


def test_authorize_refuses_a_too_short_summary(integration: Integration) -> None:
    """The auditor will not authorize a task it cannot understand."""
    session = integration.bridge.authorize_task(TASK, "do it", _names(integration))
    assert session.authorized is False
    assert "too short" in session.reason


def test_authorize_records_side_effect_flags(integration: Integration) -> None:
    session = _session(integration)
    assert dict(session.side_effects) == {
        "lookup_invoice": False,
        "send_email": True,
    }
    assert session.is_gated("send_email") is True
    assert session.is_gated("lookup_invoice") is False


def test_unknown_tools_are_treated_as_gated(integration: Integration) -> None:
    """Fail closed: an unknown tool is never assumed read-only."""
    session = _session(integration)
    assert session.is_gated("something_nobody_declared") is True


def test_grant_is_single_use(integration: Integration) -> None:
    session = _session(integration)
    assert session.grant.max_uses == 1


def test_session_permits_tracks_the_grant(integration: Integration) -> None:
    session = _session(integration)
    assert session.permits("send_email") is True
    assert session.permits("send_payment") is False


# ---------------------------------------------------------------------------
# Bridge, interaction 2: may this specific call proceed
# ---------------------------------------------------------------------------


def test_review_approves_a_normal_reminder(integration: Integration) -> None:
    result = _review(integration, _send())
    assert result.approved is True
    assert result.verdict.decision == "approve"
    assert result.hold_id is None
    assert result.held is False
    assert result.rejected is False


def test_review_holds_an_over_threshold_invoice(integration: Integration) -> None:
    result = _review(integration, _large_invoice_call())
    assert result.held is True
    assert result.hold_id is not None
    assert "10000.00" in result.verdict.reason
    assert result.owner_question


def test_review_holds_an_over_long_extension(integration: Integration) -> None:
    result = _review(integration, _send(extension_offer_days=60))
    assert result.held is True
    assert result.verdict.rule == "app.extension_limit"
    assert "14 days" in result.verdict.reason


def test_review_approves_an_extension_at_the_limit(integration: Integration) -> None:
    """14 is exactly at the limit, so it is inside the agent's authority."""
    assert _review(integration, _send(extension_offer_days=14)).approved is True


def test_review_rejects_when_contact_frequency_is_reached(
    integration: Integration,
) -> None:
    """CUST-002 was contacted 1 day ago, and the cap is 2 in 7 days. After two
    more contacts the third is refused on a trusted count, not on the agent's
    say-so."""
    integration.fact_source.record_contact("CUST-002")
    integration.fact_source.record_contact("CUST-002")
    assert integration.fact_source.contact_count_for("CUST-002", 7) == 3

    result = _review(
        integration,
        _send(
            customer_id="CUST-002",
            invoice_id="INV-1002",
            subject="A third reminder",
            body="Following up again on this invoice.",
        ),
    )
    assert result.rejected is True
    assert result.verdict.rule == "app.contact_frequency"


def test_review_rejects_a_tool_outside_the_grant(integration: Integration) -> None:
    """Refused locally, before a round trip: the grant never covered it."""
    session = _session(integration, tools=["lookup_invoice"])
    result = integration.bridge.review(
        session, Proposal.create("collect-reminder-test", "send_email", {})
    )
    assert result.rejected is True
    assert result.verdict.rule == "tool_not_granted"


def test_review_rejects_when_the_task_was_never_authorized(
    integration: Integration,
) -> None:
    session = integration.bridge.authorize_task(
        "collect.nonexistent", SUMMARY, _names(integration)
    )
    result = integration.bridge.review(
        session, Proposal.create("collect-reminder-test", "send_email", {})
    )
    assert result.rejected is True
    assert result.verdict.rule == "task_not_authorized"


def test_review_rejects_malformed_arguments(integration: Integration) -> None:
    """The schema check fires in the auditor, so bad arguments never reach the
    action side. This is what the ``input_schema()`` adapter gap would have
    silently disabled."""
    result = _review(integration, ToolCall(name="send_email", arguments={
        "invoice_id": "INV-1001",
        "subject": "missing customer and body",
    }))
    assert result.rejected is True
    assert result.verdict.rule == "baseline.arguments_validate"


def test_review_rejects_wrong_argument_types(integration: Integration) -> None:
    result = _review(integration, _send(extension_offer_days="fourteen"))
    assert result.rejected is True
    assert result.verdict.rule == "baseline.arguments_validate"


def test_review_rejects_a_negative_extension(integration: Integration) -> None:
    assert _review(integration, _send(extension_offer_days=-5)).rejected is True


def test_review_rejects_an_unknown_invoice(integration: Integration) -> None:
    """No trusted amount can be resolved, so the cap cannot be evaluated and
    the auditor refuses rather than assuming the invoice is small."""
    result = _review(integration, _send(invoice_id="INV-9999"))
    assert result.rejected is True
    assert "could not be evaluated" in result.verdict.reason


def test_review_approves_a_different_invoice_of_the_same_customer(
    integration: Integration,
) -> None:
    assert _review(integration, _send(invoice_id="INV-1004")).approved is True


def test_granted_arguments_win_over_the_models_originals(
    integration: Integration,
) -> None:
    """When the auditor clamps a value, the granted one executes. A model
    asking for 60 days and receiving 14 must not get 60."""
    result = _review(integration, _send(extension_offer_days=14))
    original = {"extension_offer_days": 14, "customer_id": "CUST-001"}
    # No clamping happened here, so the original stands.
    assert result.arguments_to_execute(original) == original


def test_arguments_to_execute_prefers_the_grant() -> None:
    from agent_integration.bridge import GateResult
    from agent_toolkit.models import Verdict

    gate = GateResult(
        verdict=Verdict(decision="approve", rule="r", reason=""),
        granted_arguments={"extension_offer_days": 14},
    )
    assert gate.arguments_to_execute({"extension_offer_days": 60}) == {
        "extension_offer_days": 14
    }


# ---------------------------------------------------------------------------
# Holds: recorded, resolvable, single-decision
# ---------------------------------------------------------------------------


def test_hold_is_recorded_and_retrievable(integration: Integration) -> None:
    result = _review(integration, _large_invoice_call())
    assert result.held is True
    hold_id = result.hold_id
    assert integration.bridge.pending_hold(hold_id) is result

    record = integration.bridge.hold_status(hold_id)
    assert record is not None
    assert record.status == "pending"
    assert record.tool == "send_email"
    # The exact arguments are preserved, so the owner approves what runs.
    assert record.arguments["invoice_id"] == "INV-1003"


def test_held_action_is_remembered_for_resume(integration: Integration) -> None:
    result = _review(integration, _large_invoice_call())
    held = integration.bridge.held_action(result.hold_id)
    assert held["tool"] == "send_email"
    assert held["arguments"]["invoice_id"] == "INV-1003"
    assert held["task_type"] == TASK


def test_resolve_hold_approves(integration: Integration) -> None:
    result = _review(integration, _large_invoice_call())
    record = integration.bridge.resolve_hold(
        result.hold_id, approve=True, decided_by="owner", note="verified by phone"
    )
    assert record.status == "approved"
    assert record.decided_by == "owner"
    assert integration.bridge.pending_hold(result.hold_id) is None
    assert integration.bridge.hold_status(result.hold_id).status == "approved"


def test_resolve_hold_rejects(integration: Integration) -> None:
    result = _review(integration, _large_invoice_call())
    record = integration.bridge.resolve_hold(
        result.hold_id, approve=False, decided_by="owner"
    )
    assert record.status == "rejected"


def test_a_hold_can_only_be_decided_once(integration: Integration) -> None:
    """``audit_agent.resolve_hold`` raises on a second decision, so an owner
    cannot be recorded as having approved twice."""
    from audit_agent.errors import AuditAgentError

    result = _review(integration, _large_invoice_call())
    integration.bridge.resolve_hold(result.hold_id, approve=True, decided_by="owner")
    with pytest.raises(AuditAgentError):
        integration.bridge.resolve_hold(
            result.hold_id, approve=True, decided_by="owner"
        )


def test_forget_held_action_is_single_use(integration: Integration) -> None:
    result = _review(integration, _large_invoice_call())
    hold_id = result.hold_id
    assert integration.bridge.forget_held_action(hold_id) is not None
    assert integration.bridge.forget_held_action(hold_id) is None
    assert integration.bridge.held_action(hold_id) is None


def test_pending_holds_lists_what_awaits_an_owner(integration: Integration) -> None:
    _review(integration, _large_invoice_call())
    _review(integration, _send(extension_offer_days=60))
    holds = integration.client.pending_holds()
    assert len(holds) == 2
    assert {h.tool for h in holds} == {"send_email"}


# ---------------------------------------------------------------------------
# Fail closed when the auditor cannot be reached
# ---------------------------------------------------------------------------


def test_unreachable_auditor_denies_rather_than_allows() -> None:
    """The property the whole design rests on: no auditor means no authority."""

    class DeadClient:
        def authorize(self, request):
            raise RuntimeError("audit agent is down")

        def adjudicate(self, request):
            raise RuntimeError("audit agent is down")

    from audit_agent.errors import TransportError

    class TransportDown:
        def authorize(self, request):
            raise TransportError("audit agent is down")

        def adjudicate(self, request):
            raise TransportError("audit agent is down")

    for client in (DeadClient(), TransportDown()):
        bridge = AuditBridge(client, app_id="a", agent_id="b")
        session = bridge.authorize_task(TASK, SUMMARY, ["send_email"])
        assert session.authorized is False
        assert "could not be reached" in session.reason

        result = bridge.review(
            session, Proposal.create("b", "send_email", {"invoice_id": "INV-1001"})
        )
        assert result.rejected is True
        assert result.approved is False


def test_bridge_needs_a_task_type() -> None:
    class Unused:
        pass

    bridge = AuditBridge(Unused(), app_id="a", agent_id="b")
    with pytest.raises(ValueError, match="task_type"):
        bridge.authorize_task("", SUMMARY, ["send_email"])


# ---------------------------------------------------------------------------
# Runtime: the gated loop
# ---------------------------------------------------------------------------


def test_runtime_completes_an_approved_reminder(integration: Integration) -> None:
    result = integration.runtime.run(
        driver=ScriptedDriver(
            [ToolCall("lookup_invoice", {"invoice_id": "INV-1001"}), _send()],
            final_text="Reminder sent.",
        ),
        task=SUMMARY,
        task_type=TASK,
    )

    assert result.status == COMPLETED
    assert len(result.actions) == 2
    # Read-only: executed, never adjudicated.
    assert result.actions[0]["tool"] == "lookup_invoice"
    assert result.actions[0]["verdict"] is None
    assert result.actions[0]["executed"] is True
    # Side effect: adjudicated, then executed.
    assert result.actions[1]["verdict"]["decision"] == "approve"
    assert result.actions[1]["executed"] is True
    assert result.final_text == "Reminder sent."
    # The email really went out.
    assert len(integration.store.sent_emails) == 1
    assert integration.fact_source.contact_count_for("CUST-001", 7) == 1


def test_runtime_only_offers_granted_tools(integration: Integration) -> None:
    """The model must not be able to ask for what the auditor withheld."""
    seen: list[list[str]] = []

    class RecordingDriver(ScriptedDriver):
        def next_calls(self, *, task, instructions, tools, history):
            seen.append([t.name for t in tools])
            return super().next_calls(
                task=task, instructions=instructions, tools=tools, history=history
            )

    integration.runtime.run(
        driver=RecordingDriver([_send()], final_text="done"),
        task=SUMMARY,
        task_type=TASK,
        requested_tools=["send_email"],
    )
    assert seen, "the driver was never asked to propose"
    assert all(offered == ["send_email"] for offered in seen)


def test_runtime_refuses_when_the_auditor_grants_nothing(
    integration: Integration,
) -> None:
    """An unauthorized task type must not reach the model at all."""
    driver = ScriptedDriver([_send()], final_text="should never run")
    result = integration.runtime.run(
        driver=driver, task=SUMMARY, task_type="collect.nonexistent"
    )

    assert result.status == REFUSED
    assert result.actions == []
    assert driver._index == 0, "the driver was consulted despite the refusal"
    assert integration.store.sent_emails == []


def test_runtime_holds_and_sends_nothing(integration: Integration) -> None:
    result = integration.runtime.run(
        driver=ScriptedDriver([_large_invoice_call()], final_text="x"),
        task="follow up on the large overdue invoice for Harbor Logistics",
        task_type=TASK,
    )

    assert result.status == HELD
    assert len(result.holds) == 1
    assert result.actions[0]["verdict"]["decision"] == "hold"
    assert result.actions[0]["executed"] is False
    assert result.final_text == "The action is awaiting an owner decision."
    assert integration.store.sent_emails == []


def test_runtime_resume_executes_only_the_held_call(
    integration: Integration,
) -> None:
    first = integration.runtime.run(
        driver=ScriptedDriver([_large_invoice_call()], final_text="x"),
        task="follow up on the large overdue invoice for Harbor Logistics",
        task_type=TASK,
    )
    assert first.status == HELD
    hold_id = first.holds[0]["hold_id"]
    assert integration.store.sent_emails == []

    resumed = integration.runtime.resume(
        driver=ScriptedDriver([], final_text="Sent after the owner approved."),
        run_id=first.run_id,
        hold_id=hold_id,
        decision=True,
    )

    assert resumed.status == COMPLETED
    assert len(resumed.actions) == 1
    assert resumed.actions[0]["executed"] is True
    assert resumed.actions[0]["verdict"]["rule"] == "owner.approved_hold"
    # Exactly one email, for exactly the held invoice.
    assert len(integration.store.sent_emails) == 1
    assert integration.store.sent_emails[0]["invoice_id"] == "INV-1003"
    # And the held action is consumed, so it cannot be resumed twice.
    assert integration.bridge.held_action(hold_id) is None


def test_runtime_resume_twice_does_not_send_twice(
    integration: Integration,
) -> None:
    """A spent hold must not become a second email."""
    first = integration.runtime.run(
        driver=ScriptedDriver([_large_invoice_call()], final_text="x"),
        task="follow up on the large overdue invoice for Harbor Logistics",
        task_type=TASK,
    )
    hold_id = first.holds[0]["hold_id"]
    integration.runtime.resume(
        driver=ScriptedDriver([], final_text="sent"),
        run_id=first.run_id,
        hold_id=hold_id,
        decision=True,
    )
    assert len(integration.store.sent_emails) == 1

    again = integration.runtime.resume(
        driver=ScriptedDriver([], final_text="sent again"),
        run_id=first.run_id,
        hold_id=hold_id,
        decision=True,
    )
    assert again.status == ERROR
    assert len(integration.store.sent_emails) == 1, "a held action ran twice"


def test_runtime_resume_rejection_sends_nothing_and_tells_the_model(
    integration: Integration,
) -> None:
    first = integration.runtime.run(
        driver=ScriptedDriver([_large_invoice_call()], final_text="x"),
        task="follow up on the large overdue invoice for Harbor Logistics",
        task_type=TASK,
    )
    hold_id = first.holds[0]["hold_id"]

    seen: list[str] = []

    class RecordingDriver(ScriptedDriver):
        def next_calls(self, *, task, instructions, tools, history):
            seen.extend(
                str(m.get("content", "")) for m in history if m.get("role") == "user"
            )
            return super().next_calls(
                task=task, instructions=instructions, tools=tools, history=history
            )

    resumed = integration.runtime.resume(
        driver=RecordingDriver([], final_text="Understood, not sending."),
        run_id=first.run_id,
        hold_id=hold_id,
        decision=False,
    )

    assert resumed.status == REFUSED
    assert resumed.actions[0]["executed"] is False
    assert integration.store.sent_emails == []
    assert any("owner rejected" in text for text in seen)
    assert integration.bridge.held_action(hold_id) is None


def test_runtime_resume_of_an_unknown_hold_fails(integration: Integration) -> None:
    result = integration.runtime.resume(
        driver=ScriptedDriver([], final_text="x"),
        run_id="run-does-not-exist",
        hold_id="hold_nope",
        decision=True,
    )
    assert result.status == ERROR
    assert "unknown" in result.detail


def test_runtime_feeds_a_denial_back_to_the_model(integration: Integration) -> None:
    """build1.md asks for exactly this: rejected, and the agent adjusts."""
    seen: list[str] = []

    class RecordingDriver(ScriptedDriver):
        def next_calls(self, *, task, instructions, tools, history):
            seen.extend(str(m.get("content", "")) for m in history)
            return super().next_calls(
                task=task, instructions=instructions, tools=tools, history=history
            )

    integration.fact_source.record_contact("CUST-002")
    integration.fact_source.record_contact("CUST-002")

    driver = RecordingDriver(
        [
            ToolCall(
                name="send_email",
                arguments={
                    "customer_id": "CUST-002",
                    "invoice_id": "INV-1002",
                    "subject": "A third reminder",
                    "body": "Following up again on this invoice.",
                },
            )
        ],
        final_text="Stopping: the contact limit is reached.",
    )
    result = integration.runtime.run(
        driver=driver,
        task="send Maple Street Dental another reminder about INV-1002",
        task_type=TASK,
    )

    assert result.status == COMPLETED  # the model closed gracefully
    assert result.actions[0]["verdict"]["decision"] == "reject"
    assert result.actions[0]["executed"] is False
    assert any("DENIED by the audit agent" in text for text in seen)
    assert integration.store.sent_emails == []


def test_runtime_stops_a_second_send_on_contact_frequency(
    integration: Integration,
) -> None:
    """End to end: the first reminder is fine, the second is refused because
    the trusted count reached the cap after the first actually executed."""
    call = ToolCall(
        name="send_email",
        arguments={
            "customer_id": "CUST-002",
            "invoice_id": "INV-1002",
            "subject": "Invoice MS-2026-112 is overdue",
            "body": "A courteous reminder about this invoice.",
        },
    )
    result = integration.runtime.run(
        driver=ScriptedDriver([call, call], final_text="Sent one, stopped at the cap."),
        task="remind Maple Street Dental about their overdue invoice",
        task_type=TASK,
    )

    assert result.status == COMPLETED
    assert result.actions[0]["verdict"]["decision"] == "approve"
    assert result.actions[0]["executed"] is True
    assert result.actions[1]["verdict"]["decision"] == "reject"
    assert result.actions[1]["verdict"]["rule"] == "app.contact_frequency"
    assert result.actions[1]["executed"] is False
    assert len(integration.store.sent_emails) == 1


def test_runtime_reauthorizes_for_each_side_effect(
    integration: Integration,
) -> None:
    """Grants are single-use, so a second send needs a second authorization —
    silently reusing a spent grant would turn every second email into a deny."""
    result = integration.runtime.run(
        driver=ScriptedDriver(
            [_send(), _send(invoice_id="INV-1004", subject="Invoice RL-2026-049")],
            final_text="Both sent.",
        ),
        task="remind Ridgeline about both of their overdue invoices",
        task_type=TASK,
    )

    assert result.status == COMPLETED
    assert len(result.actions) == 2
    assert all(a["executed"] for a in result.actions)
    assert len(integration.store.sent_emails) == 2


def test_runtime_reports_a_failing_tool_as_an_error(
    integration: Integration,
) -> None:
    """The action side validates that an invoice belongs to the named customer
    and raises when it does not. The auditor has no declarative way to express
    a two-argument pairing check, so the failure surfaces here — loudly, as an
    error, never as a silent success."""
    result = integration.runtime.run(
        driver=ScriptedDriver(
            [_send(customer_id="CUST-003", invoice_id="INV-1001")],
            final_text="x",
        ),
        task="email Harbor Logistics about the Ridgeline invoice",
        task_type=TASK,
    )

    assert result.status == ERROR
    assert result.actions[0]["executed"] is False
    assert "does not belong to customer" in result.actions[0]["output"]
    assert integration.store.sent_emails == []


def test_runtime_stops_at_the_turn_cap(integration: Integration) -> None:
    """An agent looping forever is an error, not a completed run."""
    driver = ScriptedDriver(
        [ToolCall("lookup_invoice", {"invoice_id": "INV-1001"})] * 50,
        final_text="never reached",
    )
    result = integration.runtime.run(
        driver=driver, task="look things up forever", task_type=TASK
    )

    assert result.status == ERROR
    assert result.final_text == "Stopped at the step cap."
    assert len(result.actions) == integration.runtime.max_turns


def test_runtime_survives_a_broken_driver(integration: Integration) -> None:
    """A model that throws must not be read as permission to act."""

    class BrokenDriver:
        def next_calls(self, **kwargs):
            raise RuntimeError("the model exploded")

    result = integration.runtime.run(
        driver=BrokenDriver(), task=SUMMARY, task_type=TASK
    )
    assert result.status == ERROR
    assert "model exploded" in result.detail
    assert integration.store.sent_emails == []


def test_runtime_stops_when_the_driver_returns_nothing(
    integration: Integration,
) -> None:
    result = integration.runtime.run(
        driver=ScriptedDriver([], final_text=""),
        task=SUMMARY,
        task_type=TASK,
    )
    assert result.status == COMPLETED
    assert result.actions == []


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def test_run_writes_an_action_log(integration: Integration) -> None:
    result = integration.runtime.run(
        driver=ScriptedDriver(
            [ToolCall("lookup_invoice", {"invoice_id": "INV-1001"}), _send()],
            final_text="done",
        ),
        task=SUMMARY,
        task_type=TASK,
    )
    log_path = Path(result.action_log_path)
    assert log_path.name == LOG_FILENAME
    assert log_path.exists()

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    kinds = {e["kind"] for e in entries}
    assert {"run_started", "authorization", "verdict", "run_finished"} <= kinds
    assert all(e["run_id"] == result.run_id for e in entries)
    # ActionLog numbers entries from 1 and never leaves a gap, so a removed
    # entry is visible.
    assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))


def test_action_log_records_the_grant_and_the_verdict(
    integration: Integration,
) -> None:
    result = integration.runtime.run(
        driver=ScriptedDriver([_send()], final_text="done"),
        task=SUMMARY,
        task_type=TASK,
    )
    entries = [
        json.loads(line)
        for line in Path(result.action_log_path).read_text().splitlines()
    ]
    authz = next(e for e in entries if e["kind"] == "authorization")
    assert authz["authorized"] is True
    assert set(authz["granted_tools"]) == {"lookup_invoice", "send_email"}
    assert authz["authorization_id"]

    verdict = next(e for e in entries if e["kind"] == "verdict")
    assert verdict["decision"] == "approve"
    assert verdict["proposal_id"]


def test_audit_evidence_is_hash_chained_and_verifies(
    integration: Integration,
) -> None:
    integration.runtime.run(
        driver=ScriptedDriver([_send()], final_text="done"),
        task=SUMMARY,
        task_type=TASK,
    )
    log = integration.audit_agent.log
    assert Path(log.path).exists()

    ok, problems = log.verify_chain()
    assert ok, problems


def test_audit_evidence_records_authorizations_and_decisions(
    integration: Integration,
) -> None:
    integration.runtime.run(
        driver=ScriptedDriver([_send()], final_text="done"),
        task=SUMMARY,
        task_type=TASK,
    )
    log = integration.audit_agent.log
    events = list(log.iter_events())
    assert events, "the audit log is empty"
    assert len(log.decisions()) >= 1


def test_summary_is_json_serializable(integration: Integration) -> None:
    result = integration.runtime.run(
        driver=ScriptedDriver([_send()], final_text="done"),
        task=SUMMARY,
        task_type=TASK,
    )
    payload = json.dumps(result.summary())  # must not raise
    summary = json.loads(payload)
    assert summary["status"] == COMPLETED
    assert summary["authorized"] is True
    assert summary["side_effects_executed"] == 1


def test_summary_counts_only_side_effects_not_read_only_calls(
    integration: Integration,
) -> None:
    """Defect 2: read-only calls are appended to `actions` too.

    `_execute_direct` records a successful read as an action entry with
    `executed: True` (runtime.py:248-261), so `len(self.actions)` and the
    `executed` property both over-count whenever a run reads before it acts.
    `side_effects_attempted` / `side_effects_executed` are what an operator
    reads to know how many gated actions a run took; inflating them with
    lookups makes the number meaningless.

    The discriminator is `proposal_id`: only a side-effect call is turned into
    a Proposal and adjudicated, so only those entries carry one. A read-only
    entry has `proposal_id is None`.

    This run does one lookup then one send, so the correct counts are 1 and 1.
    Before the fix, `attempted` was 2 (both actions) and `executed` was 2 (both
    had `executed: True`).
    """
    lookup = ToolCall(name="lookup_invoice", arguments={"invoice_id": "INV-1001"})
    result = integration.runtime.run(
        driver=ScriptedDriver([lookup, _send()], final_text="done"),
        task=SUMMARY,
        task_type=TASK,
    )
    summary = result.summary()
    assert summary["status"] == COMPLETED

    # Both calls happened; only one is a side effect.
    assert len(result.actions) == 2, "sanity: a read and a send were recorded"
    assert summary["side_effects_attempted"] == 1
    assert summary["side_effects_executed"] == 1


def test_summary_attempted_counts_gated_calls_that_did_not_execute(
    integration: Integration,
) -> None:
    """A denied/held side effect was still *attempted*, just not executed.

    `attempted` must count every adjudicated side-effect call regardless of
    verdict, while `executed` counts only those that ran. A denied call has a
    `proposal_id` but `executed: False`.
    """
    denied = _send(invoice_id="INV-1003")  # large invoice -> held, not executed
    result = integration.runtime.run(
        driver=ScriptedDriver([denied], final_text="done"),
        task=SUMMARY,
        task_type=TASK,
    )
    summary = result.summary()
    assert summary["side_effects_attempted"] == 1
    assert summary["side_effects_executed"] == 0


# ---------------------------------------------------------------------------
# Wiring and transport equivalence
# ---------------------------------------------------------------------------


def test_default_manifest_is_checked_in_and_governs() -> None:
    """The shipped manifest must exist, or every build fails."""
    assert DEFAULT_MANIFEST_PATH.exists(), (
        f"no manifest at {DEFAULT_MANIFEST_PATH}; generate it with "
        "`agent-integration manifest --out <path>`"
    )
    ok, problems = validate_manifest_text(
        DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8")
    )
    assert ok, problems


def test_authority_comes_from_the_manifest_not_the_caller(workspace) -> None:
    """The property that makes the auditor worth running.

    If the manifest were generated from whatever ``task_type`` the app asked
    for, the app would be writing its own permission slip and the auditor
    would grant anything. Loading a reviewed file is what stops that.
    """
    runs, data = workspace
    built = build_integration(
        app_id="governance-test",
        agent_id="governance-agent",
        runs_dir=str(runs),
        data_dir=str(data),
        allow_unsigned=True,
    )
    try:
        assert built.self_declared_manifest is False
        assert built.manifest_path == str(DEFAULT_MANIFEST_PATH)

        # The one task type the manifest declares.
        granted = built.bridge.authorize_task(
            TASK,
            "a task the reviewed manifest authorizes for this agent",
            _names(built),
        )
        assert granted.authorized is True

        # A task type nobody reviewed. Must not be authorized just because
        # the caller named it.
        invented = built.bridge.authorize_task(
            "collect.pay_suppliers",
            "pay every supplier from the company bank account immediately",
            _names(built),
        )
        assert invented.authorized is False
        assert invented.allowed_tools == ()
        assert "no policy registered" in invented.reason
    finally:
        built.close()


def test_self_declared_manifest_is_flagged(workspace) -> None:
    """Generating a manifest at runtime is allowed, but is marked as such —
    it proves nothing about governance."""
    runs, data = workspace
    built = build_integration(
        app_id="self-declared",
        agent_id="self-declared-agent",
        task_type="collect.anything",
        runs_dir=str(runs),
        data_dir=str(data),
        allow_unsigned=True,
        manifest_path=None,
    )
    try:
        assert built.self_declared_manifest is True
        assert built.manifest_path == ""
        # And it does authorize whatever it declared, which is the point of
        # the warning.
        assert built.bridge.authorize_task(
            "collect.anything",
            "a task under a self-declared task type for this agent",
            _names(built),
        ).authorized is True
    finally:
        built.close()


def test_missing_manifest_file_is_an_error(workspace) -> None:
    """Failing loudly beats silently authorizing everything."""
    runs, data = workspace
    with pytest.raises(FileNotFoundError, match="no audit manifest"):
        build_integration(
            app_id="missing-manifest",
            agent_id="missing-manifest-agent",
            runs_dir=str(runs),
            data_dir=str(data),
            allow_unsigned=True,
            manifest_path="/does/not/exist.yaml",
        )


def test_second_runtime_shares_one_auditor(integration: Integration, workspace) -> None:
    """Several action agents, one gate."""
    runs, _ = workspace
    second = build_runtime_for(
        integration, agent_id="second-agent", runs_dir=str(runs)
    )
    assert second.agent_name == "second-agent"
    assert second.bridge.agent_id == "second-agent"
    # Same auditor underneath.
    assert second.bridge.client is integration.client

    result = second.run(
        driver=ScriptedDriver([_send()], final_text="done"),
        task=SUMMARY,
        task_type=TASK,
    )
    assert result.status == COMPLETED
    assert len(integration.store.sent_emails) == 1


def test_runtime_behaves_the_same_over_every_transport() -> None:
    """inprocess, file and http are interchangeable. That is what lets the
    auditor run as its own service without changing this code."""
    for transport in ("inprocess", "file", "http"):
        runs = tempfile.mkdtemp(prefix=f"helios-{transport}-")
        data = tempfile.mkdtemp(prefix=f"helios-{transport}-data-")
        built = None
        try:
            built = build_integration(
                app_id=f"transport-{transport}",
                agent_id=f"agent-{transport}",
                transport=transport,
                runs_dir=runs,
                data_dir=data,
                allow_unsigned=True,
            )
            approved = built.runtime.run(
                driver=ScriptedDriver([_send()], final_text=f"ok via {transport}"),
                task=SUMMARY,
                task_type=TASK,
            )
            assert approved.status == COMPLETED, transport
            assert approved.actions[0]["verdict"]["decision"] == "approve", transport
            assert approved.actions[0]["executed"] is True, transport

            held = built.runtime.run(
                driver=ScriptedDriver([_large_invoice_call()], final_text="x"),
                task="follow up on the large overdue invoice for Harbor Logistics",
                task_type=TASK,
            )
            assert held.status == HELD, transport
            assert held.actions[0]["executed"] is False, transport
            assert len(built.store.sent_emails) == 1, transport  # only the first
        finally:
            if built is not None:
                built.close()
            shutil.rmtree(runs, ignore_errors=True)
            shutil.rmtree(data, ignore_errors=True)


def test_unknown_transport_is_refused(workspace) -> None:
    runs, data = workspace
    with pytest.raises(ValueError, match="unknown transport"):
        build_integration(
            app_id="bad-transport",
            agent_id="bad-transport-agent",
            transport="carrier-pigeon",
            runs_dir=str(runs),
            data_dir=str(data),
            allow_unsigned=True,
        )


def test_close_is_idempotent(workspace) -> None:
    runs, data = workspace
    built = build_integration(
        app_id="close-test",
        agent_id="close-agent",
        runs_dir=str(runs),
        data_dir=str(data),
        allow_unsigned=True,
    )
    built.close()
    built.close()  # must not raise
    assert built._servers == []


def test_a_signing_key_produces_signed_grants(workspace, monkeypatch) -> None:
    """With a real key the grant is signed, which is what makes it verifiable
    across a process boundary."""
    runs, data = workspace
    monkeypatch.setenv("HELIOS_AUDIT_SIGNING_KEY", "0" * 64)
    built = build_integration(
        app_id="signed-test",
        agent_id="signed-agent",
        runs_dir=str(runs),
        data_dir=str(data),
    )
    try:
        assert built.ephemeral_key is False
        session = built.bridge.authorize_task(TASK, SUMMARY, _names(built))
        assert session.authorized is True
        assert session.grant.unsigned is False
        assert session.grant.signature
    finally:
        built.close()


def test_without_a_key_the_demo_still_runs_on_an_ephemeral_one(
    workspace, monkeypatch
) -> None:
    """No key configured: grants are still signed, with a process-local key, so
    the replay and tamper paths are exercised rather than skipped."""
    runs, data = workspace
    monkeypatch.delenv("HELIOS_AUDIT_SIGNING_KEY", raising=False)
    built = build_integration(
        app_id="ephemeral-test",
        agent_id="ephemeral-agent",
        runs_dir=str(runs),
        data_dir=str(data),
    )
    try:
        assert built.ephemeral_key is True
        session = built.bridge.authorize_task(TASK, SUMMARY, _names(built))
        assert session.authorized is True
        assert session.grant.signature
    finally:
        built.close()


# ---------------------------------------------------------------------------
# Scripted driver determinism
# ---------------------------------------------------------------------------


def test_scripted_driver_replays_in_order() -> None:
    calls = [
        _send(),
        ToolCall("lookup_invoice", {"invoice_id": "INV-1001"}),
    ]
    driver = ScriptedDriver(calls, final_text="done")
    kwargs = dict(task="t", instructions="i", tools=[], history=[])

    assert driver.next_calls(**kwargs) == [calls[0]]
    assert driver.next_calls(**kwargs) == [calls[1]]
    assert driver.next_calls(**kwargs) == "done"
    assert driver.next_calls(**kwargs) == "done"  # idempotent once exhausted


def test_scripted_driver_defaults_to_done() -> None:
    driver = ScriptedDriver([])
    assert driver.next_calls(task="t", instructions="i", tools=[], history=[]) == (
        "done"
    )

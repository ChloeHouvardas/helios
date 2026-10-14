"""Phase 1: the financial tools reach the gated agent loop.

The claim under test (specs/financial-tools-service.md §2b): converting tools to
a service is not sufficient — they must become the tools the agent loop actually
calls. These assert the bridge produces definitions the *other* packages accept:
the action side's own validation, and the audit validator's strict manifest check.
"""
import pytest

from fin_tools import build_world
from fin_tools.toolkit_bridge import (NOT_AGENT_CALLABLE,
                                      agent_tool_definitions,
                                      as_tool_definitions, to_json_schema)

pytest.importorskip("agent_toolkit")


# -- schema conversion ----------------------------------------------------

def test_flat_schema_becomes_json_schema():
    schema = to_json_schema({"payee": "str", "amount_cents": "int",
                             "memo": "str|None"})
    assert schema["type"] == "object"
    assert schema["properties"]["payee"] == {"type": "string"}
    assert schema["properties"]["amount_cents"] == {"type": "integer"}
    # optional -> nullable, and not required
    assert schema["properties"]["memo"] == {"type": ["string", "null"]}
    assert schema["required"] == ["payee", "amount_cents"]
    assert schema["additionalProperties"] is False


def test_container_and_bool_types():
    schema = to_json_schema({"rows": "list[dict]", "flag": "bool",
                             "rate": "float", "meta": "dict"})
    assert schema["properties"]["rows"] == {"type": "array"}
    assert schema["properties"]["flag"] == {"type": "boolean"}
    assert schema["properties"]["rate"] == {"type": "number"}
    assert schema["properties"]["meta"] == {"type": "object"}


def test_optional_arguments_are_not_marked_required():
    """The bug this caught: a schema claiming a defaulted argument is required.

    The auditor validates arguments against the declared schema, so marking
    `memo` required made every well-formed payment call get denied as malformed.
    """
    bundle = build_world()
    by_name = {t.name: t for t in as_tool_definitions(bundle)}
    send = by_name["payments.send"].input_schema
    assert set(send["required"]) == {"payer", "payee", "amount_cents", "currency"}
    assert "memo" not in send["required"]
    assert send["properties"]["memo"] == {"type": ["string", "null"]}

    recv = by_name["payments.receive"].input_schema
    assert "reference" not in recv["required"]


def test_required_fields_match_the_handler_defaults():
    """No tool may require an argument its handler supplies a default for."""
    import inspect

    bundle = build_world()
    for spec in bundle.registry.list():
        handler = spec.handler
        try:
            sig = inspect.signature(handler)
        except (TypeError, ValueError):
            continue
        defaulted = {n for n, p in sig.parameters.items()
                     if p.default is not inspect.Parameter.empty
                     and p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD}
        schema = to_json_schema(spec.input_schema)
        wrongly_required = defaulted & set(schema["required"])
        assert not wrongly_required, (
            f"{spec.name} declares {sorted(wrongly_required)} as required but "
            f"its handler defaults them")


def test_every_tool_gets_a_nonempty_schema():
    bundle = build_world()
    for tool in as_tool_definitions(bundle):
        assert tool.input_schema, f"{tool.name} has an empty input schema"
        assert "properties" in tool.input_schema


# -- the action side accepts them -----------------------------------------

def test_definitions_satisfy_the_action_side_validation():
    """ToolDefinition.__post_init__ raises on any shape violation."""
    bundle = build_world()
    tools = as_tool_definitions(bundle)
    assert len(tools) == len(bundle.registry.list())
    from agent_toolkit.contracts import ToolDefinition
    assert all(isinstance(t, ToolDefinition) for t in tools)


def test_side_effect_flags_survive_conversion():
    """A silently-false flag would make a gated tool run ungated."""
    bundle = build_world()
    flags = {t.name: t.side_effect for t in as_tool_definitions(bundle)}
    assert flags["payments.send"] is True
    assert flags["email.send"] is True
    assert flags["payments.balance"] is False
    assert flags["spreadsheet.load"] is False


def test_handlers_accept_a_mapping_and_call_through():
    """agent_toolkit calls handler(arguments); fin_tools wants kwargs."""
    bundle = build_world()
    tools = {t.name: t for t in as_tool_definitions(bundle)}
    bundle.payments.receive_payment(payer="c", payee="us", amount_cents=4200,
                                    currency="CNY")
    out = tools["payments.balance"].handler({"account": "us"})
    assert out == {"balance_cents": 4200}


def test_handler_rejection_surfaces_as_an_exception():
    bundle = build_world()
    tools = {t.name: t for t in as_tool_definitions(bundle)}
    out = tools["payments.send"].handler(
        {"payer": "ops", "payee": "s", "amount_cents": 100, "currency": "CNY"})
    # The tool returns a rejected record rather than raising: the gate decided.
    assert out["state"] == "rejected"


# -- §4.1: agents cannot self-authorize -----------------------------------

def test_intent_issuing_tools_are_withheld_from_agents():
    bundle = build_world()
    names = {t.name for t in agent_tool_definitions(bundle)}
    assert not (names & NOT_AGENT_CALLABLE)
    # and no tool lets an agent mint an authorization by another name
    assert not any("confirm" in n for n in names)


def test_agent_subset_is_smaller_than_full_set():
    bundle = build_world()
    full = {t.name for t in as_tool_definitions(bundle)}
    agent = {t.name for t in agent_tool_definitions(bundle)}
    assert agent <= full


# -- the audit validator accepts them --------------------------------------

def test_generated_manifest_passes_the_audit_validator():
    """The real proof: the auditor's own strict validator accepts these tools.

    A side-effect tool with no input_schema is refused by the validator, so this
    passing means argument validation will actually fire on the financial tools.
    """
    from agent_integration.manifest_gen import build_audit_manifest, validate_generated

    bundle = build_world()
    tools = agent_tool_definitions(bundle)
    # The default mandate targets collections-specific arguments; scope rules to
    # no tool list so they apply only where the arguments exist.
    manifest = build_audit_manifest(tools, mandate={
        "extension_owner_approval_days": None,
        "invoice_owner_approval_amount": None,
        "max_contacts_in_window": None,
    })
    ok, problems = validate_generated(manifest)
    assert ok, f"audit validator rejected the unified tools: {problems}"


def test_every_gated_tool_is_declared_with_a_schema():
    """The validator denies a side-effect tool that declares no schema."""
    from agent_integration.manifest_gen import _manifest_tools

    bundle = build_world()
    declared = _manifest_tools(agent_tool_definitions(bundle))
    for entry in declared:
        if entry["side_effect"]:
            assert entry["input_schema"], f"{entry['name']} is gated but unschemad"


def test_tools_reachable_across_the_service_boundary():
    """Same definitions, but calls routed over HTTP to the service."""
    from fin_tools import HttpToolClient, ToolServer, free_port

    bundle = build_world()
    srv = ToolServer(bundle, port=free_port(), secret=b"server-only").start()
    try:
        remote = agent_tool_definitions(bundle, client=HttpToolClient(srv.base_url,
                                                                     timeout_seconds=5.0))
        by_name = {t.name: t for t in remote}
        by_name["payments.receive"].handler(
            {"payer": "c", "payee": "us", "amount_cents": 900, "currency": "CNY"})
        assert by_name["payments.balance"].handler({"account": "us"}) == \
            {"balance_cents": 900}
    finally:
        srv.stop()


def test_remote_rejection_raises_so_the_runtime_logs_a_failure():
    from fin_tools import HttpToolClient, ToolServer, free_port

    bundle = build_world()
    srv = ToolServer(bundle, port=free_port(), secret=b"server-only").start()
    try:
        remote = {t.name: t for t in
                  agent_tool_definitions(bundle,
                                         client=HttpToolClient(srv.base_url,
                                                               timeout_seconds=5.0))}
        with pytest.raises(ValueError):
            remote["payments.balance"].handler({})   # missing required argument
    finally:
        srv.stop()

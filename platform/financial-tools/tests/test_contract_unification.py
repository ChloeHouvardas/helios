"""Task 1: one contract across the three tool populations.

Three things are under test, each of which failed silently before:

1. **Two worlds in one process.** `build_world()` used to bind its services into
   module-level singletons, so the LAST world built captured every earlier
   bundle's dispatch. Two apps in one process — an app and its shadow sandbox,
   or two tenants — shared a ledger. These tests build two worlds and assert the
   money stays where it was put.

2. **A nested JSON Schema survives the bridge.** `to_json_schema` can only
   express a flat field/type map, so it types every dict-shaped field as a bare
   `object`. A teammate tool that ships real JSON Schema with enums, ranges and
   nested structure would have that detail flattened away, and the auditor would
   then accept arguments its author explicitly forbade. `input_json_schema` must
   pass through untouched.

3. **The adapters accept the other spellings.** The action side and the teammate
   packages say `side_effect` (singular) and hand over JSON Schema; this package
   says `side_effects` and a flat map. An adapter that quietly dropped the flag
   would register a side-effecting tool as read-only, which means ungated
   execution.
"""
from __future__ import annotations

import pytest

from fin_tools import TENANT_ENV, build_world, require_tenant
from fin_tools.contracts import ToolSpec, flat_from_json_schema
from fin_tools.toolkit_bridge import as_tool_definitions, to_json_schema

pytest.importorskip("agent_toolkit")


# --------------------------------------------------------------------------
# 1. two worlds, two ledgers
# --------------------------------------------------------------------------

def test_two_worlds_keep_two_separate_ledgers():
    """The trap this closes: the last build_world() used to win for both."""
    first = build_world()
    second = build_world()

    first.payments.receive_payment(payer="cust-a", payee="us",
                                   amount_cents=5_000, currency="CNY")
    second.payments.receive_payment(payer="cust-b", payee="us",
                                    amount_cents=7_000, currency="CNY")

    # Dispatch through each registry, not through the service objects directly:
    # the registry is what an agent reaches, and it is what the singleton
    # binding used to redirect.
    assert first.registry.call("payments.balance", account="us") == \
        {"balance_cents": 5_000}
    assert second.registry.call("payments.balance", account="us") == \
        {"balance_cents": 7_000}


def test_worlds_do_not_share_an_outbox():
    first = build_world()
    second = build_world()

    first.registry.call("email.send", to="a@example.com", subject="s", body="b")

    assert len(first.registry.call("email.outbox")["messages"]) == 1
    assert second.registry.call("email.outbox")["messages"] == []


def test_worlds_do_not_share_payer_profiles():
    """`simulation.forecast` read a process-global profiles dict."""
    from fin_tools import PayerProfile

    first = build_world()
    second = build_world()
    first.profiles["slow"] = PayerProfile(customer="slow", mean_delay_days=40.0)

    assert "slow" in first.profiles
    assert second.profiles == {}


def test_build_order_does_not_change_which_world_a_tool_uses():
    """Explicitly the old failure mode: build A, build B, use A."""
    a = build_world()
    b = build_world()          # would have stolen a's dispatch
    _c = build_world()         # and again

    a.payments.receive_payment(payer="p", payee="us", amount_cents=99,
                               currency="CNY")
    assert a.registry.call("payments.balance", account="us") == \
        {"balance_cents": 99}
    assert b.registry.call("payments.balance", account="us") == \
        {"balance_cents": 0}


def test_deprecated_bind_shim_still_works():
    """`bind()` is deprecated, not removed: hand-rolled callers keep working."""
    from fin_tools import payments as pay_mod

    bundle = build_world()
    pay_mod.bind(bundle.payments)
    try:
        bundle.payments.receive_payment(payer="p", payee="us", amount_cents=42,
                                        currency="CNY")
        # The singleton now points at this bundle's service, so a registry
        # registered WITHOUT an explicit instance resolves through it.
        from fin_tools.contracts import ToolRegistry
        reg = ToolRegistry()
        pay_mod.register(reg)
        assert reg.call("payments.balance", account="us") == {"balance_cents": 42}
    finally:
        pay_mod._INSTANCE.clear()


# --------------------------------------------------------------------------
# 2. a nested schema round-trips through the bridge
# --------------------------------------------------------------------------

NESTED_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "format": "email"},
        "fields": {
            "type": "object",
            "properties": {
                "amount_cents": {"type": "integer", "minimum": 1},
                "days": {"type": "integer", "enum": [15, 30, 45]},
            },
            "required": ["amount_cents"],
            "additionalProperties": False,
        },
        "idempotency_key": {"type": ["string", "null"]},
    },
    "required": ["to", "fields"],
    "additionalProperties": False,
}


def _nested_spec() -> ToolSpec:
    # A deliberately fictitious name: this spec exists to carry a nested schema
    # through the bridge, and reusing a real tool name would collide with the
    # registry once that tool exists.
    return ToolSpec(
        name="example.nested_schema",
        description="Send a templated email. Side-effecting.",
        input_schema={"to": "str", "fields": "dict",
                      "idempotency_key": "str|None"},
        output_schema={"message_id": "str"},
        side_effects=True,
        handler=lambda **kw: kw,
        tags=("email",),
        input_json_schema=NESTED_SCHEMA,
    )


def test_declared_json_schema_passes_through_untouched():
    from agent_toolkit.contracts import ToolDefinition

    spec = _nested_spec()
    definition = ToolDefinition(
        name=spec.name, description=spec.description,
        input_schema=spec.effective_input_schema(),
        output_schema=to_json_schema(spec.output_schema),
        side_effect=spec.side_effects, handler=spec.handler)

    assert definition.input_schema == NESTED_SCHEMA
    # The detail that a flat map would have destroyed:
    assert definition.input_schema["properties"]["fields"]["properties"]["days"]["enum"] \
        == [15, 30, 45]
    assert definition.input_schema["properties"]["fields"]["additionalProperties"] is False


def test_flat_conversion_alone_loses_the_nesting():
    """Why `input_json_schema` exists. Documents the loss, does not endorse it."""
    flat = to_json_schema({"to": "str", "fields": "dict",
                           "idempotency_key": "str|None"})
    assert flat["properties"]["fields"] == {"type": "object"}
    assert "enum" not in str(flat["properties"]["fields"])
    # and the JSON-Schema version does keep it
    assert "enum" in str(NESTED_SCHEMA["properties"]["fields"])


def test_effective_input_schema_prefers_json_over_flat():
    spec = _nested_spec()
    assert spec.effective_input_schema() is spec.input_json_schema


def test_effective_input_schema_falls_back_to_flat():
    bundle = build_world()
    spec = bundle.registry.get("payments.balance")
    assert spec.input_json_schema is None
    assert spec.effective_input_schema() == {"type": "object",
                                             "properties": {"account": {"type": "string"}},
                                             "required": ["account"],
                                             "additionalProperties": False}


def test_declare_emits_both_schemas_and_the_new_flags():
    decl = _nested_spec().declare()
    assert decl["input_schema"] == {"to": "str", "fields": "dict",
                                    "idempotency_key": "str|None"}
    assert decl["input_json_schema"] == NESTED_SCHEMA
    assert decl["tenant_scoped"] is False
    assert decl["records_contact"] is False


def test_bridge_uses_the_declared_schema_for_a_registered_tool():
    reg_tools = as_tool_definitions(_bundle_with_nested())
    by_name = {t.name: t for t in reg_tools}
    assert by_name["example.nested_schema"].input_schema == NESTED_SCHEMA


def _bundle_with_nested():
    bundle = build_world()
    bundle.registry.register(_nested_spec())
    return bundle


def test_round_trip_through_the_bridge_keeps_the_schema_exact():
    """spec -> ToolDefinition -> ToolSpec: the JSON Schema is unchanged."""
    bundle = _bundle_with_nested()
    definitions = {t.name: t for t in as_tool_definitions(bundle)}
    back = ToolSpec.from_definition(definitions["example.nested_schema"])
    assert back.input_json_schema == NESTED_SCHEMA
    assert back.effective_input_schema() == NESTED_SCHEMA


# --------------------------------------------------------------------------
# 3. the adapters
# --------------------------------------------------------------------------

def test_from_definition_accepts_side_effect_singular():
    from agent_toolkit.contracts import ToolDefinition

    definition = ToolDefinition(
        name="collect.refund", description="Refund. Side-effecting.",
        input_schema=NESTED_SCHEMA, output_schema={"type": "object",
                                                   "properties": {"ok": {"type": "boolean"}}},
        side_effect=True, handler=lambda args: args)

    spec = ToolSpec.from_definition(definition)
    assert spec.side_effects is True           # plural on our side
    assert spec.input_json_schema == NESTED_SCHEMA


def test_from_definition_wraps_the_mapping_handler_as_kwargs():
    from agent_toolkit.contracts import ToolDefinition

    seen = {}

    def handler(args):
        seen.update(args)
        return {"ok": True}

    definition = ToolDefinition(
        name="t", description="d", input_schema=NESTED_SCHEMA,
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        side_effect=False, handler=handler)

    spec = ToolSpec.from_definition(definition)
    assert spec.handler(to="a@example.com", fields={"amount_cents": 1}) == {"ok": True}
    assert seen == {"to": "a@example.com", "fields": {"amount_cents": 1}}


def test_from_teammate_accepts_a_plain_dict_contract():
    """The shape `email_tool.tool.TOOL` and `helios_forecast` actually use."""
    contract = {
        "name": "send_email",
        "description": "Send a templated email. Side effects, so gate it.",
        "input_schema": NESTED_SCHEMA,
        "output_schema": {"type": "object",
                          "properties": {"message_id": {"type": "string"}}},
        "side_effect": True,                    # singular, as teammates spell it
    }
    spec = ToolSpec.from_teammate(contract, handler=lambda **kw: kw,
                                  tags=("email",), records_contact=True)
    assert spec.side_effects is True
    assert spec.records_contact is True
    assert spec.tags == ("email",)
    assert spec.input_json_schema == NESTED_SCHEMA


def test_from_teammate_accepts_side_effects_plural_too():
    contract = {"name": "t", "description": "d", "side_effects": True,
                "input_schema": NESTED_SCHEMA}
    assert ToolSpec.from_teammate(contract, handler=lambda **kw: kw).side_effects is True


def test_from_teammate_reads_a_handler_key_from_the_contract():
    contract = {"name": "t", "description": "d", "side_effect": False,
                "input_schema": NESTED_SCHEMA, "handler": lambda **kw: {"ok": 1}}
    assert ToolSpec.from_teammate(contract).handler() == {"ok": 1}


def test_adapting_the_real_email_tool_contract():
    """Not a fixture: the teammate package's own TOOL dict, adapted as-is."""
    from email_tool.tool import TOOL

    spec = ToolSpec.from_teammate(TOOL, handler=lambda **kw: kw,
                                  tags=("email",), records_contact=True)
    assert spec.name == "send_email"
    assert spec.side_effects is True
    # The template enum is the detail a flat map would have dropped.
    templates = spec.effective_input_schema()["properties"]["template_id"]["enum"]
    assert set(templates) == {"invoice_delivery", "reminder_pre_due",
                              "notice_overdue", "negotiation_offer",
                              "escalate_to_owner"}


def test_adapting_the_real_forecast_registry():
    """The teammate's five tools adapt without losing their schemas."""
    from helios_forecast import list_tools

    specs = [ToolSpec.from_teammate(t, handler=lambda **kw: kw)
             for t in list_tools()]
    names = {s.name for s in specs}
    assert {"forecast_project", "forecast_simulate", "forecast_scenario",
            "behavior_profile", "promise_rate"} <= names
    for spec in specs:
        assert spec.side_effects is False        # all read-only over there
        assert spec.input_json_schema.get("properties"), spec.name


def test_adapter_rejects_a_contract_with_no_handler():
    contract = {"name": "t", "description": "d", "input_schema": NESTED_SCHEMA}
    with pytest.raises(ValueError) as exc:
        ToolSpec.from_teammate(contract)
    message = str(exc.value)
    assert "handler" in message
    assert "keyword arguments" in message


def test_adapter_reports_every_problem_at_once():
    """One round trip per typo would make these adapters unusable in practice."""
    with pytest.raises(ValueError) as exc:
        ToolSpec.from_teammate({"name": "", "description": None,
                                "input_schema": {}})
    message = str(exc.value)
    assert message.count("- ") >= 3              # name, description, schema, handler
    assert "'name'" in message
    assert "'description'" in message
    assert "'input_schema'" in message
    assert "handler" in message


def test_adapter_rejects_an_empty_schema_rather_than_registering_it():
    """A side-effecting tool with no schema is refused by the audit validator.

    Registering it anyway would produce a manifest that fails validation later,
    far from the cause.
    """
    with pytest.raises(ValueError, match="input_schema"):
        ToolSpec.from_teammate({"name": "t", "description": "d",
                                "side_effect": True, "input_schema": {}},
                               handler=lambda **kw: kw)


# --------------------------------------------------------------------------
# the flat summary derived from a JSON Schema
# --------------------------------------------------------------------------

def test_flat_summary_marks_optional_fields():
    flat = flat_from_json_schema(NESTED_SCHEMA)
    assert flat["to"] == "str"
    assert flat["fields"] == "dict"
    assert flat["idempotency_key"] == "str|None"
    assert set(flat) == {"to", "fields", "idempotency_key"}


def test_flat_summary_names_the_item_type_of_an_array():
    flat = flat_from_json_schema({
        "type": "object",
        "properties": {"rows": {"type": "array", "items": {"type": "object"}}},
        "required": ["rows"],
    })
    assert flat["rows"] == "list[dict]"


def test_flat_summary_of_a_non_schema_is_empty_not_a_crash():
    assert flat_from_json_schema(None) == {}
    assert flat_from_json_schema({"type": "object"}) == {}


# --------------------------------------------------------------------------
# tenancy
# --------------------------------------------------------------------------

def test_tenant_comes_from_the_bundle_not_an_argument():
    bundle = build_world(tenant="acme")
    assert bundle.tenant == "acme"
    assert require_tenant(bundle, tool="collect.status") == "acme"


def test_missing_tenant_is_an_error_not_a_default(monkeypatch):
    """`payments_collect` defaults a missing tenant to the string "dev".

    Letting that through would put one tenant's receivables in a bucket every
    other caller can read and refund, so the refusal is the point of the test.
    """
    monkeypatch.delenv(TENANT_ENV, raising=False)
    bundle = build_world()
    assert bundle.tenant is None
    with pytest.raises(ValueError) as exc:
        require_tenant(bundle, tool="collect.status")
    message = str(exc.value)
    assert "collect.status" in message
    assert TENANT_ENV in message
    assert "build_world(tenant=" in message


def test_tenant_is_read_from_the_environment_when_not_passed(monkeypatch):
    monkeypatch.setenv(TENANT_ENV, "from-env")
    assert build_world().tenant == "from-env"


def test_explicit_tenant_beats_the_environment(monkeypatch):
    monkeypatch.setenv(TENANT_ENV, "from-env")
    assert build_world(tenant="explicit").tenant == "explicit"

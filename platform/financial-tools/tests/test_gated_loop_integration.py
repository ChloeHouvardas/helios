"""Phase 1 proof: the gated agent loop accepts and adjudicates the financial tools.

Not a unit test of my own code — an integration assertion that the unified tool
surface survives the *other* packages' validators and that a real proposal is
adjudicated against a mandate derived from those tools.

Skips cleanly when the sibling packages are absent, so this suite still runs
offline from the financial-tools directory alone.
"""
import pytest

from fin_tools import build_world
from fin_tools.audit_manifest import (DEFAULT_FINANCIAL_MANDATE,
                                      build_financial_manifest,
                                      build_validated, manifest_to_yaml,
                                      validate)
from fin_tools.toolkit_bridge import agent_tool_definitions

pytest.importorskip("audit_agent")
pytest.importorskip("agent_toolkit")


@pytest.fixture
def tools():
    return agent_tool_definitions(build_world())


# -- manifest generation ---------------------------------------------------

def test_manifest_passes_the_audit_validator(tools):
    manifest = build_financial_manifest(tools)
    ok, problems = validate(manifest)
    assert ok, f"audit validator rejected it: {problems}"


def test_no_rule_targets_an_argument_the_tool_lacks(tools):
    """The failure this module exists to prevent."""
    manifest = build_financial_manifest(tools)
    schemas = {t["name"]: t["input_schema"].get("properties", {})
               for t in manifest["tools"]}
    for rule in manifest["rules"]:
        for limit in rule.get("limits", []):
            argument = limit.get("argument")
            if not argument:
                continue
            for name in rule["tools"]:
                assert argument in schemas[name], (
                    f"rule {rule['id']} limits '{argument}' but {name} "
                    f"does not declare it")


def test_required_argument_rules_are_derived_from_the_schemas(tools):
    manifest = build_financial_manifest(tools)
    derived = {r["id"]: r for r in manifest["rules"]
               if r["id"].endswith(".required_arguments")}
    assert derived, "expected per-tool required-argument rules"
    schemas = {t["name"]: t["input_schema"].get("required", [])
               for t in manifest["tools"]}
    for rule_id, rule in derived.items():
        tool_name = rule_id[len("fin."):-len(".required_arguments")]
        assert sorted(rule["required_arguments"]) == sorted(schemas[tool_name])


def test_collections_rules_are_absent(tools):
    """No extension_offer_days / customer_id leakage from the default mandate."""
    manifest = build_financial_manifest(tools)
    blob = manifest_to_yaml(manifest)
    assert "extension_offer_days" not in blob
    assert "customer_id" not in blob


def test_amount_ceiling_is_in_cents(tools):
    """A $10,000 ceiling is 1_000_000 cents — a 100x error otherwise."""
    assert DEFAULT_FINANCIAL_MANDATE["payment_owner_approval_cents"] == 1_000_000
    manifest = build_financial_manifest(tools)
    ceiling = next(r for r in manifest["rules"]
                   if r["id"] == "fin.large_payment_approval")
    assert ceiling["limits"][0]["max"] == 1_000_000
    # scoped only to tools that actually declare amount_cents
    assert "payments.send" in ceiling["tools"]
    assert "spreadsheet.save" not in ceiling["tools"]


def test_mandate_is_overridable(tools):
    manifest = build_financial_manifest(tools,
                                        mandate={"payment_owner_approval_cents": 500})
    ok, problems = validate(manifest)
    assert ok, problems
    ceiling = next(r for r in manifest["rules"]
                   if r["id"] == "fin.large_payment_approval")
    assert ceiling["limits"][0]["max"] == 500


def test_validator_rejects_a_rule_naming_a_missing_argument(tools):
    """The auditor refuses a manifest it could not enforce."""
    manifest = build_financial_manifest(tools)
    manifest["rules"].append({
        "id": "fin.bogus", "tools": ["payments.send"],
        "limits": [{"argument": "does_not_exist", "max": 1}],
    })
    ok, problems = validate(manifest)
    assert not ok and problems


# -- the gated loop accepts it ---------------------------------------------

def test_gated_loop_loads_the_unified_tools(tools, tmp_path):
    """End to end: write the reviewed manifest, assemble the gated runtime."""
    from agent_integration.wiring import build_integration

    bundle = build_world()
    manifest = build_validated(bundle, tools=tools, allow_unsigned_grants=True)
    path = tmp_path / "financial-audit.yaml"
    path.write_text(manifest_to_yaml(manifest), encoding="utf-8")

    integ = build_integration(
        tools=tools,
        manifest_path=path,
        app_id=manifest["app_id"],
        task_type="financial.operate",
        allow_unsigned=True,
        runs_dir=tmp_path / "runs",
        data_dir=tmp_path / "data",
    )
    wired = {t.name for t in integ.tools}
    assert "payments.send" in wired
    assert "email.send" in wired
    # the intent-issuing operations are never offered to the agent
    assert not any(n.startswith("intents.") for n in wired)


def test_large_payment_is_held_by_the_ceiling_rule(tools, tmp_path):
    """The mandate fires: a payment over the ceiling goes to the owner."""
    integ, bundle = _assemble(tools, tmp_path,
                              mandate={"payment_owner_approval_cents": 1000})
    try:
        action = _run_payment(integ, amount_cents=999_999)
        verdict = action.get("verdict") or {}
        assert verdict.get("decision") == "hold", verdict
        assert verdict.get("rule") == "fin.large_payment_approval"
        assert action.get("executed") is False
        assert bundle.payments.balance("supplier") == 0
    finally:
        integ.close()


def test_small_payment_passes_the_gate_but_the_tool_still_checks_intent(tools, tmp_path):
    """Defence in depth: the auditor approving is not the same as settling.

    The 5.00 payment is inside the 10.00 ceiling, so the auditor approves and the
    call executes — but the payment tool refuses to settle it because the auth
    code was not issued by the owner. Two independent layers, either of which
    alone would have stopped an unauthorized transfer.
    """
    integ, bundle = _assemble(tools, tmp_path,
                              mandate={"payment_owner_approval_cents": 1000})
    try:
        action = _run_payment(integ, amount_cents=500, auth_code="f" * 64)
        verdict = action.get("verdict") or {}
        assert verdict.get("decision") == "approve", verdict
        assert action.get("executed") is True
        # ...and yet no money moved, because the tool rejected the forged code.
        assert bundle.payments.balance("supplier") == 0
    finally:
        integ.close()


def _assemble(tools, tmp_path, *, mandate=None):
    """Write a reviewed manifest and build the gated runtime around it."""
    from agent_integration.wiring import build_integration

    bundle = build_world()
    manifest = build_validated(bundle, tools=tools, allow_unsigned_grants=True,
                               mandate=mandate or {})
    path = tmp_path / "financial-audit.yaml"
    path.write_text(manifest_to_yaml(manifest), encoding="utf-8")
    integ = build_integration(
        tools=tools, manifest_path=path, app_id=manifest["app_id"],
        task_type="financial.operate", allow_unsigned=True,
        runs_dir=tmp_path / "runs", data_dir=tmp_path / "data",
    )
    return integ, bundle


def _run_payment(integ, *, amount_cents, auth_code=None):
    """Drive one payment proposal through the gated loop and return its record."""
    from agent_integration.runtime import ScriptedDriver, ToolCall

    arguments = {"payer": "ops", "payee": "supplier",
                 "amount_cents": amount_cents, "currency": "CNY"}
    if auth_code is not None:
        arguments["auth_code"] = auth_code
    # The platform's baseline requires a stated intent of >= 12 characters.
    result = integ.runtime.run(
        driver=ScriptedDriver([ToolCall(name="payments.send", arguments=arguments)]),
        task="Pay supplier-x the settlement for invoice INV-1003 today",
        task_type="financial.operate",
    )
    assert result.actions, "the proposal never reached adjudication"
    return result.actions[0]

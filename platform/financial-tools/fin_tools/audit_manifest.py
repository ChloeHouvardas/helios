"""Generate the audit manifest for the unified financial tool surface.

Why this exists: the integration layer's default mandate is collections-specific.
It limits `extension_offer_days` and requires `customer_id` + `invoice_id` on
*every* gated tool, so pointing the gated loop at the financial tools fails the
auditor's own validator — the rule names an argument those tools do not have.

The fix is not to weaken validation. It is to **derive each rule from the tools
that actually declare the argument**, so a rule can never target a field that
does not exist. Required arguments are read from each tool's own schema rather
than hard-coded, which is what stops the manifest drifting from the contract.

Mandate numbers stay authored data: a ceiling is a business decision, not
something to infer.

Amounts are **integer cents** throughout. A "$10,000 needs owner approval"
ceiling is `1_000_000`. Getting this wrong is a 100x policy error, so it is
stated here and asserted in tests.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from .toolkit_bridge import agent_tool_definitions

#: Default mandate for the financial tool surface. Plain data — change and
#: regenerate rather than editing a YAML by hand.
DEFAULT_FINANCIAL_MANDATE: Dict[str, Any] = {
    # A payment at or above this many CENTS needs the owner. 10_000.00 CNY.
    "payment_owner_approval_cents": 1_000_000,
    # Receiving money needs no ceiling, but must name its parties.
    "require_parties_on_payments": True,
}


#: Tool-name families whose gated calls can move money. The owner-approval
#: ceiling is scoped to these; everything else that mentions `amount_cents`
#: is arithmetic over money, not movement of it.
MONEY_MOVING_FAMILIES = frozenset({"payments", "collect"})


def _declared_arguments(tool: Any) -> set:
    """The argument names a tool actually accepts, from its real schema."""
    return set((tool.input_schema or {}).get("properties", {}))


def _tools_declaring(tools: Sequence[Any], argument: str) -> List[str]:
    return [t.name for t in tools if argument in _declared_arguments(t)]


def _required_arguments_rule(tool: Any) -> Dict[str, Any] | None:
    """A rule requiring the tool's own mandatory arguments.

    Derived from the schema's `required` list, so it cannot name a field the
    tool does not have and cannot drift when the contract changes.
    """
    required = list((tool.input_schema or {}).get("required", []))
    if not required:
        return None
    return {
        "id": f"fin.{tool.name}.required_arguments",
        "description": (
            f"{tool.name} must name every argument it requires: "
            f"{', '.join(sorted(required))}"
        ),
        "tools": [tool.name],
        "message": f"{tool.name} was called without a required argument",
        "required_arguments": required,
    }


def build_financial_manifest(
    tools: Sequence[Any],
    *,
    app_id: str = "helios-financial-tools",
    name: str = "Financial Tools Audit Agent",
    task_type: str = "financial.operate",
    task_description: str = "Operate the platform's financial tools",
    ttl_seconds: int = 300,
    mandate: Mapping[str, Any] | None = None,
    log_path: str = "financial-audit-log.jsonl",
    allow_unsigned_grants: bool = False,
) -> Dict[str, Any]:
    """The audit manifest as data, derived from the unified tool contract."""
    tools = list(tools)
    mandate = dict(DEFAULT_FINANCIAL_MANDATE, **(mandate or {}))
    gated = [t for t in tools if t.side_effect]

    rules: List[Dict[str, Any]] = []

    ceiling = mandate.get("payment_owner_approval_cents")
    # Only gated tools in the money-moving families. A read-only cost
    # comparison or a forecast also declares `amount_cents`, and holding a
    # calculation for owner approval is friction, not safety.
    amount_tools = [
        name for name in _tools_declaring(gated, "amount_cents")
        if name.split(".", 1)[0] in MONEY_MOVING_FAMILIES
    ]
    if ceiling is not None and amount_tools:
        rules.append({
            "id": "fin.large_payment_approval",
            "description": (
                f"Owner approval for any payment at or above "
                f"{int(ceiling)} cents ({int(ceiling) / 100:,.2f})"
            ),
            "tools": amount_tools,
            "message": (
                f"this payment is at or above {int(ceiling) / 100:,.2f} and "
                f"needs owner approval"
            ),
            "limits": [{
                # An ARGUMENT limit, not a fact: it constrains what the agent
                # asked to pay. The intent auth code separately binds the human
                # to that exact amount, so understating is caught there.
                "argument": "amount_cents",
                "max": int(ceiling),
                "on_exceed": "hold",
            }],
        })

    if mandate.get("require_parties_on_payments"):
        for argument in ("payee", "payer"):
            named = _tools_declaring(gated, argument)
            if named:
                rules.append({
                    "id": f"fin.payment_names_{argument}",
                    "description": f"Every payment call must name its {argument}",
                    "tools": named,
                    "message": f"a payment call must name its {argument}",
                    "required_arguments": [argument],
                })

    # Per-tool required-argument rules, derived from each schema.
    for tool in gated:
        rule = _required_arguments_rule(tool)
        if rule is not None:
            rules.append(rule)

    manifest: Dict[str, Any] = {
        "manifest_version": "1",
        "app_id": app_id,
        "name": name,
        "description": (
            "Generated from the unified financial tool contract, so tool names "
            "and schemas cannot drift from what the runtime actually calls. "
            "Each rule targets only the tools that declare the argument it "
            "limits; the mandate numbers are authored data."
        ),
        "tools": [
            {
                "name": t.name,
                "description": t.description.strip(),
                "side_effect": bool(t.side_effect),
                "input_schema": dict(t.input_schema),
            }
            for t in tools
        ],
        "tasks": {
            task_type: {
                "description": task_description,
                "allowed_tools": [t.name for t in tools],
                "ttl_seconds": ttl_seconds,
            }
        },
        "rules": rules,
        "settings": {
            "default_ttl_seconds": ttl_seconds,
            "log_path": log_path,
            "advisory": ["heuristic"],
            "allow_unsigned_grants": bool(allow_unsigned_grants),
        },
    }
    return manifest


def manifest_to_yaml(manifest: Mapping[str, Any]) -> str:
    """Stable, reviewable YAML — insertion order kept so diffs stay meaningful."""
    import yaml

    return yaml.safe_dump(dict(manifest), sort_keys=False, allow_unicode=True,
                          width=100)


def validate(manifest: Mapping[str, Any]) -> tuple:
    """Run the audit agent's own strict validator on the generated manifest."""
    from audit_agent import validate_manifest_text

    return validate_manifest_text(manifest_to_yaml(manifest))


def build_validated(bundle: Any, **kwargs: Any) -> Dict[str, Any]:
    """Build the manifest from a bundle and refuse to return an invalid one."""
    tools = kwargs.pop("tools", None) or agent_tool_definitions(bundle)
    manifest = build_financial_manifest(tools, **kwargs)
    ok, problems = validate(manifest)
    if not ok:
        raise ValueError(
            "generated financial manifest failed the audit validator: "
            + "; ".join(str(p) for p in problems))
    return manifest


__all__ = ["DEFAULT_FINANCIAL_MANDATE", "build_financial_manifest",
           "build_validated", "manifest_to_yaml", "validate"]

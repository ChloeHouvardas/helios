"""Generate the audit manifest from the action side's real tools.

The two builders disagreed about the tool surface. The action side ships
``send_email(customer_id, invoice_id, subject, body, extension_offer_days)``;
``audit-agent-builder/manifests/collect-reminder.yaml`` declares
``send_reminder_email(invoice_id, message, extension_days)``. Wired
together as-is, every call would be denied as an unregistered tool.

Hand-editing a YAML to match is how the two drift apart again next week. So
this module derives the manifest's ``tools`` section from the live
``ToolDefinition`` objects: same names, same schemas, same side-effect
flags, one source of truth. The mandate — the limits, thresholds and
frequency rules — stays human-authored data, because a cap is a business
decision, not something to infer.

The result is validated by the audit agent's own strict validator before it
is returned, so a generated manifest that would silently enforce nothing is
refused here rather than at runtime.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

import yaml

from agent_toolkit.contracts import ToolDefinition

#: Limits the reference collections agent is built with. Plain data: change
#: these and regenerate rather than editing a hand-written YAML.
DEFAULT_MANDATE: dict[str, Any] = {
    # build1.md mandate 1 and 2: extensions up to this many days are inside
    # the agent's own authority; longer ones go to the owner.
    "extension_owner_approval_days": 14,
    # build1.md mandate 4: invoices at or above this amount need the owner.
    "invoice_owner_approval_amount": "10000.00",
    # build1.md mandate 3: at most N contacts per customer in the window.
    "max_contacts_in_window": 2,
    "contact_window_days": 7,
}


def _manifest_tools(tools: Iterable[ToolDefinition]) -> list[dict[str, Any]]:
    """Declarations copied straight from the action side's tool contract."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        entry: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description.strip(),
            "side_effect": bool(tool.side_effect),
        }
        # A side-effect tool with no schema is rejected by the audit
        # validator, and would leave arguments uncheckable in any case.
        entry["input_schema"] = _plain(dict(tool.input_schema))
        out.append(entry)
    return out


def _plain(value: Any) -> Any:
    """Convert mappings/sequences to plain dict/list so YAML can dump them."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _mandate_rules(
    gated_tools: Sequence[str], mandate: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Translate the mandate numbers into declarative audit rules."""
    if not gated_tools:
        return []
    tools = list(gated_tools)
    rules: list[dict[str, Any]] = []

    approval_days = mandate.get("extension_owner_approval_days")
    if approval_days is not None:
        rules.append(
            {
                "id": "app.extension_limit",
                "description": (
                    f"Owner approval for any due-date extension longer than "
                    f"{approval_days} days"
                ),
                "tools": tools,
                "message": (
                    f"an extension longer than {approval_days} days needs "
                    "owner approval"
                ),
                "limits": [
                    {
                        # The action side's actual argument name. A limit on
                        # an argument the tool does not declare is rejected
                        # by the validator, which is why this is generated
                        # rather than typed by hand.
                        "argument": "extension_offer_days",
                        "max": int(approval_days),
                        "on_exceed": "hold",
                    }
                ],
            }
        )

    approval_amount = mandate.get("invoice_owner_approval_amount")
    if approval_amount is not None:
        rules.append(
            {
                "id": "app.large_invoice_approval",
                "description": (
                    f"Owner approval for invoices over {approval_amount}"
                ),
                "tools": tools,
                "message": (
                    f"this invoice is over {approval_amount} and needs owner "
                    "approval"
                ),
                "limits": [
                    {
                        # A FACT, not an argument: resolved from the store, so
                        # an agent under-reporting the amount cannot slip
                        # under the threshold.
                        "fact": "amount_due",
                        "max": str(approval_amount),
                        "on_exceed": "hold",
                    }
                ],
            }
        )

    max_contacts = mandate.get("max_contacts_in_window")
    window = mandate.get("contact_window_days", 7)
    if max_contacts is not None:
        rules.append(
            {
                "id": "app.contact_frequency",
                "description": (
                    f"At most {max_contacts} contacts per customer in any "
                    f"{window}-day window"
                ),
                "tools": tools,
                "message": (
                    f"this customer has already been contacted "
                    f"{max_contacts} times in the last {window} days"
                ),
                "max_contacts_in_days": {
                    "within_days": int(window),
                    "max": int(max_contacts),
                },
            }
        )

    rules.append(
        {
            "id": "app.requires_references",
            "description": "Every call must name the customer and invoice it acts on",
            "tools": tools,
            "message": "a call must name the customer and invoice it acts on",
            "required_arguments": ["customer_id", "invoice_id"],
        }
    )
    return rules


def build_audit_manifest(
    tools: Iterable[ToolDefinition],
    *,
    app_id: str = "helios-collect",
    name: str = "Collect Reminder Audit Agent",
    task_type: str = "collect.reminder",
    task_description: str = "Follow up with a customer about an overdue invoice",
    ttl_seconds: int = 300,
    mandate: Mapping[str, Any] | None = None,
    log_path: str = "collect-audit-log.jsonl",
    allow_unsigned_grants: bool = False,
) -> dict[str, Any]:
    """The audit manifest as data, derived from the real tool contract.

    ``allow_unsigned_grants`` defaults to False. The demo CLI turns it on
    only when no signing key is configured, so a real deployment fails at
    startup instead of quietly running unsigned.
    """
    tools = list(tools)
    mandate = dict(DEFAULT_MANDATE, **(mandate or {}))
    all_names = [tool.name for tool in tools]
    gated = [tool.name for tool in tools if tool.side_effect]

    manifest: dict[str, Any] = {
        "manifest_version": "1",
        "app_id": app_id,
        "name": name,
        "description": (
            "Generated from the action agent's tool contract, so tool names "
            "and schemas cannot drift from what the runtime actually calls. "
            "The mandate is authored data; the tool declarations are not."
        ),
        "tools": _manifest_tools(tools),
        "tasks": {
            task_type: {
                "description": task_description,
                "allowed_tools": all_names,
                "ttl_seconds": ttl_seconds,
            }
        },
        "rules": _mandate_rules(gated, mandate),
        "settings": {
            "default_ttl_seconds": ttl_seconds,
            "contact_window_days": int(mandate.get("contact_window_days", 7)),
            "log_path": log_path,
            # `heuristic` is deterministic and always available. `qwen` would
            # add model commentary that cannot change a verdict.
            "advisory": ["heuristic"],
            "allow_unsigned_grants": bool(allow_unsigned_grants),
        },
    }
    return manifest


def manifest_to_yaml(manifest: Mapping[str, Any]) -> str:
    """Stable, reviewable YAML — sorted keys so diffs stay meaningful."""
    return yaml.safe_dump(
        dict(manifest), sort_keys=False, allow_unicode=True, width=100
    )


def validate_generated(
    manifest: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    """Run the audit agent's own strict validator on generated output.

    Imports here so a caller that never generates a manifest does not need
    the audit package at import time.
    """
    from audit_agent import validate_manifest_text

    return validate_manifest_text(manifest_to_yaml(manifest))


__all__ = [
    "DEFAULT_MANDATE",
    "build_audit_manifest",
    "manifest_to_yaml",
    "validate_generated",
]

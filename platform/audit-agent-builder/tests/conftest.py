"""Shared fixtures: a registry, a policy pack and an audit agent.

Offline by design — no model, no network, no API key. The audit agent's
decisions are deterministic, so every test here is reproducible.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from audit_agent import (
    ActionRequest,
    AuditAgent,
    AuthorizationRequest,
    Keyring,
    PolicyPack,
    StaticFactSource,
    ToolDescriptor,
    ToolRegistry,
)

#: Package root (parents[1] from tests/conftest.py), so the reference
#: manifests can be loaded by tests without hard-coding an absolute path.
PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFESTS = PACKAGE_ROOT / "manifests"

REMINDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["invoice_id", "message"],
    "properties": {
        "invoice_id": {"type": "string", "minLength": 1},
        "message": {"type": "string", "minLength": 1, "maxLength": 2000},
        "extension_days": {"type": "integer", "minimum": 0, "maximum": 3650},
    },
}


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDescriptor(
                name="lookup_invoice", description="read an invoice", side_effect=False
            ),
            ToolDescriptor(
                name="send_reminder_email",
                description="send a reminder",
                side_effect=True,
                input_schema=REMINDER_SCHEMA,
            ),
        ]
    )


@pytest.fixture
def collect_pack() -> PolicyPack:
    return PolicyPack.from_yaml_text(
        """
name: collect
source: app
tasks:
  collect.reminder:
    description: chase an overdue invoice
    allowed_tools: [lookup_invoice, send_reminder_email]
    ttl_seconds: 300
rules:
  - id: app.extension_limit
    tools: [send_reminder_email]
    message: an extension longer than 14 days needs owner approval
    limits:
      - argument: extension_days
        max: 14
        on_exceed: hold
  - id: app.contact_frequency
    tools: [send_reminder_email]
    message: a customer may be contacted at most twice in 7 days
    max_contacts_in_days: {within_days: 7, max: 2}
  - id: app.large_invoice_approval
    tools: [send_reminder_email]
    message: invoices over 10000 need owner approval
    limits:
      - fact: amount_due
        max: "10000.00"
        on_exceed: hold
""",
        origin="collect.yaml",
    )


@pytest.fixture
def fact_source() -> StaticFactSource:
    """Trusted facts. INV-1001 is small and untouched; INV-1002 is large.

    INV-1003 has two recent contacts, so it trips the frequency rule.
    """
    return StaticFactSource(
        amounts={
            "INV-1001": "450.00",
            "INV-1002": "24500.00",
            "INV-1003": "820.50",
        },
        contacts={"INV-1001": 0, "INV-1002": 0, "INV-1003": 2},
    )


@pytest.fixture
def log_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def agent(
    registry: ToolRegistry,
    collect_pack: PolicyPack,
    fact_source: StaticFactSource,
    log_path: pathlib.Path,
) -> AuditAgent:
    return AuditAgent.build(
        registry,
        app_packs=[collect_pack],
        fact_source=fact_source,
        log_path=log_path,
        keyring=Keyring(b"test-signing-key", "test-key"),
    )


def make_authorization(
    agent: AuditAgent,
    *,
    request_id: str = "r1",
    invoice_id: str = "INV-1001",
    tools: list[str] | None = None,
    app_id: str = "helios-collect",
    agent_id: str = "collect-reminder",
    principal: str = "owner",
    summary: str = "Chase the overdue invoice and offer a short extension",
):
    """Ask the audit agent what an action agent may do for a task."""
    return agent.authorize(
        AuthorizationRequest(
            request_id=request_id,
            app_id=app_id,
            agent_id=agent_id,
            principal=principal,
            task_type="collect.reminder",
            task_summary=summary,
            requested_tools=tools or ["lookup_invoice", "send_reminder_email"],
            context={"invoice_id": invoice_id},
        )
    )


def make_action(
    agent: AuditAgent,
    arguments: dict[str, Any],
    authorization: Any,
    *,
    request_id: str = "c1",
    tool: str = "send_reminder_email",
    app_id: str = "helios-collect",
    agent_id: str = "collect-reminder",
    principal: str = "owner",
    resolved_facts: dict[str, Any] | None = None,
):
    """Ask the audit agent whether one concrete call may proceed."""
    return agent.adjudicate(
        ActionRequest(
            request_id=request_id,
            app_id=app_id,
            agent_id=agent_id,
            principal=principal,
            task_type="collect.reminder",
            tool=tool,
            arguments=arguments,
            authorization=authorization,
            resolved_facts=resolved_facts or {},
        )
    )

"""Helios agent integration: make the two builders work together.

``action-agent-builder`` provides the tool contract, mock tools, store,
manifests and action log. ``audit-agent-builder`` provides the standalone
auditor: signed grants, deterministic fail-closed rules and hash-chained
evidence. Neither imports the other, and neither should — the audit agent is
only trustworthy because the app it audits cannot modify it.

This package is the seam between them, and the one place in the repo allowed
to know about both:

- ``adapters``    — closes the shape mismatch, including the one where a
  gated tool would have registered as read-only and never been checked.
- ``manifest_gen``— derives the audit manifest from the real tool contract,
  so the two cannot drift apart again.
- ``bridge``      — authorize a task, then adjudicate every side effect.
- ``runtime``     — the gated agent loop Build 1 specified but never shipped.
- ``wiring``      — ``build_integration()``, one call to assemble it all.
- ``model_driver``— the real model (GLM-5.3, OpenAI-compatible), stdlib HTTP, imported lazily.
- ``cli``         — demo, run, inspect, manifest, selfcheck.

Start with ``build_integration()``, or ``python -m agent_integration.cli demo``.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .adapters import HeliosStoreFactSource, ToolContractView, tool_views
from .bridge import AuditBridge, GateResult, TaskSession
from .manifest_gen import (
    DEFAULT_MANDATE,
    build_audit_manifest,
    manifest_to_yaml,
    validate_generated,
)
from .runtime import (
    COMPLETED,
    ERROR,
    HELD,
    REFUSED,
    Driver,
    GatedAgentRuntime,
    RunResult,
    ScriptedDriver,
    ToolCall,
)
from .wiring import Integration, build_integration, build_runtime_for

__all__ = [
    "__version__",
    # Adapters
    "HeliosStoreFactSource",
    "ToolContractView",
    "tool_views",
    # Bridge
    "AuditBridge",
    "GateResult",
    "TaskSession",
    # Manifest generation
    "DEFAULT_MANDATE",
    "build_audit_manifest",
    "manifest_to_yaml",
    "validate_generated",
    # Runtime
    "COMPLETED",
    "Driver",
    "ERROR",
    "GatedAgentRuntime",
    "HELD",
    "REFUSED",
    "RunResult",
    "ScriptedDriver",
    "ToolCall",
    # Wiring
    "Integration",
    "build_integration",
    "build_runtime_for",
]

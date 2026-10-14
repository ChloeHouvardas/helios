"""Wire the two builders into one runnable system.

``build_integration()`` is the single entry point: point it at the action
side's tools and store, and it returns a bundle containing a live auditor,
a bridge, and a gated runtime that are already connected. This is what the
CLI and the tests both use, so there is one assembly path rather than two
that can drift.

The auditor's manifest is *generated* from the action side's tool contract
(``manifest_gen``), which is what stops the two packages disagreeing about
which tools exist. Nothing here edits either package.

Transports
----------
``transport="inprocess"`` is the default and needs no ports or files.
``"file"`` and ``"http"`` start the auditor's own server thread and talk to
it over the wire, which is how the deployment shape looks: the auditor is
reachable only through the contract, never by import.

Signing key
-----------
``HELIOS_AUDIT_SIGNING_KEY`` is used when set. Without it, a random
ephemeral key is generated so the demo still exercises *signed* grants —
which is the more honest demo, since an unsigned grant is not what a
deployment runs. The bundle reports ``ephemeral_key`` so callers can warn.
Set the environment variable for anything real.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import yaml

from agent_toolkit.contracts import ToolDefinition
from agent_toolkit.store import Store
from agent_toolkit.tools import build_mock_tools

from .adapters import HeliosStoreFactSource, ToolContractView
from .bridge import AuditBridge
from .manifest_gen import build_audit_manifest, manifest_to_yaml
from .runtime import GatedAgentRuntime

#: The checked-in manifest the auditor is governed by. Sibling packages are
#: editable installs, so resolve it from this file rather than the cwd.
DEFAULT_MANIFEST_PATH: Path = (
    Path(__file__).resolve().parents[2] / "manifests" / "collect-reminder.yaml"
)


@dataclass
class Integration:
    """Everything needed to run one gated agent, already connected."""

    store: Store
    tools: list[ToolDefinition]
    fact_source: HeliosStoreFactSource
    audit_agent: Any
    client: Any
    bridge: AuditBridge
    runtime: GatedAgentRuntime
    manifest: dict[str, Any]
    #: True when the signing key was generated for this process only.
    ephemeral_key: bool = False
    #: True when the manifest was generated from this call's arguments instead
    #: of loaded from a reviewed file. A self-declared manifest is not a
    #: constraint on the app that declared it.
    self_declared_manifest: bool = False
    #: Where the manifest came from, when it was loaded.
    manifest_path: str = ""
    #: Servers to shut down; empty for in-process.
    _servers: list[Any] = field(default_factory=list)

    def close(self) -> None:
        """Stop any transport server started for this bundle."""
        for server in self._servers:
            stop = getattr(server, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        self._servers.clear()

    def capabilities(self) -> dict[str, Any]:
        """The auditor's own description of what it governs."""
        return self.client.capabilities()


def _keyring(*, allow_unsigned: bool = False) -> tuple[Any, bool]:
    """Resolve the signing keyring, reporting whether it is ephemeral."""
    from audit_agent import Keyring

    raw = os.environ.get(Keyring.ENV_VAR, "").strip()
    key_id = os.environ.get("HELIOS_AUDIT_SIGNING_KEY_ID", "").strip()
    if raw:
        return Keyring(key=raw.encode("utf-8"), key_id=key_id or "helios-audit-local"), False
    if allow_unsigned:
        return Keyring(allow_unsigned=True), True
    # Ephemeral, process-local. Grants are still signed, so the replay and
    # tamper checks are exercised; they simply do not survive a restart.
    return Keyring(
        key=secrets.token_hex(32).encode("utf-8"),
        key_id=key_id or "helios-audit-ephemeral",
    ), True


def build_integration(
    *,
    store: Store | None = None,
    tools: Sequence[ToolDefinition] | None = None,
    app_id: str = "helios-collect",
    agent_id: str = "collect-reminder",
    agent_name: str | None = None,
    task_type: str = "collect.reminder",
    instructions: str = "",
    principal: str = "owner",
    transport: str = "inprocess",
    runs_dir: str | Path = ".runs",
    data_dir: str | Path = ".helios-data",
    mandate: dict[str, Any] | None = None,
    contact_window_days: int = 7,
    allow_unsigned: bool = False,
    audit_log_path: str | Path | None = None,
    manifest_path: str | Path | None = DEFAULT_MANIFEST_PATH,
) -> Integration:
    """Assemble the auditor, the bridge and the runtime.

    ``manifest_path`` defaults to the checked-in generated manifest. Loading a
    file rather than regenerating from the caller's arguments matters: if the
    manifest were generated from whatever ``task_type`` the app asked for, the
    app would be writing its own permission slip, and the auditor would grant
    anything. The manifest is a reviewed artifact; ``agent-toolkit`` can ask
    for a task type, but only the manifest decides whether one exists.

    Pass ``manifest_path=None`` to generate one on the fly — useful in tests,
    and clearly marked as self-declared in the returned bundle.
    """
    from audit_agent import (
        AuditManifest,
        FileTransport,
        FileTransportServer,
        HttpTransport,
        HttpTransportServer,
        InProcessTransport,
        build_audit_agent,
    )

    store = store or Store.from_fixtures()
    tools = list(tools) if tools is not None else build_mock_tools(store)

    runs_dir = Path(runs_dir)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    log_target = audit_log_path or (data_dir / "collect-audit-log.jsonl")

    # 1. The audit manifest: loaded when a file is configured, generated only
    #    when explicitly asked for. ``manifest_dict`` is exactly the reviewed
    #    data — no model round-trip, so nothing is silently reshaped.
    if manifest_path is not None:
        path = Path(manifest_path)
        if not path.exists():
            raise FileNotFoundError(
                f"no audit manifest at {path}. Generate one with "
                "`agent-integration manifest --out <path>` and review it; a "
                "manifest the app generates for itself at runtime is not a "
                "constraint on the app."
            )
        text = path.read_text(encoding="utf-8")
        manifest_dict = yaml.safe_load(text) or {}
        manifest = AuditManifest.parse(text, origin=str(path))
        self_declared = False
    else:
        manifest_dict = build_audit_manifest(
            tools,
            app_id=app_id,
            task_type=task_type,
            mandate=mandate,
            log_path=str(log_target),
            allow_unsigned_grants=allow_unsigned,
        )
        manifest = AuditManifest.parse(
            manifest_to_yaml(manifest_dict), origin="<generated>"
        )
        self_declared = True

    # 2. Trusted facts, read from the same store the tools write to.
    fact_source = HeliosStoreFactSource(store, contact_window_days=contact_window_days)

    # 3. A live auditor. Its registry is built from adapted views of the
    #    action side's tools, so the side-effect flags and schemas match.
    keyring, ephemeral = _keyring(allow_unsigned=allow_unsigned)
    audit_agent = build_audit_agent(
        manifest,
        fact_source=fact_source,
        keyring=keyring,
        log_path=Path(log_target),
    )
    # The auditor's registry comes from the manifest. Register any action-side
    # tool the manifest does not declare, so the auditor can at least answer
    # "not registered" with the correct side-effect flag rather than treating
    # it as unknown-but-read-only.
    for view in (ToolContractView(tool) for tool in tools):
        if not audit_agent.registry.has(view.name):
            from audit_agent import ToolDescriptor

            audit_agent.registry.register(ToolDescriptor.from_contract(view))

    # 4. A client over the chosen transport.
    servers: list[Any] = []
    if transport == "inprocess":
        client: Any = InProcessTransport(audit_agent)
    elif transport == "file":
        spool = data_dir / "audit-spool"
        server = FileTransportServer(audit_agent, spool).start()
        servers.append(server)
        client = FileTransport(spool)
    elif transport == "http":
        server = HttpTransportServer(audit_agent).start()
        servers.append(server)
        client = HttpTransport(server.base_url)
    else:
        raise ValueError(
            f"unknown transport {transport!r}; use 'inprocess', 'file' or 'http'"
        )

    # 5. The bridge and the runtime it feeds.
    bridge = AuditBridge(
        client, app_id=manifest.app_id, agent_id=agent_id, principal=principal
    )
    runtime = GatedAgentRuntime(
        bridge=bridge,
        tools=tools,
        store=store,
        fact_source=fact_source,
        runs_dir=runs_dir,
        agent_name=agent_name or agent_id,
        instructions=instructions,
    )

    return Integration(
        store=store,
        tools=tools,
        fact_source=fact_source,
        audit_agent=audit_agent,
        client=client,
        bridge=bridge,
        runtime=runtime,
        manifest=manifest_dict,
        ephemeral_key=ephemeral,
        self_declared_manifest=self_declared,
        manifest_path=str(path) if manifest_path is not None else "",
        _servers=servers,
    )


def build_runtime_for(
    integration: Integration,
    *,
    agent_id: str,
    instructions: str = "",
    runs_dir: str | Path = ".runs",
) -> GatedAgentRuntime:
    """A second runtime sharing one auditor — several action agents, one gate.

    The bridge is per-agent (its identity goes into every request), so each
    agent gets its own while sharing the auditor, store and fact source.
    """
    bridge = AuditBridge(
        integration.client,
        app_id=integration.audit_agent.manifest.app_id,
        agent_id=agent_id,
        principal=integration.bridge.principal,
    )
    return GatedAgentRuntime(
        bridge=bridge,
        tools=integration.tools,
        store=integration.store,
        fact_source=integration.fact_source,
        runs_dir=Path(runs_dir),
        agent_name=agent_id,
        instructions=instructions,
    )


__all__ = ["Integration", "build_integration", "build_runtime_for"]

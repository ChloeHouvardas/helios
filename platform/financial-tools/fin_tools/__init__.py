"""fin_tools — the Financial Tools Developer Interface (prototype).

One entry point wires an in-memory world plus every tool family into the
shared ToolRegistry (specs/interfaces.md §6):

    from fin_tools import build_world
    bundle = build_world()
    bundle.registry.catalog()          # what an agent/audit gate sees
    bundle.registry.call("payments.balance", account="acme")

Nothing here touches the network, a database, or real money.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional

from .contracts import (
    REGISTRY,
    TENANT_ENV,
    ToolRegistry,
    ToolSpec,
    World,
    contract_problems,
    flat_from_json_schema,
    problem_message,
    require_tenant,
)
from .collect import Collect
from .email_sender import EmailSender
from .intent_verification import IntentService, Intent, AuthCode, VerificationError
from .knowledge_graph import KnowledgeGraph
from .payments import Payments
from .simulation import PayerProfile, SimPolicy, simulate, what_if, sandbox_run
from . import collect as _collect_mod
from . import email_sender as _email_mod
from . import forecast as _forecast_mod
from .market_data import tools as _market_mod
from .market_data import build_resolver as _build_market_resolver
from . import payments as _pay_mod
from . import spreadsheet_tools as _sheet_mod
from . import knowledge_graph as _graph_mod
from . import simulation as _sim_mod
from .service import (
    SECRET_ENV,
    HttpToolClient,
    InProcessToolClient,
    ServiceUnavailable,
    ToolClient,
    ToolServer,
    free_port,
)
from .toolkit_bridge import (
    NOT_AGENT_CALLABLE,
    agent_tool_definitions,
    as_tool_definitions,
    to_json_schema,
)

#: Environment variable naming the directory for anything that must survive a
#: process: idempotency records, the market-data cache. Never a secret.
DATA_DIR_ENV = "HELIOS_DATA_DIR"


@dataclass
class Bundle:
    world: World
    registry: ToolRegistry
    intents: IntentService
    payments: Payments
    email: EmailSender
    graph: KnowledgeGraph
    collect: Collect
    #: The tenant this bundle's data belongs to. `None` means "not multi-tenant
    #: yet": tenant-scoped tools refuse rather than silently defaulting, because
    #: a wrong default writes one tenant's money into another's books.
    tenant: Optional[str] = None
    #: Directory for state that must outlive the process. `None` keeps
    #: everything in memory, which is what tests want.
    data_dir: Optional[str] = None
    #: This bundle's payer profiles. Was process-global `_PROFILES`; now per
    #: bundle so two worlds cannot share a payer book.
    profiles: Dict[str, PayerProfile] = field(default_factory=dict)


def build_world(*, registry: Optional[ToolRegistry] = None,
                secret: bytes = b"dev-only-secret-rotate-me",
                clock=None,
                tenant: Optional[str] = None,
                data_dir: Optional[str] = None) -> Bundle:
    """Create a fresh world + services and register all tools onto a registry.

    Each registration receives its own service instance, so the closures are
    bound to THIS bundle. That is deliberate: the module-level `bind()` /
    `set_profiles()` singletons made the last `build_world()` in a process win
    for every earlier bundle's dispatch, which corrupts state as soon as two
    worlds exist — an app and its shadow sandbox, or two tenants. The singletons
    are still callable for backwards compatibility but are no longer used here.

    Args:
        registry: register onto an existing registry. Omit for a fresh one.
        secret: HMAC key for intent auth codes. Server-side only.
        clock: injectable clock for deterministic tests.
        tenant: tenant id for tenant-scoped tools. Falls back to `HELIOS_TENANT`.
        data_dir: directory for durable state. Falls back to `HELIOS_DATA_DIR`.
    """
    reg = registry if registry is not None else ToolRegistry()
    resolved_tenant = tenant if tenant is not None else (
        os.environ.get(TENANT_ENV) or None)
    resolved_data_dir = data_dir if data_dir is not None else (
        os.environ.get(DATA_DIR_ENV) or None)

    world = World()
    intents = IntentService(secret=secret, **({"clock": clock} if clock else {}))
    payments = Payments(world=world, intents=intents,
                        **({"clock": clock} if clock else {}))
    # The tenant selects this bundle's SMTP credentials and idempotency store,
    # so it has to reach the sender rather than be looked up per call.
    email = EmailSender(world=world, tenant=resolved_tenant,
                        data_dir=resolved_data_dir)
    graph = KnowledgeGraph()
    profiles: Dict[str, PayerProfile] = {}
    # Receivables share this bundle's tenant, ledger and intent service: a
    # refund verifies an auth code through the same IntentService payments.send
    # uses, and captures/refunds mirror into the same world.ledger.
    collect = Collect(tenant=resolved_tenant, payments=payments, intents=intents)
    # Market data resolves per this bundle's mode/data_dir; market.rate publishes
    # into this bundle's graph, so the rate service gets the same `graph`.
    market_resolver = _build_market_resolver(data_dir=resolved_data_dir)

    _pay_mod.register(reg, payments)
    _email_mod.register(reg, email)
    _sheet_mod.register(reg)
    _graph_mod.register(reg, graph)
    _sim_mod.register(reg, profiles)
    _collect_mod.register(reg, collect)
    _forecast_mod.register(reg)
    _market_mod.register(reg, market_resolver, graph)
    return Bundle(world=world, registry=reg, intents=intents,
                  payments=payments, email=email, graph=graph, collect=collect,
                  tenant=resolved_tenant, data_dir=resolved_data_dir,
                  profiles=profiles)


__all__ = [
    "REGISTRY", "ToolRegistry", "ToolSpec", "World", "Bundle", "build_world",
    "TENANT_ENV", "DATA_DIR_ENV", "require_tenant",
    "IntentService", "Intent", "AuthCode", "VerificationError",
    "Payments", "EmailSender", "KnowledgeGraph", "Collect",
    "PayerProfile", "SimPolicy", "simulate", "what_if", "sandbox_run",
    "ToolClient", "InProcessToolClient", "HttpToolClient", "ToolServer",
    "ServiceUnavailable", "SECRET_ENV", "free_port",
    "to_json_schema", "as_tool_definitions", "agent_tool_definitions",
    "NOT_AGENT_CALLABLE",
    "flat_from_json_schema", "contract_problems", "problem_message",
]

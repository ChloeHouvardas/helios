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

from dataclasses import dataclass
from typing import Optional

from .contracts import REGISTRY, ToolRegistry, ToolSpec, World
from .email_sender import EmailSender
from .intent_verification import IntentService, Intent, AuthCode, VerificationError
from .knowledge_graph import KnowledgeGraph
from .payments import Payments
from .simulation import PayerProfile, SimPolicy, simulate, what_if, sandbox_run
from . import email_sender as _email_mod
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


@dataclass
class Bundle:
    world: World
    registry: ToolRegistry
    intents: IntentService
    payments: Payments
    email: EmailSender
    graph: KnowledgeGraph


def build_world(*, registry: Optional[ToolRegistry] = None,
                secret: bytes = b"dev-only-secret-rotate-me",
                clock=None) -> Bundle:
    """Create a fresh world + services and register all tools onto a registry."""
    reg = registry if registry is not None else ToolRegistry()
    world = World()
    intents = IntentService(secret=secret, **({"clock": clock} if clock else {}))
    payments = Payments(world=world, intents=intents,
                        **({"clock": clock} if clock else {}))
    email = EmailSender(world=world)
    graph = KnowledgeGraph()

    _pay_mod.bind(payments)
    _email_mod.bind(email)

    _pay_mod.register(reg)
    _email_mod.register(reg)
    _sheet_mod.register(reg)
    _graph_mod.register(reg, graph)
    _sim_mod.register(reg)
    return Bundle(world=world, registry=reg, intents=intents,
                  payments=payments, email=email, graph=graph)


__all__ = [
    "REGISTRY", "ToolRegistry", "ToolSpec", "World", "Bundle", "build_world",
    "IntentService", "Intent", "AuthCode", "VerificationError",
    "Payments", "EmailSender", "KnowledgeGraph",
    "PayerProfile", "SimPolicy", "simulate", "what_if", "sandbox_run",
    "ToolClient", "InProcessToolClient", "HttpToolClient", "ToolServer",
    "ServiceUnavailable", "SECRET_ENV", "free_port",
]

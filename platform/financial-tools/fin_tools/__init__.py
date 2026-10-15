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
from . import market_data as _market_mod
from .market_data import MarketDataError, TushareClient
from .service import (
    SECRET_ENV,
    HttpToolClient,
    InProcessToolClient,
    ServiceUnavailable,
    ToolClient,
    ToolServer,
    UpstreamError,
    free_port,
)
from .toolkit_bridge import (
    NOT_AGENT_CALLABLE,
    agent_tool_definitions,
    as_tool_definitions,
    to_json_schema,
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
                clock=None,
                market_client: Optional[TushareClient] = None,
                post_json=None) -> Bundle:
    """Create a fresh world + services and register all tools onto a registry.

    Args:
        registry: register onto an existing registry; omit for a fresh one.
        secret: HMAC key for intent auth codes. Server-side only.
        clock: injectable clock, so tests are deterministic.
        market_client: a preconfigured `TushareClient`. Tests pass one with a
            fake `post_json` and never reach a socket.
        post_json: shorthand for injecting only the transport. Ignored when
            `market_client` is given. Same idea as `clock=`: the seam that lets a
            network-touching family be tested offline.

    The market tools are the only ones that may touch the network, and only when
    called with `TUSHARE_TOKEN` set. Everything else stays offline and mock.
    """
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

    if market_client is None:
        market_client = TushareClient(**({"post_json": post_json}
                                         if post_json is not None else {}))
    # The market tools are handed the client, never the `world`: they cannot
    # reach ledger or balance state. See market_data's docstring.
    _market_mod.register(reg, market_client)

    return Bundle(world=world, registry=reg, intents=intents,
                  payments=payments, email=email, graph=graph)


__all__ = [
    "REGISTRY", "ToolRegistry", "ToolSpec", "World", "Bundle", "build_world",
    "IntentService", "Intent", "AuthCode", "VerificationError",
    "Payments", "EmailSender", "KnowledgeGraph",
    "PayerProfile", "SimPolicy", "simulate", "what_if", "sandbox_run",
    "ToolClient", "InProcessToolClient", "HttpToolClient", "ToolServer",
    "ServiceUnavailable", "UpstreamError", "SECRET_ENV", "free_port",
    "TushareClient", "MarketDataError",
    "to_json_schema", "as_tool_definitions", "agent_tool_definitions",
    "NOT_AGENT_CALLABLE",
]

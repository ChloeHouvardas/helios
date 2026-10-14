"""Tool Contract + registry (specs/interfaces.md §6).

Every financial tool in this package registers here with:
  - name, description
  - input schema / output schema (plain dicts describing fields)
  - side_effects flag: True  -> call becomes a *proposal* (audit gate / intent check)
                       False -> read-only, runs directly

Any implementation following this contract (including mocks) can be swapped in
without changing the agent runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, str]
    output_schema: Dict[str, str]
    side_effects: bool
    handler: Callable[..., Any]
    tags: tuple = ()

    def declare(self) -> Dict[str, Any]:
        """JSON-able declaration, the shape an agent/audit gate consumes."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "side_effects": self.side_effects,
            "tags": list(self.tags),
        }


class ToolRegistry:
    """Minimal registry: register, discover, dispatch."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, str],
        output_schema: Dict[str, str],
        side_effects: bool,
        tags: tuple = (),
    ) -> Callable[[Callable[..., Any]], ToolSpec]:
        def deco(fn: Callable[..., Any]) -> ToolSpec:
            return self.register(
                ToolSpec(name, description, input_schema, output_schema,
                         side_effects, fn, tags)
            )
        return deco

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name}") from None

    def list(self, *, side_effects: Optional[bool] = None) -> List[ToolSpec]:
        tools = list(self._tools.values())
        if side_effects is not None:
            tools = [t for t in tools if t.side_effects is side_effects]
        return sorted(tools, key=lambda t: t.name)

    def catalog(self) -> List[Dict[str, Any]]:
        return [t.declare() for t in self.list()]

    def call(self, name: str, **kwargs: Any) -> Any:
        return self.get(name).handler(**kwargs)


#: The single shared registry for this package.
REGISTRY = ToolRegistry()


# ---------------------------------------------------------------------------
# World state shared by the prototypes (kept deliberately tiny)
# ---------------------------------------------------------------------------

@dataclass
class World:
    """In-memory world the tools act on.

    Everything money-shaped is integer cents. No floats, ever.
    """
    ledger: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # payment_id -> record
    outbox: List[Dict[str, Any]] = field(default_factory=list)        # sent emails
    invoices: List[Dict[str, Any]] = field(default_factory=list)      # invoice rows
    graph: Dict[str, Dict[str, Any]] = field(default_factory=dict)    # node_id -> node
    edges: List[Dict[str, Any]] = field(default_factory=list)
    balances: Dict[str, int] = field(default_factory=dict)            # account -> cents

"""The tool contract: how every tool, real or mock, is described to agents
and to the audit gate.

A tool is a ToolDefinition: name, description, input schema, output schema,
a side-effect flag, and a handler. The side-effect flag decides whether the
runtime executes a call directly or routes it through the audit gate as a
proposal. Any implementation following this contract — mock or real — can
be swapped in without changing the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

ToolHandler = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class ToolDefinition:
    """One tool following the Helios tool contract."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    side_effect: bool
    handler: ToolHandler

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("tool name must be a non-empty string")
        if not isinstance(self.description, str) or not self.description:
            raise ValueError(f"tool {self.name}: description must be a non-empty string")
        for field_name, schema in (
            ("input_schema", self.input_schema),
            ("output_schema", self.output_schema),
        ):
            if not isinstance(schema, Mapping) or not schema:
                raise ValueError(
                    f"tool {self.name}: {field_name} must be a non-empty JSON schema object"
                )
        if not isinstance(self.side_effect, bool):
            raise ValueError(f"tool {self.name}: side_effect must be a boolean")
        if not callable(self.handler):
            raise ValueError(f"tool {self.name}: handler must be callable")

    def to_spec(self) -> dict[str, Any]:
        """Model-facing tool spec in Qwen-Agent function-calling format."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.input_schema),
        }


class ToolRegistry:
    """Name -> ToolDefinition lookup for one running agent."""

    def __init__(self, tools: Iterable[ToolDefinition]):
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name!r}") from None

    def specs(self) -> list[dict[str, Any]]:
        return [tool.to_spec() for tool in self._tools.values()]

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

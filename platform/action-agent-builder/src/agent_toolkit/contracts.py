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
    #: True when a successful call contacts a customer and must therefore be
    #: counted by the fact source's contact-frequency rules. The runtime reads
    #: this flag rather than matching a hard-coded tool name, so any tool that
    #: contacts a customer — the platform's ``send_email``, a ``fin_tools``
    #: ``email.send``, or the teammate email tool — can take part in the same
    #: rule simply by declaring it. Default False: a tool that does not contact
    #: anyone must not inflate a contact count.
    records_contact: bool = False

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
        if not isinstance(self.records_contact, bool):
            raise ValueError(f"tool {self.name}: records_contact must be a boolean")
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

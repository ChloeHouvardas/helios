"""Bridge: expose the financial tools on the action side's tool contract.

Unifies the two tool populations (specs/financial-tools-service.md §2b) so the
gated agent loop can actually reach the tools that carry the intent-verification
guarantee, instead of them sitting beside it.

Two shape gaps are closed here, and both fail *silently* if left unadapted:

1. **Schema shape.** `fin_tools` declares ``{"payer": "str"}`` — a flat
   field-name/type map. The action side and the audit validator need real JSON
   Schema (``properties``, ``required``, ``additionalProperties``). Without the
   conversion the auditor's argument validation never fires and malformed calls
   reach execution. A tool that already declares a full JSON Schema
   (``ToolSpec.input_json_schema``, which is what every imported teammate
   package ships) is passed through **untouched**: converting it via the flat
   map would drop its enums, ranges and nested structure, and with them the
   auditor's ability to reject a malformed argument.

2. **Handler calling convention.** `fin_tools` handlers take keyword arguments;
   ``agent_toolkit`` calls ``handler(arguments)`` with a Mapping.

``agent_toolkit`` is imported lazily so this package keeps no hard dependency on
the action side — the bridge is only needed when an app is assembled from both.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .contracts import ToolSpec

#: `fin_tools` type-name -> JSON Schema type. `X|None` marks an optional field.
_JSON_TYPES: Dict[str, str] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}


def _json_type(declared: str) -> Any:
    """Translate one `fin_tools` type annotation into a JSON Schema type."""
    base = declared.split("[")[0].strip()          # list[dict] -> list
    optional = base.endswith("|None") or "|None" in declared
    base = base.replace("|None", "").strip()
    kind = _JSON_TYPES.get(base, "string")
    # A dict-shaped field carries arbitrary content; declare it loosely rather
    # than pretend to a structure we do not have.
    return kind if not optional else [kind, "null"]


def to_json_schema(flat: Mapping[str, str]) -> Dict[str, Any]:
    """Convert a flat field map into a JSON Schema object.

    Every declared field is required unless annotated ``|None``. Optional fields
    are typed ``[t, "null"]`` so a caller may pass null explicitly.
    """
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for field_name, declared in flat.items():
        properties[field_name] = {"type": _json_type(declared)}
        if "|None" not in declared:
            required.append(field_name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def as_tool_definitions(
    bundle: Any,
    *,
    names: Optional[Sequence[str]] = None,
    client: Optional[Any] = None,
) -> List[Any]:
    """Return the financial tools as ``agent_toolkit`` ``ToolDefinition``s.

    Args:
        bundle: a `fin_tools.Bundle` from `build_world()`.
        names: restrict to these tool names. Omit for all registered tools.
        client: when given, calls are routed through it (e.g. an
            `HttpToolClient`) so the agent reaches the tools across the service
            boundary rather than in-process. Omit for direct in-process calls.

    The returned objects satisfy the action side's own validation — non-empty
    schema, boolean side-effect flag, callable handler — so they can be handed to
    `build_integration(tools=...)` unchanged.
    """
    from agent_toolkit.contracts import ToolDefinition

    specs: List[ToolSpec] = (
        [bundle.registry.get(n) for n in names] if names else bundle.registry.list()
    )
    tools: List[Any] = []
    for spec in specs:
        if client is not None:
            tool_name = spec.name

            def handler(args: Mapping[str, Any], _name: str = tool_name,
                        _client: Any = client) -> Any:
                envelope = _client.call(_name, **dict(args))
                if not envelope.get("ok", False):
                    # Surface a rejection as an exception so the runtime logs it
                    # as a failed action rather than a successful one.
                    raise ValueError(envelope.get("error", "tool call rejected"))
                return envelope["result"]
        else:
            def handler(args: Mapping[str, Any], _spec: ToolSpec = spec) -> Any:
                return _spec.handler(**dict(args))

        tools.append(ToolDefinition(
            name=spec.name,
            description=spec.description.strip(),
            # Pass a declared JSON Schema through untouched; convert only when
            # all the tool gave us is the flat map.
            input_schema=spec.effective_input_schema(),
            output_schema=to_json_schema(spec.output_schema),
            side_effect=bool(spec.side_effects),
            handler=handler,
        ))
    return tools


#: Tools that must never be offered to an agent: the human confirmation path.
#: If an agent could capture and confirm its own intent it could mint its own
#: authorization, and "an app cannot grant itself permission" would be false.
NOT_AGENT_CALLABLE: frozenset = frozenset({
    "intents.capture", "intents.confirm", "intents.revoke",
})


def agent_tool_definitions(bundle: Any, *, client: Optional[Any] = None) -> List[Any]:
    """The subset of financial tools an action agent may be given.

    Same as `as_tool_definitions` with the intent-issuing operations excluded.
    This is the list to pass to `build_integration(tools=...)`.
    """
    allowed = [t.name for t in bundle.registry.list()
               if t.name not in NOT_AGENT_CALLABLE]
    return as_tool_definitions(bundle, names=allowed, client=client)


__all__ = ["to_json_schema", "as_tool_definitions", "agent_tool_definitions",
           "NOT_AGENT_CALLABLE"]

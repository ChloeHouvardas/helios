"""Tool Contract + registry (specs/interfaces.md §6).

Every financial tool in this package registers here with:
  - name, description
  - input schema / output schema (plain dicts describing fields)
  - side_effects flag: True  -> call becomes a *proposal* (audit gate / intent check)
                       False -> read-only, runs directly

Any implementation following this contract (including mocks) can be swapped in
without changing the agent runtime.

Two schema shapes coexist, and the difference is not cosmetic:

  - `input_schema` is a FLAT field/type map (`{"amount_cents": "int"}`). It is
    what the twelve original tools declare, and it cannot express a nested
    object — `to_json_schema` has to type every dict-shaped field loosely.
  - `input_json_schema` is a full JSON Schema. Teammate packages already ship
    one, so keeping only the flat map would throw away their enums, ranges and
    nested structure — and with them the auditor's ability to reject a
    malformed argument. When present it WINS.

`effective_input_schema()` resolves the two so no caller has to branch.

The adapters (`from_definition`, `from_teammate`) exist because the action side
and the teammate packages spell the same idea differently: `side_effect`
singular vs `side_effects`, JSON Schema vs flat map, `handler(Mapping)` vs
`handler(**kwargs)`. Adapting here keeps those differences from leaking into
every tool family.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, str]
    output_schema: Dict[str, str]
    side_effects: bool
    handler: Callable[..., Any]
    tags: tuple = ()
    #: Full JSON Schema for the arguments. Wins over the flat `input_schema`
    #: map when set, because the flat map cannot express a nested object.
    input_json_schema: Optional[Dict[str, Any]] = None
    #: True when the tool reads or writes one tenant's data only. The tenant is
    #: taken from the bundle, never from an argument, so an agent cannot name
    #: somebody else's tenant and no tool can be called cross-tenant.
    tenant_scoped: bool = False
    #: True when a successful call records contact with a person outside the
    #: system. The audit gate uses it for contact-frequency mandates; leaving it
    #: off a tool that does contact someone silently disables that mandate.
    records_contact: bool = False

    def effective_input_schema(self) -> Dict[str, Any]:
        """The JSON Schema an agent or audit gate must validate arguments against.

        `input_json_schema` when the tool declared one, else the flat map
        converted. Always a JSON Schema object, so callers never branch.
        """
        if self.input_json_schema is not None:
            return self.input_json_schema
        # Imported lazily: toolkit_bridge imports this module, so a module-level
        # import here would be a cycle.
        from .toolkit_bridge import to_json_schema

        return to_json_schema(self.input_schema)

    def declare(self) -> Dict[str, Any]:
        """JSON-able declaration, the shape an agent/audit gate consumes."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "side_effects": self.side_effects,
            "tags": list(self.tags),
            # Authoritative JSON Schema, whichever way the tool declared it.
            "input_json_schema": self.effective_input_schema(),
            "tenant_scoped": self.tenant_scoped,
            "records_contact": self.records_contact,
        }

    # -- adapters -----------------------------------------------------------

    @classmethod
    def from_definition(
        cls,
        definition: Any,
        *,
        tags: tuple = (),
        tenant_scoped: bool = False,
        records_contact: bool = False,
        handler: Optional[Callable[..., Any]] = None,
    ) -> "ToolSpec":
        """Adapt an `agent_toolkit.ToolDefinition`, or anything shaped like one.

        The action side spells the flag `side_effect` (singular) and always uses
        JSON Schema, so the schema is kept verbatim on `input_json_schema` and
        the flat map is derived as a lossy summary. Its handler takes a Mapping
        while ours takes kwargs, so the definition's handler is wrapped here; a
        caller-supplied `handler` is used as-is and must take kwargs.
        """
        target = definition.handler if handler is None else handler
        problems = contract_problems(
            name=getattr(definition, "name", None),
            description=getattr(definition, "description", None),
            input_schema=getattr(definition, "input_schema", None),
            handler=target,
            source=f"ToolDefinition {getattr(definition, 'name', '?')!r}",
        )
        if problems:
            raise ValueError(problem_message(problems))

        if handler is not None:
            fn: Callable[..., Any] = handler
        else:
            raw = definition.handler

            def fn(**kwargs: Any) -> Any:   # the Mapping -> kwargs seam
                return raw(kwargs)

        return cls(
            name=str(definition.name),
            description=str(definition.description),
            input_schema=flat_from_json_schema(definition.input_schema),
            output_schema=flat_from_json_schema(
                getattr(definition, "output_schema", None)),
            side_effects=bool(getattr(definition, "side_effect",
                                      getattr(definition, "side_effects", False))),
            handler=fn,
            tags=tags,
            input_json_schema=dict(definition.input_schema),
            tenant_scoped=tenant_scoped,
            records_contact=records_contact,
        )

    @classmethod
    def from_teammate(
        cls,
        contract: Mapping[str, Any],
        *,
        tags: tuple = (),
        tenant_scoped: bool = False,
        records_contact: bool = False,
        handler: Optional[Callable[..., Any]] = None,
    ) -> "ToolSpec":
        """Adapt a teammate's plain-dict tool contract.

        Teammate packages declare a tool as a dict — `email_tool.tool.TOOL`, or
        each entry of `helios_forecast.tools.list_tools()` — spelling the flag
        `side_effect` singular and giving `input_schema` as JSON Schema. Both
        spellings of the flag are accepted so this adapter need not know which
        package it is looking at.

        A dict carries no callable, so `handler` must be given unless the dict
        has one. The resulting error names what to pass, rather than letting the
        gap surface as an obscure failure at call time.
        """
        target = handler if handler is not None else contract.get("handler")
        problems = contract_problems(
            name=contract.get("name"),
            description=contract.get("description"),
            input_schema=contract.get("input_schema"),
            handler=target,
            source=f"teammate contract {contract.get('name', '?')!r}",
        )
        if problems:
            raise ValueError(problem_message(problems))

        return cls(
            name=str(contract["name"]),
            description=str(contract["description"]),
            input_schema=flat_from_json_schema(contract.get("input_schema")),
            output_schema=flat_from_json_schema(contract.get("output_schema")),
            side_effects=bool(contract.get("side_effect",
                                           contract.get("side_effects", False))),
            handler=target,
            tags=tags,
            input_json_schema=dict(contract["input_schema"]),
            tenant_scoped=tenant_scoped,
            records_contact=records_contact,
        )


#: JSON Schema type -> the flat type name `input_schema` uses.
_FLAT_TYPES: Dict[str, str] = {
    "string": "str", "integer": "int", "number": "float",
    "boolean": "bool", "array": "list", "object": "dict",
}


def flat_from_json_schema(schema: Any) -> Dict[str, str]:
    """Summarise a JSON Schema as a flat field map.

    Lossy by construction: nesting collapses to `dict`/`list[...]` and every
    constraint (enum, range, pattern) is dropped. It exists so the existing
    consumers of `input_schema` keep working; `input_json_schema` stays
    authoritative. A field outside `required` is marked `|None`, matching the
    convention `to_json_schema` reads back.
    """
    if not isinstance(schema, Mapping):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    required = set(schema.get("required") or ())
    flat: Dict[str, str] = {}
    for field_name, spec in properties.items():
        declared = spec.get("type") if isinstance(spec, Mapping) else None
        if isinstance(declared, list):
            concrete = [t for t in declared if t != "null"]
        elif isinstance(declared, str):
            concrete = [declared]
        else:
            concrete = []
        base = _FLAT_TYPES.get(concrete[0], "dict") if concrete else "dict"
        items = spec.get("items") if isinstance(spec, Mapping) else None
        if base == "list" and isinstance(items, Mapping):
            item_type = items.get("type")
            if isinstance(item_type, str):
                base = f"list[{_FLAT_TYPES.get(item_type, 'dict')}]"
        flat[str(field_name)] = (
            base if field_name in required else f"{base}|None")
    return flat


def contract_problems(*, name: Any, description: Any, input_schema: Any,
                      handler: Any, source: str) -> List[str]:
    """Every reason this contract is unusable, collected in one pass.

    Reporting all problems at once matters because these adapters run over
    teammate code: fixing one field per failed attempt would cost a round trip
    per typo. Fail closed — an unusable contract is never registered.
    """
    problems: List[str] = []
    if not isinstance(name, str) or not name.strip():
        problems.append(
            f"{source}: 'name' must be a non-empty string, got {name!r}; "
            f"write the tool's dotted name, e.g. 'collect.status'")
    if not isinstance(description, str) or not description.strip():
        problems.append(
            f"{source}: 'description' must be a non-empty string, got "
            f"{description!r}; state what the tool does and whether it has "
            f"side effects")
    if not isinstance(input_schema, Mapping) or not input_schema:
        problems.append(
            f"{source}: 'input_schema' must be a non-empty JSON Schema object, "
            f"got {input_schema!r}; a tool taking no arguments still needs "
            f"{{'type': 'object', 'properties': {{}}}}")
    if not callable(handler):
        problems.append(
            f"{source}: no callable handler; pass handler=<callable> taking "
            f"keyword arguments, or add a 'handler' key to the contract")
    return problems


def problem_message(problems: List[str]) -> str:
    return "tool contract is not usable:\n  - " + "\n  - ".join(problems)


#: Environment variable naming the tenant a bundle belongs to.
TENANT_ENV = "HELIOS_TENANT"


def require_tenant(bundle: Any, *, tool: str, env: str = TENANT_ENV) -> str:
    """The tenant a tenant-scoped tool must operate as. Raises if unset.

    Fail closed on purpose. The imported receivables store defaults a missing
    tenant to the string ``"dev"``, so letting nothing through would write one
    tenant's receivables into a shared development bucket that any other caller
    can read and refund. A wrong default there is silent cross-tenant data
    mixing, which costs far more than a refused call. The error names the tool
    and the exact fix, rather than surfacing much later as a not-found.
    """
    tenant = getattr(bundle, "tenant", None)
    if not tenant or not str(tenant).strip():
        raise ValueError(
            f"{tool} is tenant-scoped but this bundle has no tenant; build it "
            f"with build_world(tenant=<id>) or set the {env} environment "
            f"variable. Refusing to fall back to a default tenant, which would "
            f"mix this tenant's data with every other caller's.")
    return str(tenant)


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
        *,
        input_json_schema: Optional[Dict[str, Any]] = None,
        tenant_scoped: bool = False,
        records_contact: bool = False,
    ) -> Callable[[Callable[..., Any]], ToolSpec]:
        def deco(fn: Callable[..., Any]) -> ToolSpec:
            return self.register(
                ToolSpec(name, description, input_schema, output_schema,
                         side_effects, fn, tags,
                         input_json_schema=input_json_schema,
                         tenant_scoped=tenant_scoped,
                         records_contact=records_contact)
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

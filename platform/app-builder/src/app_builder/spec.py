"""The pydantic models for `helios-app.yaml` — the app spec.

The spec is the contract between a user's idea and a generated application. It is
validated with `extra="forbid"` on every model, so an unknown key is an error
rather than something quietly ignored.

Why that is strict rather than convenient
-----------------------------------------
A generated spec that is silently misread produces an app that looks correct and
behaves differently from what the user asked for. In a financial application the
worst failure is not a crash; it is a wrong number that arrived without an error.
A typo'd key (`entites:`) that pydantic ignored would generate an app with no
tables at all, and nothing would say so.

Every error message is written for a model to act on: it names the field, the
problem, and what to write instead. `AppSpecError` reports *all* problems at once,
because the errors are independent and one round trip per error through a revision
loop is slow.

Two layers, deliberately separate
---------------------------------
* This module is **syntax**: shape, closed sets, mutual exclusion, identifier
  safety. It answers "is this a well-formed spec".
* `app_builder.validate` is **semantics**: the spec against the manifests and the
  code it points at. It answers "is this a coherent app".

Keeping them apart means a spec can be rejected as malformed without any file
being read from disk, and the cross-file checks can assume the shape is sound.
"""
from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    PrivateAttr,
    field_validator,
    model_validator,
)

__all__ = [
    "AppSpec",
    "AppSpecError",
    "FIELD_TYPES",
    "FieldType",
    "UI_PATTERNS",
    "UiPattern",
    "looks_monetary",
    "SPEC_VERSION",
]

#: Bumped only on an incompatible shape change, so a spec from an older run is
#: rejected with a clear message rather than misparsed.
SPEC_VERSION = "1"


class UiPattern(StrEnum):
    """The closed set of page shapes `ui.pages[*].pattern` may name.

    A pattern is a documented composition of kit templates. Adding one means
    adding a template to `app_builder/kit/`, not relaxing this set — an unknown
    pattern would generate a page with nothing to render.
    """

    SUMMARY = "summary"
    TABLE = "table"
    DETAIL = "detail"
    FORM = "form"
    APPROVAL_QUEUE = "approval_queue"
    SETTINGS = "settings"


class FieldType(StrEnum):
    """The closed set of entity field types.

    `float` is absent on purpose and its absence is enforced with a specific
    message (see `FieldSpec`): money is `decimal` or integer cents, never a
    binary float, because a rounding error in an invoice total is a real
    financial error rather than a display artefact.
    """

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    JSON = "json"


UI_PATTERNS: frozenset[str] = frozenset(p.value for p in UiPattern)
FIELD_TYPES: frozenset[str] = frozenset(t.value for t in FieldType)

#: A field is "money-shaped" if its name suggests currency. Matched on whole
#: words, not substrings, and only when the type could actually carry money.
MONEY_WORDS = frozenset({
    "amount", "amounts", "balance", "balances", "money", "price", "prices",
    "total", "totals", "cost", "costs", "fee", "fees", "value", "values",
    "subtotal", "credit", "debit", "payment", "paid", "owed", "due_amount",
})

#: Types that can never carry money. `string` is in here too, and that is the
#: fix for the false positive that flagged `payer_behavior` (a free-text label
#: about how a customer pays) as monetary because it contains the word
#: "payment". A heuristic that cries wolf trains people to ignore the checker,
#: which is worse than not having one — so it only fires on a field that is
#: numeric-looking, where a float really would be a bug.
NON_MONETARY_TYPES = frozenset({
    FieldType.DATE, FieldType.DATETIME, FieldType.BOOLEAN,
    FieldType.JSON, FieldType.STRING,
})

#: Entity and field names become SQLite identifiers in generated DDL. They are
#: constrained here rather than escaped later, because generated code that
#: interpolates a name into `CREATE TABLE` is an injection point: a spec naming
#: a field `x INTEGER); DROP TABLE invoice; --` would otherwise be executed by
#: the app's own schema step. Quoting is not a substitute for refusing.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_IDENTIFIER_HINT = (
    "lowercase letters, digits and underscores, not starting with a digit"
)

KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def looks_monetary(field_name: str, field_type: Any) -> bool:
    """True when a field name suggests money and its type could carry money."""
    if field_type in NON_MONETARY_TYPES or str(field_type) in NON_MONETARY_TYPES:
        return False
    words = set(re.split(r"[^a-z0-9]+", field_name.lower())) - {"", "cents"}
    return bool(words & MONEY_WORDS)


def _identifier(name: str, kind: str) -> str:
    """Validate a SQL identifier, with a message that says what to write."""
    if not _IDENTIFIER.match(name):
        raise ValueError(
            f"{kind} {name!r} is not a safe SQL identifier; write a name using "
            f"{_IDENTIFIER_HINT}. Names are interpolated into generated DDL, so "
            "anything else is refused rather than escaped."
        )
    return name


class _Strict(BaseModel):
    """Base for every spec model: unknown keys are errors, not noise."""

    model_config = ConfigDict(extra="forbid")


class IdentitySpec(_Strict):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""

    @field_validator("id")
    @classmethod
    def _kebab(cls, v: str) -> str:
        if not KEBAB.match(v):
            raise ValueError(
                f"identity.id {v!r} is not kebab-case; write lowercase letters, "
                "digits and single hyphens (e.g. `invoice-payer`). This value "
                "becomes the audit manifest's app_id and the app's URL slug, so "
                "it cannot carry case, spaces or underscores."
            )
        return v


class ExistingSpec(_Strict):
    """What already exists, if anything.

    Null (or absent) means a standalone app. This section is what makes
    integration possible: `stack.match_existing`, `data.integration` and
    `ui.align_with` all read from it, so it must be filled in before any of them.
    """

    system: str | None = None
    stack: str | None = None
    data: str | None = None
    ui: str | None = None


class StackSpec(_Strict):
    """Exactly one of `use` or `match_existing`.

    Both is contradictory; neither leaves the scaffolder guessing. Presence is
    tested with `model_fields_set` rather than truthiness, so an explicit
    `match_existing: false` still counts as declared and is reported as the
    contradiction it is.
    """

    use: str | None = None
    match_existing: bool | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        declared = sorted(self.model_fields_set & {"use", "match_existing"})
        if len(declared) != 1:
            got = ", ".join(f"{k}={getattr(self, k)!r}" for k in declared) or "neither"
            raise ValueError(
                "stack must set exactly one of `use` or `match_existing`, got "
                f"{got}; write `use: helios-python-web-1` for a named stack, or "
                "`match_existing: true` to adopt the stack recorded in "
                "`existing.stack`."
            )
        return self


class FieldSpec(_Strict):
    name: str = Field(min_length=1)
    type: FieldType
    required: bool = False

    @field_validator("name")
    @classmethod
    def _safe_name(cls, v: str) -> str:
        return _identifier(v, "field name")

    @field_validator("type", mode="before")
    @classmethod
    def _refuse_float(cls, v: Any) -> Any:
        """Reject `float` with a message that names the two correct options.

        `float` is outside the closed set, so the enum would reject it anyway —
        but as one item in a list of seven allowed values. Money is the reason
        the set is closed, so this says that instead.
        """
        if isinstance(v, str) and v.strip().lower() == "float":
            raise ValueError(
                "field type 'float' is never correct; write `decimal` for a "
                "currency amount, or `integer` and name the field `*_cents`. "
                "A binary float cannot represent 0.1 exactly, so an invoice "
                "total summed in floats can be wrong by a cent."
            )
        if not (isinstance(v, FieldType) or (isinstance(v, str) and v in FIELD_TYPES)):
            raise ValueError(
                f"field type {v!r} is not in the closed set; write one of "
                f"{sorted(FIELD_TYPES)}"
            )
        return v


class EntitySpec(_Strict):
    name: str = Field(min_length=1)
    fields: list[FieldSpec] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, v: str) -> str:
        return _identifier(v, "entity name")

    @model_validator(mode="after")
    def _unique_field_names(self) -> Self:
        seen: set[str] = set()
        dupes: list[str] = []
        for f in self.fields:
            if f.name in seen and f.name not in dupes:
                dupes.append(f.name)
            seen.add(f.name)
        if dupes:
            raise ValueError(
                f"entity {self.name!r} declares the field(s) {sorted(dupes)} more "
                "than once; a duplicate column cannot exist in the generated "
                "table, so remove the extra declaration."
            )
        return self


class LocalDataSpec(_Strict):
    entities: list[EntitySpec] = Field(default_factory=list)


class IntegrationDataSpec(_Strict):
    """Data the app reads from somewhere else and must not create.

    Produces no DDL. Data the app does not own is not created by it; generating
    a table for it would produce a second, divergent copy of someone else's
    record.
    """

    source: str = Field(min_length=1)
    access: str | None = None
    entities: list[str] = Field(default_factory=list)

    @field_validator("entities")
    @classmethod
    def _safe_names(cls, v: list[str]) -> list[str]:
        return [_identifier(n, "entity name") for n in v]


class DataSpec(_Strict):
    """Exactly one of `local` or `integration`."""

    local: LocalDataSpec | None = None
    integration: IntegrationDataSpec | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        declared = sorted(self.model_fields_set & {"local", "integration"})
        if len(declared) != 1:
            got = ", ".join(declared) or "neither"
            raise ValueError(
                "data must set exactly one of `local` or `integration`, got "
                f"{got}; write `local:` with the entities this app owns, or "
                "`integration:` with the source it reads from."
            )
        return self


class GraphLinkSpec(_Strict):
    entity: str = Field(min_length=1)
    events: list[str] = Field(default_factory=list)


class GraphSpec(_Strict):
    """A declaration today and a wiring point later.

    The shared graph's read/write interface is still being settled, so this
    records intent and the scaffolder generates the call sites. When the
    transport lands, connecting an existing app is filling in a transport rather
    than restructuring it.
    """

    reads: list[GraphLinkSpec] = Field(default_factory=list)
    publishes: list[GraphLinkSpec] = Field(default_factory=list)


class AgentsSpec(_Strict):
    """Relative paths to the two manifests.

    The app spec does not restate their contents — it points at them, and the
    validator checks the files exist and agree with each other.
    """

    action_manifest: str = Field(min_length=1)
    audit_manifest: str = Field(min_length=1)


class PageSpec(_Strict):
    id: str = Field(min_length=1)
    title: str = ""
    pattern: UiPattern

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$", v):
            raise ValueError(
                f"ui page id {v!r} must be lowercase alphanumeric with optional "
                "hyphens or underscores (e.g. `approval_queue`); it becomes the "
                "route name and the template filename."
            )
        return v

    @field_validator("pattern", mode="before")
    @classmethod
    def _closed(cls, v: Any) -> Any:
        if not (isinstance(v, UiPattern) or (isinstance(v, str) and v in UI_PATTERNS)):
            raise ValueError(
                f"ui pattern {v!r} is not a known pattern; write one of "
                f"{sorted(UI_PATTERNS)}. A pattern names a template in the UI "
                "kit, so an unknown one would generate a page with nothing to "
                "render."
            )
        return v


class UiSpec(_Strict):
    pages: list[PageSpec] = Field(default_factory=list)
    align_with: str | None = None


class ToolEntry(_Strict):
    """A tool named with an explicit side-effect flag.

    `tools: [lookup_invoice, {name: reimbursements_send, side_effect: true}]` —
    the object form exists so an author never has to rely on the scaffolder's
    name-based guess for a tool whose name does not say what it does. A stated
    flag beats the guess and is not marked for review.
    """

    name: str
    side_effect: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("a tool entry needs a non-empty name; write "
                             "{name: <tool>, side_effect: true|false}")
        return value.strip()


class AppSpec(_Strict):
    """The whole spec. `AppSpec.load(path)` is the entry point."""

    spec_version: Literal["1"] = SPEC_VERSION
    identity: IdentitySpec
    existing: ExistingSpec | None = None
    stack: StackSpec
    data: DataSpec
    graph: GraphSpec = Field(default_factory=GraphSpec)
    agents: AgentsSpec
    ui: UiSpec = Field(default_factory=UiSpec)
    tools: list[str | ToolEntry] = Field(default_factory=list)
    _tool_flags: dict[str, bool] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _normalise_tools(self) -> "AppSpec":
        """Flatten tool entries to names and keep any explicit side-effect flags.

        `tools` accepts plain names and `{name, side_effect}` objects. After
        validation `tools` is always a list of names, so every consumer keeps
        reading it as strings, and `side_effect_for(name)` returns the flag the
        author stated or None when the scaffolder must guess.
        """
        names: list[str] = []
        flags: dict[str, bool] = {}
        for entry in self.tools:
            if isinstance(entry, ToolEntry):
                names.append(entry.name)
                if entry.side_effect is not None:
                    flags[entry.name] = entry.side_effect
            else:
                names.append(entry)
        self.tools = names
        self._tool_flags = flags
        return self

    def side_effect_for(self, tool: str) -> bool | None:
        """The author's explicit flag for a tool, or None if none was stated."""
        return getattr(self, "_tool_flags", {}).get(tool)

    # -- convenience accessors -------------------------------------------
    #
    # These exist so callers do not each re-implement the "which branch is set"
    # logic. `local_entities` is empty for an integration app, which is the
    # correct answer for DDL generation: there is nothing to create.

    @property
    def app_id(self) -> str:
        return self.identity.id

    @property
    def local_entities(self) -> list[EntitySpec]:
        return list(self.data.local.entities) if self.data.local else []

    @property
    def integration(self) -> IntegrationDataSpec | None:
        return self.data.integration

    @property
    def declares_local_data(self) -> bool:
        return self.data.local is not None

    def entity(self, name: str) -> EntitySpec | None:
        for e in self.local_entities:
            if e.name == name:
                return e
        return None

    @property
    def pages(self) -> list[PageSpec]:
        return list(self.ui.pages)

    def page_ids_by_pattern(self, pattern: UiPattern | str) -> list[str]:
        want = str(pattern)
        return [p.id for p in self.ui.pages if str(p.pattern) == want]

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str) -> "AppSpec":
        """Parse and validate a spec file.

        Raises `AppSpecError` listing every problem at once, each as
        `<yaml path>: <problem>; write <what instead>`.
        """
        path = Path(path)
        if not path.exists():
            raise AppSpecError(
                [f"{path}: no such file; write the app spec to this path before "
                 "validating it"]
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AppSpecError([f"{path}: cannot be read ({exc}); check the path "
                                "and permissions"]) from exc
        return cls.parse(raw, origin=str(path))

    @classmethod
    def parse(cls, text: str, *, origin: str = "helios-app.yaml") -> "AppSpec":
        """Validate spec text. Same contract as `load`, no filesystem."""
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise AppSpecError(
                [f"{origin}: does not parse as YAML ({_one_line(exc)}); fix the "
                 "syntax — check indentation and quoting first"]
            ) from exc

        if data is None:
            raise AppSpecError(
                [f"{origin}: the file is empty; write a mapping with at least "
                 "`spec_version`, `identity`, `stack`, `data`, `agents`"]
            )
        if not isinstance(data, dict):
            raise AppSpecError(
                [f"{origin}: the top level is {type(data).__name__}, not a "
                 "mapping; write `spec_version: \"1\"` and the sections as keys"]
            )

        problems: list[str] = []
        spec: AppSpec | None = None
        try:
            spec = cls.model_validate(data)
        except ValidationError as exc:
            problems.extend(format_validation_errors(exc, origin=origin))

        if spec is None:
            raise AppSpecError(problems)

        problems.extend(spec._cross_section_problems(origin=origin))
        if problems:
            raise AppSpecError(problems)
        return spec

    def _cross_section_problems(self, *, origin: str) -> list[str]:
        """Checks that span sections, so no single model can express them.

        Run after the shape is known to be sound, which is why these can
        dereference without guards.
        """
        problems: list[str] = []
        existing = self.existing or ExistingSpec()

        # The three integration options each read from `existing`. Matching
        # nothing is not matching: without the recorded convention the
        # scaffolder would fall back to the default kit and silently generate a
        # competing stack, database or UI next to the real one.
        if self.stack.match_existing and not existing.stack:
            problems.append(
                f"{origin} stack.match_existing: true requires a populated "
                "existing.stack, which is empty; write the existing system's "
                "language, web framework and database under `existing.stack` "
                "(e.g. \"FastAPI + Postgres\"), or set `stack.use` to a named "
                "stack instead."
            )
        if self.data.integration and not existing.data:
            problems.append(
                f"{origin} data.integration: requires a populated existing.data, "
                "which is empty; write where the data lives and how this app may "
                "access it under `existing.data`, or declare `data.local` "
                "instead."
            )
        if self.ui.align_with and not existing.ui:
            problems.append(
                f"{origin} ui.align_with: {self.ui.align_with!r} requires a "
                "populated existing.ui, which is empty; write the existing "
                "system's layout, components and styling under `existing.ui`, or "
                "remove `align_with` and use the kit's defaults."
            )

        # An entity in both sections is ambiguous in a way that fails silently:
        # the schema tool would create a table for data the app does not own,
        # and two copies of one record diverge without either being wrong.
        local_names = {e.name for e in self.local_entities}
        integ_names = set(self.data.integration.entities) if self.data.integration else set()
        both = sorted(local_names & integ_names)
        if both:
            problems.append(
                f"{origin} data: entity/entities {both} appear in both "
                "data.local and data.integration; declare each entity in exactly "
                "one. Data the app does not own is not created by it — leave it "
                "under `integration` so no table is generated for it."
            )

        # Manifest paths are relative to the app directory. An absolute path
        # would resolve against the validator's cwd, so it would pass on one
        # machine and fail on another.
        for key, value in (("action_manifest", self.agents.action_manifest),
                           ("audit_manifest", self.agents.audit_manifest)):
            if value.startswith("/") or value.startswith("~"):
                problems.append(
                    f"{origin} agents.{key}: {value!r} is an absolute path; write "
                    "a path relative to the app directory "
                    f"(e.g. `agents/{self.app_id}-{'action.json' if key == 'action_manifest' else 'audit.yaml'}`)."
                )

        return problems


class AppSpecError(ValueError):
    """Raised when a spec is invalid. Carries every problem, not the first.

    `str(exc)` is the report: one line per problem, each in the house format
    `<yaml path>: <problem>; write <what instead>`.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        body = "\n".join(f"  {p}" for p in self.problems)
        super().__init__(
            f"{len(self.problems)} problem(s) in the app spec:\n{body}"
        )


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------

#: pydantic error type -> what to write instead. Generic per error kind; a
#: validator that knows better raises ValueError with a specific message, and
#: that message wins (see `_problem_for`).
_FIXES: dict[str, str] = {
    "extra_forbidden": "remove it, or write one of the keys this section accepts",
    "literal_error": "write the exact value shown in the allowed list",
    "enum": "write one of the allowed values",
    "string_type": "write a quoted string",
    "bool_type": "write true or false",
    "int_type": "write a whole number",
    "list_type": "write a YAML list",
    "dict_type": "write a YAML mapping",
    "string_too_short": "write a non-empty value",
    "too_short": "write at least one item",
}


def _one_line(text: Any) -> str:
    return " ".join(str(text).split())


def _format_loc(loc: tuple[Any, ...], origin: str) -> str:
    """Turn a pydantic location tuple into a YAML path.

    `('data', 'local', 'entities', 0, 'fields', 2, 'type')` becomes
    `helios-app.yaml data.local.entities[0].fields[2].type` — the path a reader
    can find in the file, including which list item.
    """
    out = origin
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}"
    return out


def _problem_for(err: dict[str, Any]) -> str:
    """One `<problem>; write <what instead>` string for a pydantic error."""
    ctx = err.get("ctx") or {}

    # A ValueError raised by one of our validators already carries the specific
    # problem and the specific fix. Use it verbatim.
    if "error" in ctx:
        return _one_line(ctx["error"])

    kind = err.get("type", "")
    msg = _one_line(err.get("msg", "is invalid"))

    if kind == "missing":
        field = str(err.get("loc", ("",))[-1])
        return (f"{msg}; write a `{field}` key in this section")

    if kind == "extra_forbidden":
        # Name the offending key, because "extra inputs are not permitted" on a
        # path tells a model nothing about which key it mistyped.
        bad = err.get("loc", ("",))[-1]
        return f"unknown key {bad!r}; {_FIXES[kind]}"

    fix = _FIXES.get(kind)
    if fix:
        return f"{msg}; {fix}"
    return msg


def format_validation_errors(exc: ValidationError, *, origin: str) -> list[str]:
    """Render a ValidationError as a list of actionable problem lines."""
    return [
        f"{_format_loc(tuple(err.get('loc', ())), origin)}: {_problem_for(err)}"
        for err in exc.errors()
    ]

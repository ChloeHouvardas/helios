"""The 16-question intake, and the first spec that can be built from the answers.

Two halves:

`questions_yaml()` prints the fixed intake as a YAML template with `answer: null`
and `assumption: null` per item. The questions are fixed so two runs over the same
idea produce comparable specs, and so a missing answer is *visible* rather than
assumed.

`spec_from_intake()` builds a first spec from the answers it can actually map —
identity, the existing block, entities with fields, tools, pages, graph — and leaves
a `# TODO` marker for everything else. An unanswered question is recorded as
`answer: not sure` rather than guessed.

Why guessing is refused
-----------------------
The intake's own rule is that an answer of "not sure" is recorded as such. The
validator then reports an incomplete spec and the agent goes back to the user. It
does not invent a data model to make the file parse, because a plausible-looking
generated entity is worse than an absent one: someone has to notice the absence,
whereas a wrong one gets built on.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from .spec import FIELD_TYPES, UI_PATTERNS

__all__ = ["QUESTIONS", "questions_yaml", "spec_from_intake", "IntakeError"]


class IntakeError(ValueError):
    """The intake cannot be turned into a spec. Reports every problem at once."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        body = "\n".join(f"  {p}" for p in self.problems)
        super().__init__(f"{len(self.problems)} problem(s) in the intake:\n{body}")


#: The fixed intake, verbatim from `specs/app-builder.md`. The `id` is what the
#: generated spec and the answers key off; the `group` is the section heading.
QUESTIONS: tuple[dict[str, str], ...] = (
    {"id": "job", "group": "What the app does",
     "question": "What is the job this app performs, in the user's own words?"},
    {"id": "users", "group": "What the app does",
     "question": "Who uses it, and what do they need to decide or do?"},
    {"id": "success", "group": "What the app does",
     "question": "What does success look like — what changes for the user when it works?"},
    {"id": "existing_system", "group": "What already exists",
     "question": "Is there an existing system this app joins, or is it standalone?"},
    {"id": "existing_stack", "group": "What already exists",
     "question": "If existing: what stack does it use — language, web framework, database?"},
    {"id": "existing_data", "group": "What already exists",
     "question": "If existing: where does its data live, and how may this app access it?"},
    {"id": "existing_ui", "group": "What already exists",
     "question": "If existing: what UI conventions does it follow — layout, components, styling?"},
    {"id": "records", "group": "What data",
     "question": "What records does the app need? For each: what fields, and which are required?"},
    {"id": "data_ownership", "group": "What data",
     "question": "Which of those already exist elsewhere, and which must this app create?"},
    {"id": "graph", "group": "What data",
     "question": "What must be shared across apps — read from the knowledge graph, or published to it?"},
    {"id": "agent_work", "group": "What agents",
     "question": "What work should an agent do without being asked each time?"},
    {"id": "mandate", "group": "What agents",
     "question": "For each agent: what is it allowed to do, what are its limits, and which actions need the owner's approval? (This becomes the mandate.)"},
    {"id": "tools", "group": "What tools",
     "question": "Which financial tools does that work need — lookups, email, payments, spreadsheets, simulation, intent verification?"},
    {"id": "side_effects", "group": "What tools",
     "question": "Which of those have side effects? (Each one becomes a gated proposal.)"},
    {"id": "pages", "group": "What pages",
     "question": "What does the user need to see, and what do they need to act on?"},
    {"id": "patterns", "group": "What pages",
     "question": "Which UI pattern fits each page?"},
)

#: Answer text that means "the user did not answer", recorded rather than guessed.
NOT_SURE = "not sure"
_NOT_SURE_VALUES = frozenset({
    "", "null", "none", "not sure", "unsure", "unknown", "n/a", "na", "todo", "?",
})


def questions_yaml() -> str:
    """The intake as a fillable YAML template."""
    lines = [
        "# Helios app intake — the fixed 16 questions.",
        "#",
        "# Answer in order and do not skip the integration block (questions 4-7).",
        "# Those come early on purpose: whether something already exists changes",
        "# every later answer, and discovering that after the data model is drafted",
        "# means drafting it twice.",
        "#",
        "# Leave `answer: null` for anything the user has not decided. An unanswered",
        "# question is recorded as \"not sure\" and the generated spec carries a TODO,",
        "# rather than being filled in with a guess. A plausible-looking wrong entity",
        "# is worse than an absent one: the absence gets noticed, the guess gets",
        "# built on.",
        "#",
        "# `assumption` is for what YOU inferred in order to keep moving. Write it",
        "# whenever you answer something the user did not quite say, so the",
        "# assumption is visible to them and to whoever reads the spec next.",
        "",
    ]
    number = 0
    current_group = None
    for item in QUESTIONS:
        number += 1
        if item["group"] != current_group:
            current_group = item["group"]
            lines.append("")
            lines.append(f"# --- {current_group} ---")
        lines.append(f"q{number:02d}_{item['id']}:")
        lines.append(f"  question: >")
        for chunk in _wrap(item["question"], 74):
            lines.append(f"    {chunk}")
        lines.append("  answer: null")
        lines.append("  assumption: null")
    lines.append("")
    return "\n".join(lines)


def spec_from_intake(intake: dict[str, Any], *, app_id: str | None = None) -> str:
    """Build a first `helios-app.yaml` from the intake answers.

    Only what the answers actually support is emitted. Everything else is a TODO
    comment naming the question that would fill it, so the gap is visible in the
    file rather than silent in the model.
    """
    answers = _normalise(intake)
    problems: list[str] = []

    identity_id = _resolve_app_id(app_id, answers, problems)
    if not identity_id:
        raise IntakeError(problems or [
            "cannot derive identity.id from the intake; pass --app-id, or answer "
            "q01_job with something that can be turned into a kebab-case id"])

    name = _title_from_id(identity_id)
    description = _answered(answers, "job") or ""
    existing = _existing_block(answers)
    stack_block = _stack_block(answers, existing)
    data_block, data_warnings = _data_block(answers, problems)
    graph_block = _graph_block(answers)
    tools = _tools(answers)
    pages_block = _pages_block(answers, problems)

    if problems:
        raise IntakeError(problems)

    lines: list[str] = []
    lines.append("# Generated from the intake by `helios-app spec-from-intake`.")
    lines.append("#")
    lines.append("# This is a FIRST spec, not a finished one. Every `TODO` below is")
    lines.append("# something the intake did not answer; fill it in before")
    lines.append("# scaffolding, or `helios-app validate` will report the gap.")
    lines.append("")
    lines.append('spec_version: "1"')
    lines.append("")
    lines.append("identity:")
    lines.append(f"  id: {identity_id}")
    lines.append(f"  name: {name}")
    lines.append("  description: >")
    if description:
        for chunk in _wrap(description, 74):
            lines.append(f"    {chunk}")
    else:
        lines.append("    TODO: q01_job was not answered. Write what this app does,")
        lines.append("    in the user's own words — this text becomes the audit")
        lines.append("    manifest's description and the app's own summary.")
    lines.append("")
    lines.extend(existing)
    lines.append("")
    lines.extend(stack_block)
    lines.append("")
    lines.extend(data_block)
    lines.append("")
    lines.extend(graph_block)
    lines.append("")
    lines.append("agents:")
    lines.append(f"  action_manifest: agents/{identity_id}-action.json")
    lines.append(f"  audit_manifest: agents/{identity_id}-audit.yaml")
    lines.append("")
    lines.extend(pages_block)
    lines.append("")
    lines.append("# The tools this app's agents may use. Each must also be declared")
    lines.append("# in the audit manifest — the audit agent is the registry of what")
    lines.append("# may be granted at all.")
    lines.append("tools:")
    if tools:
        for tool in tools:
            lines.append(f"  - {tool}")
    else:
        lines.append("  # TODO: q13_tools was not answered. A spec with no tools")
        lines.append("  # cannot scaffold: an audit manifest with no tools will not")
        lines.append("  # load. List at least one read-only lookup to start.")
        lines.append("  []")
    for warning in data_warnings:
        lines.insert(0, f"# NOTE: {warning}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Answer handling
# ---------------------------------------------------------------------------

def _normalise(intake: dict[str, Any]) -> dict[str, str]:
    """Flatten `q01_job: {question, answer, assumption}` into `{job: answer}`.

    Accepts both the full template shape and a flat `{job: "..."}` mapping, so an
    intake can be written by hand without the boilerplate. Unanswered questions
    become the literal string "not sure" rather than disappearing, which keeps the
    gap visible downstream.
    """
    out: dict[str, str] = {}
    known = {item["id"] for item in QUESTIONS}

    for key, value in intake.items():
        short = _short_id(str(key), known)
        if isinstance(value, dict):
            answer = value.get("answer")
        else:
            answer = value
        out[short] = _clean(answer)
    return out


def _short_id(key: str, known: set[str]) -> str:
    """`q01_job` -> `job`; `job` -> `job`; anything else passes through."""
    if key in known:
        return key
    match = re.match(r"^q\d+_(.+)$", key)
    if match and match.group(1) in known:
        return match.group(1)
    return key


def _clean(answer: Any) -> str:
    """An answer as text, with an empty/unanswered one normalised to `not sure`.

    A structured answer (a list of entities, a mapping of fields) is dumped as ONE
    YAML document. Dumping each element separately and joining them with "; " — the
    obvious implementation — produces text that is not valid YAML, so
    `_parse_entities` cannot read it back and falls through to line parsing, which
    turns `[{"name": "invoice", ...}]` into an entity called `name`. The data was
    already structured; flattening it loses that.
    """
    if answer is None:
        return NOT_SURE
    if isinstance(answer, (list, tuple, dict)):
        if _is_blank(answer):
            return NOT_SURE
        return yaml.safe_dump(answer if isinstance(answer, dict) else list(answer),
                              sort_keys=False, default_flow_style=False,
                              allow_unicode=True).strip()
    text = str(answer).strip()
    if text.lower() in _NOT_SURE_VALUES:
        return NOT_SURE
    return text


def _is_blank(value: Any) -> bool:
    """True when a structured answer carries no content.

    Deliberately not recursive through `_clean` — that recursed on a dict answer,
    because the emptiness check passed the dict back into the function that called
    it. A container of not-sure values is blank; anything else is not, and the
    caller decides how to render it.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _NOT_SURE_VALUES
    if isinstance(value, (list, tuple)):
        return all(_is_blank(item) for item in value)
    if isinstance(value, dict):
        return all(_is_blank(v) for v in value.values())
    return False


def _answered(answers: dict[str, str], key: str) -> str:
    """The answer for `key`, or '' when it was not given."""
    value = answers.get(key, NOT_SURE)
    return "" if value == NOT_SURE else value


def _resolve_app_id(explicit: str | None, answers: dict[str, str],
                    problems: list[str]) -> str:
    """An app id from `--app-id` if given, else derived from the job answer.

    An explicit id is validated too. Passing it straight through would put
    `identity.id: !!!` into the generated spec, which then fails inside
    `AppSpec.parse` with a message about the spec rather than about the argument —
    and the user has no way to connect the two.
    """
    if explicit:
        candidate = explicit.strip()
        if re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", candidate):
            return candidate
        derived = _kebab(candidate)
        if derived and derived != candidate:
            problems.append(
                f"--app-id {explicit!r} is not kebab-case; write lowercase letters, "
                f"digits and single hyphens (for example {derived!r}). This value "
                "becomes the audit manifest's app_id and the app's URL slug.")
        else:
            problems.append(
                f"--app-id {explicit!r} cannot be turned into a kebab-case id; "
                "write lowercase letters, digits and single hyphens. This value "
                "becomes the audit manifest's app_id and the app's URL slug.")
        return ""

    return _derive_app_id(answers, problems)


def _derive_app_id(answers: dict[str, str], problems: list[str]) -> str:
    """A kebab-case id from the job description, or '' if there is nothing to use."""
    explicit = _answered(answers, "app_id")
    if explicit:
        candidate = _kebab(explicit)
        if candidate:
            return candidate
        problems.append(
            f"app_id {explicit!r} cannot be turned into a kebab-case id; write "
            "lowercase letters, digits and single hyphens")
        return ""

    job = _answered(answers, "job")
    if not job:
        return ""
    return _kebab(job)


def _kebab(text: str, *, max_words: int = 3) -> str:
    """The first few meaningful words of a sentence, kebab-cased.

    Stopwords are dropped so "Chases overdue invoices for a small business" yields
    `chases-overdue-invoices` rather than `chases-overdue-invoices-for`. Three
    words because a generated id is a slug, an audit `app_id` and a URL prefix all
    at once: `pays-supplier-invoices-small` is technically fine and unpleasant to
    type, and the fourth word of a job description is almost never the
    distinguishing one.
    """
    stopwords = frozenset({
        "a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "at", "by",
        "with", "that", "this", "it", "is", "are", "be", "as", "from", "when",
        "what", "who", "how", "your", "my", "their", "its",
    })
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in stopwords]
    if not words:
        return ""
    return "-".join(words[:max_words])


def _title_from_id(app_id: str) -> str:
    return " ".join(part.capitalize() for part in app_id.split("-"))


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _existing_block(answers: dict[str, str]) -> list[str]:
    system = _answered(answers, "existing_system")
    stack = _answered(answers, "existing_stack")
    data = _answered(answers, "existing_data")
    ui = _answered(answers, "existing_ui")

    lines = ["# What already exists. Null means a standalone app. This section is",
             "# read by stack.match_existing, data.integration and ui.align_with,",
             "# so it must be filled in before any of them."]
    lines.append("existing:")
    lines.append(f"  system: {_yaml_scalar(system)}")
    lines.append(f"  stack: {_yaml_scalar(stack)}")
    lines.append(f"  data: {_yaml_scalar(data)}")
    lines.append(f"  ui: {_yaml_scalar(ui)}")
    return lines


def _stack_block(answers: dict[str, str], existing: list[str]) -> list[str]:
    """`match_existing` only when the intake says a system exists AND records a stack.

    Matching nothing is not matching, and the validator refuses it — so this falls
    back to the named stack rather than emitting a spec that cannot load.
    """
    has_existing = any(line.strip().startswith("stack:") and "null" not in line
                       for line in existing)
    lines = ["stack:",
             "  # Exactly one of `use` or `match_existing`."]
    if has_existing:
        lines.append("  # The intake records an existing stack, so match it.")
        lines.append("  match_existing: true")
    else:
        lines.append("  use: helios-python-web-1")
    return lines


def _data_block(answers: dict[str, str], problems: list[str]
                ) -> tuple[list[str], list[str]]:
    """The data section, from q08_records and q09_data_ownership."""
    warnings: list[str] = []
    records = _answered(answers, "records")
    ownership = _answered(answers, "data_ownership")

    entities = _parse_entities(records)
    integrated = _parse_names(ownership) if ownership else []

    if not entities:
        # An entity list the intake did not supply is a gap, not an error: the spec
        # is explicitly a first draft and the TODO says what to write.
        return (["data:",
                 "  # TODO: q08_records was not answered, so no entities were",
                 "  # generated. List each record type with its fields and which",
                 "  # are required. A `decimal` for money, never a float.",
                 "  local:",
                 "    entities:",
                 "      - name: example",
                 "        fields:",
                 "          - { name: id, type: string, required: true }"],
                warnings)

    lines = ["data:"]
    if integrated:
        warnings.append(
            f"q09_data_ownership suggests {integrated} already exist elsewhere. "
            "They are still generated under data.local — move them to "
            "data.integration yourself, because data the app does not own is not "
            "created by it.")
    lines.append("  local:")
    lines.append("    entities:")
    for entity in entities:
        lines.append(f"      - name: {entity['name']}")
        lines.append("        fields:")
        for field in entity["fields"]:
            required = "true" if field["required"] else "false"
            lines.append(f"          - {{ name: {field['name']}, "
                         f"type: {field['type']}, required: {required} }}")
    return lines, warnings


def _parse_entities(text: str) -> list[dict[str, Any]]:
    """Entities from a free-text answer.

    Understands three shapes, because that is what an interview produces:
      * `invoice: id (string, required), amount (decimal, required)`
      * a line per entity with `field: type` pairs
      * a YAML mapping, when the answer was structured

    Anything it cannot parse is skipped rather than guessed. A field whose type is
    not in the closed set is emitted as `string` with a note, because `string` is
    the only type that cannot be wrong about magnitude.
    """
    if not text:
        return []

    if text.lstrip().startswith(("{", "-", "name:")):
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        entities = _entities_from_structured(data)
        if entities:
            return entities

    entities = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*# ").strip()
        if not line or ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = _snake(name.strip())
        if not name:
            continue
        fields = _parse_fields(rest)
        if fields:
            entities.append({"name": name, "fields": fields})
    return entities


def _entities_from_structured(data: Any) -> list[dict[str, Any]]:
    """Entities from a YAML answer: a list of mappings, or a mapping of lists."""
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = _snake(str(item.get("name", "")))
            if not name:
                continue
            out.append({"name": name,
                        "fields": _fields_from_structured(item.get("fields"))})
    elif isinstance(data, dict):
        for name, value in data.items():
            snake = _snake(str(name))
            if not snake:
                continue
            if isinstance(value, dict) and "fields" in value:
                out.append({"name": snake,
                            "fields": _fields_from_structured(value["fields"])})
            else:
                out.append({"name": snake, "fields": _fields_from_structured(value)})
    return [e for e in out if e["fields"]]


def _fields_from_structured(value: Any) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                name = _snake(str(item.get("name", "")))
                if not name:
                    continue
                fields.append({
                    "name": name,
                    "type": _field_type(str(item.get("type", "string"))),
                    "required": bool(item.get("required", False)),
                })
            elif isinstance(item, str):
                parsed = _parse_fields(item)
                fields.extend(parsed)
    elif isinstance(value, dict):
        for name, spec in value.items():
            snake = _snake(str(name))
            if not snake:
                continue
            if isinstance(spec, dict):
                fields.append({
                    "name": snake,
                    "type": _field_type(str(spec.get("type", "string"))),
                    "required": bool(spec.get("required", False)),
                })
            else:
                fields.append({"name": snake, "type": _field_type(str(spec)),
                               "required": False})
    if not any(f["name"] == "id" for f in fields):
        fields.insert(0, {"name": "id", "type": "string", "required": True})
    return fields


def _parse_fields(text: str) -> list[dict[str, Any]]:
    """`id (string, required), amount (decimal, required)` -> field dicts."""
    fields: list[dict[str, Any]] = []
    for chunk in re.split(r",(?![^(]*\))", text):
        chunk = chunk.strip().strip(";")
        if not chunk:
            continue
        match = re.match(r"^([A-Za-z_][\w\s]*?)\s*(?:\(([^)]*)\))?$", chunk)
        if not match:
            continue
        name = _snake(match.group(1))
        if not name:
            continue
        detail = match.group(2) or ""
        parts = [p.strip().lower() for p in detail.split(",") if p.strip()]
        field_type = _field_type(next((p for p in parts if p in FIELD_TYPES
                                       or p == "float"), "string"))
        required = any(p in ("required", "req", "not null", "mandatory") for p in parts)
        fields.append({"name": name, "type": field_type, "required": required})
    if not any(f["name"] == "id" for f in fields):
        fields.insert(0, {"name": "id", "type": "string", "required": True})
    return fields


def _field_type(name: str) -> str:
    """A closed-set field type. `float` becomes `decimal`, with the reason recorded.

    Money is never a float, and an interview answer saying "float" almost always
    means "a number with a decimal point" — so it is mapped rather than refused,
    because refusing would stall the intake on a wording choice.
    """
    lowered = name.strip().lower()
    if lowered in FIELD_TYPES:
        return lowered
    if lowered in ("float", "double", "number", "numeric", "money", "currency"):
        return "decimal"
    if lowered in ("int", "long", "count"):
        return "integer"
    if lowered in ("bool", "flag"):
        return "boolean"
    if lowered in ("text", "str"):
        return "string"
    if lowered in ("timestamp", "time"):
        return "datetime"
    if lowered in ("dict", "object", "list", "array"):
        return "json"
    return "string"


def _parse_names(text: str) -> list[str]:
    """Entity-ish names out of a free-text answer."""
    out = []
    for chunk in re.split(r"[,;\n]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.match(r"^([A-Za-z_][\w\s]{0,40}?)(?:\s*[\(\[:].*)?$", chunk)
        if match:
            snake = _snake(match.group(1))
            if snake and len(snake) <= 40:
                out.append(snake)
    return out


def _snake(text: str) -> str:
    """A SQL-safe identifier. Refuses what it cannot make safe.

    The spec model would reject an unsafe name later with a better message, so
    anything that cannot be normalised is dropped here rather than emitted broken.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    if not cleaned or not re.match(r"^[a-z_][a-z0-9_]*$", cleaned):
        return ""
    return cleaned


def _graph_block(answers: dict[str, str]) -> list[str]:
    raw = _answered(answers, "graph")
    reads: list[str] = []
    publishes: list[str] = []
    if raw:
        for name in _parse_names(raw):
            publishes.append(name)
    lines = ["# Cross-app state. A declaration today and a wiring point later:",
             "# the scaffolder generates the publish and subscribe call sites."]
    lines.append("graph:")
    lines.append("  reads:")
    if reads:
        for entity in reads:
            lines.append(f"    - {{ entity: {entity}, events: [changed] }}")
    else:
        lines.append("    []")
    lines.append("  publishes:")
    if publishes:
        for entity in publishes:
            lines.append(f"    # TODO: name the real events for {entity}")
            lines.append(f"    - {{ entity: {entity}, events: [changed] }}")
    else:
        lines.append("    []")
    return lines


def _tools(answers: dict[str, str]) -> list[str]:
    """Tool names from q13, filtered to things that look like tool names."""
    raw = _answered(answers, "tools")
    if not raw:
        return []
    out: list[str] = []
    for chunk in re.split(r"[,;\n]", raw):
        chunk = chunk.strip().strip("`\"'")
        if not chunk:
            continue
        # A tool name is an identifier, optionally namespaced with a dot. Prose
        # ("lookups, email, payments") is not, and emitting it would produce a
        # manifest the loader rejects.
        if re.match(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$", chunk):
            if chunk not in out:
                out.append(chunk)
    return out


def _pages_block(answers: dict[str, str], problems: list[str]) -> list[str]:
    """Pages from q15 and q16, plus an approval_queue page if one is missing.

    The approval queue is added rather than merely warned about: every side-effect
    action can be held, so an app without one has agents that pause invisibly and
    the owner discovers it only when they wonder why nothing happened. The
    validator warns about the same gap, so leaving it out would generate a spec
    that is immediately non-clean.
    """
    raw_pages = _answered(answers, "pages")
    raw_patterns = _answered(answers, "patterns")
    pages: list[tuple[str, str, str]] = []

    if raw_pages:
        names = _parse_names(raw_pages)
        patterns = _parse_patterns(raw_patterns) if raw_patterns else []
        for index, name in enumerate(names):
            pattern = patterns[index] if index < len(patterns) else (
                patterns[0] if patterns else "table")
            title = " ".join(w.capitalize() for w in name.split("_"))
            pages.append((name, title, pattern))

    if not pages:
        pages = [("dashboard", "Overview", "summary")]

    if not any(p[2] == "approval_queue" for p in pages):
        pages.append(("holds", "Needs your approval", "approval_queue"))

    lines = ["ui:",
             "  # Either `pages` for a standalone app, or `align_with` to follow an",
             "  # existing system's conventions.",
             "  pages:"]
    for page_id, title, pattern in pages:
        lines.append(f"    - {{ id: {page_id}, title: {title}, pattern: {pattern} }}")
    lines.append("  align_with: null")
    return lines


def _parse_patterns(text: str) -> list[str]:
    """UI patterns named in the answer, IN THE ORDER THE USER WROTE THEM.

    Order is load-bearing: `_pages_block` zips this list against the page names
    positionally, so `summary, table, detail, approval_queue, settings` has to come
    back in that sequence or page 1 gets page 3's pattern.

    Scanning the whole answer for members of `UI_PATTERNS` — the obvious
    implementation — gets this wrong twice over. `UI_PATTERNS` is a frozenset, so
    the match order is arbitrary, and a comma-separated list is then read in that
    arbitrary order. Both patterns are matched per chunk instead, which preserves
    the user's sequence and lets one chunk name no pattern at all.
    """
    out: list[str] = []
    for chunk in re.split(r"[,;\n]", text):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        matched = next((p for p in UI_PATTERNS if p == chunk), None)
        if matched is None:
            # A chunk that names a pattern loosely ("approval queue", "a table of
            # invoices") still has to resolve to something, or the positional zip
            # shifts every later page.
            matched = next((p for p in UI_PATTERNS if p in chunk), None)
        if matched is None:
            matched = _guess_pattern(chunk)
        out.append(matched or "table")
    return out


def _guess_pattern(text: str) -> str:
    lowered = text.lower()
    if any(w in lowered for w in ("approv", "hold", "queue", "decide", "pending")):
        return "approval_queue"
    if any(w in lowered for w in ("dashboard", "summary", "overview", "totals")):
        return "summary"
    if any(w in lowered for w in ("setting", "limit", "config", "policy", "preference")):
        return "settings"
    if any(w in lowered for w in ("form", "input", "entry", "add", "new", "edit")):
        return "form"
    if any(w in lowered for w in ("detail", "record", "single", "one ")):
        return "detail"
    if any(w in lowered for w in ("list", "table", "rows", "all ")):
        return "table"
    return ""


def _yaml_scalar(value: str) -> str:
    """A YAML scalar, or `null` for an unanswered question."""
    if not value:
        return "null"
    if re.match(r"^[A-Za-z0-9 _.,:/()+-]+$", value) and not value[0].isdigit():
        return value
    return json_quote(value)


def json_quote(value: str) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines or [""]

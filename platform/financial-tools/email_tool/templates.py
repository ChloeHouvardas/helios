"""Template registry and renderer for the email sender.

Each template is a markdown file whose first ``**Subject**:`` line becomes the
email subject and the remaining lines the body. Placeholders use Jinja2
syntax. Variables that only appear inside ``{% if %}`` conditions are
optional and may be omitted by the caller; every other variable is required
and must be a non-null value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, Undefined, meta, nodes
from jinja2.exceptions import TemplateSyntaxError, UndefinedError

_SUBJECT_LINE = re.compile(r"^\*\*Subject\*\*:\s*(.+?)\s*$")
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class TemplateError(Exception):
    """A template is missing, malformed, or cannot be rendered."""


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    body: str


@dataclass(frozen=True)
class _Template:
    subject_source: str
    body_source: str
    required_variables: frozenset[str]
    optional_variables: frozenset[str]


class _OptionalUndefined(Undefined):
    """Undefined placeholder for omitted optional variables.

    Behaves like an ordinary undefined in ``{% if %}`` tests (falsy), but
    raises if it would be rendered into the output, so an omitted variable
    can never silently become empty or partial text in an email.
    """

    __slots__ = ()

    def __str__(self) -> str:
        self._fail_with_undefined_error()


def _walk(root: nodes.Node):
    for child in root.iter_child_nodes():
        yield child
        yield from _walk(child)


def _if_condition_names(ast: nodes.Template) -> set[str]:
    names: set[str] = set()
    for node in _walk(ast):
        if isinstance(node, nodes.If):
            condition = node.test
            # find_all() only yields descendants, so a bare Name condition
            # (e.g. "{% if days_until %}") must be checked directly.
            if isinstance(condition, nodes.Name):
                names.add(condition.name)
            names.update(name.name for name in condition.find_all(nodes.Name))
    return names


def _split_subject_and_body(raw: str, template_id: str) -> tuple[str, str]:
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        match = _SUBJECT_LINE.match(line)
        if match:
            body_lines = lines[index + 1 :]
            while body_lines and not body_lines[0].strip():
                body_lines.pop(0)
            body = "\n".join(body_lines).rstrip()
            if body:
                body += "\n"
            return match.group(1), body
    raise TemplateError(f"template {template_id!r} has no '**Subject**:' line")


def _analyse_variables(
    env: Environment, template_id: str, source: str
) -> tuple[frozenset[str], frozenset[str]]:
    try:
        ast = env.parse(source)
    except TemplateSyntaxError as exc:
        raise TemplateError(
            f"template {template_id!r} has invalid syntax: {exc.message}"
        ) from exc
    used = meta.find_undeclared_variables(ast)
    optional = _if_condition_names(ast) & used
    return frozenset(used - optional), frozenset(optional)


class TemplateRegistry:
    """Loads the markdown templates from a directory and renders them."""

    def __init__(self, templates_dir: Path | str = DEFAULT_TEMPLATES_DIR) -> None:
        self._dir = Path(templates_dir)
        self._env = Environment(
            undefined=_OptionalUndefined,
            keep_trailing_newline=True,
            autoescape=False,
        )
        self._templates: dict[str, _Template] = {}
        self._load()

    def template_ids(self) -> list[str]:
        return sorted(self._templates)

    def required_variables(self, template_id: str) -> frozenset[str]:
        return self._get(template_id).required_variables

    def optional_variables(self, template_id: str) -> frozenset[str]:
        return self._get(template_id).optional_variables

    def render(self, template_id: str, variables: Mapping[str, object]) -> RenderedEmail:
        template = self._get(template_id)
        missing = sorted(
            name for name in template.required_variables if variables.get(name) is None
        )
        if missing:
            raise TemplateError(
                f"template {template_id!r} is missing required variables: "
                f"{', '.join(missing)}"
            )
        try:
            subject = self._env.from_string(template.subject_source).render(variables)
            body = self._env.from_string(template.body_source).render(variables)
        except UndefinedError as exc:
            raise TemplateError(
                f"template {template_id!r} failed to render: {exc.message}"
            ) from exc
        return RenderedEmail(subject=subject, body=body)

    def _load(self) -> None:
        for path in sorted(self._dir.glob("*.md")):
            if path.name == "README.md":
                continue
            template_id = path.stem
            raw = path.read_text(encoding="utf-8")
            subject_source, body_source = _split_subject_and_body(raw, template_id)
            required, optional = _analyse_variables(
                self._env, template_id, f"{subject_source}\n{body_source}"
            )
            self._templates[template_id] = _Template(
                subject_source, body_source, required, optional
            )
        if not self._templates:
            raise TemplateError(f"no templates found in {self._dir}")

    def _get(self, template_id: str) -> _Template:
        try:
            return self._templates[template_id]
        except KeyError:
            available = ", ".join(self._templates) or "none"
            raise TemplateError(
                f"unknown template {template_id!r}; available templates: {available}"
            ) from None

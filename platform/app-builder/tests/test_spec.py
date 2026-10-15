"""Tests for the spec models.

Structure: one test per invariant the spec enforces, plus the two real apps as
regression fixtures. Each negative test asserts the *yaml path* appears in the
message, because a problem that does not say where it is costs a round trip — and
`AppSpecError`'s whole job is to remove round trips.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app_builder.spec import (
    AppSpec,
    AppSpecError,
    FIELD_TYPES,
    UI_PATTERNS,
    looks_monetary,
)

REPO = Path(__file__).resolve().parents[3]
REAL_SPECS = [
    REPO / "apps" / "helios-collect" / "helios-app.yaml",
    REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml",
]

MINIMAL = """
spec_version: "1"
identity:
  id: tiny-app
  name: Tiny App
stack:
  use: helios-python-web-1
data:
  local:
    entities:
      - name: thing
        fields:
          - { name: id, type: string, required: true }
          - { name: amount, type: decimal, required: true }
agents:
  action_manifest: agents/tiny-app-action.json
  audit_manifest: agents/tiny-app-audit.yaml
ui:
  pages:
    - { id: dashboard, title: Overview, pattern: summary }
    - { id: holds, title: Approvals, pattern: approval_queue }
tools:
  - lookup_thing
"""


def _problems(text: str) -> list[str]:
    """Load spec text and return the problem list. Fails the test if it parsed."""
    with pytest.raises(AppSpecError) as excinfo:
        AppSpec.parse(text)
    return excinfo.value.problems


def _assert_one_problem(text: str, *, path: str, mentions: str) -> None:
    problems = _problems(text)
    assert len(problems) == 1, f"expected exactly 1 problem, got {problems}"
    assert path in problems[0], f"expected {path!r} in {problems[0]!r}"
    assert mentions.lower() in problems[0].lower(), (
        f"expected {mentions!r} in {problems[0]!r}")


# ---------------------------------------------------------------------------
# The real apps load
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", REAL_SPECS, ids=lambda p: p.parent.name)
def test_a_real_app_spec_loads(path: Path) -> None:
    """Both checked-in specs must keep parsing, or the generator has drifted."""
    spec = AppSpec.load(path)
    assert spec.app_id, "every app has an id"
    assert spec.local_entities, "a real app declares entities"
    assert spec.tools, "a real app declares tools"


def test_the_two_real_specs_agree_on_shape() -> None:
    """They differ in content but not in structure — that is what the kit relies on."""
    specs = [AppSpec.load(p) for p in REAL_SPECS]
    for s in specs:
        assert s.spec_version == "1"
        assert s.stack.use == "helios-python-web-1"
        assert s.data.local is not None
        assert s.data.integration is None
        assert s.existing is not None and s.existing.system is None
        assert s.page_ids_by_pattern("approval_queue"), (
            "every app with gated actions needs somewhere for the owner to decide")


# ---------------------------------------------------------------------------
# Minimal spec is valid
# ---------------------------------------------------------------------------

def test_the_minimal_spec_parses() -> None:
    spec = AppSpec.parse(MINIMAL)
    assert spec.app_id == "tiny-app"
    assert [e.name for e in spec.local_entities] == ["thing"]
    assert spec.tools == ["lookup_thing"]


# ---------------------------------------------------------------------------
# Unknown keys (extra="forbid")
# ---------------------------------------------------------------------------

def test_an_unknown_top_level_key_is_refused() -> None:
    _assert_one_problem(
        MINIMAL + "\nnot_a_section: true\n",
        path="not_a_section", mentions="unknown key")


def test_an_unknown_nested_key_is_refused() -> None:
    _assert_one_problem(
        MINIMAL.replace("identity:\n  id: tiny-app", "identity:\n  ident: tiny-app\n  id: tiny-app"),
        path="identity", mentions="unknown key")


def test_a_typoed_entities_key_is_caught_rather_than_ignored() -> None:
    """`entites:` would silently generate an app with no tables."""
    problems = _problems(MINIMAL.replace("    entities:", "    entites:"))
    assert any("entites" in p for p in problems), problems


# ---------------------------------------------------------------------------
# spec_version
# ---------------------------------------------------------------------------

def test_a_wrong_spec_version_is_refused() -> None:
    _assert_one_problem(
        MINIMAL.replace('spec_version: "1"', 'spec_version: "2"'),
        path="spec_version", mentions="write")


def test_an_unquoted_spec_version_is_refused() -> None:
    """`spec_version: 1` parses as the int 1, which is not the string "1"."""
    problems = _problems(MINIMAL.replace('spec_version: "1"', "spec_version: 1"))
    assert any("spec_version" in p for p in problems), problems


# ---------------------------------------------------------------------------
# identity.id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_id", ["Tiny_App", "tiny app", "tiny-app-", "-tiny", "TINY"])
def test_a_non_kebab_identity_id_is_refused(bad_id: str) -> None:
    problems = _problems(MINIMAL.replace("id: tiny-app", f"id: {bad_id}"))
    assert any("identity.id" in p and "kebab-case" in p for p in problems), problems


def test_a_kebab_identity_id_is_accepted() -> None:
    assert AppSpec.parse(MINIMAL.replace("tiny-app", "a-b-c-1")).app_id == "a-b-c-1"


# ---------------------------------------------------------------------------
# stack: exactly one of use / match_existing
# ---------------------------------------------------------------------------

def test_a_stack_with_neither_option_is_refused() -> None:
    _assert_one_problem(
        MINIMAL.replace("stack:\n  use: helios-python-web-1", "stack: {}"),
        path="stack", mentions="exactly one")


def test_a_stack_with_both_options_is_refused() -> None:
    _assert_one_problem(
        MINIMAL.replace("stack:\n  use: helios-python-web-1",
                        "stack:\n  use: helios-python-web-1\n  match_existing: true"),
        path="stack", mentions="exactly one")


def test_match_existing_without_a_recorded_stack_is_refused() -> None:
    """Matching nothing is not matching."""
    text = (MINIMAL
            .replace("stack:\n  use: helios-python-web-1", "stack:\n  match_existing: true")
            .replace("agents:", "existing:\n  system: ACME back office\nagents:"))
    _assert_one_problem(text, path="stack.match_existing", mentions="existing.stack")


def test_match_existing_with_a_recorded_stack_is_accepted() -> None:
    text = (MINIMAL
            .replace("stack:\n  use: helios-python-web-1", "stack:\n  match_existing: true")
            .replace("agents:", "existing:\n  stack: FastAPI + Postgres\nagents:"))
    spec = AppSpec.parse(text)
    assert spec.stack.match_existing is True


# ---------------------------------------------------------------------------
# data: exactly one of local / integration
# ---------------------------------------------------------------------------

def test_data_with_neither_option_is_refused() -> None:
    problems = _problems(MINIMAL.replace(
        "data:\n  local:\n    entities:\n      - name: thing\n        fields:\n"
        "          - { name: id, type: string, required: true }\n"
        "          - { name: amount, type: decimal, required: true }\n",
        "data: {}\n"))
    assert any("exactly one" in p for p in problems), problems


def test_integration_without_a_recorded_data_source_is_refused() -> None:
    text = MINIMAL.replace(
        "data:\n  local:\n    entities:\n      - name: thing\n        fields:\n"
        "          - { name: id, type: string, required: true }\n"
        "          - { name: amount, type: decimal, required: true }\n",
        "data:\n  integration:\n    source: acme_erp\n    entities: [invoice]\n")
    _assert_one_problem(text, path="data.integration", mentions="existing.data")


def test_a_partial_local_entity_is_accepted() -> None:
    """`required` defaults to False and `fields` may be short."""
    text = MINIMAL.replace(
        "          - { name: amount, type: decimal, required: true }", "")
    assert AppSpec.parse(text).local_entities[0].fields[0].required is True


# ---------------------------------------------------------------------------
# field types
# ---------------------------------------------------------------------------

def test_a_float_field_type_is_refused_with_the_money_reason() -> None:
    problems = _problems(MINIMAL.replace("type: decimal", "type: float"))
    assert any("float" in p and "decimal" in p and "cents" in p for p in problems), problems


def test_an_unknown_field_type_lists_the_closed_set() -> None:
    problems = _problems(MINIMAL.replace("type: decimal", "type: currency"))
    assert any(sorted(FIELD_TYPES)[0] in p for p in problems), problems


@pytest.mark.parametrize("field_type", sorted(FIELD_TYPES))
def test_every_member_of_the_closed_set_is_accepted(field_type: str) -> None:
    text = MINIMAL.replace("{ name: amount, type: decimal, required: true }",
                           f"{{ name: amount, type: {field_type}, required: true }}")
    assert AppSpec.parse(text).local_entities[0].fields[1].type == field_type


# ---------------------------------------------------------------------------
# identifier safety (SQL injection guard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_name", [
    "x INTEGER); DROP TABLE thing; --",
    "1thing",
    "Thing",
    "some-field",
    "a b",
])
def test_an_unsafe_entity_or_field_name_is_refused(bad_name: str) -> None:
    """Names are interpolated into generated DDL, so they are refused, not escaped."""
    problems = _problems(MINIMAL.replace("- { name: amount,", f"- {{ name: {bad_name!r},"))
    assert any("SQL identifier" in p for p in problems), problems


def test_an_unsafe_entity_name_is_refused() -> None:
    problems = _problems(MINIMAL.replace("- name: thing", '- name: "thing; DROP TABLE x"'))
    assert any("SQL identifier" in p for p in problems), problems


def test_a_duplicate_field_name_is_refused() -> None:
    text = MINIMAL.replace(
        "          - { name: amount, type: decimal, required: true }",
        "          - { name: id, type: decimal, required: true }")
    problems = _problems(text)
    assert any("more than once" in p for p in problems), problems


# ---------------------------------------------------------------------------
# ui patterns
# ---------------------------------------------------------------------------

def test_an_unknown_ui_pattern_lists_the_closed_set() -> None:
    problems = _problems(MINIMAL.replace("pattern: summary", "pattern: dashboard"))
    assert any("known pattern" in p and sorted(UI_PATTERNS)[0] in p for p in problems), problems


@pytest.mark.parametrize("pattern", sorted(UI_PATTERNS))
def test_every_known_pattern_is_accepted(pattern: str) -> None:
    text = MINIMAL.replace("pattern: summary", f"pattern: {pattern}")
    assert AppSpec.parse(text).pages[0].pattern == pattern


def test_a_bad_page_id_is_refused() -> None:
    problems = _problems(MINIMAL.replace("id: dashboard", "id: Dash Board"))
    assert any("ui page id" in p for p in problems), problems


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

def test_a_missing_audit_manifest_path_is_refused() -> None:
    problems = _problems(MINIMAL.replace("  audit_manifest: agents/tiny-app-audit.yaml\n", ""))
    assert any("audit_manifest" in p for p in problems), problems


def test_an_absolute_manifest_path_is_refused() -> None:
    problems = _problems(
        MINIMAL.replace("action_manifest: agents/tiny-app-action.json",
                        "action_manifest: /etc/passwd"))
    assert any("absolute path" in p and "relative" in p for p in problems), problems


# ---------------------------------------------------------------------------
# All problems at once
# ---------------------------------------------------------------------------

def test_every_problem_is_reported_in_one_pass() -> None:
    """The point of `AppSpecError`: a revision loop must not pay per error."""
    broken = (MINIMAL
              .replace('spec_version: "1"', 'spec_version: "9"')
              .replace("id: tiny-app", "id: Tiny_App")
              .replace("type: decimal", "type: float")
              .replace("pattern: summary", "pattern: no-such-pattern"))
    problems = _problems(broken)
    assert len(problems) >= 4, problems
    joined = "\n".join(problems)
    for needle in ("spec_version", "identity.id", "float", "ui"):
        assert needle in joined, f"{needle!r} missing from:\n{joined}"


def test_an_unparseable_file_says_so() -> None:
    problems = _problems("spec_version: \"1\"\n  bad: : indentation:\n")
    assert any("YAML" in p for p in problems), problems


def test_an_empty_file_says_what_to_write() -> None:
    problems = _problems("")
    assert any("empty" in p for p in problems), problems


def test_a_non_mapping_top_level_says_what_to_write() -> None:
    problems = _problems("- just\n- a\n- list\n")
    assert any("mapping" in p for p in problems), problems


def test_a_missing_file_is_reported_with_its_path(tmp_path: Path) -> None:
    with pytest.raises(AppSpecError) as excinfo:
        AppSpec.load(tmp_path / "nope.yaml")
    assert "nope.yaml" in str(excinfo.value)
    assert "no such file" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The money heuristic
# ---------------------------------------------------------------------------

def test_the_money_heuristic_ignores_free_text_labels() -> None:
    """`payer_behavior` is prose about how a customer pays, not a sum of money.

    This is the false positive the old checker had: it matched the substring
    "payment" inside a `string` field and demanded `decimal`, which would have
    been nonsense. A heuristic that cries wolf gets ignored.
    """
    assert not looks_monetary("payer_behavior", "string")
    assert not looks_monetary("payment_method", "string")
    assert not looks_monetary("payment_details_changed", "boolean")


def test_the_money_heuristic_still_catches_a_numeric_money_field() -> None:
    assert looks_monetary("amount_due", "decimal")
    assert looks_monetary("credit_limit", "integer")
    assert looks_monetary("total", "decimal")


def test_the_money_heuristic_ignores_dates() -> None:
    """The old substring version flagged `due_date` because it contains "due"."""
    assert not looks_monetary("due_date", "date")
    assert not looks_monetary("paid_at", "datetime")


def test_a_monetary_string_field_is_accepted_by_the_spec() -> None:
    """End to end: the heuristic must not turn into a validation failure."""
    text = MINIMAL.replace(
        "          - { name: amount, type: decimal, required: true }",
        "          - { name: amount, type: decimal, required: true }\n"
        "          - { name: payer_behavior, type: string, required: false }")
    spec = AppSpec.parse(text)
    assert spec.local_entities[0].fields[2].name == "payer_behavior"

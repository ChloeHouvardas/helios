"""Tests for the intake: the 16 questions, and the first spec built from answers.

The load-bearing rule is that an unanswered question is recorded as "not sure"
rather than guessed. A plausible-looking wrong entity is worse than an absent one:
the absence gets noticed, the guess gets built on.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app_builder.cli import main
from app_builder.intake import (
    IntakeError,
    QUESTIONS,
    questions_yaml,
    spec_from_intake,
)
from app_builder.scaffold import load_spec, scaffold
from app_builder.spec import AppSpec, AppSpecError, UI_PATTERNS
from app_builder.validate import validate_app

REPO = Path(__file__).resolve().parents[3]

#: An intake filled in for invoice-payer, in the template's own shape.
INVOICE_PAYER_INTAKE = {
    "q01_job": {"question": "What is the job?",
                "answer": "Pays supplier invoices for a small business",
                "assumption": None},
    "q02_users": {"answer": "The owner, who confirms each payment"},
    "q03_success": {"answer": "Invoices are paid on time and nothing is paid twice"},
    "q04_existing_system": {"answer": "standalone"},
    "q05_existing_stack": {"answer": None},
    "q06_existing_data": {"answer": None},
    "q07_existing_ui": {"answer": None},
    "q08_records": {"answer": (
        "supplier: id (string, required), name (string, required), "
        "email (string, required), bank_account (string, required), "
        "payment_details_changed (boolean, required)\n"
        "invoice: id (string, required), supplier_id (string, required), "
        "amount (decimal, required), currency (string, required), "
        "due_date (date, required), status (string, required)\n"
        "account: id (string, required), balance_cents (integer, required)")},
    "q09_data_ownership": {"answer": None},
    "q10_graph": {"answer": "payment"},
    "q11_agent_work": {"answer": "Pay due invoices and notify the supplier"},
    "q12_mandate": {"answer": None},
    "q13_tools": {"answer": "payments.balance, payments.send, email.send, lookup_invoice"},
    "q14_side_effects": {"answer": "payments.send and email.send"},
    "q15_pages": {"answer": "dashboard, invoices, invoice, holds, policy"},
    "q16_patterns": {"answer": "summary, table, detail, approval_queue, settings"},
}


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------

def test_the_template_lists_all_sixteen_questions() -> None:
    assert len(QUESTIONS) == 16


def test_the_template_is_valid_yaml_with_a_null_answer_per_item() -> None:
    document = yaml.safe_load(questions_yaml())
    assert len(document) == 16
    for key, item in document.items():
        assert item["question"], key
        assert item["answer"] is None, key
        # `assumption` is where a model records what it inferred; without the key
        # there is nowhere to put it and the inference stays invisible.
        assert "assumption" in item, key


def test_the_template_keys_are_numbered_in_order() -> None:
    document = yaml.safe_load(questions_yaml())
    numbers = [int(k.split("_")[0][1:]) for k in document]
    assert numbers == list(range(1, 17))


def test_the_integration_block_comes_early() -> None:
    """Whether something already exists changes every later answer."""
    document = yaml.safe_load(questions_yaml())
    keys = list(document)
    existing = [k for k in keys if "existing" in k]
    records = next(i for i, k in enumerate(keys) if "records" in k)
    assert all(keys.index(k) < records for k in existing), (existing, keys)


def test_the_template_explains_why_not_to_guess() -> None:
    text = questions_yaml()
    assert "not sure" in text
    assert "assumption" in text


# ---------------------------------------------------------------------------
# spec-from-intake: a complete intake
# ---------------------------------------------------------------------------

def test_a_filled_intake_produces_a_valid_spec() -> None:
    text = spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer")
    spec = AppSpec.parse(text)
    assert spec.app_id == "invoice-payer"
    assert spec.identity.name == "Invoice Payer"


def test_the_entities_come_through_with_their_fields_and_types() -> None:
    spec = AppSpec.parse(spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer"))
    names = [e.name for e in spec.local_entities]
    assert names == ["supplier", "invoice", "account"]

    supplier = spec.entity("supplier")
    assert {f.name for f in supplier.fields} == {
        "id", "name", "email", "bank_account", "payment_details_changed"}
    types = {f.name: str(f.type) for f in supplier.fields}
    assert types["payment_details_changed"] == "boolean"

    invoice = spec.entity("invoice")
    invoice_types = {f.name: str(f.type) for f in invoice.fields}
    assert invoice_types["amount"] == "decimal"
    assert invoice_types["due_date"] == "date"

    account = spec.entity("account")
    assert {f.name: str(f.type) for f in account.fields}["balance_cents"] == "integer"


def test_required_flags_survive_the_intake() -> None:
    spec = AppSpec.parse(spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer"))
    invoice = spec.entity("invoice")
    assert all(f.required for f in invoice.fields)


def test_the_tools_come_through_in_order() -> None:
    spec = AppSpec.parse(spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer"))
    assert spec.tools == ["payments.balance", "payments.send", "email.send",
                          "lookup_invoice"]


def test_pages_keep_the_order_the_user_wrote_them_in() -> None:
    """The pattern list is zipped against the page list positionally.

    Scanning the whole answer for known patterns instead of matching per chunk
    returns them in `UI_PATTERNS` iteration order — and `UI_PATTERNS` is a
    frozenset, so that order is arbitrary. The result was `holds` getting
    `settings` and `policy` getting `approval_queue`.
    """
    spec = AppSpec.parse(spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer"))
    assert [(p.id, str(p.pattern)) for p in spec.pages] == [
        ("dashboard", "summary"),
        ("invoices", "table"),
        ("invoice", "detail"),
        ("holds", "approval_queue"),
        ("policy", "settings"),
    ]


def test_a_standalone_answer_selects_a_named_stack() -> None:
    spec = AppSpec.parse(spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer"))
    assert spec.stack.use == "helios-python-web-1"
    assert spec.stack.match_existing is None


def test_the_graph_declaration_comes_through() -> None:
    spec = AppSpec.parse(spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer"))
    assert [p.entity for p in spec.graph.publishes] == ["payment"]


def test_the_manifest_paths_match_the_app_id() -> None:
    spec = AppSpec.parse(spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer"))
    assert spec.agents.action_manifest == "agents/invoice-payer-action.json"
    assert spec.agents.audit_manifest == "agents/invoice-payer-audit.yaml"


# ---------------------------------------------------------------------------
# The end-to-end acceptance criterion
# ---------------------------------------------------------------------------

def test_an_intake_derived_spec_scaffolds_into_an_app_that_validates(tmp_path: Path) -> None:
    """Task 6's acceptance criterion, end to end.

    intake -> spec -> scaffold -> validate with the manifests in place. Nothing in
    this chain is hand-written.
    """
    text = spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer")
    spec_path = tmp_path / "helios-app.yaml"
    spec_path.write_text(text, encoding="utf-8")

    spec = load_spec(spec_path)
    app_dir = tmp_path / "app"
    scaffold(app_dir, spec, spec_path=spec_path, intake=INVOICE_PAYER_INTAKE)

    report = validate_app(app_dir)
    assert report.errors == [], "\n".join(report.errors)


# ---------------------------------------------------------------------------
# Unanswered questions are recorded, not guessed
# ---------------------------------------------------------------------------

def test_an_unanswered_job_leaves_a_todo_rather_than_inventing_one() -> None:
    intake = dict(INVOICE_PAYER_INTAKE, q01_job={"answer": None})
    text = spec_from_intake(intake, app_id="invoice-payer")
    assert "TODO" in text
    assert "q01_job" in text


def test_an_unanswered_records_answer_leaves_a_todo_rather_than_inventing_entities() -> None:
    """The specific failure the intake exists to prevent."""
    intake = dict(INVOICE_PAYER_INTAKE, q08_records={"answer": None})
    text = spec_from_intake(intake, app_id="invoice-payer")
    assert "TODO" in text
    assert "q08_records" in text
    spec = AppSpec.parse(text)
    # A placeholder entity, not a plausible-looking invented data model.
    assert [e.name for e in spec.local_entities] == ["example"]


def test_an_unanswered_tools_answer_produces_an_empty_list_with_a_todo() -> None:
    """An empty tool list cannot scaffold, and the TODO says exactly that."""
    intake = dict(INVOICE_PAYER_INTAKE, q13_tools={"answer": None})
    text = spec_from_intake(intake, app_id="invoice-payer")
    assert "TODO" in text
    spec = AppSpec.parse(text)
    assert spec.tools == []


@pytest.mark.parametrize("blank", [None, "", "not sure", "unsure", "unknown", "N/A", "TODO", "?"])
def test_every_spelling_of_no_answer_is_treated_as_unanswered(blank) -> None:
    intake = dict(INVOICE_PAYER_INTAKE, q01_job={"answer": blank})
    text = spec_from_intake(intake, app_id="invoice-payer")
    assert "TODO" in text


def test_an_explicit_app_id_is_not_required_when_the_job_answers() -> None:
    text = spec_from_intake(INVOICE_PAYER_INTAKE)
    spec = AppSpec.parse(text)
    # Derived from the job answer, stopwords dropped, kebab-cased.
    assert spec.app_id == "pays-supplier-invoices"


def test_an_app_id_that_cannot_be_derived_is_reported() -> None:
    intake = {k: {"answer": None} for k in
              ("q01_job", "q08_records", "q13_tools", "q15_pages", "q16_patterns")}
    with pytest.raises(IntakeError, match="identity.id"):
        spec_from_intake(intake)


def test_an_app_id_that_is_not_kebab_able_is_reported() -> None:
    with pytest.raises(IntakeError) as excinfo:
        spec_from_intake(INVOICE_PAYER_INTAKE, app_id="!!!")
    assert "kebab-case" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Answer parsing
# ---------------------------------------------------------------------------

def test_a_flat_intake_mapping_is_accepted_too() -> None:
    """An intake can be written by hand without the question/answer boilerplate."""
    flat = {
        "job": "Pays supplier invoices",
        "records": "invoice: id (string, required), amount (decimal, required)",
        "tools": "lookup_invoice, payments.send",
        "pages": "dashboard, holds",
        "patterns": "summary, approval_queue",
    }
    text = spec_from_intake(flat, app_id="flat-intake")
    spec = AppSpec.parse(text)
    assert spec.tools == ["lookup_invoice", "payments.send"]
    assert [e.name for e in spec.local_entities] == ["invoice"]


def test_a_float_answer_is_mapped_to_decimal_with_the_reason_recorded() -> None:
    """An interview answer of "float" means "a number with a decimal point".

    Refusing would stall the intake on a wording choice, and emitting `float` would
    fail the spec model. Mapping is the only option that keeps moving, and money is
    never a float either way.
    """
    intake = dict(INVOICE_PAYER_INTAKE, q08_records={
        "answer": "invoice: id (string, required), amount (float, required)"})
    spec = AppSpec.parse(spec_from_intake(intake, app_id="x"))
    types = {f.name: str(f.type) for f in spec.entity("invoice").fields}
    assert types["amount"] == "decimal"


def test_an_unknown_field_type_falls_back_to_string() -> None:
    """`string` is the only type that cannot be wrong about magnitude."""
    intake = dict(INVOICE_PAYER_INTAKE, q08_records={
        "answer": "invoice: id (string, required), weird (uuid, required)"})
    spec = AppSpec.parse(spec_from_intake(intake, app_id="x"))
    types = {f.name: str(f.type) for f in spec.entity("invoice").fields}
    assert types["weird"] == "string"


def test_a_missing_id_field_is_added() -> None:
    """Every generated table needs an addressable primary key."""
    intake = dict(INVOICE_PAYER_INTAKE, q08_records={
        "answer": "invoice: amount (decimal, required)"})
    spec = AppSpec.parse(spec_from_intake(intake, app_id="x"))
    fields = [f.name for f in spec.entity("invoice").fields]
    assert fields[0] == "id"


def test_prose_in_the_tools_answer_is_not_emitted_as_a_tool_name() -> None:
    """`lookups, email, payments` is prose; emitting it would produce a manifest the
    loader rejects."""
    intake = dict(INVOICE_PAYER_INTAKE, q13_tools={
        "answer": "lookups, email, payments, and intent verification"})
    text = spec_from_intake(intake, app_id="x")
    spec = AppSpec.parse(text)
    for tool in spec.tools:
        assert " " not in tool
        assert tool.replace(".", "").replace("_", "").isalnum(), tool


def test_a_structured_records_answer_is_accepted() -> None:
    intake = dict(INVOICE_PAYER_INTAKE, q08_records={"answer": [
        {"name": "invoice", "fields": [
            {"name": "id", "type": "string", "required": True},
            {"name": "amount", "type": "decimal", "required": True}]},
    ]})
    spec = AppSpec.parse(spec_from_intake(intake, app_id="x"))
    assert spec.entity("invoice") is not None
    assert spec.entity("invoice").fields[1].type == "decimal"


def test_an_unsafe_entity_name_is_dropped_rather_than_emitted() -> None:
    """The spec model would refuse it later; dropping it here keeps the output valid."""
    intake = dict(INVOICE_PAYER_INTAKE, q08_records={
        "answer": "DROP TABLE invoice: id (string, required)\n"
                  "safe_entity: id (string, required)"})
    text = spec_from_intake(intake, app_id="x")
    spec = AppSpec.parse(text)
    assert "safe_entity" in [e.name for e in spec.local_entities]


# ---------------------------------------------------------------------------
# Pages and patterns
# ---------------------------------------------------------------------------

def test_an_approval_queue_page_is_added_when_the_answers_omit_it() -> None:
    """Every side-effect action can be held, so without one the agents pause
    invisibly and the owner discovers it when they wonder why nothing happened."""
    intake = dict(INVOICE_PAYER_INTAKE,
                  q15_pages={"answer": "dashboard, invoices"},
                  q16_patterns={"answer": "summary, table"})
    spec = AppSpec.parse(spec_from_intake(intake, app_id="x"))
    patterns = {str(p.pattern) for p in spec.pages}
    assert "approval_queue" in patterns


def test_an_existing_approval_queue_page_is_not_duplicated() -> None:
    spec = AppSpec.parse(spec_from_intake(INVOICE_PAYER_INTAKE, app_id="invoice-payer"))
    queues = [p for p in spec.pages if str(p.pattern) == "approval_queue"]
    assert len(queues) == 1


def test_no_pages_answer_produces_a_summary_page_and_a_queue() -> None:
    intake = dict(INVOICE_PAYER_INTAKE, q15_pages={"answer": None},
                  q16_patterns={"answer": None})
    spec = AppSpec.parse(spec_from_intake(intake, app_id="x"))
    patterns = [str(p.pattern) for p in spec.pages]
    assert "summary" in patterns
    assert "approval_queue" in patterns


@pytest.mark.parametrize("answer,expected", [
    ("holds, settings", "approval_queue"),
    ("approval queue", "approval_queue"),
    ("dashboard", "summary"),
    ("overview of totals", "summary"),
    ("policy limits", "settings"),
    ("a list of invoices", "table"),
    ("one invoice detail", "detail"),
    ("add a new record", "form"),
])
def test_a_loosely_worded_page_gets_a_sensible_pattern(answer: str, expected: str) -> None:
    intake = dict(INVOICE_PAYER_INTAKE, q15_pages={"answer": "page"},
                  q16_patterns={"answer": answer})
    spec = AppSpec.parse(spec_from_intake(intake, app_id="x"))
    assert str(spec.pages[0].pattern) == expected
    assert expected in UI_PATTERNS


def test_more_pages_than_patterns_does_not_shift_the_mapping() -> None:
    """A chunk that names no pattern must not be skipped, or every later page gets
    the wrong pattern."""
    intake = dict(INVOICE_PAYER_INTAKE,
                  q15_pages={"answer": "a, b, c"},
                  q16_patterns={"answer": "summary, nonsense, settings"})
    spec = AppSpec.parse(spec_from_intake(intake, app_id="x"))
    patterns = [str(p.pattern) for p in spec.pages]
    assert patterns[0] == "summary"
    assert patterns[2] == "settings"


# ---------------------------------------------------------------------------
# existing / stack interaction
# ---------------------------------------------------------------------------

def test_an_existing_stack_selects_match_existing() -> None:
    intake = dict(INVOICE_PAYER_INTAKE,
                  q04_existing_system={"answer": "ACME back office"},
                  q05_existing_stack={"answer": "FastAPI + Postgres"},
                  q06_existing_data={"answer": "read-only SQL view"},
                  q07_existing_ui={"answer": "Bootstrap 5, dark sidebar"})
    text = spec_from_intake(intake, app_id="integrated")
    spec = AppSpec.parse(text)
    assert spec.stack.match_existing is True
    assert spec.existing.stack == "FastAPI + Postgres"
    assert spec.existing.data == "read-only SQL view"
    assert spec.existing.ui == "Bootstrap 5, dark sidebar"


def test_an_existing_system_with_no_recorded_stack_still_uses_a_named_stack() -> None:
    """`match_existing` with an empty existing.stack is refused by the validator,
    so the intake must not emit it."""
    intake = dict(INVOICE_PAYER_INTAKE,
                  q04_existing_system={"answer": "ACME back office"},
                  q05_existing_stack={"answer": None})
    spec = AppSpec.parse(spec_from_intake(intake, app_id="integrated"))
    assert spec.stack.use == "helios-python-web-1"


def test_ownership_answer_produces_a_note_rather_than_a_silent_choice() -> None:
    """Whether an entity is local or integrated changes whether a table is created."""
    intake = dict(INVOICE_PAYER_INTAKE,
                  q09_data_ownership={"answer": "supplier already exists in the ERP"})
    text = spec_from_intake(intake, app_id="x")
    assert "data.integration" in text
    assert "NOTE" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_questions_writes_to_stdout(capsys) -> None:
    assert main(["questions"]) == 0
    assert "q01_job" in capsys.readouterr().out


def test_cli_questions_writes_to_a_file(tmp_path: Path) -> None:
    out = tmp_path / "intake.yaml"
    assert main(["questions", "-o", str(out)]) == 0
    assert yaml.safe_load(out.read_text(encoding="utf-8"))


def test_cli_questions_refuses_to_clobber_an_existing_file(tmp_path: Path, capsys) -> None:
    """The output is usually a spec someone is about to edit by hand."""
    out = tmp_path / "intake.yaml"
    out.write_text("existing work\n", encoding="utf-8")
    assert main(["questions", "-o", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == "existing work\n"


def test_cli_spec_from_intake_writes_a_loadable_spec(tmp_path: Path) -> None:
    intake_path = tmp_path / "intake.yaml"
    intake_path.write_text(yaml.safe_dump(INVOICE_PAYER_INTAKE), encoding="utf-8")
    out = tmp_path / "helios-app.yaml"

    assert main(["spec-from-intake", str(intake_path), "--app-id", "invoice-payer",
                 "-o", str(out)]) == 0
    assert AppSpec.load(out).app_id == "invoice-payer"


def test_cli_spec_from_intake_reports_an_unusable_intake(tmp_path: Path, capsys) -> None:
    intake_path = tmp_path / "intake.yaml"
    intake_path.write_text("q01_job: {answer: null}\n", encoding="utf-8")
    assert main(["spec-from-intake", str(intake_path)]) == 1
    assert "identity.id" in capsys.readouterr().err


def test_cli_a_missing_intake_file_is_a_usage_error(tmp_path: Path, capsys) -> None:
    assert main(["spec-from-intake", str(tmp_path / "nope.yaml")]) == 2


def test_cli_new_scaffolds_an_app(tmp_path: Path, capsys) -> None:
    app_dir = tmp_path / "app"
    code = main(["new", str(app_dir), "--spec",
                 str(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")])
    assert code == 0
    assert (app_dir / "helios-app.yaml").is_file()
    out = capsys.readouterr().out
    assert "Next: write the policy" in out


def test_cli_new_reports_a_bad_spec_without_writing(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text('spec_version: "9"\n', encoding="utf-8")
    app_dir = tmp_path / "app"
    assert main(["new", str(app_dir), "--spec", str(bad)]) == 1
    assert not app_dir.exists()


def test_cli_new_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    app_dir = tmp_path / "app"
    assert main(["new", str(app_dir), "--dry-run", "--spec",
                 str(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")]) == 0
    assert not app_dir.exists()
    assert "would be written" in capsys.readouterr().out


def test_cli_new_says_that_force_is_ignored(tmp_path: Path, capsys) -> None:
    """Overwriting would destroy policy and scenarios someone wrote into the app."""
    app_dir = tmp_path / "app"
    spec = str(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")
    main(["new", str(app_dir), "--spec", spec])
    capsys.readouterr()

    (app_dir / "tools.py").write_text("# hand written\n", encoding="utf-8")
    assert main(["new", str(app_dir), "--spec", spec, "--force"]) == 0
    assert "--force is ignored" in capsys.readouterr().err
    assert (app_dir / "tools.py").read_text(encoding="utf-8") == "# hand written\n"


@pytest.mark.parametrize("app,expected", [
    ("apps/helios-collect", 0),
    ("entry-point/examples/invoice-payer", 0),
    ("platform/app-builder/tests/fixtures/broken-app", 1),
])
def test_cli_validate_exit_codes(app: str, expected: int, capsys) -> None:
    assert main(["validate", str(REPO / app)]) == expected
    capsys.readouterr()


def test_cli_validate_strict_turns_warnings_into_failures(capsys) -> None:
    """helios-collect is error-free but carries graph warnings."""
    assert main(["validate", str(REPO / "apps" / "helios-collect")]) == 0
    capsys.readouterr()
    assert main(["validate", "--strict", str(REPO / "apps" / "helios-collect")]) == 1
    assert "--strict" in capsys.readouterr().out


def test_cli_validate_reports_a_missing_directory_as_usage(capsys, tmp_path: Path) -> None:
    assert main(["validate", str(tmp_path / "nope")]) == 2


def test_cli_with_no_command_is_a_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_cli_an_unknown_command_is_a_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == 2

"""Tests for the scaffolder.

The acceptance criteria are the ones that matter operationally:

* scaffold from `invoice-payer`'s spec -> `validate_app` reports 0 errors
* the generated app's own tests pass
* scaffolding again writes nothing (idempotent)
* `--dry-run` into `apps/helios-collect` lists only files that do not exist there
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app_builder.scaffold import (
    ScaffoldError,
    guess_side_effect,
    load_spec,
    scaffold,
)
from app_builder.validate import validate_app

REPO = Path(__file__).resolve().parents[3]
INVOICE_PAYER = REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml"
COLLECT = REPO / "apps" / "helios-collect" / "helios-app.yaml"
COLLECT_DIR = REPO / "apps" / "helios-collect"


@pytest.fixture
def spec():
    return load_spec(INVOICE_PAYER)


@pytest.fixture
def generated(spec, tmp_path: Path) -> Path:
    """A freshly scaffolded app directory."""
    app_dir = tmp_path / "app"
    scaffold(app_dir, spec, spec_path=INVOICE_PAYER)
    return app_dir


# ---------------------------------------------------------------------------
# What gets generated
# ---------------------------------------------------------------------------

def test_the_skeleton_contains_every_expected_file(generated: Path) -> None:
    for rel in [
        "helios-app.yaml", "helios.yaml",
        "agents/invoice-payer-audit.yaml", "agents/invoice-payer-action.json",
        "facts.py", "tools.py", "graph.py", "store.py", "app.py", "server.py",
        "data/schema.sql", "data/README.md",
        "tests/test_controls.py", "README.md",
        "templates/base.html.j2", "templates/_held_action.html.j2",
        "static/tokens.css", "static/htmx.min.js",
    ]:
        assert (generated / rel).is_file(), rel


def test_only_the_declared_patterns_are_copied(generated: Path) -> None:
    """invoice-payer declares five patterns and no `form`, so no form template.

    Copying all six would leave a template nothing routes to, and a reader would
    reasonably assume it was wired up.
    """
    templates = {p.name for p in (generated / "templates").glob("*.j2")}
    assert "form.html.j2" not in templates
    assert "approval_queue.html.j2" in templates
    assert "summary.html.j2" in templates


def test_the_spec_is_copied_verbatim_with_its_comments(generated: Path) -> None:
    """A spec carries the author's reasoning; re-dumping the model destroys it."""
    original = INVOICE_PAYER.read_text(encoding="utf-8")
    copied = (generated / "helios-app.yaml").read_text(encoding="utf-8")
    assert copied == original
    assert "# kebab-case" in copied  # a comment from the source file survived


# ---------------------------------------------------------------------------
# The generated skeleton validates clean
# ---------------------------------------------------------------------------

def test_a_freshly_scaffolded_app_validates_with_zero_errors(generated: Path) -> None:
    report = validate_app(generated)
    assert report.errors == [], "\n".join(report.errors)
    assert report.exit_code == 0


def test_scaffolding_from_the_other_real_app_also_validates_clean(tmp_path: Path) -> None:
    s = load_spec(COLLECT)
    app_dir = tmp_path / "collect"
    scaffold(app_dir, s, spec_path=COLLECT)
    report = validate_app(app_dir)
    assert report.errors == [], "\n".join(report.errors)


def test_the_generated_audit_manifest_loads_under_its_own_validator(generated: Path) -> None:
    from audit_agent.manifest import AuditManifest
    manifest = AuditManifest.load(generated / "agents" / "invoice-payer-audit.yaml")
    assert manifest.app_id == "invoice-payer"
    assert {t.name for t in manifest.tools} == set(load_spec(INVOICE_PAYER).tools)
    assert manifest.tasks, "a manifest with no tasks denies everything"


def test_the_generated_action_manifest_loads_under_its_own_validator(generated: Path) -> None:
    from agent_toolkit.manifest import validate_manifest
    text = (generated / "agents" / "invoice-payer-action.json").read_text(encoding="utf-8")
    manifest = validate_manifest(json.loads(text))
    assert manifest.name == "invoice-payer"


def test_the_generated_action_manifest_carries_the_five_point_contract(generated: Path) -> None:
    """Points 3 and 4 are what make an agent legible under constraint."""
    document = json.loads(
        (generated / "agents" / "invoice-payer-action.json").read_text(encoding="utf-8"))
    instructions = document["instructions"].lower()
    assert "read before you act" in instructions
    assert "authorization" in instructions
    assert "held" in instructions
    assert "reword" in instructions  # do not reword and retry
    assert "denied" in instructions
    assert "report per item" in instructions


def test_the_generated_mandate_limits_are_empty(generated: Path) -> None:
    """The loader accepts only four receivables-shaped keys.

    Guessing those into an unrelated app would be worse than an empty block: the
    authoritative policy is the audit manifest, and a mandate limit that looks
    meaningful but belongs to another domain invites a reader to trust it.
    """
    document = json.loads(
        (generated / "agents" / "invoice-payer-action.json").read_text(encoding="utf-8"))
    assert document["mandate"]["limits"] == {}


def test_the_generated_manifests_declare_no_unknown_keys(generated: Path) -> None:
    """`extra="forbid"`: even a `_comment` is rejected by the action loader."""
    document = json.loads(
        (generated / "agents" / "invoice-payer-action.json").read_text(encoding="utf-8"))
    allowed = {"name", "version", "description", "instructions", "tools",
               "mandate", "model", "max_turns"}
    assert set(document) <= allowed


def test_no_rule_id_claims_the_baseline_namespace(generated: Path) -> None:
    """Baseline rules belong to the platform and redefining one is refused at load."""
    text = (generated / "agents" / "invoice-payer-audit.yaml").read_text(encoding="utf-8")
    manifest = yaml.safe_load(text)
    for rule in manifest.get("rules") or []:
        assert not rule["id"].startswith("baseline.")


# ---------------------------------------------------------------------------
# Every generated Python file compiles
# ---------------------------------------------------------------------------

GENERATED_PYTHON = ["store.py", "facts.py", "tools.py", "graph.py", "app.py",
                    "server.py", "tests/test_controls.py"]


@pytest.mark.parametrize("name", GENERATED_PYTHON)
def test_generated_python_compiles(generated: Path, name: str) -> None:
    source = (generated / name).read_text(encoding="utf-8")
    compile(source, name, "exec")


def test_the_generated_tools_import_and_build(generated: Path) -> None:
    """`ToolDefinition` requires a non-empty output_schema, so a stub that omits it
    would not even construct."""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import tools\n"
        "built = tools.build_tools()\n"
        "print(len(built), sorted(t.name for t in built))\n"
        "print(sorted((t.name, t.side_effect) for t in built))\n"
    ) % str(generated)
    proc = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                          text=True, cwd=str(generated))
    assert proc.returncode == 0, proc.stderr
    assert "'payments.send', True" in proc.stdout
    assert "'lookup_invoice', False" in proc.stdout


def test_the_generated_tools_agree_with_the_generated_audit_manifest(generated: Path) -> None:
    """Both are generated from one spec, so they must not disagree."""
    report = validate_app(generated)
    assert not [e for e in report.errors if "side_effect" in e], report.errors


def test_a_tool_stub_refuses_with_an_actionable_message(generated: Path) -> None:
    """A stub returning a plausible empty result would let a demo 'succeed'."""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import tools\n"
        "tool = next(t for t in tools.build_tools() if t.name == 'payments.send')\n"
        "try:\n"
        "    tool.handler({'amount_cents': 100})\n"
        "except NotImplementedError as exc:\n"
        "    print('RAISED', exc)\n"
    ) % str(generated)
    proc = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                          text=True, cwd=str(generated))
    assert "RAISED" in proc.stdout, proc.stderr
    assert "payments.send" in proc.stdout
    assert "tools.py" in proc.stdout


# ---------------------------------------------------------------------------
# The generated app's own tests pass
# ---------------------------------------------------------------------------

def test_the_generated_tests_pass(generated: Path, tmp_path: Path) -> None:
    """Acceptance criterion: the generated `tests/` pass.

    A `conftest.py` is written next to them to put the platform packages on the
    path, which is what `scripts/bootstrap-venv.sh` does for a real checkout. The
    generated app cannot assume it is installed.
    """
    paths = [
        REPO / "platform" / "audit-agent-builder" / "src",
        REPO / "platform" / "action-agent-builder" / "src",
        REPO / "platform" / "agent-integration" / "src",
        REPO / "platform" / "financial-tools",
        REPO / "platform" / "app-builder" / "src",
    ]
    conftest = "import sys\n" + "".join(f"sys.path.insert(0, {str(p)!r})\n" for p in paths)
    conftest += "import os\nsys.path.insert(0, os.path.dirname(__file__))\n"
    (generated / "conftest.py").write_text(conftest, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "tests", "-q",
         "--override-ini=addopts="],
        capture_output=True, text=True, cwd=str(generated))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "passed" in proc.stdout


# ---------------------------------------------------------------------------
# Idempotency and never overwriting
# ---------------------------------------------------------------------------

def test_scaffolding_again_writes_nothing(spec, generated: Path) -> None:
    before = {p: p.read_bytes() for p in generated.rglob("*") if p.is_file()}
    result = scaffold(generated, spec, spec_path=INVOICE_PAYER)
    assert result.written == []
    assert result.skipped, "the second run should report every file as skipped"

    after = {p: p.read_bytes() for p in generated.rglob("*") if p.is_file()}
    assert set(after) == set(before)
    assert all(after[p] == before[p] for p in before), "a file was modified"


def test_an_existing_file_is_never_overwritten(spec, generated: Path) -> None:
    """Regenerating over someone's in-progress work costs hours and errors silently."""
    target = generated / "tools.py"
    target.write_text("# my work in progress\n", encoding="utf-8")

    result = scaffold(generated, spec, spec_path=INVOICE_PAYER)
    assert target in result.skipped
    assert target.read_text(encoding="utf-8") == "# my work in progress\n"


def test_a_dry_run_writes_nothing_at_all(spec, tmp_path: Path) -> None:
    app_dir = tmp_path / "dry"
    result = scaffold(app_dir, spec, dry_run=True, spec_path=INVOICE_PAYER)
    assert result.written, "a dry run should still report what it would write"
    assert not app_dir.exists() or list(app_dir.rglob("*")) == []


def test_a_dry_run_into_a_real_app_lists_only_missing_files(spec) -> None:
    """Acceptance criterion, against the real helios-collect directory."""
    result = scaffold(COLLECT_DIR, load_spec(COLLECT), dry_run=True, spec_path=COLLECT)
    assert result.written, "helios-collect is missing generated files"
    for path in result.written:
        assert not path.exists(), f"dry run would overwrite {path}"
    for path in result.skipped:
        assert path.exists(), f"{path} was skipped but does not exist"


def test_a_dry_run_does_not_create_the_directory(spec, tmp_path: Path) -> None:
    app_dir = tmp_path / "never-created"
    scaffold(app_dir, spec, dry_run=True, spec_path=INVOICE_PAYER)
    assert not app_dir.exists()


# ---------------------------------------------------------------------------
# helios.yaml — only what the engine accepts
# ---------------------------------------------------------------------------

def test_the_deployment_manifest_uses_only_accepted_fields(generated: Path) -> None:
    """Emit exactly what the engine's Manifest declares, read at generation time.

    The engine rejects unknown fields, so an extra key is a deploy-time error;
    a missing `audit_manifest` / `data_dir` / `env` would be a manifest that looks
    configured and is not. Both directions are pinned against the real engine.
    """
    document = yaml.safe_load((generated / "helios.yaml").read_text(encoding="utf-8"))
    from app_builder.deployment import discover_manifest_fields
    fields = discover_manifest_fields(REPO)
    assert set(document) <= set(fields.fields)
    for key in ("audit_manifest", "data_dir", "env"):
        assert (key in document) == fields.accepts(key)


def test_the_deployment_manifest_names_the_asgi_entry(generated: Path) -> None:
    document = yaml.safe_load((generated / "helios.yaml").read_text(encoding="utf-8"))
    assert document["entry"] == "server:app"
    assert document["name"] == "invoice-payer"
    assert document["port"] == 3000


def test_the_deployment_manifest_permissions_are_within_the_engine_set(generated: Path) -> None:
    """An unknown permission is a hard ContractError at deploy time."""
    document = yaml.safe_load((generated / "helios.yaml").read_text(encoding="utf-8"))
    from app_builder.deployment import DEFAULT_ALLOWED_TOOLS
    assert set(document["permissions"]) <= DEFAULT_ALLOWED_TOOLS


def test_permissions_are_derived_from_the_tool_families(generated: Path) -> None:
    document = yaml.safe_load((generated / "helios.yaml").read_text(encoding="utf-8"))
    # invoice-payer declares payments.* and email.* tools.
    assert "payments" in document["permissions"]
    assert "email" in document["permissions"]


def test_known_families_map_to_engine_permissions_and_unknown_ones_are_dropped(tmp_path: Path) -> None:
    """A family the engine knows becomes a permission; one it does not must not
    make the app undeployable, and is named in a REVIEW note instead."""
    from app_builder.scaffold import deployment_manifest
    from app_builder.deployment import discover_manifest_fields
    from app_builder.spec import AppSpec

    s = AppSpec.parse(f"""
spec_version: "1"
identity: {{ id: forecasty, name: Forecasty }}
stack: {{ use: helios-python-web-1 }}
data:
  local:
    entities:
      - name: thing
        fields: [{{ name: id, type: string, required: true }}]
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools:
  - forecast.run
  - email.send
  - bogus.run
""")
    text = deployment_manifest(s, discover_manifest_fields(REPO))
    document = yaml.safe_load(text)
    assert document["permissions"] == ["email", "forecast"]
    assert "bogus" in text  # named in the REVIEW comment
    assert "REVIEW" in text


def test_the_field_set_is_read_from_the_engine_when_it_exists(tmp_path: Path) -> None:
    """When platform/deployment/ lands, generated manifests widen automatically."""
    from app_builder.deployment import discover_manifest_fields

    engine = tmp_path / "platform" / "deployment" / "engine"
    engine.mkdir(parents=True)
    (tmp_path / "specs").mkdir()
    (engine / "contracts.py").write_text(
        "from pydantic import BaseModel\n"
        "ALLOWED_TOOLS = {'payments', 'email', 'knowledge-graph', 'events'}\n"
        "class Manifest(BaseModel):\n"
        "    name: str\n"
        "    tenant: str = ''\n"
        "    entry: str = 'main:app'\n"
        "    port: int = 3000\n"
        "    permissions: list[str] = []\n"
        "    data_dir: str = ''\n",
        encoding="utf-8")

    fields = discover_manifest_fields(tmp_path)
    assert fields.discovered is True
    assert "data_dir" in fields.fields

    from app_builder.scaffold import deployment_manifest

    document = yaml.safe_load(deployment_manifest(load_spec(INVOICE_PAYER), fields))
    assert document["data_dir"] == "/data"
    # Widening must not drop the fields that were already accepted.
    assert document["entry"] == "server:app"
    assert set(document["permissions"]) <= fields.allowed_tools


# ---------------------------------------------------------------------------
# server.py
# ---------------------------------------------------------------------------

def test_the_server_module_compiles_and_defers_building_the_app(generated: Path) -> None:
    """`build_app` opens a database and loads both manifests, so importing it
    eagerly would make the engine's smoke test depend on data being present."""
    source = (generated / "server.py").read_text(encoding="utf-8")
    assert "from app import build_app" in source
    # ...and that import must be inside a function, not at module scope.
    module_scope = [l for l in source.splitlines()
                    if l.startswith("from app import") or l.startswith("import app")]
    assert not module_scope, module_scope


def test_the_server_page_routes_are_flat_so_relative_urls_resolve(generated: Path) -> None:
    """The host strips /apps/<slug>, and the kit's URLs are relative.

    A nested route like /holds/123 would resolve "static/..." against /holds/, so
    the kit's relative-URL rule only holds while PAGE routes stay flat.

    Only GET routes are checked. `POST /holds/decide` is an HTMX form target that
    returns a single card fragment rather than a page, so it never renders the base
    layout and its depth is irrelevant to asset resolution.
    """
    source = (generated / "server.py").read_text(encoding="utf-8")
    pages = [line for line in source.splitlines() if '@app.get("' in line]
    assert pages, "no page routes were generated"
    for route in pages:
        path = route.split('"')[1]
        assert path.count("/") == 1, f"nested page route {path!r} breaks relative URLs"


def test_the_server_exposes_a_health_endpoint(generated: Path) -> None:
    """The deployment contract: helios.yaml + ASGI entry + GET /health."""
    source = (generated / "server.py").read_text(encoding="utf-8")
    assert '@app.get("/health")' in source


# ---------------------------------------------------------------------------
# graph.py
# ---------------------------------------------------------------------------

def test_the_graph_module_publishes_each_declared_event(generated: Path) -> None:
    source = (generated / "graph.py").read_text(encoding="utf-8")
    spec = load_spec(INVOICE_PAYER)
    for link in spec.graph.publishes:
        for event in link.events:
            fn = f"publish_{link.entity}_{event}".replace("-", "_")
            assert fn in source, fn


def test_the_graph_module_uses_the_real_api_not_a_guessed_one(generated: Path) -> None:
    """There is no `publish_verified`; the real method is `upsert_verified`."""
    source = (generated / "graph.py").read_text(encoding="utf-8")
    assert "upsert_verified" in source
    assert "publish_verified" not in source


def test_the_graph_module_warns_about_an_unowned_published_entity(spec, tmp_path: Path) -> None:
    """invoice-payer publishes `payment`, which it does not declare locally."""
    result = scaffold(tmp_path / "app", spec, spec_path=INVOICE_PAYER)
    assert any("payment" in w and "not declared in data.local" in w
               for w in result.warnings), result.warnings


def test_a_declared_read_produces_a_stub_and_a_warning(tmp_path: Path) -> None:
    """A read with no consumer is worth saying out loud: the app will not react to
    changes originating elsewhere, and the declaration implies it will."""
    from app_builder.spec import AppSpec

    s = AppSpec.parse("""
spec_version: "1"
identity: { id: graph-reader, name: Graph Reader }
stack: { use: helios-python-web-1 }
data:
  local:
    entities:
      - name: invoice
        fields:
          - { name: id, type: string, required: true }
          - { name: customer_id, type: string, required: true }
graph:
  reads:
    - { entity: payment, events: [confirmed] }
  publishes:
    - { entity: invoice, events: [reminder_sent] }
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools:
  - lookup_invoice
""")
    app_dir = tmp_path / "reader"
    result = scaffold(app_dir, s)
    source = (app_dir / "graph.py").read_text(encoding="utf-8")

    assert "subscribe_payment" in source
    assert 'READS: tuple[tuple[str, str], ...] = (\n    ("payment", "confirmed"),' in source
    assert any("subscribe_payment" in w for w in result.warnings), result.warnings
    # A published entity that IS declared locally gets no ownership warning.
    assert not any("not declared in data.local" in w for w in result.warnings), \
        result.warnings


# ---------------------------------------------------------------------------
# facts.py and store.py
# ---------------------------------------------------------------------------

def test_the_generated_fact_source_implements_the_whole_protocol(generated: Path) -> None:
    source = (generated / "facts.py").read_text(encoding="utf-8")
    for method in ("amount_for", "contact_count_for", "counterparty_for",
                   "payment_details_changed_for", "extra_facts_for",
                   "record_contact"):
        assert f"def {method}(" in source, method


def test_the_generated_fact_source_states_the_unknown_is_none_rule(generated: Path) -> None:
    """Unknown must be None, never zero, or a fail-closed control becomes fail-open."""
    source = (generated / "facts.py").read_text(encoding="utf-8")
    assert "never zero" in source
    # Both invariants are stated in the generated file, because they are the two a
    # helpful edit is most likely to break.
    assert "Nothing here may consult the request" in source


def test_the_generated_store_is_parameterised_only(generated: Path) -> None:
    """Generated SQL interpolating a value would be an injection point."""
    source = (generated / "store.py").read_text(encoding="utf-8")
    assert "conn.execute(sql, [data[c] for c in cols])" in source
    # No f-string may carry a value into SQL text.
    for line in source.splitlines():
        if "execute(" in line and "{" in line:
            assert "?" in line or "SELECT" in line or "INSERT" in line or "UPDATE" in line, line


def test_the_generated_ddl_matches_the_spec_entities(generated: Path) -> None:
    ddl = (generated / "data" / "schema.sql").read_text(encoding="utf-8")
    for entity in load_spec(INVOICE_PAYER).local_entities:
        assert f'CREATE TABLE IF NOT EXISTS "{entity.name}"' in ddl


# ---------------------------------------------------------------------------
# side_effect guessing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("payments.send", True), ("email.send", True), ("record_payment", True),
    ("publish_invoice", True), ("create_invoice", True), ("save_draft", True),
    ("refund_payment", True), ("reschedule_meeting", True),
    ("payments.balance", False), ("lookup_invoice", False),
    ("list_overdue_invoices", False), ("read_record", False),
    # object-first names: the verb is at the END
    ("reimbursements_send", True), ("draw_credit_line", True),
    ("escalate_to_owner", True), ("supplier_profile", False),
    ("check_funds", False), ("funding_options", False),
])
def test_the_side_effect_guess_matches_the_tool_name(name: str, expected: bool) -> None:
    assert guess_side_effect(name) is expected


def test_the_side_effect_guess_matches_whole_segments_only() -> None:
    """`resend_count` contains the letters of `send` but is a read."""
    assert guess_side_effect("resend_count") is False
    assert guess_side_effect("send_reminder_email") is True


def test_a_stated_flag_beats_the_guess_and_is_not_marked_for_review(tmp_path: Path) -> None:
    from app_builder.scaffold import action_manifest, audit_manifest, controls_tests
    from app_builder.spec import AppSpec

    s = AppSpec.parse("""
spec_version: "1"
identity: { id: expenses, name: Expenses }
stack: { use: helios-python-web-1 }
data:
  local:
    entities:
      - name: claim
        fields: [{ name: id, type: string, required: true }]
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools:
  - claims_lookup
  - { name: budget_snapshot, side_effect: true }
  - { name: send_nothing, side_effect: false }
  - reimbursements_send
""")
    assert s.tools == ["claims_lookup", "budget_snapshot", "send_nothing",
                       "reimbursements_send"]
    assert s.side_effect_for("budget_snapshot") is True
    assert s.side_effect_for("send_nothing") is False
    assert s.side_effect_for("claims_lookup") is None

    audit = audit_manifest(s)

    def declaration(tool: str) -> str:
        start = audit.index(f"- name: {tool}")
        rest = audit[start + 1:]
        end = rest.find("- name: ")
        return audit[start:] if end < 0 else audit[start:start + 1 + end]

    assert "side_effect: true  # declared in helios-app.yaml" in declaration("budget_snapshot")
    assert "side_effect: false  # declared in helios-app.yaml" in declaration("send_nothing")
    assert "side_effect: true  # REVIEW" in declaration("reimbursements_send")

    action = json.loads(action_manifest(s))
    assert action["mandate"]["allowed_tools"] == ["budget_snapshot", "reimbursements_send"]

    tests = controls_tests(s)
    assert 'GATED_TOOLS = ["budget_snapshot", "reimbursements_send"]' in tests


def test_a_fresh_scaffold_validates_clean_immediately(generated: Path) -> None:
    """The rehearsal blocker: a scaffolded app must not be born invalid."""
    report = validate_app(generated)
    assert report.exit_code == 0, report.errors
    manifest = json.loads((generated / "agents" / "invoice-payer-action.json").read_text())
    assert manifest["mandate"]["allowed_tools"], "mandate.allowed_tools is empty"
    tests = (generated / "tests" / "test_controls.py").read_text(encoding="utf-8")
    assert "GATED_TOOLS = []" not in tests


def test_every_guessed_flag_is_marked_for_review(generated: Path) -> None:
    """A guess about whether a call is gated must not look like a decision."""
    text = (generated / "agents" / "invoice-payer-audit.yaml").read_text(encoding="utf-8")
    assert text.count("# REVIEW: side_effect guessed") == len(load_spec(INVOICE_PAYER).tools)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_a_spec_with_no_tools_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """An audit manifest with no tools will not load, so the skeleton cannot validate."""
    from app_builder.spec import AppSpec
    s = AppSpec.parse("""
spec_version: "1"
identity: { id: toolless, name: Toolless }
stack: { use: helios-python-web-1 }
data:
  local:
    entities:
      - name: thing
        fields: [{ name: id, type: string, required: true }]
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools: []
""")
    app_dir = tmp_path / "toolless"
    with pytest.raises(ScaffoldError, match="no tools"):
        scaffold(app_dir, s)
    assert not app_dir.exists(), "nothing should be written when generation fails"


def test_a_match_existing_spec_is_refused_rather_than_guessing_a_stack(tmp_path: Path) -> None:
    """Generating SQLite tables for an app joining a Postgres system would be wrong
    in a way that looks fine until it runs."""
    from app_builder.spec import AppSpec
    s = AppSpec.parse("""
spec_version: "1"
identity: { id: integrated, name: Integrated }
existing:
  system: ACME
  stack: Rails + Postgres
stack: { match_existing: true }
data:
  local:
    entities:
      - name: thing
        fields: [{ name: id, type: string, required: true }]
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools: [lookup_thing]
""")
    with pytest.raises(ScaffoldError, match="match_existing"):
        scaffold(tmp_path / "integrated", s)


def test_a_failure_part_way_through_leaves_no_partial_app(tmp_path: Path) -> None:
    """Everything is rendered before anything is written, so an aborted run cannot
    leave a half-generated app that passes some checks and fails others."""
    app_dir = tmp_path / "partial"
    # A spec whose entity name is a SQL keyword is fine, but force a generator
    # failure by pointing at a stack the type map cannot serve.
    from app_builder.spec import AppSpec
    s = load_spec(INVOICE_PAYER)
    broken = AppSpec.parse(
        INVOICE_PAYER.read_text(encoding="utf-8").replace(
            "use: helios-python-web-1", "use: no-such-stack"))
    with pytest.raises(ScaffoldError):
        scaffold(app_dir, broken)
    assert not app_dir.exists() or list(app_dir.rglob("*")) == []

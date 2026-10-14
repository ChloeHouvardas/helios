"""Tests for stacks.yaml, DDL generation and the generated store module."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app_builder.schema import SchemaError, ddl_for, seed_from_json, store_module_for
from app_builder.spec import AppSpec, AppSpecError
from app_builder.stacks import (
    STACKS_YAML,
    StacksError,
    default_stack,
    load_stacks,
    stack_for_spec,
)

REPO = Path(__file__).resolve().parents[3]
REAL_APPS = [
    REPO / "apps" / "helios-collect",
    REPO / "entry-point" / "examples" / "invoice-payer",
]


@pytest.fixture
def spec() -> AppSpec:
    return AppSpec.load(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")


@pytest.fixture
def collect_spec() -> AppSpec:
    return AppSpec.load(REPO / "apps" / "helios-collect" / "helios-app.yaml")


@pytest.fixture
def store(spec: AppSpec, tmp_path: Path):
    """A real generated store.py, written to disk and imported.

    Written and imported rather than exec'd into a dict, so that a generated module
    is tested the way it will actually be used — and so that `HERE`-relative paths
    inside it resolve correctly.
    """
    import importlib.util

    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "data" / "schema.sql").write_text(ddl_for(spec), encoding="utf-8")
    (app / "store.py").write_text(store_module_for(spec), encoding="utf-8")

    module_spec = importlib.util.spec_from_file_location("gen_store", app / "store.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    conn = module.connect(app / "data" / "test.sqlite3")
    module.init_db(conn)
    return module, conn


# ---------------------------------------------------------------------------
# stacks.yaml
# ---------------------------------------------------------------------------

def test_stacks_yaml_ships_with_the_package() -> None:
    assert STACKS_YAML.exists(), STACKS_YAML


def test_stacks_yaml_parses_and_defines_the_default_stack() -> None:
    data = load_stacks()
    assert data["default"] in data["stacks"]


def test_the_named_stack_is_pinned_not_floating() -> None:
    """"Latest" is not a version: an app generated in week one must build in week ten."""
    stack = default_stack()
    assert stack.language == "python 3.13"
    assert stack.database == "sqlite"
    for component in ("language", "web_framework", "server", "database", "frontend"):
        version = str((stack.definition.get(component) or {}).get("version", ""))
        assert version, f"{component} has no pinned version"
        assert version.lower() not in ("latest", "lts", "*"), component


def test_every_spec_field_type_has_a_type_map_entry() -> None:
    """A field type with no mapping would raise at generation time, not load time."""
    from app_builder.spec import FIELD_TYPES
    stack = default_stack()
    for field_type in sorted(FIELD_TYPES):
        assert field_type in stack.type_map, field_type


def test_every_referenced_check_fragment_exists() -> None:
    data = load_stacks()
    stack = default_stack()
    for field_type, entry in stack.type_map.items():
        key = entry.get("check")
        if key:
            assert key in data["checks"], (
                f"type_map.{field_type} refers to check {key!r}, which stacks.yaml "
                "does not define")


def test_the_vendored_htmx_matches_its_recorded_digest() -> None:
    """The pin is verifiable rather than asserted.

    stacks.yaml records a sha256 and a byte count for the vendored htmx.min.js. If
    the file ever changes without the record changing, this fails — which is the
    only way a supply-chain swap of a vendored dependency becomes visible.
    """
    import hashlib

    from app_builder.stacks import kit_dir

    stack = default_stack()
    frontend = stack.frontend
    path = kit_dir() / frontend["vendored"]
    assert path.exists(), path

    data = path.read_bytes()
    assert len(data) == int(frontend["bytes"])
    assert hashlib.sha256(data).hexdigest() == frontend["sha256"]
    assert frontend["version"].encode() in data


def test_the_vendored_htmx_is_a_real_htmx_bundle() -> None:
    """Guard against a vendored file that is an HTML error page or an empty stub.

    A download that returns 200 with a "not found" body would satisfy the digest
    test above the moment someone re-records it, so assert on content too.
    """
    from app_builder.stacks import kit_dir

    text = (kit_dir() / "static" / "htmx.min.js").read_text(encoding="utf-8")
    assert text.startswith("var htmx=function()"), text[:120]
    assert "<html" not in text.lower(), "the vendored file looks like an HTML page"


def test_package_data_resolves_from_inside_the_package() -> None:
    """`stacks.yaml` must be found relative to the module, not the repo layout.

    An editable install hides this bug: the source tree IS the install, so a
    repo-relative path works everywhere until someone pip-installs the wheel, where
    the repo layout does not exist at all. Verified by building a wheel and
    installing it non-editable during development of this package.
    """
    from app_builder.stacks import _STACKS_CANDIDATES, STACKS_YAML, kit_dir

    resolved = Path(STACKS_YAML)
    assert resolved.exists()
    # The resolved path must be one of the searched locations, so this test fails
    # if the search list and the resolution ever drift apart.
    assert resolved in _STACKS_CANDIDATES, (resolved, _STACKS_CANDIDATES)
    assert kit_dir().is_dir()
    assert (kit_dir() / "static" / "htmx.min.js").exists()


def test_a_missing_stacks_yaml_names_the_locations_it_tried(tmp_path: Path) -> None:
    """A broken install must say where it looked, not just that something is absent."""
    from app_builder import stacks

    original = stacks._STACKS_CANDIDATES
    try:
        stacks._STACKS_CANDIDATES = (tmp_path / "nope.yaml", tmp_path / "also-nope.yaml")
        with pytest.raises(stacks.StacksError) as excinfo:
            stacks._find_stacks_yaml()
        message = str(excinfo.value)
        assert "nope.yaml" in message and "also-nope.yaml" in message
    finally:
        stacks._STACKS_CANDIDATES = original


def test_match_existing_has_no_stack_to_generate_from() -> None:
    """An integration stack has no type map, so generating SQLite would be wrong."""
    s = AppSpec.parse("""
spec_version: "1"
identity: { id: integrated-app, name: Integrated }
existing:
  system: ACME back office
  stack: Rails + Postgres
  data: read-only SQL view
  ui: null
stack: { match_existing: true }
data:
  local:
    entities:
      - name: invoice
        fields:
          - { name: id, type: string, required: true }
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools: []
""")
    assert s.stack.match_existing is True
    assert s.stack.use is None
    with pytest.raises(StacksError, match="match_existing"):
        stack_for_spec(s)


def test_an_unknown_stack_name_lists_the_real_ones() -> None:
    s = AppSpec.parse("""
spec_version: "1"
identity: { id: unknown-stack, name: Unknown }
stack: { use: no-such-stack }
data:
  local:
    entities:
      - name: invoice
        fields:
          - { name: id, type: string, required: true }
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools: []
""")
    with pytest.raises(StacksError) as excinfo:
        stack_for_spec(s)
    assert "helios-python-web-1" in str(excinfo.value)
    assert "no-such-stack" in str(excinfo.value)


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app", REAL_APPS, ids=lambda p: p.name)
def test_ddl_generates_one_table_per_entity(app: Path) -> None:
    s = AppSpec.load(app / "helios-app.yaml")
    ddl = ddl_for(s)
    for entity in s.local_entities:
        assert f'CREATE TABLE IF NOT EXISTS "{entity.name}"' in ddl, entity.name


def test_ddl_is_valid_sql(spec: AppSpec) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl_for(spec))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {e.name for e in spec.local_entities}


def test_required_fields_are_not_null_and_optional_are_not(spec: AppSpec) -> None:
    ddl = ddl_for(spec)
    invoice = next(e for e in spec.local_entities if e.name == "invoice")
    for field in invoice.fields:
        line = next(l for l in ddl.splitlines() if l.strip().startswith(f'"{field.name}"'))
        if field.required and field.name != "id":
            assert "NOT NULL" in line, field.name
        elif not field.required:
            assert "NOT NULL" not in line, field.name


def test_an_id_field_becomes_the_primary_key(spec: AppSpec) -> None:
    assert '"id" TEXT PRIMARY KEY' in ddl_for(spec)


def test_a_decimal_field_is_text_with_a_check(spec: AppSpec) -> None:
    """REAL would put a binary float in the database."""
    ddl = ddl_for(spec)
    line = next(l for l in ddl.splitlines() if l.strip().startswith('"amount"'))
    assert "TEXT" in line and "REAL" not in line
    assert "CHECK" in line


def test_an_integer_cents_field_is_integer(spec: AppSpec) -> None:
    ddl = ddl_for(spec)
    line = next(l for l in ddl.splitlines() if l.strip().startswith('"balance_cents"'))
    assert line.strip().startswith('"balance_cents" INTEGER')


def test_a_boolean_field_is_integer_zero_or_one(spec: AppSpec) -> None:
    """SQLite has no boolean type; BOOLEAN affinity is INTEGER."""
    ddl = ddl_for(spec)
    line = next(l for l in ddl.splitlines() if l.strip().startswith('"duplicate"'))
    assert "INTEGER" in line
    assert '"duplicate" IN (0, 1)' in line


def test_a_date_field_stores_iso_text(spec: AppSpec) -> None:
    ddl = ddl_for(spec)
    line = next(l for l in ddl.splitlines() if l.strip().startswith('"due_date"'))
    assert "TEXT" in line and "CHECK" in line


def test_a_json_field_is_validated(spec: AppSpec) -> None:
    ddl = ddl_for(AppSpec.load(REPO / "apps" / "helios-collect" / "helios-app.yaml"))
    line = next(l for l in ddl.splitlines() if l.strip().startswith('"payload"'))
    assert "json_valid" in line


def test_identifiers_are_quoted_so_reserved_words_work() -> None:
    """A legal spec naming a field `order` must still generate parseable DDL.

    The spec model's identifier pattern admits SQL keywords, and a generated
    statement that does not parse is a worse outcome than a refused spec.
    """
    text = f"""
spec_version: "1"
identity: {{ id: reserved-words, name: Reserved }}
stack: {{ use: helios-python-web-1 }}
data:
  local:
    entities:
      - name: order
        fields:
          - {{ name: id, type: string, required: true }}
          - {{ name: order, type: integer, required: true }}
          - {{ name: select, type: string, required: false }}
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools: []
"""
    s = AppSpec.parse(text)
    ddl = ddl_for(s)
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl)  # would raise if a keyword broke the statement
    conn.execute('INSERT INTO "order" ("id", "order", "select") VALUES (?, ?, ?)',
                 ("o1", 5, "x"))
    assert conn.execute('SELECT "order" FROM "order"').fetchone()[0] == 5


# ---------------------------------------------------------------------------
# The CHECK constraints actually fire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_value,why", [
    ("abc", "not a number"),
    ("1e5", "scientific notation is not a decimal string"),
    ("1,234", "a thousands separator does not parse"),
    ("", "empty is not zero"),
    ("1.2.3", "two decimal points"),
    ("NaN", "not a number is not a number"),
    ("Infinity", "not finite"),
    ("--3", "two signs"),
    ("3-2", "a sign in the middle"),
])
def test_the_decimal_check_refuses_a_non_decimal_string(bad_value: str, why: str) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        'CREATE TABLE t (v TEXT CHECK ('
        + yaml.safe_load(STACKS_YAML.read_text())["checks"]["decimal"].strip().format(col="v")
        + "))")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO t VALUES (?)", (bad_value,))


@pytest.mark.parametrize("good_value", ["0", "12", "12.50", "-3.25", "0.01", ".5", "12."])
def test_the_decimal_check_accepts_a_decimal_string(good_value: str) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        'CREATE TABLE t (v TEXT CHECK ('
        + yaml.safe_load(STACKS_YAML.read_text())["checks"]["decimal"].strip().format(col="v")
        + "))")
    conn.execute("INSERT INTO t VALUES (?)", (good_value,))


def test_the_date_check_refuses_a_digit_shaped_but_impossible_date() -> None:
    """GLOB matches digit SHAPE only, so month 13 needs an explicit range clause.

    Found while probing: '2026-13-45' satisfies the shape pattern and was accepted
    until the BETWEEN clauses were added.
    """
    checks = yaml.safe_load(STACKS_YAML.read_text())["checks"]
    conn = sqlite3.connect(":memory:")
    conn.executescript('CREATE TABLE t (v TEXT CHECK (' + checks["date"].strip().format(col="v") + "))")
    conn.execute("INSERT INTO t VALUES ('2026-10-15')")
    for bad in ("2026-13-45", "2026-00-10", "2026-10-00", "2026-10-5"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO t VALUES (?)", (bad,))


def test_the_datetime_check_refuses_impossible_times() -> None:
    checks = yaml.safe_load(STACKS_YAML.read_text())["checks"]
    conn = sqlite3.connect(":memory:")
    conn.executescript('CREATE TABLE t (v TEXT CHECK (' + checks["datetime"].strip().format(col="v") + "))")
    conn.execute("INSERT INTO t VALUES ('2026-10-15T09:30:00')")
    for bad in ("2026-13-45T99:99:99", "2026-10-15T24:00:00", "2026-10-15T09:60:00"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO t VALUES (?)", (bad,))


# ---------------------------------------------------------------------------
# The generated store
# ---------------------------------------------------------------------------

def test_the_generated_store_compiles_and_imports(store) -> None:
    module, _ = store
    assert callable(module.connect)
    assert callable(module.insert_invoice)
    assert callable(module.get_invoice)


def test_a_decimal_round_trips_exactly(store) -> None:
    """The point of storing money as TEXT: no rounding on the way back out."""
    module, conn = store
    module.insert_supplier(conn, {"id": "S1", "name": "N", "email": "a@b.c",
                                  "bank_account": "123", "payment_details_changed": False})
    module.insert_invoice(conn, {
        "id": "INV-1", "supplier_id": "S1", "amount": "1234.56",
        "currency": "USD", "due_date": "2026-11-01", "status": "open",
        "duplicate": False})
    row = module.get_invoice(conn, "INV-1")
    assert row["amount"] == "1234.56"
    assert Decimal(row["amount"]) == Decimal("1234.56")


def test_an_unknown_id_returns_none_rather_than_raising(store) -> None:
    """None means "no such record"; raising would make every lookup a try block."""
    module, conn = store
    assert module.get_invoice(conn, "does-not-exist") is None


def test_a_list_filter_returns_matching_rows_only(store) -> None:
    module, conn = store
    module.insert_supplier(conn, {"id": "S1", "name": "N", "email": "a@b.c",
                                  "bank_account": "1", "payment_details_changed": False})
    for inv_id, status in (("I1", "open"), ("I2", "paid"), ("I3", "open")):
        module.insert_invoice(conn, {
            "id": inv_id, "supplier_id": "S1", "amount": "1.00", "currency": "USD",
            "due_date": "2026-11-01", "status": status, "duplicate": False})
    assert len(module.list_invoice(conn)) == 3
    assert len(module.list_invoice(conn, status="open")) == 2


def test_an_unknown_list_filter_is_refused_by_name(store) -> None:
    """A filter on a column that does not exist must not silently match nothing."""
    module, conn = store
    with pytest.raises(module.StoreError, match="statuss"):
        module.list_invoice(conn, statuss="open")


def test_an_update_returns_the_new_row(store) -> None:
    module, conn = store
    module.insert_supplier(conn, {"id": "S1", "name": "N", "email": "a@b.c",
                                  "bank_account": "1", "payment_details_changed": False})
    module.insert_invoice(conn, {
        "id": "I1", "supplier_id": "S1", "amount": "1.00", "currency": "USD",
        "due_date": "2026-11-01", "status": "open", "duplicate": False})
    updated = module.update_invoice(conn, "I1", status="paid")
    assert updated["status"] == "paid"
    assert module.get_invoice(conn, "I1")["status"] == "paid"


def test_an_update_of_an_unknown_row_returns_none(store) -> None:
    module, conn = store
    assert module.update_invoice(conn, "nope", status="paid") is None


def test_a_missing_required_field_names_every_missing_one(store) -> None:
    """All problems at once: one round trip per missing field is slow."""
    module, conn = store
    with pytest.raises(module.StoreError) as excinfo:
        module.insert_invoice(conn, {"id": "I1", "supplier_id": "S1"})
    message = str(excinfo.value)
    for field in ("amount", "currency", "due_date", "status", "duplicate"):
        assert field in message


def test_an_unknown_field_is_refused_by_name(store) -> None:
    module, conn = store
    with pytest.raises(module.StoreError, match="amountt"):
        module.insert_invoice(conn, {
            "id": "I1", "supplier_id": "S1", "amountt": "1.00", "currency": "USD",
            "due_date": "2026-11-01", "status": "open", "duplicate": False})


def test_a_float_is_refused_at_the_python_boundary(store) -> None:
    """The guard that cannot live in the DDL.

    SQLite applies column affinity before a CHECK runs, so binding 12.5 into a TEXT
    column arrives as '12.5' and passes every constraint. Without this refusal a
    float silently becomes a decimal string, and the caller never learns that the
    value was already imprecise.
    """
    module, conn = store
    with pytest.raises(module.StoreError, match="never a float"):
        module.insert_invoice(conn, {
            "id": "I1", "supplier_id": "S1", "amount": 12.5, "currency": "USD",
            "due_date": "2026-11-01", "status": "open", "duplicate": False})


def test_a_bool_is_refused_for_a_decimal_field(store) -> None:
    """bool is an int subclass; True must not become 1 unit of money."""
    module, conn = store
    with pytest.raises(module.StoreError, match="bool"):
        module.insert_invoice(conn, {
            "id": "I1", "supplier_id": "S1", "amount": True, "currency": "USD",
            "due_date": "2026-11-01", "status": "open", "duplicate": False})


def test_a_decimal_object_is_accepted(store) -> None:
    module, conn = store
    module.insert_supplier(conn, {"id": "S1", "name": "N", "email": "a@b.c",
                                  "bank_account": "1", "payment_details_changed": False})
    module.insert_invoice(conn, {
        "id": "I1", "supplier_id": "S1", "amount": Decimal("99.99"), "currency": "USD",
        "due_date": "2026-11-01", "status": "open", "duplicate": False})
    assert module.get_invoice(conn, "I1")["amount"] == "99.99"


def test_a_bad_date_string_is_refused_by_name(store) -> None:
    module, conn = store
    with pytest.raises(module.StoreError, match="due_date"):
        module.insert_invoice(conn, {
            "id": "I1", "supplier_id": "S1", "amount": "1.00", "currency": "USD",
            "due_date": "15/10/2026", "status": "open", "duplicate": False})


def test_an_optional_checked_column_accepts_null(collect_spec: AppSpec) -> None:
    """Regression: a CHECK on an optional column must not reject NULL.

    A CHECK is satisfied when it evaluates to NULL, but `typeof(col) = 'text'` is
    FALSE for NULL, so an unguarded constraint rejects every absent optional value.
    helios-collect's `last_contacted` (an optional date) hit exactly this. Without
    the `IS NULL OR` guard, any generated app with an optional money or date field
    would fail to store a record that legitimately does not have that value yet.
    """
    ddl = ddl_for(collect_spec)
    line = next(l for l in ddl.splitlines() if l.strip().startswith('"last_contacted"'))
    assert "IS NULL OR" in line, line

    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl)
    conn.execute(
        'INSERT INTO "customer" ("id", "name", "email", "credit_limit", "last_contacted")'
        ' VALUES (?, ?, ?, ?, ?)',
        ("C1", "N", "a@b.c", "100.00", None))
    assert conn.execute('SELECT "last_contacted" FROM "customer"').fetchone()[0] is None


def test_a_required_checked_column_still_rejects_a_bad_value(collect_spec: AppSpec) -> None:
    """The NULL allowance must not weaken the constraint on required columns."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl_for(collect_spec))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            'INSERT INTO "customer" ("id", "name", "email", "credit_limit")'
            ' VALUES (?, ?, ?, ?)',
            ("C1", "N", "a@b.c", "not-a-number"))


def test_the_store_writes_null_for_an_omitted_optional_field(collect_spec: AppSpec,
                                                            tmp_path: Path) -> None:
    """End to end: an omitted optional date stores NULL rather than raising."""
    import importlib.util

    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "data" / "schema.sql").write_text(ddl_for(collect_spec), encoding="utf-8")
    (app / "store.py").write_text(store_module_for(collect_spec), encoding="utf-8")
    ms = importlib.util.spec_from_file_location("gen_store_null", app / "store.py")
    module = importlib.util.module_from_spec(ms)
    ms.loader.exec_module(module)

    conn = module.connect(app / "data" / "t.sqlite3")
    module.init_db(conn)
    module.insert_customer(conn, {
        "id": "C1", "name": "N", "email": "a@b.c", "credit_limit": "100.00"})
    row = module.get_customer(conn, "C1")
    assert row["last_contacted"] is None
    assert row["contact_history_days"] is None


def test_a_json_field_accepts_a_dict_and_refuses_a_bad_string(collect_spec: AppSpec,
                                                              tmp_path: Path) -> None:
    import importlib.util

    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "data" / "schema.sql").write_text(ddl_for(collect_spec), encoding="utf-8")
    (app / "store.py").write_text(store_module_for(collect_spec), encoding="utf-8")
    ms = importlib.util.spec_from_file_location("gen_store2", app / "store.py")
    module = importlib.util.module_from_spec(ms)
    ms.loader.exec_module(module)
    conn = module.connect(app / "data" / "c.sqlite3")
    module.init_db(conn)

    module.insert_customer(conn, {
        "id": "C1", "name": "N", "email": "a@b.c", "credit_limit": "100.00",
        "contact_history_days": [1, 2, 3]})
    row = module.get_customer(conn, "C1")
    assert row["contact_history_days"] == "[1, 2, 3]"

    with pytest.raises(module.StoreError, match="not valid JSON"):
        module.insert_customer(conn, {
            "id": "C2", "name": "N", "email": "a@b.c", "credit_limit": "1.00",
            "contact_history_days": "{not json"})


def test_a_boolean_accepts_python_spellings(store) -> None:
    module, conn = store
    module.insert_supplier(conn, {
        "id": "S1", "name": "N", "email": "a@b.c", "bank_account": "1",
        "payment_details_changed": "true"})
    assert module.get_supplier(conn, "S1")["payment_details_changed"] == 1


# ---------------------------------------------------------------------------
# data.integration
# ---------------------------------------------------------------------------

INTEGRATION_SPEC = """
spec_version: "1"
identity: { id: integrated-app, name: Integrated }
existing:
  system: ACME back office
  stack: FastAPI + Postgres
  data: read-only SQL view
stack: { use: helios-python-web-1 }
data:
  integration:
    source: acme_erp
    access: read_only_sql
    entities: [invoice, customer]
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools: []
"""


def test_an_integration_spec_produces_no_ddl() -> None:
    """Data the app does not own is not created by it."""
    s = AppSpec.parse(INTEGRATION_SPEC)
    assert ddl_for(s) == ""


def test_an_integration_store_raises_naming_the_source(tmp_path: Path) -> None:
    """An empty result would read as "there is nothing"; the truth is "not wired"."""
    import importlib.util
    s = AppSpec.parse(INTEGRATION_SPEC)
    (tmp_path / "store.py").write_text(store_module_for(s), encoding="utf-8")
    ms = importlib.util.spec_from_file_location("integ_store", tmp_path / "store.py")
    module = importlib.util.module_from_spec(ms)
    ms.loader.exec_module(module)

    assert module.SOURCE == "acme_erp"
    with pytest.raises(NotImplementedError) as excinfo:
        module.get_invoice(module.connect(), "INV-1")
    assert "acme_erp" in str(excinfo.value)


def test_an_integration_store_creates_no_tables(tmp_path: Path) -> None:
    import importlib.util
    s = AppSpec.parse(INTEGRATION_SPEC)
    (tmp_path / "store.py").write_text(store_module_for(s), encoding="utf-8")
    ms = importlib.util.spec_from_file_location("integ_store2", tmp_path / "store.py")
    module = importlib.util.module_from_spec(ms)
    ms.loader.exec_module(module)
    conn = module.connect()
    module.init_db(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    assert tables == []


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def test_seeding_loads_a_list_of_rows(store, tmp_path: Path) -> None:
    module, conn = store
    s = AppSpec.load(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")
    data = tmp_path / "data"
    data.mkdir()
    (data / "suppliers.json").write_text(
        '[{"id": "S1", "name": "N", "email": "a@b.c", "bank_account": "1",'
        ' "payment_details_changed": false}]', encoding="utf-8")
    counts = seed_from_json(conn, data, entities=[s.entity("supplier")])
    assert counts["supplier"] == 1
    assert module.get_supplier(conn, "S1")["name"] == "N"


def test_a_missing_fixture_is_skipped_not_an_error(store, tmp_path: Path) -> None:
    """A generated app ships with a schema and no data; fixtures arrive later."""
    _, conn = store
    s = AppSpec.load(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")
    counts = seed_from_json(conn, tmp_path / "empty", entities=s.local_entities)
    assert counts == {e.name: 0 for e in s.local_entities}


def test_a_fixture_holding_a_float_for_a_decimal_field_is_refused(store, tmp_path: Path) -> None:
    """Refused at seed time with the field named, not written as a wrong number."""
    _, conn = store
    s = AppSpec.load(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")
    data = tmp_path / "data"
    data.mkdir()
    (data / "invoice.json").write_text(
        '[{"id": "I1", "supplier_id": "S1", "amount": 12.5, "currency": "USD",'
        ' "due_date": "2026-11-01", "status": "open", "duplicate": false}]',
        encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        seed_from_json(conn, data, entities=[s.entity("invoice")])
    assert "float" in str(excinfo.value)


def test_a_malformed_fixture_is_reported_with_its_path(store, tmp_path: Path) -> None:
    _, conn = store
    s = AppSpec.load(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")
    data = tmp_path / "data"
    data.mkdir()
    (data / "supplier.json").write_text("[{not json", encoding="utf-8")
    with pytest.raises(SchemaError, match="not valid JSON"):
        seed_from_json(conn, data, entities=[s.entity("supplier")])


def test_a_fixture_mapping_with_a_named_list_is_accepted(store, tmp_path: Path) -> None:
    _, conn = store
    s = AppSpec.load(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")
    data = tmp_path / "data"
    data.mkdir()
    (data / "supplier.json").write_text(
        '{"supplier": [{"id": "S1", "name": "N", "email": "a@b.c",'
        ' "bank_account": "1", "payment_details_changed": false}]}', encoding="utf-8")
    counts = seed_from_json(conn, data, entities=[s.entity("supplier")])
    assert counts["supplier"] == 1


def test_a_non_object_entry_is_reported_with_its_index(store, tmp_path: Path) -> None:
    _, conn = store
    s = AppSpec.load(REPO / "entry-point" / "examples" / "invoice-payer" / "helios-app.yaml")
    data = tmp_path / "data"
    data.mkdir()
    (data / "supplier.json").write_text('["just a string"]', encoding="utf-8")
    with pytest.raises(SchemaError, match=r"entries \[0\]"):
        seed_from_json(conn, data, entities=[s.entity("supplier")])


# ---------------------------------------------------------------------------
# Real apps end to end
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app", REAL_APPS, ids=lambda p: p.name)
def test_both_real_apps_generate_working_schemas(app: Path) -> None:
    """The specs that ship in the repo must produce runnable databases."""
    s = AppSpec.load(app / "helios-app.yaml")
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl_for(s))
    for entity in s.local_entities:
        assert conn.execute(
            f'SELECT count(*) FROM "{entity.name}"').fetchone()[0] == 0


def test_the_generated_store_for_each_real_app_compiles() -> None:
    for app in REAL_APPS:
        s = AppSpec.load(app / "helios-app.yaml")
        src = store_module_for(s)
        compile(src, f"{app.name}/store.py", "exec")


def test_a_float_field_is_refused_upstream_by_the_spec_model() -> None:
    """Task 3's requirement: `float` never reaches the schema tool at all."""
    text = """
spec_version: "1"
identity: { id: floaty, name: Floaty }
stack: { use: helios-python-web-1 }
data:
  local:
    entities:
      - name: thing
        fields:
          - { name: id, type: string, required: true }
          - { name: amount, type: float, required: true }
agents:
  action_manifest: agents/a.json
  audit_manifest: agents/a.yaml
tools: []
"""
    with pytest.raises(AppSpecError) as excinfo:
        AppSpec.parse(text)
    assert "float" in str(excinfo.value)

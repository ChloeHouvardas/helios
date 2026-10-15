from pathlib import Path

import pytest

from fin_tools import build_world
from fin_tools.spreadsheet_tools import (aggregate, check_schema, filter_rows,
                                         load_csv, save_csv)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_send_template_lands_in_outbox():
    bundle = build_world()
    msg = bundle.email.send_template(to="ap@acme.example",
                                     template="payment_reminder",
                                     invoice_id="INV-1", due_date="2026-10-10",
                                     contact="AP", amount="10.00")
    assert len(bundle.world.outbox) == 1
    assert msg["subject"].startswith("Invoice INV-1")
    assert bundle.world.outbox[0]["transport"] == "local-outbox"


def test_invalid_recipient_rejected():
    bundle = build_world()
    with pytest.raises(ValueError):
        bundle.email.send(to="nope", subject="s", body="b")


def test_unknown_template():
    bundle = build_world()
    with pytest.raises(KeyError):
        bundle.email.render("nope", a=1)


def test_render_fills_all_fields():
    bundle = build_world()
    out = bundle.email.render("payment_confirmation", payment_id="p1",
                              contact="A", amount="1", reference="r",
                              sender_name="Helios")
    assert "$" not in out


def test_render_tolerates_missing_fields():
    bundle = build_world()
    out = bundle.email.render("payment_confirmation", payment_id="p1")
    assert "$contact" in out  # safe_substitute: no crash, placeholder kept


def test_load_fixture_csv_coerces_cents():
    rows = load_csv(FIXTURES / "invoices.csv")
    assert rows[0]["amount_cents"] == 250000
    assert isinstance(rows[0]["amount_cents"], int)


def test_load_fixture_csv_first_column_is_not_bom_polluted():
    """A UTF-8 BOM must not become part of the first column's name.

    Read with plain `utf-8`, the BOM is not stripped by Python, so the header
    `invoice_id` arrives as `\\ufeffinvoice_id` — a key no caller can guess and
    one that breaks on every comparison, lookup and schema check. Only the
    FIRST column is affected, which is why the other tests here (they read
    `amount_cents` and `customer`) never caught it.
    """
    rows = load_csv(FIXTURES / "invoices.csv")
    keys = set(rows[0])
    assert "invoice_id" in keys, sorted(keys)
    assert not any(k.startswith("\ufeff") for k in keys), sorted(keys)


def test_load_csv_reads_a_bom_file_with_plain_column_names(tmp_path):
    """The fixture is one instance; the loader must handle any BOM file.

    Excel writes UTF-8 CSVs with a BOM by default, so a file exported from a
    spreadsheet and handed to these tools is the normal case, not an edge case.
    """
    path = tmp_path / "bom.csv"
    path.write_bytes("\ufeffinvoice_id,customer,amount_cents\n".encode("utf-8")
                     + b"INV-1,acme,100\n")
    rows = load_csv(path)
    assert rows == [{"invoice_id": "INV-1", "customer": "acme",
                     "amount_cents": 100}]


def test_the_shipped_fixture_has_no_bom():
    """The fixture itself is cleaned up, so nothing depends on the tolerance."""
    raw = (FIXTURES / "invoices.csv").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "invoices.csv still carries a UTF-8 BOM"


def test_filter_and_aggregate(tmp_path):
    rows = load_csv(FIXTURES / "invoices.csv")
    acme = filter_rows(rows, customer="acme")
    assert len(acme) == 2
    totals = aggregate(rows, group_by="customer", sum_column="amount_cents")
    assert totals["acme"] == 370000
    path = tmp_path / "out.csv"
    save_csv(path, acme)
    assert len(load_csv(path)) == 2


def test_check_schema_flags_missing():
    problems = check_schema([{"a": 1}, {"a": 1, "b": 2}], required=["a", "b"])
    assert problems == [{"row": 0, "column": "b", "problem": "missing"}]

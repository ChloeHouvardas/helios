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

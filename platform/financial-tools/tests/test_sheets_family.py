"""The `sheets.*` family: statement reconciliation over the imported package.

What these tests pin down:

- **Offline and model-free.** No tool reaches a model, and no `QWEN_*` credential
  is read. The facade injects `NullQwen`, so behaviour is identical with and
  without credentials.
- **Integer fen, no conversion.** Unlike the forecast family there is no float
  boundary here: the package already stores money as integer minor units, so
  amounts pass through unchanged (1000.00 -> 100000).
- **Tenant isolation and no default.** Two bundles do not see each other's
  records, and a bundle with no tenant refuses rather than using a shared bucket.
- **The silent-empty-sheet trap is closed.** A misspelled query key used to match
  nothing and render zero rows, which reads as "this tenant has no obligations".
  It now raises with the field named.
- **Protected columns stay protected.** Money/date/payee writes become proposals
  for a human, and only approval applies them.

Everything here runs in memory against a `NullQwen` service.
"""
from __future__ import annotations

import pytest

from fin_tools import build_world
from fin_tools.sheets import RECORD_TYPES, Sheets

TENANT = "acme"

AR_CSV = (
    "客户名称,发票号,金额,到期日,方向\n"
    "Acme Ltd,INV-1001,1000.00,2026-11-30,应收\n"
    "Beta Co,INV-2001,500.50,2026-12-15,应收\n"
)


@pytest.fixture
def bundle(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    return build_world(tenant=TENANT)


def _import_obligations(bundle, csv_text: str = AR_CSV) -> str:
    """Upload + import, returning the file id. The common happy path."""
    file_id = bundle.registry.call("sheets.upload", filename="ar.csv",
                                   content=csv_text)["file_id"]
    bundle.registry.call("sheets.import_sheet", file_id=file_id,
                         record_type="Obligation")
    return file_id


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def test_sheets_tools_are_registered_with_honest_flags(bundle):
    registry = bundle.registry
    read_only = {"sheets.records", "sheets.list_proposals"}
    for name in read_only:
        assert registry.get(name).side_effects is False, name
    # Everything that writes records, settles obligations or applies an edit is
    # gated. Marking one of these read-only would be an ungated side effect.
    for name in ("sheets.upload", "sheets.import_sheet", "sheets.reconcile",
                 "sheets.resolve_match", "sheets.render_sheet",
                 "sheets.write_cell", "sheets.resolve_proposal"):
        assert registry.get(name).side_effects is True, name


def test_all_sheets_tools_are_tenant_scoped(bundle):
    for spec in bundle.registry.list():
        if spec.name.startswith("sheets."):
            assert spec.tenant_scoped is True, spec.name


def test_sheets_namespace_does_not_collide_with_spreadsheet(bundle):
    """The standalone CSV helpers keep their own namespace."""
    names = {t.name for t in bundle.registry.list()}
    assert {"spreadsheet.load", "spreadsheet.save", "spreadsheet.aggregate"} <= names
    assert not (names & {f"sheets.{n}" for n in
                         ("load", "save", "aggregate")})


def test_facade_is_bound_to_the_bundle(bundle):
    assert isinstance(bundle.sheets, Sheets)
    assert bundle.sheets.tenant == TENANT


def test_two_bundles_do_not_share_reconciliation_state():
    """The per-bundle binding, not a module singleton (the Task 1 trap)."""
    acme = build_world(tenant="acme")
    beta = build_world(tenant="beta")
    _import_obligations(acme)
    assert acme.registry.call("sheets.records", record_type="Obligation")["count"] == 2
    assert beta.registry.call("sheets.records", record_type="Obligation")["count"] == 0


# --------------------------------------------------------------------------
# offline and model-free
# --------------------------------------------------------------------------

def test_no_model_client_is_configured(bundle):
    """NullQwen means no tool here can reach a model or read QWEN_API_KEY."""
    service = bundle.sheets.service
    assert service.qwen.configured is False


def test_tools_work_without_any_qwen_credentials(bundle, monkeypatch):
    for var in ("QWEN_BASE_URL", "QWEN_API_KEY", "QWEN_MODEL"):
        monkeypatch.delenv(var, raising=False)
    _import_obligations(bundle)
    result = bundle.registry.call("sheets.reconcile")
    assert result["applied"] == []          # nothing to match yet, but no error


def test_ambiguous_mapping_is_refused_rather_than_guessed(bundle):
    """With no model fallback, an unmappable header set must error, not invent."""
    file_id = bundle.registry.call(
        "sheets.upload", filename="odd.csv",
        content="甲,乙,丙\n1,2,3\n")["file_id"]
    with pytest.raises(ValueError) as exc:
        bundle.registry.call("sheets.import_sheet", file_id=file_id,
                             record_type="Obligation")
    assert "mapping_ambiguous" in str(exc.value)


# --------------------------------------------------------------------------
# upload + import
# --------------------------------------------------------------------------

def test_upload_reports_size_and_hash(bundle):
    out = bundle.registry.call("sheets.upload", filename="ar.csv", content=AR_CSV)
    assert out["filename"] == "ar.csv"
    assert out["size"] == len(AR_CSV.encode("utf-8"))
    assert out["sha"] and out["file_id"]


def test_upload_rejects_an_unknown_encoding(bundle):
    with pytest.raises(ValueError, match="encoding"):
        bundle.registry.call("sheets.upload", filename="a.csv", content="x",
                             encoding="not-a-codec")


def test_import_parses_amounts_as_integer_fen(bundle):
    _import_obligations(bundle)
    records = bundle.registry.call("sheets.records", record_type="Obligation")
    assert records["count"] == 2
    amounts = sorted(r["amount"] for r in records["items"])
    assert amounts == [50_050, 100_000]
    assert all(isinstance(a, int) for a in amounts)


def test_import_dry_run_writes_nothing(bundle):
    file_id = bundle.registry.call("sheets.upload", filename="ar.csv",
                                   content=AR_CSV)["file_id"]
    dry = bundle.registry.call("sheets.import_sheet", file_id=file_id,
                               record_type="Obligation", dry_run=True)
    assert dry["dry_run"] is True
    assert dry["imported"] == 0
    assert dry["mapping"]["金额"] == "amount"
    assert bundle.registry.call("sheets.records",
                                record_type="Obligation")["count"] == 0


def test_import_rejects_an_unknown_record_type(bundle):
    file_id = bundle.registry.call("sheets.upload", filename="a.csv",
                                   content=AR_CSV)["file_id"]
    with pytest.raises(ValueError, match="record_type"):
        bundle.registry.call("sheets.import_sheet", file_id=file_id,
                             record_type="Invoice")


def test_records_rejects_an_unknown_record_type(bundle):
    with pytest.raises(ValueError, match="record_type"):
        bundle.registry.call("sheets.records", record_type="Nope")


def test_import_accepts_an_explicit_column_map(bundle):
    """The escape hatch when deterministic inference cannot decide."""
    file_id = bundle.registry.call(
        "sheets.upload", filename="custom.csv",
        content="who,ref,how_much\nAcme,INV-9,250.00\n")["file_id"]
    out = bundle.registry.call(
        "sheets.import_sheet", file_id=file_id, record_type="Obligation",
        column_map={"who": "counterparty", "ref": "invoice_number",
                    "how_much": "amount"})
    assert out["imported"] == 1


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------

def _seed_matchable(bundle, tenant=TENANT):
    """One obligation and the transaction that settles it, by invoice reference."""
    from datetime import timedelta

    from spreadsheets.models import Obligation, Transaction
    from spreadsheets.store import now

    service = bundle.sheets.service
    service.kg.add_obligation(Obligation(
        id="ob1", business_id=tenant, source="test", created_at=now(),
        amount=100_00, direction="receivable", counterparty="Acme",
        due_at=now() + timedelta(days=5), invoice_number="INV-1",
        status="open"), emit=False)
    service.kg.add_transaction(Transaction(
        id="tx1", business_id=tenant, source="test", created_at=now(),
        amount=100_00, direction="in", counterparty="Acme", channel="bank",
        occurred_at=now(), channel_ref="ref1",
        note="Payment for INV-1"), emit=False)
    return service


def test_reconcile_applies_an_exact_reference_match(bundle):
    service = _seed_matchable(bundle)
    out = bundle.registry.call("sheets.reconcile")
    assert len(out["applied"]) == 1
    assert out["applied"][0]["rule"] == "exact_reference"
    assert service.kg.by_id["ob1"].status == "paid"


def test_reconcile_dry_run_does_not_settle(bundle):
    service = _seed_matchable(bundle)
    out = bundle.registry.call("sheets.reconcile", dry_run=True)
    assert out["dry_run"] is True
    assert service.kg.by_id["ob1"].status == "open"


def test_reconcile_with_a_date_range_still_matches(bundle):
    """Regression: a scoped reconcile used to silently drop every transaction.

    `_unmatched` injected a `_range` pseudo-key into the record query. The
    matcher compares an unknown key against None, so nothing ever matched and
    reconcile reported zero matches instead of erroring -- a silently wrong
    financial answer. The date range is applied by `_in_scope`.
    """
    from datetime import timedelta

    from spreadsheets.store import now

    _seed_matchable(bundle)
    out = bundle.registry.call("sheets.reconcile", scope={
        "from": (now() - timedelta(days=30)).isoformat(),
        "to": (now() + timedelta(days=30)).isoformat(),
    })
    assert len(out["applied"]) == 1


def test_reconcile_scope_filter_that_matches_nothing_applies_nothing(bundle):
    """The filter still filters; the fix did not make scope a no-op."""
    _seed_matchable(bundle)
    out = bundle.registry.call("sheets.reconcile",
                               scope={"counterparty": "Nonexistent Ltd"})
    assert out["applied"] == []


def test_resolve_match_rejects_an_unknown_decision(bundle):
    with pytest.raises(ValueError, match="decision"):
        bundle.registry.call("sheets.resolve_match", match_id="m1",
                             decision="maybe")


# --------------------------------------------------------------------------
# render + the protected-column control
# --------------------------------------------------------------------------

def test_render_sheet_defaults_to_a_file_target(bundle):
    _import_obligations(bundle)
    out = bundle.registry.call(
        "sheets.render_sheet", query={"type": "Obligation"},
        columns=["invoice_number", "amount", "status"])
    assert out["target"] == "csv"           # not the package's live "feishu"
    assert out["view_id"] and out["file_id"]
    # `rows` is a COUNT, not the row data -- asserted so a caller does not
    # mistake it for the sheet contents.
    assert out["rows"] == 2
    assert isinstance(out["rows"], int)


def test_record_type_is_accepted_as_an_alias_for_type(bundle):
    _import_obligations(bundle)
    via_alias = bundle.registry.call(
        "sheets.render_sheet", query={"record_type": "Obligation"},
        columns=["invoice_number"])
    via_type = bundle.registry.call(
        "sheets.render_sheet", query={"type": "Obligation"},
        columns=["invoice_number"])
    assert via_alias["rows"] == via_type["rows"] == 2


def test_a_misspelled_query_key_raises_instead_of_rendering_empty(bundle):
    """The trap this family closes: zero rows looked like 'no obligations'."""
    _import_obligations(bundle)
    with pytest.raises(ValueError, match="unknown key"):
        bundle.registry.call("sheets.render_sheet",
                             query={"recordtype": "Obligation"},
                             columns=["invoice_number"])


def test_an_unknown_record_type_in_a_query_raises(bundle):
    with pytest.raises(ValueError, match="query type"):
        bundle.registry.call("sheets.render_sheet", query={"type": "Invoice"},
                             columns=["amount"])


def test_conflicting_type_keys_raise(bundle):
    with pytest.raises(ValueError, match="only one"):
        bundle.registry.call(
            "sheets.render_sheet",
            query={"type": "Obligation", "record_type": "Transaction"},
            columns=["amount"])


def test_a_non_object_query_raises(bundle):
    with pytest.raises(ValueError, match="query must be an object"):
        bundle.registry.call("sheets.render_sheet", query="Obligation",
                             columns=["amount"])


def test_protected_column_write_becomes_a_proposal_and_only_approval_applies(bundle):
    """The human checkpoint: money is never edited directly."""
    _import_obligations(bundle)
    view = bundle.registry.call(
        "sheets.render_sheet", query={"type": "Obligation"},
        columns=["amount"], editable_columns=["amount"],
        protected_columns=["amount"])
    record_id = bundle.registry.call(
        "sheets.records", record_type="Obligation")["items"][0]["id"]
    original = bundle.registry.call(
        "sheets.records", record_type="Obligation")["items"][0]["amount"]

    # Through the registered tool: a protected field is queued, not applied.
    written = bundle.registry.call(
        "sheets.write_cell", view_id=view["view_id"], record_id=record_id,
        field="amount", new_value=999_00, actor="agent", actor_kind="agent",
        reason="demo")
    assert written["applied"] is False
    assert written["status"] == "pending"

    # Not applied yet.
    assert bundle.sheets.service.kg.by_id[record_id].amount == original

    proposals = bundle.registry.call("sheets.list_proposals")
    assert proposals["count"] == 1
    proposal = proposals["items"][0]
    assert proposal["proposal_id"]
    assert proposal["needs_human"] is True

    resolved = bundle.registry.call(
        "sheets.resolve_proposal", proposal_id=proposal["proposal_id"],
        decision="approved", resolved_by="owner", resolved_by_kind="user")
    assert resolved["status"] == "approved"
    # Approval must actually APPLY the value. The imported package leaves its
    # proposal_sink unwired, so without this facade's sink an approved proposal
    # would report success while the record kept its old amount.
    assert bundle.sheets.service.kg.by_id[record_id].amount == 999_00


def test_an_agent_cannot_approve_its_own_protected_edit(bundle):
    """The control the proposal queue exists to enforce.

    `resolved_by_kind` defaults to "agent", so an agent that does not explicitly
    claim human authority is refused by the engine -- and the amount is
    untouched.
    """
    _import_obligations(bundle)
    view = bundle.registry.call(
        "sheets.render_sheet", query={"type": "Obligation"}, columns=["amount"],
        editable_columns=["amount"], protected_columns=["amount"])
    record_id = bundle.registry.call(
        "sheets.records", record_type="Obligation")["items"][0]["id"]
    original = bundle.sheets.service.kg.by_id[record_id].amount

    bundle.registry.call("sheets.write_cell", view_id=view["view_id"],
                         record_id=record_id, field="amount", new_value=1,
                         actor="agent", actor_kind="agent")
    proposal_id = bundle.registry.call(
        "sheets.list_proposals")["items"][0]["proposal_id"]

    with pytest.raises(ValueError, match="permission_denied"):
        bundle.registry.call("sheets.resolve_proposal",
                             proposal_id=proposal_id, decision="approve")

    assert bundle.sheets.service.kg.by_id[record_id].amount == original


def test_rejecting_a_proposal_leaves_the_record_unchanged(bundle):
    _import_obligations(bundle)
    view = bundle.registry.call(
        "sheets.render_sheet", query={"type": "Obligation"}, columns=["amount"],
        editable_columns=["amount"], protected_columns=["amount"])
    record_id = bundle.registry.call(
        "sheets.records", record_type="Obligation")["items"][0]["id"]
    original = bundle.sheets.service.kg.by_id[record_id].amount

    bundle.registry.call("sheets.write_cell", view_id=view["view_id"],
                         record_id=record_id, field="amount", new_value=1,
                         actor="agent", actor_kind="agent")
    proposal_id = bundle.registry.call(
        "sheets.list_proposals")["items"][0]["proposal_id"]

    out = bundle.registry.call("sheets.resolve_proposal",
                               proposal_id=proposal_id, decision="rejected")
    assert out["status"] == "rejected"
    assert bundle.sheets.service.kg.by_id[record_id].amount == original


def test_a_view_is_read_only_unless_a_column_is_declared_editable(bundle):
    """The safe default: an undeclared column refuses a direct write."""
    _import_obligations(bundle)
    view = bundle.registry.call("sheets.render_sheet",
                                query={"type": "Obligation"},
                                columns=["amount"])
    record_id = bundle.registry.call(
        "sheets.records", record_type="Obligation")["items"][0]["id"]
    # Through the tool, the engine's protected_column error is translated to a
    # ValueError carrying the code, so a caller gets an actionable rejection.
    with pytest.raises(ValueError, match="protected_column"):
        bundle.registry.call("sheets.write_cell", view_id=view["view_id"],
                             record_id=record_id, field="amount",
                             new_value=999_00, actor="agent", actor_kind="agent")


def test_write_cell_applies_an_unprotected_editable_column(bundle):
    """A column declared editable and NOT protected writes straight through."""
    _import_obligations(bundle)
    view = bundle.registry.call(
        "sheets.render_sheet", query={"type": "Obligation"},
        columns=["invoice_number"], editable_columns=["invoice_number"])
    record_id = bundle.registry.call(
        "sheets.records", record_type="Obligation")["items"][0]["id"]
    out = bundle.registry.call("sheets.write_cell", view_id=view["view_id"],
                               record_id=record_id, field="invoice_number",
                               new_value="INV-CORRECTED", actor="owner",
                               actor_kind="user")
    assert out["applied"] is True
    assert bundle.sheets.service.kg.by_id[record_id].invoice_number == \
        "INV-CORRECTED"


def test_write_cell_is_gated(bundle):
    assert bundle.registry.get("sheets.write_cell").side_effects is True


def test_resolve_proposal_rejects_an_unknown_decision(bundle):
    with pytest.raises(ValueError, match="decision"):
        bundle.registry.call("sheets.resolve_proposal", proposal_id="p1",
                             decision="sure")


def test_list_proposals_is_empty_for_a_fresh_tenant(bundle):
    out = bundle.registry.call("sheets.list_proposals")
    assert out["count"] == 0
    assert out["items"] == []


# --------------------------------------------------------------------------
# tenancy
# --------------------------------------------------------------------------

def test_no_tenant_is_refused_not_defaulted(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    bundle = build_world()
    with pytest.raises(ValueError, match="tenant"):
        bundle.registry.call("sheets.records", record_type="Obligation")


def test_upload_without_a_tenant_is_refused(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    bundle = build_world()
    with pytest.raises(ValueError, match="tenant"):
        bundle.registry.call("sheets.upload", filename="a.csv", content="x")


def test_a_second_tenant_cannot_read_the_first_tenants_files(bundle):
    file_id = bundle.registry.call("sheets.upload", filename="ar.csv",
                                   content=AR_CSV)["file_id"]
    other = build_world(tenant="beta")
    # Calling the teammate service directly raises its own HeliosError; going
    # through a sheets.* tool would translate it to ValueError. Either way the
    # cross-tenant read is refused, and the file id is reported as unknown
    # rather than as belonging to somebody else.
    from spreadsheets.errors import HeliosError

    with pytest.raises(HeliosError, match="unknown file_id"):
        other.sheets.service.import_sheet(file_id, record_type="Obligation")


def test_cross_tenant_read_is_also_refused_through_the_tool(bundle):
    """Same refusal, but via the tool, so the error is the translated shape."""
    file_id = bundle.registry.call("sheets.upload", filename="ar.csv",
                                   content=AR_CSV)["file_id"]
    other = build_world(tenant="beta")
    with pytest.raises(ValueError, match="validation_error"):
        other.registry.call("sheets.import_sheet", file_id=file_id,
                            record_type="Obligation")


def test_record_types_constant_matches_the_engine():
    from spreadsheets.tools import RECORD_TYPES as ENGINE_TYPES

    assert set(RECORD_TYPES) == set(ENGINE_TYPES)

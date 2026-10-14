import pytest

from email_tool.idempotency import DuplicateKeyError, IdempotencyStore, derive_key

RESPONSE = {
    "ok": True,
    "message_id": "smtp-42",
    "idempotency_key": "key-1",
    "template_id": "notice_overdue",
    "tenant_id": "acme",
    "invoice_ref": "INV-2031",
    "rendered_subject": "Overdue: Invoice INV-2031 — 12 days past due",
    "rendered_body_preview": "Dear Dana Cole, our records show...",
    "sent_at": "2026-10-14T10:30:00+00:00",
    "thread_id": None,
}


def test_put_then_get_returns_the_stored_response(tmp_path):
    store = IdempotencyStore(tmp_path / "sends.jsonl")
    store.put("key-1", RESPONSE)
    assert store.get("key-1") == RESPONSE


def test_get_for_unknown_key_returns_none(tmp_path):
    store = IdempotencyStore(tmp_path / "sends.jsonl")
    assert store.get("missing") is None


def test_put_rejects_a_duplicate_key(tmp_path):
    store = IdempotencyStore(tmp_path / "sends.jsonl")
    store.put("key-1", RESPONSE)
    with pytest.raises(DuplicateKeyError):
        store.put("key-1", RESPONSE)


def test_records_survive_reopening_the_store(tmp_path):
    path = tmp_path / "sends.jsonl"
    IdempotencyStore(path).put("key-1", RESPONSE)
    assert IdempotencyStore(path).get("key-1") == RESPONSE


def test_partially_written_trailing_line_is_ignored(tmp_path):
    path = tmp_path / "sends.jsonl"
    store = IdempotencyStore(path)
    store.put("key-1", RESPONSE)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"key": "key-2", "resp')
    assert store.get("key-1") == RESPONSE
    assert store.get("key-2") is None


def test_store_creates_parent_directories(tmp_path):
    store = IdempotencyStore(tmp_path / "tenants" / "acme" / "sends.jsonl")
    store.put("key-1", RESPONSE)
    assert store.get("key-1") == RESPONSE


def test_derive_key_is_deterministic_and_distinct():
    assert derive_key("INV-1", "notice_overdue") == derive_key("INV-1", "notice_overdue")
    assert derive_key("INV-1", "notice_overdue") != derive_key("INV-1", "notice_overdue", step=2)
    assert derive_key("INV-1", "notice_overdue") != derive_key("INV-2", "notice_overdue")
    assert derive_key("INV-1", "notice_overdue") != derive_key("INV-1", "reminder_pre_due")


def test_derive_key_is_a_sha256_hex_digest():
    key = derive_key("INV-1", "notice_overdue")
    assert len(key) == 64
    assert all(character in "0123456789abcdef" for character in key)

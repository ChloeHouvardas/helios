from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator

from email_tool.idempotency import IdempotencyStore, derive_key
from email_tool.templates import TemplateRegistry
from email_tool.tool import TOOL, EmailSender, InvalidRequestError
from email_tool.transport import SendError, SentMessage

FIXED_NOW = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)
TEMPLATES = TemplateRegistry()

NOTICE_OVERDUE_VARIABLES = {
    "invoice_ref": "INV-2031",
    "sme_name": "Acme Studio",
    "customer_name": "Dana Cole",
    "currency": "EUR",
    "amount": "1,250.00",
    "days_overdue": 12,
    "due_date": "2026-11-01",
}


def make_request(**overrides):
    request = {
        "template_id": "notice_overdue",
        "to": "dana@example.com",
        "variables": dict(NOTICE_OVERDUE_VARIABLES),
        "invoice_ref": "INV-2031",
        "tenant_id": "acme",
    }
    request.update(overrides)
    return request


class FakeTransport:
    """Records deliveries and hands back sequential message ids."""

    def __init__(self):
        self.deliveries = []

    def send(self, *, to, subject, body, thread_id=None):
        self.deliveries.append(
            {"to": to, "subject": subject, "body": body, "thread_id": thread_id}
        )
        return SentMessage(
            message_id=f"smtp-{len(self.deliveries)}", thread_id=thread_id
        )


class FailingTransport:
    def send(self, **kwargs):
        raise SendError("SMTP send to smtp.acme.example.com:587 failed: boom")


def make_sender(tmp_path, *, transport=None):
    if transport is None:
        transport = FakeTransport()
    sender = EmailSender(
        transport_factory=lambda tenant_id: transport,
        store_factory=lambda tenant_id: IdempotencyStore(
            tmp_path / "tenants" / tenant_id / "sends.jsonl"
        ),
        clock=lambda: FIXED_NOW,
    )
    return sender, transport


def store_path(tmp_path, tenant_id="acme"):
    return tmp_path / "tenants" / tenant_id / "sends.jsonl"


def test_tool_contract_declares_name_description_and_side_effect():
    assert TOOL["name"] == "send_email"
    assert isinstance(TOOL["description"], str) and TOOL["description"]
    assert TOOL["side_effect"] is True


def test_tool_contract_schemas_are_valid_draft_2020_12_schemas():
    Draft202012Validator.check_schema(TOOL["input_schema"])
    Draft202012Validator.check_schema(TOOL["output_schema"])


def test_send_returns_the_documented_success_response(tmp_path):
    sender, _ = make_sender(tmp_path)
    response = sender.send(make_request())
    rendered = TEMPLATES.render("notice_overdue", NOTICE_OVERDUE_VARIABLES)
    assert response == {
        "ok": True,
        "message_id": "smtp-1",
        "idempotency_key": derive_key("INV-2031", "notice_overdue"),
        "template_id": "notice_overdue",
        "tenant_id": "acme",
        "invoice_ref": "INV-2031",
        "rendered_subject": rendered.subject,
        "rendered_body_preview": rendered.body[:200],
        "sent_at": "2026-10-14T10:30:00+00:00",
        "thread_id": None,
    }


def test_the_transport_receives_the_rendered_email(tmp_path):
    sender, transport = make_sender(tmp_path)
    sender.send(make_request())
    rendered = TEMPLATES.render("notice_overdue", NOTICE_OVERDUE_VARIABLES)
    (delivery,) = transport.deliveries
    assert delivery["to"] == "dana@example.com"
    assert delivery["subject"] == rendered.subject
    assert delivery["body"] == rendered.body
    assert delivery["thread_id"] is None


def test_the_body_preview_is_truncated_to_200_characters(tmp_path):
    rendered = TEMPLATES.render("notice_overdue", NOTICE_OVERDUE_VARIABLES)
    assert len(rendered.body) > 200
    sender, _ = make_sender(tmp_path)
    response = sender.send(make_request())
    assert response["rendered_body_preview"] == rendered.body[:200]


def test_thread_id_is_passed_to_the_transport_and_response(tmp_path):
    sender, transport = make_sender(tmp_path)
    response = sender.send(
        make_request(thread_id="<previous-42@acme.example.com>")
    )
    assert transport.deliveries[0]["thread_id"] == "<previous-42@acme.example.com>"
    assert response["thread_id"] == "<previous-42@acme.example.com>"


def test_a_repeated_request_replays_the_original_response(tmp_path):
    sender, transport = make_sender(tmp_path)
    first = sender.send(make_request())
    second = sender.send(make_request())
    assert second == first
    assert len(transport.deliveries) == 1


def test_an_explicit_idempotency_key_overrides_derivation(tmp_path):
    sender, transport = make_sender(tmp_path)
    first = sender.send(make_request(idempotency_key="key-1"))
    second = sender.send(
        make_request(idempotency_key="key-1", to="somewhere-else@example.com")
    )
    assert second == first
    assert second["idempotency_key"] == "key-1"
    assert len(transport.deliveries) == 1
    assert transport.deliveries[0]["to"] == "dana@example.com"


def test_distinct_follow_up_steps_send_separately(tmp_path):
    sender, transport = make_sender(tmp_path)
    first = sender.send(make_request(step=1))
    second = sender.send(make_request(step=2))
    assert first["idempotency_key"] == derive_key("INV-2031", "notice_overdue", 1)
    assert second["idempotency_key"] == derive_key("INV-2031", "notice_overdue", 2)
    assert first["idempotency_key"] != second["idempotency_key"]
    assert len(transport.deliveries) == 2


def test_a_replay_does_not_need_the_transport(tmp_path):
    sender, _ = make_sender(tmp_path)
    first = sender.send(make_request())
    replaying_sender, _ = make_sender(tmp_path, transport=FailingTransport())
    assert replaying_sender.send(make_request()) == first


def test_each_tenant_has_its_own_idempotency_store(tmp_path):
    sender, transport = make_sender(tmp_path)
    for_acme = sender.send(make_request())
    for_beta = sender.send(make_request(tenant_id="beta", to="pay@example.com"))
    assert for_acme["tenant_id"] == "acme"
    assert for_beta["tenant_id"] == "beta"
    assert len(transport.deliveries) == 2
    assert store_path(tmp_path, "acme").exists()
    assert store_path(tmp_path, "beta").exists()


def test_a_failed_send_is_not_recorded_and_can_be_retried(tmp_path):
    sender, _ = make_sender(tmp_path, transport=FailingTransport())
    request = make_request()
    with pytest.raises(SendError):
        sender.send(request)
    assert (
        IdempotencyStore(store_path(tmp_path)).get(
            derive_key("INV-2031", "notice_overdue")
        )
        is None
    )

    retrying_sender, transport = make_sender(tmp_path)
    response = retrying_sender.send(request)
    assert response["ok"] is True
    assert len(transport.deliveries) == 1


def test_the_response_is_recorded_in_the_tenant_store(tmp_path):
    sender, _ = make_sender(tmp_path)
    response = sender.send(make_request())
    store = IdempotencyStore(store_path(tmp_path))
    assert store.get(response["idempotency_key"]) == response


INVALID_REQUESTS = [
    pytest.param(
        {key: value for key, value in make_request().items() if key != "to"},
        id="missing-required-field-to",
    ),
    pytest.param(make_request(template_id="welcome_aboard"), id="unknown-template"),
    pytest.param(make_request(tenant_id="Acme"), id="invalid-tenant-id"),
    pytest.param(make_request(unexpected="nope"), id="unknown-property"),
    pytest.param(make_request(to="not-an-email"), id="malformed-recipient"),
    pytest.param(make_request(step=0), id="step-below-minimum"),
    pytest.param(make_request(idempotency_key=""), id="empty-idempotency-key"),
]


@pytest.mark.parametrize("invalid_request", INVALID_REQUESTS)
def test_invalid_requests_are_rejected_before_sending(tmp_path, invalid_request):
    sender, transport = make_sender(tmp_path)
    with pytest.raises(InvalidRequestError):
        sender.send(invalid_request)
    assert transport.deliveries == []
    assert not store_path(tmp_path).exists()


def test_the_store_location_can_be_configured_via_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAILTOOL_DATA_DIR", str(tmp_path / "mail-data"))
    transport = FakeTransport()
    sender = EmailSender(transport_factory=lambda tenant_id: transport)
    sender.send(make_request())
    assert (tmp_path / "mail-data" / "tenants" / "acme" / "sends.jsonl").exists()

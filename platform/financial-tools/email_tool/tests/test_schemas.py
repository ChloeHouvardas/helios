import json
from pathlib import Path

from email_tool.templates import TemplateRegistry

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

TEMPLATE_IDS = [
    "escalate_to_owner",
    "invoice_delivery",
    "negotiation_offer",
    "notice_overdue",
    "reminder_pre_due",
]


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


def test_request_schema_declares_the_documented_required_fields():
    schema = load_schema("request.json")
    assert schema["type"] == "object"
    assert schema["required"] == ["template_id", "to", "variables", "invoice_ref", "tenant_id"]


def test_request_template_enum_matches_the_registry():
    schema = load_schema("request.json")
    assert sorted(schema["properties"]["template_id"]["enum"]) == TEMPLATE_IDS
    assert sorted(TemplateRegistry().template_ids()) == TEMPLATE_IDS


def test_success_response_schema_declares_the_documented_required_fields():
    schema = load_schema("success_response.json")
    assert schema["type"] == "object"
    assert schema["required"] == [
        "ok",
        "message_id",
        "idempotency_key",
        "template_id",
        "tenant_id",
        "invoice_ref",
        "rendered_subject",
        "rendered_body_preview",
        "sent_at",
        "thread_id",
    ]


def test_success_response_schema_pins_ok_to_true():
    schema = load_schema("success_response.json")
    assert schema["properties"]["ok"]["const"] is True


def test_response_template_enum_matches_the_request_enum():
    request = load_schema("request.json")
    response = load_schema("success_response.json")
    assert (
        response["properties"]["template_id"]["enum"]
        == request["properties"]["template_id"]["enum"]
    )

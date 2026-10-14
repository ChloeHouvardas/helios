import json

from email_tool.templates import TemplateRegistry
from email_tool.tool import SCHEMAS_DIR

# The schemas ship inside the package, so ask the package where they are
# rather than deriving a path from this file's location. This suite lives
# under platform/financial-tools/tests/, not inside email_tool/, so the
# upstream `Path(__file__).parent.parent / "schemas"` no longer resolves — and
# hard-coding a new relative hop would just break again at the next move.
# Importing SCHEMAS_DIR also keeps the test honest: it validates the very
# files the tool loads at runtime.

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

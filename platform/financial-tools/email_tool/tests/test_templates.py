import pytest

from email_tool.templates import TemplateError, TemplateRegistry

REGISTRY = TemplateRegistry()

SAMPLE_VARIABLES = {
    "escalate_to_owner": {
        "invoice_ref": "INV-2031",
        "customer_name": "Dana Cole",
        "owner_name": "Sam Rivera",
        "currency": "EUR",
        "amount": "1,250.00",
        "days_overdue": 42,
        "due_date": "2026-11-01",
        "last_message_summary": "no reply to two reminders",
        "reason_for_escalation": "42 days overdue with no customer response",
        "suggested_action": "pause outreach and call the customer",
    },
    "invoice_delivery": {
        "invoice_ref": "INV-2031",
        "sme_name": "Acme Studio",
        "customer_name": "Dana Cole",
        "service_description": "branding sprint in March",
        "currency": "EUR",
        "amount": "1,250.00",
        "due_date": "2026-11-01",
        "payment_link": "https://pay.example/INV-2031",
    },
    "negotiation_offer": {
        "invoice_ref": "INV-2031",
        "sme_name": "Acme Studio",
        "customer_name": "Dana Cole",
        "currency": "EUR",
        "amount": "1,250.00",
        "offered_new_date": "2026-11-15",
        "plan_terms": "three monthly instalments",
    },
    "notice_overdue": {
        "invoice_ref": "INV-2031",
        "sme_name": "Acme Studio",
        "customer_name": "Dana Cole",
        "currency": "EUR",
        "amount": "1,250.00",
        "days_overdue": 12,
        "due_date": "2026-11-01",
    },
    "reminder_pre_due": {
        "invoice_ref": "INV-2031",
        "sme_name": "Acme Studio",
        "customer_name": "Dana Cole",
        "currency": "EUR",
        "amount": "1,250.00",
        "due_date": "2026-11-01",
        "days_until": 7,
    },
}


def test_registry_loads_exactly_the_five_documented_templates():
    assert REGISTRY.template_ids() == [
        "escalate_to_owner",
        "invoice_delivery",
        "negotiation_offer",
        "notice_overdue",
        "reminder_pre_due",
    ]


@pytest.mark.parametrize("template_id", sorted(SAMPLE_VARIABLES))
def test_renders_subject_and_body_without_leftover_placeholders(template_id):
    rendered = REGISTRY.render(template_id, SAMPLE_VARIABLES[template_id])
    assert rendered.subject
    assert rendered.body
    assert "{{" not in rendered.subject
    assert "{{" not in rendered.body
    assert "{%" not in rendered.body


@pytest.mark.parametrize("template_id", sorted(SAMPLE_VARIABLES))
def test_rendered_values_appear_in_subject_and_body(template_id):
    rendered = REGISTRY.render(template_id, SAMPLE_VARIABLES[template_id])
    assert "INV-2031" in rendered.subject
    assert "INV-2031" in rendered.body


def test_reminder_pre_due_classifies_the_conditional_variable_as_optional():
    assert REGISTRY.required_variables("reminder_pre_due") == {
        "amount",
        "currency",
        "customer_name",
        "due_date",
        "invoice_ref",
        "sme_name",
    }
    assert REGISTRY.optional_variables("reminder_pre_due") == {"days_until"}


def test_conditional_variable_can_be_omitted():
    variables = {
        key: value
        for key, value in SAMPLE_VARIABLES["reminder_pre_due"].items()
        if key != "days_until"
    }
    rendered = REGISTRY.render("reminder_pre_due", variables)
    assert "days from today" not in rendered.body
    assert "2026-11-01" in rendered.body


def test_conditional_variable_is_rendered_when_provided():
    rendered = REGISTRY.render("reminder_pre_due", SAMPLE_VARIABLES["reminder_pre_due"])
    assert "(7 days from today)" in rendered.body


def test_missing_required_variable_is_rejected():
    variables = {
        key: value
        for key, value in SAMPLE_VARIABLES["invoice_delivery"].items()
        if key != "payment_link"
    }
    with pytest.raises(TemplateError, match="payment_link"):
        REGISTRY.render("invoice_delivery", variables)


def test_null_required_variable_is_rejected():
    variables = dict(SAMPLE_VARIABLES["invoice_delivery"])
    variables["customer_name"] = None
    with pytest.raises(TemplateError, match="customer_name"):
        REGISTRY.render("invoice_delivery", variables)


def test_unknown_template_id_is_rejected():
    with pytest.raises(TemplateError, match="unknown template"):
        REGISTRY.render("welcome_aboard", {})


def test_template_without_subject_line_is_rejected(tmp_path):
    (tmp_path / "broken.md").write_text("Just a body, no subject.", encoding="utf-8")
    with pytest.raises(TemplateError, match="Subject"):
        TemplateRegistry(tmp_path)


def test_template_with_invalid_jinja_syntax_is_rejected(tmp_path):
    (tmp_path / "broken.md").write_text("**Subject**: Hi\n\n{{ unclosed", encoding="utf-8")
    with pytest.raises(TemplateError, match="invalid syntax"):
        TemplateRegistry(tmp_path)


def test_empty_templates_directory_is_rejected(tmp_path):
    with pytest.raises(TemplateError, match="no templates"):
        TemplateRegistry(tmp_path)

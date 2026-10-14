[THIS IS A ROUGH DRAFT]

Templates for different types of emails to be sent out to customers/SME owner.

1. `invoice_delivery.md`: delivers a receipt of an invoice to a customer. Tone is neutral and transactional.
2. `reminder_pre_due.md`: provides a reminder to the user about an upcoming invoice payment due date. Tone is friendly, no blame.
3. `notice_overdue.md`: sends a notice to the user about an overdue, unpaid invoice. Tone is firm and factual.
4. `negotiation_offer.md`: provides alternatives to the user for an unpaid invoice. Tone is constructive.
5. `escalate_to_owner.md`: a default, "catch-all" template used when agent is unsure of what kind of email to send. This email is internal only, and never reaches the user - only the SME owner.

## Format

Each template is a markdown file where the first `**Subject**:` line becomes the email subject and the remaining lines the body. Placeholders use Jinja2 syntax (`{{ variable }}`).

Variables that only appear inside an `{% if %}` condition are optional and may be omitted — for example `days_until` in `reminder_pre_due.md`, which drops the "(N days from today)" aside when absent. Every other variable is required and must be a non-null value.

`TemplateRegistry` (in `../templates.py`) loads every `.md` file in this directory except this README and works out each template's required and optional variables automatically.
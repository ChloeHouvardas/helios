**Subject**: Quick reminder — Invoice {{ invoice_ref }} due {{ due_date }}

Hi {{ customer_name }},

Just a friendly note that invoice {{ invoice_ref }} for {{ currency }} {{ amount }} comes due on {{ due_date }}{% if days_until %} ({{ days_until }} days from today){% endif %}.

If it's already on its way, please disregard this email. If you'd like a copy or a payment link again, just reply and we'll resend it.

Kind regards,

{{ sme_name }}
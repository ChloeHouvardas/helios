"""Email sender: compose, render, queue — and deliver, once SMTP is configured.

Offline by default: sent mail lands in `world.outbox` (an append-only list of
dicts) so simulations, audits and tests can inspect exactly what an agent would
have sent. Nothing touches a socket unless the bundle's tenant has
`EMAILTOOL_<TENANT>_SMTP_HOST` set.

Two template systems live here and they are NOT interchangeable:

- `self.templates` / `render()` — the original `$`-placeholder strings
  (`payment_reminder`, `payment_confirmation`), used by `send_template()` and by
  the shadow sandbox.
- `email_tool`'s five Jinja2 templates — reached through the `email.send_template`
  TOOL, which validates against a JSON Schema and keeps an idempotency store.

Delivery mechanics live in `email_delivery`; this module owns the outbox record
and the tool registrations.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from string import Template
from typing import Any, Callable, Dict, List, Optional

from . import email_delivery as delivery
from .contracts import REGISTRY, World, require_tenant

DEFAULT_TEMPLATES: Dict[str, str] = {
    "payment_reminder": (
        "Subject: Invoice $invoice_id — payment due $due_date\n"
        "\n"
        "Hi $contact,\n"
        "\n"
        "Our records show invoice $invoice_id for $amount is due on $due_date.\n"
        "If payment is already on the way, please disregard this note.\n"
        "\n"
        "Regards,\n"
        "$sender_name\n"
    ),
    "payment_confirmation": (
        "Subject: Payment received — $payment_id\n"
        "\n"
        "Hi $contact,\n"
        "\n"
        "We confirm receipt of $amount against reference $reference.\n"
        "Thank you.\n"
        "\n"
        "Regards,\n"
        "$sender_name\n"
    ),
}


@dataclass
class EmailSender:
    world: World
    sender_name: str = "Helios Accounts"
    templates: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_TEMPLATES))
    clock: Callable[[], float] = time.time
    #: Tenant whose SMTP credentials to use. `None` means offline: every send is
    #: recorded in the outbox and nothing is delivered.
    tenant: Optional[str] = None
    #: Directory for the idempotency store, so a replay survives a restart.
    #: `None` keeps replays in memory for the life of the process.
    data_dir: Optional[str] = None
    #: Test seams. `transport` replaces free-form SMTP delivery; the two
    #: factories replace the template path's transport and idempotency store.
    transport: Optional[Any] = None
    transport_factory: Optional[Callable[[str], Any]] = None
    #: Owned by this sender, not rebuilt per call: an offline replay is only
    #: detectable if the same store instance is consulted twice.
    _store: Optional[Any] = field(default=None, repr=False)

    def _idempotency_store(self) -> Any:
        if self._store is None:
            self._store = delivery.resolve_store(self.tenant or "", self.data_dir)
        return self._store

    # -- the original $-template path ---------------------------------------

    def render(self, template: str, **fields: Any) -> str:
        if template not in self.templates:
            raise KeyError(f"unknown email template: {template}")
        return Template(self.templates[template]).safe_substitute(**fields)

    def send(self, *, to: str, subject: str, body: str,
             template: Optional[str] = None) -> Dict[str, Any]:
        """Queue an email, and deliver it if this tenant configured SMTP.

        The outbox entry is written either way, so the audit trail is identical
        offline and live; `transport` and `provider_message_id` say which
        happened. Delivery errors propagate rather than being downgraded to a
        silent outbox write — see `EmailDeliveryError`.
        """
        if not to or "@" not in to:
            raise ValueError(f"invalid recipient: {to!r}")

        sent = delivery.deliver_freeform(
            tenant=self.tenant, to=to, subject=subject, body=body,
            transport=self.transport)

        msg: Dict[str, Any] = {
            "message_id": f"mail_{secrets.token_hex(6)}",
            "to": to, "subject": subject, "body": body,
            "template": template, "sent_at": self.clock(),
            "transport": sent.transport,
            "tenant": self.tenant,
        }
        if sent.delivered:
            # The mail server's own id: what ties this record to what was
            # actually accepted, so a bounce can be matched back to the send.
            msg["provider_message_id"] = sent.provider_message_id
        self.world.outbox.append(msg)
        return msg

    def send_template(self, *, to: str, template: str, **fields: Any) -> Dict[str, Any]:
        rendered = self.render(template, sender_name=self.sender_name, **fields)
        head, _, body = rendered.partition("\n\n")
        subject = head.removeprefix("Subject: ").strip()
        return self.send(to=to, subject=subject, body=body, template=template)

    # -- the five Jinja2 templates, via email_tool ---------------------------

    def deliver_tool_template(self, *, to: str, template: str,
                              fields: Dict[str, Any],
                              idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Render and send one of the five `email_tool` templates.

        Requires a tenant: the teammate's request schema validates `tenant_id`
        against `^[a-z0-9][a-z0-9_-]{0,63}$` and uses it to select both the SMTP
        credentials and the idempotency store, so there is no safe default.

        Replays are detected by `email_tool`'s store and return the original
        response without a second send; `replayed` says so, and the outbox is not
        appended to twice.
        """
        tenant = require_tenant(self, tool="email.send_template")

        if not to or "@" not in to:
            raise ValueError(f"invalid recipient: {to!r}")

        known = delivery.template_names()
        if template not in known:
            raise KeyError(
                f"unknown email template: {template!r}; expected one of "
                f"{', '.join(known)}. Call email.templates for each template's "
                f"required fields.")

        response = delivery.deliver_template(
            tenant=tenant, to=to, template=template, fields=fields,
            idempotency_key=idempotency_key, data_dir=self.data_dir,
            store=self._idempotency_store(),
            transport_factory=self.transport_factory)

        # `replayed` comes from the delivery layer, which looks the key up
        # before sending: the teammate's own response carries no such flag, and
        # discovering a replay afterwards would already mean a second email went
        # out.
        replayed = bool(response.get("replayed", False))
        transport = response.get("transport", delivery.OUTBOX_TRANSPORT)
        live = transport == delivery.SMTP_TRANSPORT

        # A replay must not create a second outbox entry: the point of the
        # idempotency key is that one send happened, and a duplicated audit
        # record would claim two contacts with the same customer.
        if not replayed:
            self.world.outbox.append({
                "message_id": f"mail_{secrets.token_hex(6)}",
                "to": to,
                "subject": response.get("rendered_subject", ""),
                "body_preview": response.get("rendered_body_preview", ""),
                "template": template,
                "sent_at": self.clock(),
                "transport": transport,
                "tenant": tenant,
                "idempotency_key": response.get("idempotency_key"),
                "provider_message_id": response.get("message_id"),
                "records_contact": True,
            })

        return {
            "message_id": response.get("message_id"),
            "to": to,
            "subject": response.get("rendered_subject", ""),
            "template": template,
            "tenant": tenant,
            "transport": transport,
            "delivered": live,
            "replayed": replayed,
            "idempotency_key": response.get("idempotency_key"),
            "invoice_ref": response.get("invoice_ref"),
            "sent_at": response.get("sent_at"),
        }

    def outbox(self) -> List[Dict[str, Any]]:
        return list(self.world.outbox)

    def templates_catalog(self) -> List[Dict[str, Any]]:
        """The five templates with their required and optional fields."""
        return delivery.list_templates()


def register(registry=REGISTRY, sender: Optional[EmailSender] = None) -> None:
    """Register the email tools.

    `sender` binds the closures to ONE instance so two bundles in a process
    cannot cross-talk; see `bind()` for why the fallback is deprecated.
    """

    def _bound() -> EmailSender:
        return sender if sender is not None else _instance()

    #: Schema for `email.send_template`, built from the teammate's own registry
    #: so the enum cannot drift from the Jinja2 sources. Nested `fields` needs a
    #: real JSON Schema — the flat map would type it as a bare object and the
    #: auditor could not then reject a template variable it was never told about.
    template_enum = delivery.template_names()
    send_template_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "format": "email",
                   "description": "Recipient address. The SME owner for "
                                  "escalate_to_owner, the customer otherwise."},
            "template": {"type": "string", "enum": template_enum,
                         "description": "Which template to render. Call "
                                        "email.templates for the required fields "
                                        "of each."},
            "fields": {"type": "object",
                       "description": "Template variables. Must include every "
                                      "required field for the chosen template, "
                                      "including 'invoice_ref'.",
                       "additionalProperties": True},
            "idempotency_key": {"type": ["string", "null"],
                                "description": "Replaying this key returns the "
                                               "original response instead of "
                                               "sending a second email."},
        },
        "required": ["to", "template", "fields"],
        "additionalProperties": False,
    }

    @registry.tool(
        "email.send", "Send an email to a customer/contact. Side-effecting "
        "(visible to a human outside the system).",
        {"to": "str", "subject": "str", "body": "str", "template": "str|None"},
        {"message_id": "str", "to": "str", "subject": "str", "transport": "str"},
        side_effects=True, tags=("email",),
        # Contacts a person outside the system, so the gate can apply a
        # contact-frequency mandate. Recorded whether or not SMTP is configured:
        # the outbox entry is the record of contact either way.
        tenant_scoped=True, records_contact=True,
    )
    def _send(to: str, subject: str, body: str,
              template: Optional[str] = None) -> Dict[str, Any]:
        return _bound().send(to=to, subject=subject, body=body, template=template)

    @registry.tool(
        "email.send_template",
        "Render and send one of the five collection templates (invoice delivery, "
        "pre-due reminder, overdue notice, negotiation offer, owner escalation). "
        "Side-effecting: it contacts a person outside the system. Replaying the "
        "same idempotency_key returns the original response instead of sending a "
        "second email. The tenant comes from the bundle, never from an argument. "
        "Requires a tenant, because the tenant selects both the SMTP credentials "
        "and the idempotency store.",
        {"to": "str", "template": "str", "fields": "dict",
         "idempotency_key": "str|None"},
        {"message_id": "str", "to": "str", "subject": "str", "template": "str",
         "transport": "str", "delivered": "bool", "replayed": "bool"},
        side_effects=True, tags=("email",),
        input_json_schema=send_template_schema,
        tenant_scoped=True, records_contact=True,
    )
    def _send_template(to: str, template: str, fields: Dict[str, Any],
                       idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        return _bound().deliver_tool_template(
            to=to, template=template, fields=fields or {},
            idempotency_key=idempotency_key)

    @registry.tool(
        "email.templates", "List the five email templates with the fields each "
        "one requires. Read-only.",
        {}, {"templates": "list[dict]"},
        side_effects=False, tags=("email",),
    )
    def _templates() -> Dict[str, Any]:
        return {"templates": _bound().templates_catalog()}

    @registry.tool(
        "email.outbox", "List emails queued by this app. Read-only.",
        {}, {"messages": "list[dict]"},
        side_effects=False, tags=("email",),
    )
    def _outbox() -> Dict[str, Any]:
        return {"messages": _bound().outbox()}


_INSTANCE: Dict[str, EmailSender] = {}


def _instance() -> EmailSender:
    return _INSTANCE["email"]


def bind(sender: EmailSender) -> None:
    """Deprecated: point the module-level singleton at `sender`.

    Process-global state — the last `build_world()` to call this wins and
    silently redirects every earlier bundle's email dispatch. Pass the instance
    to `register()` instead.
    """
    _INSTANCE["email"] = sender

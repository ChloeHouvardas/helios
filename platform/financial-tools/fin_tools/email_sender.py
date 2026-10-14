"""Email sender: compose, render, queue. Offline prototype — NO SMTP, NO network.

Sent mail lands in `world.outbox` (append-only list of dicts) so simulations,
audits and tests can inspect exactly what an agent would have sent. Swapping in
a real transport later means replacing `transport` with an SMTP/ESP client; the
compose/record/audit shape stays.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from string import Template
from typing import Any, Callable, Dict, List, Optional

from .contracts import REGISTRY, World

DEFAULT_TEMPLATES: Dict[str, str] = {
    "payment_reminder": (
        "Subject: Invoice $invoice_id — payment due $due_date\n"
        "\n"
        "Hi $contact,\n"
        "\n"
        "Our records show invoice $invoice_id for $amount is due on $due_date.\n"
        "If payment is already on its way, please disregard this note.\n"
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

    def render(self, template: str, **fields: Any) -> str:
        if template not in self.templates:
            raise KeyError(f"unknown email template: {template}")
        return Template(self.templates[template]).safe_substitute(**fields)

    def send(self, *, to: str, subject: str, body: str,
             template: Optional[str] = None) -> Dict[str, Any]:
        if not to or "@" not in to:
            raise ValueError(f"invalid recipient: {to!r}")
        msg = {
            "message_id": f"mail_{secrets.token_hex(6)}",
            "to": to, "subject": subject, "body": body,
            "template": template, "sent_at": self.clock(),
            "transport": "local-outbox",
        }
        self.world.outbox.append(msg)
        return msg

    def send_template(self, *, to: str, template: str, **fields: Any) -> Dict[str, Any]:
        rendered = self.render(template, sender_name=self.sender_name, **fields)
        head, _, body = rendered.partition("\n\n")
        subject = head.removeprefix("Subject: ").strip()
        return self.send(to=to, subject=subject, body=body, template=template)

    def outbox(self) -> List[Dict[str, Any]]:
        return list(self.world.outbox)


def register(registry=REGISTRY) -> None:
    @registry.tool(
        "email.send", "Send an email to a customer/contact. Side-effecting "
        "(visible to a human outside the system).",
        {"to": "str", "subject": "str", "body": "str", "template": "str|None"},
        {"message_id": "str", "to": "str", "subject": "str"},
        side_effects=True, tags=("email",),
    )
    def _send(to: str, subject: str, body: str,
              template: Optional[str] = None) -> Dict[str, Any]:
        return _instance().send(to=to, subject=subject, body=body, template=template)

    @registry.tool(
        "email.outbox", "List emails queued by this app. Read-only.",
        {}, {"messages": "list[dict]"},
        side_effects=False, tags=("email",),
    )
    def _outbox() -> Dict[str, Any]:
        return {"messages": _instance().outbox()}


_INSTANCE: Dict[str, EmailSender] = {}


def _instance() -> EmailSender:
    return _INSTANCE["email"]


def bind(sender: EmailSender) -> None:
    _INSTANCE["email"] = sender

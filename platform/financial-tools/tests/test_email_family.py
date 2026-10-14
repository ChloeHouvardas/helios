"""Task 2: the email family.

Two send paths, deliberately different, because the teammate package forces the
split:

- `email.send` — free-form (to, subject, body). Delivered straight through
  `email_tool.transport.SmtpTransport`; `EmailSender` itself refuses free-form
  because its schema demands a `template_id`.
- `email.send_template` — one of the five Jinja2 templates, routed through
  `EmailSender`, which owns rendering, schema validation and idempotency.

Both write to `world.outbox` by default. Both go live only when
`EMAILTOOL_<TENANT>_SMTP_HOST` is set for the bundle's tenant, and both carry
the `records_contact` marker so the gate can apply a contact-frequency mandate.

Every test here is offline: delivery is driven through an injected fake
transport, never a socket. The single live test at the bottom is skipped unless
real SMTP env vars are present.
"""
from __future__ import annotations

import os

import pytest

from fin_tools import build_world
from fin_tools.email_delivery import (OUTBOX_TRANSPORT, SMTP_TRANSPORT,
                                      smtp_configured, smtp_host_env)
from email_tool.transport import SentMessage

# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeTransport:
    """Stands in for `SmtpTransport`: records deliveries, hands back ids.

    Matches the transport contract exactly (keyword-only `send`, returns a
    `SentMessage`) so the code under test cannot tell the difference.
    """

    def __init__(self) -> None:
        self.deliveries: list[dict] = []

    def send(self, *, to: str, subject: str, body: str,
             thread_id: str | None = None) -> SentMessage:
        self.deliveries.append({"to": to, "subject": subject, "body": body,
                                "thread_id": thread_id})
        return SentMessage(message_id=f"provider-{len(self.deliveries)}",
                           thread_id=thread_id)


class FailingTransport:
    """A transport that refuses, to prove a failure is not recorded as a send."""

    def send(self, **kwargs):
        from email_tool.transport import SendError

        raise SendError("smtp refused")


TENANT = "acme"

# A complete offline SMTP env for the tenant, so `smtp_configured` is true and
# the injected fake transport is used instead of a real socket.
SMTP_ENV = {
    smtp_host_env(TENANT): "smtp.example.com",
    f"EMAILTOOL_{TENANT.upper()}_FROM": "billing@acme.example",
    f"EMAILTOOL_{TENANT.upper()}_SMTP_TLS": "none",
}


@pytest.fixture
def smtp_env(monkeypatch):
    """Set the tenant's SMTP env so the code takes the live-delivery path."""
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    # Keep any ambient tenant from leaking into these tests.
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    return SMTP_ENV


def _fields(template: str) -> dict:
    """Minimal valid variables for one of the five templates."""
    common = {"invoice_ref": "INV-2031", "customer_name": "Dana Cole",
              "currency": "CNY", "amount": "1,250.00", "due_date": "2026-11-01"}
    extra = {
        "reminder_pre_due": {"sme_name": "Acme Studio", "days_until": 5},
        "notice_overdue": {"sme_name": "Acme Studio", "days_overdue": 12},
        "negotiation_offer": {"sme_name": "Acme Studio",
                              "offered_new_date": "2026-12-01",
                              "plan_terms": "50% now, 50% on 2026-12-01"},
        "invoice_delivery": {"sme_name": "Acme Studio",
                             "service_description": "Design retainer",
                             "payment_link": "https://pay.helios.test/l/abc"},
        "escalate_to_owner": {"owner_name": "Owner", "days_overdue": 40,
                              "last_message_summary": "no reply to two notices",
                              "reason_for_escalation": "60 days overdue",
                              "suggested_action": "call the owner"},
    }
    return {**common, **extra[template]}


# --------------------------------------------------------------------------
# email.send — free form, offline default
# --------------------------------------------------------------------------

def test_send_writes_to_outbox_and_delivers_nothing_by_default(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    bundle = build_world(tenant=TENANT)     # no SMTP env set
    msg = bundle.registry.call("email.send", to="ap@acme.example",
                               subject="Hi", body="There")
    assert msg["transport"] == OUTBOX_TRANSPORT
    assert "provider_message_id" not in msg
    assert len(bundle.world.outbox) == 1


def test_send_delivers_through_the_transport_when_smtp_is_configured(smtp_env):
    bundle = build_world(tenant=TENANT)
    fake = FakeTransport()
    bundle.email.transport = fake           # test seam: no real socket

    msg = bundle.registry.call("email.send", to="ap@acme.example",
                               subject="Due soon", body="Please pay")
    assert msg["transport"] == SMTP_TRANSPORT
    assert msg["provider_message_id"] == "provider-1"
    assert fake.deliveries == [{"to": "ap@acme.example", "subject": "Due soon",
                                "body": "Please pay", "thread_id": None}]
    # the outbox still records it, now tied to the provider id
    assert bundle.world.outbox[0]["provider_message_id"] == "provider-1"


def test_send_failure_is_not_recorded_as_a_send(smtp_env):
    bundle = build_world(tenant=TENANT)
    bundle.email.transport = FailingTransport()
    from email_tool.transport import SendError

    with pytest.raises(SendError):
        bundle.registry.call("email.send", to="ap@acme.example",
                             subject="x", body="y")
    assert bundle.world.outbox == []        # nothing claimed as sent


def test_send_rejects_a_bad_recipient_offline(monkeypatch):
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    bundle = build_world(tenant=TENANT)
    with pytest.raises(ValueError):
        bundle.registry.call("email.send", to="not-an-email", subject="s", body="b")


# --------------------------------------------------------------------------
# email.templates — read-only catalog
# --------------------------------------------------------------------------

def test_templates_tool_lists_the_five_with_required_fields():
    bundle = build_world(tenant=TENANT)
    catalog = bundle.registry.call("email.templates")["templates"]
    names = {t["template"] for t in catalog}
    assert names == {"reminder_pre_due", "notice_overdue", "negotiation_offer",
                     "invoice_delivery", "escalate_to_owner"}
    by_name = {t["template"]: t for t in catalog}
    assert "invoice_ref" in by_name["notice_overdue"]["required_fields"]
    # a variable used only inside {% if %} is optional, not required
    assert "days_until" in by_name["reminder_pre_due"]["optional_fields"]
    assert "days_until" not in by_name["reminder_pre_due"]["required_fields"]


def test_templates_tool_is_read_only():
    bundle = build_world(tenant=TENANT)
    assert bundle.registry.get("email.templates").side_effects is False


# --------------------------------------------------------------------------
# email.send_template — the five templates, gated
# --------------------------------------------------------------------------

def test_send_template_is_gated_and_marked_records_contact():
    bundle = build_world(tenant=TENANT)
    spec = bundle.registry.get("email.send_template")
    assert spec.side_effects is True
    assert spec.records_contact is True
    assert spec.tenant_scoped is True
    # and email.send carries the same contact marker
    assert bundle.registry.get("email.send").records_contact is True


def test_send_template_renders_and_sends_offline():
    bundle = build_world(tenant=TENANT)
    out = bundle.registry.call("email.send_template", to="dana@example.com",
                               template="notice_overdue",
                               fields=_fields("notice_overdue"))
    assert out["template"] == "notice_overdue"
    assert out["replayed"] is False
    assert out["transport"] == OUTBOX_TRANSPORT     # no SMTP configured
    assert out["delivered"] is False
    assert len(bundle.world.outbox) == 1


def test_send_template_delivers_live_when_smtp_configured(smtp_env):
    bundle = build_world(tenant=TENANT)
    fake = FakeTransport()
    bundle.email.transport_factory = lambda tid: fake

    out = bundle.registry.call("email.send_template", to="dana@example.com",
                               template="reminder_pre_due",
                               fields=_fields("reminder_pre_due"))
    assert out["transport"] == SMTP_TRANSPORT
    assert out["delivered"] is True
    assert len(fake.deliveries) == 1
    assert fake.deliveries[0]["to"] == "dana@example.com"


def test_send_template_is_idempotent_on_replay(tmp_path):
    """A repeated key replays the original response and does NOT re-send."""
    bundle = build_world(tenant=TENANT, data_dir=str(tmp_path))
    fake = FakeTransport()
    bundle.email.transport_factory = lambda tid: fake

    args = {"to": "dana@example.com", "template": "notice_overdue",
            "fields": _fields("notice_overdue"), "idempotency_key": "key-1"}
    first = bundle.registry.call("email.send_template", **args)
    second = bundle.registry.call("email.send_template", **args)

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert len(fake.deliveries) == 1          # only one real send
    assert len(bundle.world.outbox) == 1      # only one audit record


def test_send_template_derives_a_key_when_none_is_given(tmp_path):
    """Same invoice + template + no explicit key -> same derived key -> replay."""
    bundle = build_world(tenant=TENANT, data_dir=str(tmp_path))
    fake = FakeTransport()
    bundle.email.transport_factory = lambda tid: fake

    args = {"to": "dana@example.com", "template": "notice_overdue",
            "fields": _fields("notice_overdue")}
    bundle.registry.call("email.send_template", **args)
    second = bundle.registry.call("email.send_template", **args)

    assert second["replayed"] is True
    assert len(fake.deliveries) == 1


def test_send_template_without_invoice_ref_is_rejected_actionably():
    bundle = build_world(tenant=TENANT)
    fields = _fields("notice_overdue")
    del fields["invoice_ref"]
    with pytest.raises(ValueError) as exc:
        bundle.email.deliver_tool_template(to="dana@example.com",
                                           template="notice_overdue",
                                           fields=fields)
    message = str(exc.value)
    assert "invoice_ref" in message
    assert "email.templates" in message       # points at the fix


def test_send_template_needs_a_tenant(monkeypatch):
    """A tenant selects the SMTP creds and the store; there is no safe default."""
    monkeypatch.delenv("HELIOS_TENANT", raising=False)
    bundle = build_world()                    # tenant is None
    with pytest.raises(ValueError) as exc:
        bundle.registry.call("email.send_template", to="dana@example.com",
                             template="notice_overdue",
                             fields=_fields("notice_overdue"))
    assert "tenant" in str(exc.value).lower()


def test_send_template_unknown_template_names_the_valid_set():
    bundle = build_world(tenant=TENANT)
    with pytest.raises(KeyError) as exc:
        bundle.email.deliver_tool_template(to="dana@example.com",
                                           template="not_a_template", fields={})
    assert "notice_overdue" in str(exc.value)


# --------------------------------------------------------------------------
# delivery seam
# --------------------------------------------------------------------------

def test_smtp_configured_is_false_without_a_host(monkeypatch):
    monkeypatch.delenv(smtp_host_env(TENANT), raising=False)
    assert smtp_configured(TENANT) is False


def test_smtp_configured_is_true_with_a_host(smtp_env):
    assert smtp_configured(TENANT) is True


def test_smtp_configured_rejects_a_malformed_tenant(monkeypatch):
    """An uppercase/garbled tenant degrades to offline rather than raising."""
    monkeypatch.setenv(smtp_host_env("ACME"), "smtp.example.com")
    assert smtp_configured("") is False
    assert smtp_configured(None) is False


# --------------------------------------------------------------------------
# the one live test — skipped unless real SMTP is configured
# --------------------------------------------------------------------------

_LIVE_TENANT = os.environ.get("HELIOS_EMAIL_LIVE_TENANT", "")
_LIVE_TO = os.environ.get("HELIOS_EMAIL_LIVE_TO", "")
_have_live_smtp = bool(_LIVE_TENANT and _LIVE_TO
                       and os.environ.get(smtp_host_env(_LIVE_TENANT)))


@pytest.mark.skipif(not _have_live_smtp,
                    reason="no live SMTP env: set HELIOS_EMAIL_LIVE_TENANT, "
                           "HELIOS_EMAIL_LIVE_TO and EMAILTOOL_<TENANT>_SMTP_*")
def test_live_send_template_delivers():
    """Actually sends through the tenant's real SMTP. Opt-in only."""
    bundle = build_world(tenant=_LIVE_TENANT)
    out = bundle.registry.call("email.send_template", to=_LIVE_TO,
                               template="notice_overdue",
                               fields=_fields("notice_overdue"))
    assert out["delivered"] is True
    assert out["transport"] == SMTP_TRANSPORT
    assert out["message_id"]

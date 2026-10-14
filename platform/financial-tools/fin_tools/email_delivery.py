"""Delivery seam between the fin_tools email tools and the imported `email_tool`.

The two populations do not line up, and papering over it would hide a real
distinction:

- `email.send` takes a free-form (to, subject, body). `email_tool.EmailSender`
  will not accept that — its JSON Schema requires a `template_id` from a
  five-value enum and allows no unknown properties. So free-form delivery goes
  straight to `email_tool.transport.SmtpTransport`, the layer that actually
  speaks SMTP.
- `email.send_template` takes one of those five templates, so it goes through
  `EmailSender`, which owns the Jinja2 rendering, the schema validation and the
  idempotency store.

Default behaviour is offline: with no SMTP configured for the bundle's tenant,
everything lands in `world.outbox` and nothing touches a socket. Delivery goes
live when `EMAILTOOL_<TENANT>_SMTP_HOST` is set, and the provider's message id
is recorded on the outbox entry so an audit can tie the record to what the mail
server accepted.

The `email_tool` imports are lazy so that importing this module — and therefore
the registry — does not pull in jinja2 and jsonschema. Registering the email
tools does need them; they are pinned dependencies of this distribution.

Secrets are never read here. SMTP credentials are resolved inside `email_tool`
from the environment, and `SmtpConfig.password` is already excluded from its
`repr`, so no error or log line from this layer can carry one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

#: Prefix of the per-tenant SMTP variables, e.g. `EMAILTOOL_ACME_SMTP_HOST`.
ENV_PREFIX = "EMAILTOOL"

#: Transport label recorded on an outbox entry that only reached the outbox.
OUTBOX_TRANSPORT = "local-outbox"
#: Transport label recorded on an outbox entry handed to a real mail server.
SMTP_TRANSPORT = "smtp"


class EmailDeliveryError(Exception):
    """A send asked for live delivery but cannot be performed.

    Raised rather than silently downgraded. An app that configured SMTP and then
    quietly got an outbox entry would believe a customer had been contacted when
    nothing left the building, which is worse than a refused send.
    """


@dataclass(frozen=True)
class Sent:
    """What one delivery attempt produced."""

    transport: str                              # OUTBOX_TRANSPORT or SMTP_TRANSPORT
    provider_message_id: Optional[str] = None   # the mail server's id, if delivered

    @property
    def delivered(self) -> bool:
        return self.transport == SMTP_TRANSPORT


class _InMemoryIdempotencyStore:
    """Stand-in for `email_tool.idempotency.IdempotencyStore` with no data dir.

    Same `get`/`put` contract, so a replay still replays within the process
    instead of either crashing or silently allowing a duplicate send.
    """

    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return self._records.get(key)

    def put(self, key: str, response: Dict[str, Any]) -> None:
        self._records[key] = response


class _NoOpTransport:
    """Transport for the offline path: accepts the message, delivers nothing.

    `EmailSender` requires a transport and reads only `message_id`/`thread_id`
    off the result, so this satisfies it without a socket. The caller records
    `OUTBOX_TRANSPORT` and no provider id, which is what makes an offline send
    distinguishable from a delivered one in the audit trail.
    """

    def __init__(self, id_prefix: str = "outbox") -> None:
        self._prefix = id_prefix
        self.calls: List[Dict[str, Any]] = []

    def send(self, *, to: str, subject: str, body: str,
             thread_id: Optional[str] = None) -> Any:
        from email_tool.transport import SentMessage

        self.calls.append({"to": to, "subject": subject, "body": body,
                           "thread_id": thread_id})
        return SentMessage(message_id=f"{self._prefix}-{len(self.calls)}",
                           thread_id=thread_id)


def smtp_host_env(tenant: str) -> str:
    """The environment variable whose presence switches this tenant to live."""
    return f"{ENV_PREFIX}_{tenant.upper()}_SMTP_HOST"


def smtp_configured(tenant: Optional[str],
                    environ: Optional[Mapping[str, str]] = None) -> bool:
    """True when a real SMTP host is configured for `tenant`.

    A tenant id the mail config would reject counts as not configured, so a
    malformed `HELIOS_TENANT` degrades to the outbox rather than raising at send
    time. Only the host variable is inspected here; `load_smtp_config` does the
    full validation when delivery actually happens.
    """
    if not tenant or not str(tenant).strip():
        return False
    env = os.environ if environ is None else environ
    return bool(env.get(smtp_host_env(str(tenant).strip())))


def make_smtp_transport(tenant: str,
                        smtp_factory: Optional[Callable[[str, int], Any]] = None) -> Any:
    """A real `SmtpTransport` for `tenant`.

    `smtp_factory` replaces only the socket underneath, which keeps the
    header-injection checks on the assertion path in tests.
    """
    from email_tool.config import ConfigError, load_smtp_config
    from email_tool.transport import SmtpTransport

    try:
        config = load_smtp_config(tenant)
    except ConfigError as exc:
        # `load_smtp_config` already names the missing variable; keep that and
        # add the escape hatch, so the message is actionable either way.
        raise EmailDeliveryError(
            f"SMTP is configured for tenant {tenant!r} but incomplete: {exc}. "
            f"Set every variable named in that message, or unset "
            f"{smtp_host_env(tenant)} to send to the local outbox instead.") from exc

    if smtp_factory is None:
        return SmtpTransport(config)
    return SmtpTransport(config, smtp_factory=smtp_factory)


def deliver_freeform(*, tenant: Optional[str], to: str, subject: str, body: str,
                     transport: Optional[Any] = None,
                     smtp_factory: Optional[Callable[[str, int], Any]] = None,
                     ) -> Sent:
    """Deliver a free-form message over SMTP, or report that it stayed local.

    `transport` is the test seam: pass any object with a keyword-only `send`
    returning something with a `message_id`. Omit it and delivery happens only
    if this tenant configured SMTP.
    """
    if transport is None:
        if not smtp_configured(tenant):
            return Sent(transport=OUTBOX_TRANSPORT)
        transport = make_smtp_transport(str(tenant), smtp_factory=smtp_factory)

    # SendError / HeaderInjectionError propagate: a refused recipient or an
    # injected header must not be reported as a success.
    sent = transport.send(to=to, subject=subject, body=body)
    return Sent(transport=SMTP_TRANSPORT,
                provider_message_id=getattr(sent, "message_id", None))


def make_email_sender(*, tenant: str,
                      data_dir: Optional[str],
                      store: Any,
                      transport_factory: Optional[Callable[[str], Any]] = None,
                      smtp_factory: Optional[Callable[[str, int], Any]] = None,
                      clock: Optional[Callable[[], Any]] = None) -> Any:
    """Build the teammate's `EmailSender` wired to this bundle's tenant.

    With no injected transport the factory picks real SMTP for a tenant that
    configured it and the no-op transport otherwise, so one code path serves
    both and an offline run never opens a socket.

    `store` is supplied by the caller, which owns it across calls; see
    `resolve_store` for why an offline in-memory store cannot be rebuilt here.
    """
    from email_tool.tool import EmailSender

    if transport_factory is None:
        if smtp_configured(tenant):
            def transport_factory(tid: str) -> Any:      # noqa: E306 — real SMTP
                return make_smtp_transport(tid, smtp_factory=smtp_factory)
        else:
            offline = _NoOpTransport()

            def transport_factory(tid: str) -> Any:      # noqa: E306,F811 — offline
                return offline

    def store_factory(_tid: str) -> Any:                 # one store, always
        return store

    kwargs: Dict[str, Any] = {"transport_factory": transport_factory,
                              "store_factory": store_factory}
    if clock is not None:
        kwargs["clock"] = clock
    return EmailSender(**kwargs)


def resolve_store(tenant: str, data_dir: Optional[str],
                  cached: Optional[Any] = None) -> Any:
    """The idempotency store for one tenant.

    With a `data_dir` the store is JSONL-backed, so it can be constructed per
    call and still see earlier sends — the file is the state. Without one the
    store is in memory and MUST be reused, so the caller passes the instance it
    already holds as `cached`; building a fresh one every call would make every
    replay look like a first send and email the customer twice.
    """
    if data_dir:
        from email_tool.idempotency import IdempotencyStore

        return IdempotencyStore(Path(data_dir) / "tenants" / tenant / "sends.jsonl")
    return cached if cached is not None else _InMemoryIdempotencyStore()


def idempotency_key_for(invoice_ref: str, template: str,
                        explicit: Optional[str], step: Optional[int] = None) -> str:
    """The key `EmailSender` will enforce, computed the same way it computes it.

    Needed to answer "is this a replay?" BEFORE sending: the teammate returns no
    such flag, and finding out afterwards would mean a second contact with the
    customer — exactly what the key exists to prevent.
    """
    if explicit:
        return explicit
    from email_tool.idempotency import derive_key

    return derive_key(invoice_ref, template, step)


def deliver_template(*, tenant: str, to: str, template: str,
                     fields: Mapping[str, Any],
                     idempotency_key: Optional[str],
                     data_dir: Optional[str],
                     store: Any,
                     transport_factory: Optional[Callable[[str], Any]] = None,
                     smtp_factory: Optional[Callable[[str, int], Any]] = None,
                     clock: Optional[Callable[[], Any]] = None,
                     ) -> Dict[str, Any]:
    """Render and send one of the five templates through `EmailSender`.

    `EmailSender.send` validates against a JSON Schema that requires
    `invoice_ref` and `tenant_id` and allows no unknown properties, so both are
    supplied here rather than passed through blindly. `invoice_ref` comes out of
    `fields`, which all five templates require anyway; if a caller omitted it the
    error says so in terms the caller can act on, instead of surfacing as a
    schema rejection naming a field they never saw.

    `store` is the caller's idempotency store, passed in so replays are detected
    across calls. Returns the teammate's response plus three fields it does not
    supply: `replayed`, `transport`, `enforced_idempotency_key`.
    """
    invoice_ref = fields.get("invoice_ref")
    if not invoice_ref:
        raise ValueError(
            f"email.send_template needs fields['invoice_ref']: template "
            f"{template!r} renders it into the subject and body, and it seeds the "
            f"idempotency key. Pass fields={{'invoice_ref': <ref>, ...}} — call "
            f"email.templates for the full required-field list per template.")

    request: Dict[str, Any] = {
        "template_id": template,
        "to": to,
        "variables": dict(fields),
        "invoice_ref": str(invoice_ref),
        "tenant_id": tenant,
    }
    if idempotency_key:
        request["idempotency_key"] = idempotency_key

    key = idempotency_key_for(str(invoice_ref), template, idempotency_key)
    replayed = store.get(key) is not None

    sender = make_email_sender(
        tenant=tenant, data_dir=data_dir, transport_factory=transport_factory,
        store=store, smtp_factory=smtp_factory, clock=clock)
    response = dict(sender.send(request))

    response["replayed"] = replayed
    response["transport"] = (SMTP_TRANSPORT if smtp_configured(tenant)
                             else OUTBOX_TRANSPORT)
    response["enforced_idempotency_key"] = key
    return response


def list_templates() -> List[Dict[str, Any]]:
    """The five templates with their required and optional fields.

    Read from the teammate's own registry rather than re-declared here, so this
    cannot drift from the Jinja2 sources. `required_variables` comes from parsing
    each template's AST, which is why a variable used only inside an `{% if %}`
    correctly comes back optional.
    """
    from email_tool.templates import TemplateRegistry

    registry = TemplateRegistry()
    return [
        {
            "template": template_id,
            "required_fields": sorted(registry.required_variables(template_id)),
            "optional_fields": sorted(registry.optional_variables(template_id)),
        }
        for template_id in registry.template_ids()
    ]


def template_names() -> List[str]:
    """The five template ids, for the tool's input-schema enum."""
    from email_tool.templates import TemplateRegistry

    return TemplateRegistry().template_ids()

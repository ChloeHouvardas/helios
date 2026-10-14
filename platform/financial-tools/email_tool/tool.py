"""Helios tool-contract entry point for the email sender.

``TOOL`` declares the shared Helios tool contract: name, description,
input schema, output schema, and the side-effect flag. Sending an email
changes the real world, so ``side_effect`` is true and agents must route
calls through the audit gate.

``EmailSender.send()`` validates the request against the input schema,
replays a stored response when the idempotency key was seen before,
renders the template, sends through the tenant's SMTP transport, and
records the schema-validated response in the tenant's idempotency store.
A failed send leaves no idempotency record, so the request can be
retried.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .config import load_smtp_config
from .idempotency import IdempotencyStore, derive_key
from .templates import TemplateRegistry
from .transport import SmtpTransport

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
BODY_PREVIEW_LENGTH = 200


class InvalidRequestError(Exception):
    """The request does not match the tool's input schema."""


class InvalidResponseError(Exception):
    """The assembled response does not match the tool's output schema."""


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


_REQUEST_SCHEMA = _load_schema("request.json")
_RESPONSE_SCHEMA = _load_schema("success_response.json")

TOOL: dict[str, Any] = {
    "name": "send_email",
    "description": (
        "Send a templated email — invoice delivery, pre-due reminder, overdue "
        "notice, negotiation offer, or owner escalation — on behalf of one SME "
        "tenant. Repeated requests with the same idempotency key replay the "
        "original response instead of sending a second email. Sending mail "
        "has side effects, so calls must go through the audit gate."
    ),
    "input_schema": _REQUEST_SCHEMA,
    "output_schema": _RESPONSE_SCHEMA,
    "side_effect": True,
}

_REQUEST_VALIDATOR = Draft202012Validator(
    _REQUEST_SCHEMA, format_checker=FormatChecker()
)
_RESPONSE_VALIDATOR = Draft202012Validator(
    _RESPONSE_SCHEMA, format_checker=FormatChecker()
)


def _validate_request(request: dict[str, Any]) -> None:
    errors = sorted(_REQUEST_VALIDATOR.iter_errors(request), key=lambda e: e.json_path)
    if errors:
        details = "; ".join(f"{error.json_path}: {error.message}" for error in errors)
        raise InvalidRequestError(f"invalid send_email request: {details}")


def _validate_response(response: dict[str, Any]) -> None:
    errors = sorted(
        _RESPONSE_VALIDATOR.iter_errors(response), key=lambda e: e.json_path
    )
    if errors:
        details = "; ".join(f"{error.json_path}: {error.message}" for error in errors)
        raise InvalidResponseError(
            f"assembled send_email response is invalid: {details}"
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_data_dir(data_dir: Path | str | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    env_value = os.environ.get("EMAILTOOL_DATA_DIR")
    if env_value:
        return Path(env_value)
    return Path(__file__).resolve().parent.parent / "data"


class EmailSender:
    """Tool entry point: one call sends (or replays) one templated email."""

    def __init__(
        self,
        *,
        templates: TemplateRegistry | None = None,
        transport_factory: Callable[[str], SmtpTransport] | None = None,
        store_factory: Callable[[str], IdempotencyStore] | None = None,
        data_dir: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._templates = templates if templates is not None else TemplateRegistry()

        if transport_factory is None:

            def transport_factory(tenant_id: str) -> SmtpTransport:
                return SmtpTransport(load_smtp_config(tenant_id))

        self._transport_factory = transport_factory

        if store_factory is None:
            base_dir = _resolve_data_dir(data_dir)

            def store_factory(tenant_id: str) -> IdempotencyStore:
                return IdempotencyStore(
                    base_dir / "tenants" / tenant_id / "sends.jsonl"
                )

        self._store_factory = store_factory
        self._clock = clock if clock is not None else _utc_now

    def send(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Send one templated email, or replay the original response.

        The replay check happens before the tenant's configuration is
        loaded or the template rendered, so a repeated request succeeds
        even if the mailbox credentials have since changed. A send that
        fails anywhere is not recorded, leaving the request retryable.
        """
        request = dict(request)
        _validate_request(request)
        tenant_id = request["tenant_id"]
        key = request.get("idempotency_key")
        if key is None:
            key = derive_key(
                request["invoice_ref"], request["template_id"], request.get("step")
            )

        store = self._store_factory(tenant_id)
        stored = store.get(key)
        if stored is not None:
            return stored

        rendered = self._templates.render(request["template_id"], request["variables"])
        sent = self._transport_factory(tenant_id).send(
            to=request["to"],
            subject=rendered.subject,
            body=rendered.body,
            thread_id=request.get("thread_id"),
        )
        response = {
            "ok": True,
            "message_id": sent.message_id,
            "idempotency_key": key,
            "template_id": request["template_id"],
            "tenant_id": tenant_id,
            "invoice_ref": request["invoice_ref"],
            "rendered_subject": rendered.subject,
            "rendered_body_preview": rendered.body[:BODY_PREVIEW_LENGTH],
            "sent_at": self._clock().isoformat(),
            "thread_id": sent.thread_id,
        }
        _validate_response(response)
        store.put(key, response)
        return response

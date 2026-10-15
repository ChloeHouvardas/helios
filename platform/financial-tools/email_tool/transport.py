"""SMTP transport for the email sender.

A rendered email is turned into an ``EmailMessage`` and delivered with
``smtplib`` using the tenant's ``SmtpConfig``. Line breaks are rejected
in every header value — including the rendered subject, whose template
variables are caller-controlled — so no request can inject additional
headers such as Bcc. The connection factory is injectable so the tests
run without a real SMTP server.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Callable, Protocol

from .config import SmtpConfig


class SendError(Exception):
    """The message could not be delivered."""


class HeaderInjectionError(Exception):
    """A header value contains a line break."""


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    thread_id: str | None


class SmtpConnection(Protocol):
    """The part of ``smtplib.SMTP`` the transport relies on."""

    def starttls(self) -> Any: ...

    def login(self, username: str, password: str) -> Any: ...

    def send_message(self, msg: EmailMessage) -> Any: ...

    def close(self) -> None: ...


SmtpFactory = Callable[[str, int], SmtpConnection]


def _reject_line_breaks(field: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise HeaderInjectionError(f"{field} must not contain line breaks")


class SmtpTransport:
    """Delivers rendered emails over SMTP using a tenant configuration."""

    def __init__(
        self,
        config: SmtpConfig,
        *,
        smtp_factory: SmtpFactory | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._config = config
        if smtp_factory is None:

            def smtp_factory(host: str, port: int) -> SmtpConnection:
                connect = smtplib.SMTP_SSL if config.tls == "ssl" else smtplib.SMTP
                return connect(host, port, timeout=timeout)

        self._smtp_factory = smtp_factory

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
    ) -> SentMessage:
        _reject_line_breaks("to", to)
        _reject_line_breaks("subject", subject)
        _reject_line_breaks("from address", self._config.from_address)
        if thread_id is not None:
            _reject_line_breaks("thread_id", thread_id)

        message_id = make_msgid(domain=self._from_domain())
        message = EmailMessage()
        message["From"] = self._config.from_address
        message["To"] = to
        message["Subject"] = subject
        message["Message-ID"] = message_id
        if thread_id is not None:
            message["In-Reply-To"] = thread_id
            message["References"] = thread_id
        message.set_content(body)

        try:
            connection = self._smtp_factory(self._config.host, self._config.port)
            try:
                if self._config.tls == "starttls":
                    connection.starttls()
                if self._config.username is not None:
                    connection.login(self._config.username, self._config.password or "")
                refused = connection.send_message(message)
            finally:
                connection.close()
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(
                f"SMTP send to {self._config.host}:{self._config.port} failed: {exc}"
            ) from exc
        if refused:
            raise SendError(
                "SMTP server refused recipients: "
                + ", ".join(
                    f"{recipient} (code {refused[recipient][0]})"
                    for recipient in sorted(refused)
                )
            )
        return SentMessage(message_id=message_id, thread_id=thread_id)

    def _from_domain(self) -> str | None:
        if "@" not in self._config.from_address:
            return None
        return self._config.from_address.rsplit("@", 1)[1].strip().rstrip(">")

import smtplib

import pytest

from email_tool.config import SmtpConfig
from email_tool.transport import HeaderInjectionError, SendError, SmtpTransport

BODY = "Dear Dana Cole,\n\nPlease find the overdue notice below.\n"
SUBJECT = "Overdue: Invoice INV-2031"


class FakeConnection:
    """Stands in for ``smtplib.SMTP`` and records every interaction."""

    def __init__(self, *, refused=None, fail=None):
        self.refused = refused or {}
        self.fail = fail
        self.connect_args = None
        self.starttls_called = False
        self.login_credentials = None
        self.sent_messages = []
        self.closed = False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_credentials = (username, password)

    def send_message(self, message):
        if self.fail is not None:
            raise self.fail
        self.sent_messages.append(message)
        return self.refused

    def close(self):
        self.closed = True


def make_factory(connection):
    def smtp_factory(host, port):
        connection.connect_args = (host, port)
        return connection

    return smtp_factory


def make_config(**overrides):
    settings = {
        "host": "smtp.acme.example.com",
        "port": 587,
        "from_address": "billing@acme.example.com",
        "username": None,
        "password": None,
        "tls": "starttls",
    }
    settings.update(overrides)
    return SmtpConfig(**settings)


def make_transport(connection, **config_overrides):
    return SmtpTransport(
        make_config(**config_overrides), smtp_factory=make_factory(connection)
    )


def test_delivers_a_message_with_the_expected_headers():
    connection = FakeConnection()
    sent = make_transport(connection).send(
        to="dana@example.com", subject=SUBJECT, body=BODY
    )
    (message,) = connection.sent_messages
    assert message["From"] == "billing@acme.example.com"
    assert message["To"] == "dana@example.com"
    assert message["Subject"] == SUBJECT
    assert message["Message-ID"] == sent.message_id
    assert message.get_content() == BODY


def test_message_id_is_scoped_to_the_from_domain():
    connection = FakeConnection()
    sent = make_transport(connection).send(
        to="dana@example.com", subject=SUBJECT, body=BODY
    )
    assert sent.message_id.startswith("<")
    assert sent.message_id.endswith("@acme.example.com>")


def test_thread_headers_are_set_when_a_thread_id_is_given():
    connection = FakeConnection()
    thread_id = "<previous-42@acme.example.com>"
    sent = make_transport(connection).send(
        to="dana@example.com", subject=SUBJECT, body=BODY, thread_id=thread_id
    )
    (message,) = connection.sent_messages
    assert message["In-Reply-To"] == thread_id
    assert message["References"] == thread_id
    assert sent.thread_id == thread_id


def test_a_new_conversation_has_no_thread_headers():
    connection = FakeConnection()
    sent = make_transport(connection).send(
        to="dana@example.com", subject=SUBJECT, body=BODY
    )
    (message,) = connection.sent_messages
    assert message.get("In-Reply-To") is None
    assert message.get("References") is None
    assert sent.thread_id is None


def test_starttls_negotiates_tls_after_connecting():
    connection = FakeConnection()
    make_transport(connection, tls="starttls").send(
        to="dana@example.com", subject=SUBJECT, body=BODY
    )
    assert connection.starttls_called


@pytest.mark.parametrize("tls", ["ssl", "none"])
def test_non_starttls_modes_do_not_call_starttls(tls):
    connection = FakeConnection()
    make_transport(connection, tls=tls).send(
        to="dana@example.com", subject=SUBJECT, body=BODY
    )
    assert not connection.starttls_called


def test_login_uses_the_configured_credentials():
    connection = FakeConnection()
    config = make_config(username="billing@acme.example.com", password="hunter2")
    SmtpTransport(config, smtp_factory=make_factory(connection)).send(
        to="dana@example.com", subject=SUBJECT, body=BODY
    )
    assert connection.login_credentials == ("billing@acme.example.com", "hunter2")


def test_login_is_skipped_without_credentials():
    connection = FakeConnection()
    make_transport(connection).send(
        to="dana@example.com", subject=SUBJECT, body=BODY
    )
    assert connection.login_credentials is None


def test_connects_to_the_configured_host_and_port():
    connection = FakeConnection()
    config = make_config(host="smtp.relais.example.com", port=2525)
    SmtpTransport(config, smtp_factory=make_factory(connection)).send(
        to="dana@example.com", subject=SUBJECT, body=BODY
    )
    assert connection.connect_args == ("smtp.relais.example.com", 2525)


def test_the_connection_is_closed_after_sending():
    connection = FakeConnection()
    make_transport(connection).send(
        to="dana@example.com", subject=SUBJECT, body=BODY
    )
    assert connection.closed


def test_the_connection_is_closed_even_when_sending_fails():
    connection = FakeConnection(fail=smtplib.SMTPException("server went away"))
    transport = SmtpTransport(make_config(), smtp_factory=make_factory(connection))
    with pytest.raises(SendError):
        transport.send(to="dana@example.com", subject=SUBJECT, body=BODY)
    assert connection.closed


def test_smtp_failures_are_wrapped_in_send_error():
    connection = FakeConnection(fail=smtplib.SMTPException("server went away"))
    transport = SmtpTransport(make_config(), smtp_factory=make_factory(connection))
    with pytest.raises(SendError, match="smtp.acme.example.com:587"):
        transport.send(to="dana@example.com", subject=SUBJECT, body=BODY)


def test_network_failures_are_wrapped_in_send_error():
    connection = FakeConnection(fail=OSError("connection refused"))
    transport = SmtpTransport(make_config(), smtp_factory=make_factory(connection))
    with pytest.raises(SendError, match="failed"):
        transport.send(to="dana@example.com", subject=SUBJECT, body=BODY)


def test_refused_recipients_are_reported():
    connection = FakeConnection(refused={"dana@example.com": (550, b"User unknown")})
    transport = SmtpTransport(make_config(), smtp_factory=make_factory(connection))
    with pytest.raises(SendError) as excinfo:
        transport.send(to="dana@example.com", subject=SUBJECT, body=BODY)
    assert "dana@example.com" in str(excinfo.value)
    assert "550" in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("to", "dana@example.com\nBcc: evil@example.com", id="recipient"),
        pytest.param("subject", "Overdue\nBcc: evil@example.com", id="subject"),
        pytest.param(
            "subject", "Overdue\rBcc: evil@example.com", id="subject-carriage-return"
        ),
        pytest.param(
            "thread_id",
            "<previous-42@example.com>\nBcc: evil@example.com",
            id="thread-id",
        ),
    ],
)
def test_line_breaks_in_headers_are_rejected(field, value):
    connection = FakeConnection()
    transport = SmtpTransport(make_config(), smtp_factory=make_factory(connection))
    kwargs = {
        "to": "dana@example.com",
        "subject": SUBJECT,
        "body": BODY,
        "thread_id": None,
    }
    kwargs[field] = value
    with pytest.raises(HeaderInjectionError, match="must not contain line breaks"):
        transport.send(**kwargs)
    assert connection.sent_messages == []


def test_line_breaks_in_the_configured_from_address_are_rejected():
    connection = FakeConnection()
    config = make_config(
        from_address="billing@acme.example.com\nBcc: evil@example.com"
    )
    transport = SmtpTransport(config, smtp_factory=make_factory(connection))
    with pytest.raises(HeaderInjectionError, match="line breaks"):
        transport.send(to="dana@example.com", subject=SUBJECT, body=BODY)
    assert connection.sent_messages == []


def test_the_default_factory_uses_smtp_ssl_for_tls_ssl(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda host, port, timeout: connection)
    transport = SmtpTransport(make_config(tls="ssl", port=465))
    sent = transport.send(to="dana@example.com", subject=SUBJECT, body=BODY)
    assert connection.sent_messages
    assert sent.thread_id is None


def test_the_default_factory_uses_plain_smtp_for_starttls(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout: connection)
    transport = SmtpTransport(make_config(tls="starttls"))
    transport.send(to="dana@example.com", subject=SUBJECT, body=BODY)
    assert connection.starttls_called

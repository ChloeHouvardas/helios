"""Per-tenant SMTP configuration read from environment variables.

Each SME (tenant) sends from its own mailbox, so every setting is looked
up with the tenant id in the variable name: for tenant ``acme`` the SMTP
host comes from ``EMAILTOOL_ACME_SMTP_HOST``. Secrets are read from the
environment only, never logged, and kept out of ``repr`` output.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

TLS_MODES = ("starttls", "ssl", "none")
_DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}
_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ConfigError(Exception):
    """Tenant SMTP configuration is missing or invalid."""


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    from_address: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    tls: str = "starttls"


def env_var_name(tenant_id: str, setting: str) -> str:
    """Return the environment variable name for one tenant setting."""
    return f"EMAILTOOL_{tenant_id.upper()}_{setting}"


def load_smtp_config(
    tenant_id: str, environ: Mapping[str, str] | None = None
) -> SmtpConfig:
    """Load one tenant's SMTP settings from the environment.

    ``SMTP_HOST`` and ``FROM`` are required. ``SMTP_USER`` and
    ``SMTP_PASSWORD`` must be set together and enable SMTP login when
    they are. ``SMTP_TLS`` defaults to ``starttls`` and ``SMTP_PORT`` to
    the TLS mode's standard port.
    """
    if environ is None:
        environ = os.environ
    if not _TENANT_ID.fullmatch(tenant_id):
        raise ConfigError(
            f"invalid tenant id {tenant_id!r}: must be 1-64 characters of "
            "lowercase letters, digits, hyphens, or underscores"
        )

    def value(setting: str) -> str | None:
        raw = environ.get(env_var_name(tenant_id, setting))
        return raw if raw else None

    host = value("SMTP_HOST")
    from_address = value("FROM")
    missing = [
        env_var_name(tenant_id, setting)
        for setting, raw in (("SMTP_HOST", host), ("FROM", from_address))
        if raw is None
    ]
    if missing:
        raise ConfigError(
            f"missing environment variables for tenant {tenant_id!r}: "
            + ", ".join(sorted(missing))
        )

    username = value("SMTP_USER")
    password = value("SMTP_PASSWORD")
    if (username is None) != (password is None):
        raise ConfigError(
            f"tenant {tenant_id!r}: {env_var_name(tenant_id, 'SMTP_USER')} and "
            f"{env_var_name(tenant_id, 'SMTP_PASSWORD')} must be set together"
        )

    tls = (environ.get(env_var_name(tenant_id, "SMTP_TLS")) or "starttls").lower()
    if tls not in TLS_MODES:
        raise ConfigError(
            f"tenant {tenant_id!r}: invalid TLS mode {tls!r}; "
            f"expected one of: {', '.join(TLS_MODES)}"
        )

    port_raw = value("SMTP_PORT")
    if port_raw is None:
        port = _DEFAULT_PORTS[tls]
    else:
        try:
            port = int(port_raw)
        except ValueError:
            raise ConfigError(
                f"tenant {tenant_id!r}: {env_var_name(tenant_id, 'SMTP_PORT')} "
                f"must be an integer, got {port_raw!r}"
            ) from None
        if not 1 <= port <= 65535:
            raise ConfigError(
                f"tenant {tenant_id!r}: port {port} is outside 1-65535"
            ) from None

    return SmtpConfig(
        host=host,
        port=port,
        from_address=from_address,
        username=username,
        password=password,
        tls=tls,
    )

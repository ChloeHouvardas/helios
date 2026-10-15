"""Test configuration: keep the suite offline, permanently.

A driver test once pointed at ``DEFAULT_BASE_URL`` and made a real request to
the public model endpoint, which answered with a live 401. The assertion failed
for the right reason but for the wrong cause, and the suite was not hermetic.

So socket connections are blocked for every test, with an explicit allowance
for loopback only. The mock endpoint in ``test_model_driver.py`` binds
``127.0.0.1``, so it keeps working, and anything aimed at the internet fails
immediately with a clear message instead of quietly reaching a vendor.
"""

from __future__ import annotations

import socket

import pytest

#: Hosts a test may connect to. Loopback only.
ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


def _guard(address) -> None:
    """Refuse any target that is not loopback."""
    if isinstance(address, tuple) and address:
        host = str(address[0])
    else:  # unix socket path or similar
        host = str(address)
    if host in ALLOWED_HOSTS:
        return
    raise AssertionError(
        f"a test tried to connect to {host!r}. This suite must stay offline: "
        "point the driver at a local mock server on 127.0.0.1 instead."
    )


def _guarded_connect(self, address):
    _guard(address)
    return _REAL_CONNECT(self, address)


def _guarded_connect_ex(self, address):
    _guard(address)
    return _REAL_CONNECT_EX(self, address)


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Applied to every test in this package."""
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    yield


@pytest.fixture(autouse=True)
def clear_model_credentials(monkeypatch):
    """No test may inherit a real token from the developer's shell.

    A leaked credential would make a test depend on whoever's environment it
    ran in — and could spend real quota.
    """
    from agent_integration.model_driver import TOKEN_ENV_VARS

    for name in TOKEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield

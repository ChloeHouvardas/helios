"""Offline test isolation for helios-payables.

Blocks every non-loopback socket for the whole suite, mirroring
`platform/agent-integration/tests/conftest.py`. Two reasons this matters here:

  * the suite must prove the controls work with no network, no API key and no
    vendor endpoint, so a test that quietly reaches the internet is not testing
    what it claims to;
  * `funding.py` quotes rates through the platform's market package, which is
    pinned to fixture mode below. If anything ever reached for a live fetch, this
    guard turns it into a loud failure rather than a test whose result depends
    on whose shell it ran in.

Also puts the app directory on `sys.path`, so `import config` works the same way
the app itself does (flat modules, no package).
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

# Fixture mode regardless of the shell: a TUSHARE_TOKEN in the environment must
# not turn a control test into a live call.
os.environ["HELIOS_MARKET_MODE"] = "fixture"

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]

for _p in (
    REPO / "platform" / "audit-agent-builder" / "src",
    REPO / "platform" / "action-agent-builder" / "src",
    REPO / "platform" / "agent-integration" / "src",
    REPO / "platform" / "financial-tools",
    HERE,
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class OfflineViolation(AssertionError):
    """Raised when a test tries to open a non-loopback connection."""


_REAL_SOCKET = socket.socket
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", None, ""}


def _guarded_connect(self, address):  # noqa: ANN001, ANN202
    host = address[0] if isinstance(address, tuple) and address else None
    if host not in _ALLOWED_HOSTS:
        raise OfflineViolation(
            f"helios-payables tests must run offline; blocked a connection to "
            f"{host!r}. Rate quotes are pinned fixtures by design.")
    return _REAL_SOCKET.connect(self, address)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    yield


@pytest.fixture(autouse=True)
def _no_model_key(monkeypatch):
    """Force the scripted path: no test may depend on a configured API key."""
    for var in ("DASHSCOPE_API_KEY", "HELIOS_LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield

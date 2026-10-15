"""Offline test isolation for helios-cash-planner.

Blocks every non-loopback socket for the whole suite, pins market data to
fixture mode so a Tushare token in the shell cannot turn a control test into a
live call, and puts the app directory on `sys.path` so `import config` works the
way the app itself does.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

os.environ["HELIOS_MARKET_MODE"] = "fixture"

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]

for _p in (
    REPO / "platform" / "audit-agent-builder" / "src",
    REPO / "platform" / "action-agent-builder" / "src",
    REPO / "platform" / "agent-integration" / "src",
    REPO / "platform" / "financial-tools",
    REPO / "platform" / "deployment",
    HERE,
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class OfflineViolation(AssertionError):
    """Raised when a test tries to open a non-loopback connection."""


_real_connect = socket.socket.connect


def _guarded_connect(self, address):  # type: ignore[no-untyped-def]
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise OfflineViolation(f"test attempted a network connection to {host!r}")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    yield

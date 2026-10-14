"""Phase 0: the tool service boundary (specs/financial-tools-service.md §5).

One client interface, two interchangeable ways to reach the tools:

  - `InProcessToolClient` — direct calls. Tests and local development stay
    offline and instant.
  - `HttpToolClient` + `ToolServer` — across a process/container boundary.

The point of the split is **where trust material lives**. Only the server holds
the signing secret; a client can never mint an authorization, so an app cannot
grant itself permission (§4 rule 1, §3 "never crosses"). Fail closed: if the
service is unreachable, a side-effecting call does not proceed (§4 rule 3).

Stdlib only — `http.server`, mirroring the transport pattern the audit package
already uses, so both agents are decoupled the same way.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Protocol, runtime_checkable
from urllib.error import URLError
from urllib.request import Request, urlopen

from .contracts import ToolRegistry
from .intent_verification import VerificationError

#: Environment variable the server reads its signing secret from. Never
#: hard-coded, never returned to a client, never logged (specs/tech-stack.md).
SECRET_ENV = "HELIOS_INTENT_SECRET"


class ServiceUnavailable(Exception):
    """Raised when the tool service cannot be reached. Callers fail closed."""


@runtime_checkable
class ToolClient(Protocol):
    """The boundary both agents code against."""

    def catalog(self) -> Dict[str, Any]: ...
    def call(self, name: str, **kwargs: Any) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# In-process
# ---------------------------------------------------------------------------

@dataclass
class InProcessToolClient:
    """Direct calls into a local bundle. Same interface as the HTTP client."""

    bundle: Any  # fin_tools.Bundle

    def catalog(self) -> Dict[str, Any]:
        return {"transport": "in-process", "tools": self.bundle.registry.catalog()}

    def call(self, name: str, **kwargs: Any) -> Dict[str, Any]:
        return _wrap(name, lambda: self.bundle.registry.call(name, **kwargs))


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------

def make_handler(bundle: Any, *, secret: Optional[bytes] = None) -> type:
    """Build a request handler bound to a bundle.

    The secret is resolved once here — server-side only. A client asking for
    `intents.confirm` still gets an auth code back (that is the human's
    confirmation), but the *material* that makes codes unforgeable never leaves.
    """
    resolved = secret if secret is not None else (
        os.environ.get(SECRET_ENV, "").encode() or None)
    if resolved:
        bundle.intents.secret = resolved

    class ToolHandler(BaseHTTPRequestHandler):
        server_version = "HeliosFinancialTools/0.1"

        def _send(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") == "/tools":
                self._send(200, {"transport": "http",
                                 "tools": bundle.registry.catalog()})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/call":
                self._send(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length) or b"{}")
                name = req["tool"]
                args = req.get("args") or {}
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self._send(400, {"ok": False, "error": f"bad request: {exc}"})
                return
            self._send(200, _wrap(name, lambda: bundle.registry.call(name, **args)))

        def log_message(self, *_args: Any) -> None:
            pass  # keep the console clean; the action log is the audit trail

    return ToolHandler


class ToolServer:
    """Threaded HTTP server on an ephemeral port unless one is given."""

    def __init__(self, bundle: Any, *, host: str = "127.0.0.1", port: int = 0,
                 secret: Optional[bytes] = None) -> None:
        self._httpd = ThreadingHTTPServer(
            (host, port), make_handler(bundle, secret=secret))
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "ToolServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------

class HttpToolClient:
    """Reaches the tools across a boundary. Holds no trust material."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds

    def _get(self, path: str) -> Dict[str, Any]:
        return self._request(Request(f"{self.base_url}{path}", method="GET"))

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode()
        req = Request(f"{self.base_url}{path}", data=body, method="POST",
                      headers={"Content-Type": "application/json"})
        return self._request(req)

    def _request(self, req: Request) -> Dict[str, Any]:
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except URLError as exc:
            # Fail closed: an unreachable service is not a licence to proceed.
            raise ServiceUnavailable(f"tool service unreachable: {exc.reason}") from exc
        except socket.timeout as exc:
            raise ServiceUnavailable("tool service timed out") from exc

    def catalog(self) -> Dict[str, Any]:
        return self._get("/tools")

    def call(self, name: str, **kwargs: Any) -> Dict[str, Any]:
        return self._post("/call", {"tool": name, "args": kwargs})


# ---------------------------------------------------------------------------
# Shared result envelope
# ---------------------------------------------------------------------------

def _wrap(name: str, fn: Any) -> Dict[str, Any]:
    """Uniform envelope so a client can tell 'tool said no' from 'wire broke'."""
    try:
        return {"ok": True, "tool": name, "result": fn()}
    except VerificationError as exc:
        return {"ok": False, "tool": name, "rejected": True, "error": str(exc)}
    except (KeyError, ValueError) as exc:
        return {"ok": False, "tool": name, "rejected": True,
                "error": str(exc).strip("'")}
    except Exception as exc:  # noqa: BLE001 — never leak a traceback to a caller
        return {"ok": False, "tool": name, "rejected": True,
                "error": f"{type(exc).__name__}: {exc}"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

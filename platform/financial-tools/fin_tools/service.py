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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contracts import ToolRegistry
from .intent_verification import VerificationError

#: Environment variable the server reads its signing secret from. Never
#: hard-coded, never returned to a client, never logged (specs/tech-stack.md).
SECRET_ENV = "HELIOS_INTENT_SECRET"

#: Environment variable for the HTTP execution secret. The deployment engine's
#: core container guards its routes with `Bearer <HELIOS_EXECUTION_SECRET>`; this
#: service adopts the same scheme so the engine can run it. Distinct from the
#: intent signing secret above: that one makes auth codes unforgeable, this one
#: authenticates the HTTP caller.
EXECUTION_SECRET_ENV = "HELIOS_EXECUTION_SECRET"


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

def _resolve_execution_secret(explicit: Optional[str]) -> Optional[str]:
    """The bearer secret to enforce, or None to leave the HTTP layer open.

    Explicit argument wins; else the environment. When neither is set the
    service keeps its historical open-on-localhost behaviour, so existing
    callers and tests are unaffected -- but a deployment that sets
    HELIOS_EXECUTION_SECRET gets every non-/health route authenticated.
    """
    if explicit is not None:
        return explicit or None
    return os.environ.get(EXECUTION_SECRET_ENV) or None


def make_handler(bundle: Any, *, secret: Optional[bytes] = None,
                 execution_secret: Optional[str] = None,
                 persistence: Optional[Any] = None) -> type:
    """Build a request handler bound to a bundle.

    The intent `secret` is resolved once here — server-side only. A client asking
    for `intents.confirm` still gets an auth code back (that is the human's
    confirmation), but the *material* that makes codes unforgeable never leaves.

    `execution_secret`, when set, makes every route except `GET /health` require
    an `Authorization: Bearer <secret>` header. The comparison is constant-time so
    a caller cannot time their way to the secret. `persistence`, when given, is
    flushed after each mutating call so state survives a restart.
    """
    resolved = secret if secret is not None else (
        os.environ.get(SECRET_ENV, "").encode() or None)
    if resolved:
        bundle.intents.secret = resolved

    bearer = _resolve_execution_secret(execution_secret)
    import hmac as _hmac

    def _authorized(header_value: Optional[str]) -> bool:
        if bearer is None:
            return True                     # open mode: nothing configured
        if not header_value:
            return False
        scheme, _, token = header_value.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        return _hmac.compare_digest(token, bearer)

    class ToolHandler(BaseHTTPRequestHandler):
        server_version = "HeliosFinancialTools/0.2"

        def _send(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            """True if the request may proceed; sends 401 and returns False if not."""
            if _authorized(self.headers.get("Authorization")):
                return True
            # 401 with a WWW-Authenticate hint, per RFC 6750. The message names
            # the scheme and the env var, never the secret itself.
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"ok": False, "error": "missing or invalid bearer "
                               f"token; send Authorization: Bearer <{EXECUTION_SECRET_ENV}>"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.rstrip("/")
            # /health is always open: the orchestrator probes it before it has
            # any secret to present, and it leaks nothing but liveness.
            if path == "/health":
                self._send(200, {"status": "ok", "role": "helios-financial-tools",
                                 "tools": len(bundle.registry.list())})
                return
            if not self._authed():
                return
            if path == "/tools":
                self._send(200, {"transport": "http",
                                 "tools": bundle.registry.catalog()})
            elif path == "/kg/nodes":
                self._send(200, {"nodes": list(bundle.graph.nodes.values())})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authed():
                return
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
            envelope = _wrap(name, lambda: bundle.registry.call(name, **args))
            # Persist after a call that changed state, so a restart resumes.
            if persistence is not None and envelope.get("ok"):
                try:
                    persistence.save(bundle)
                except Exception:            # noqa: BLE001 - persistence is best-effort
                    pass                    # a failed flush must not fail the call
            self._send(200, envelope)

        def log_message(self, *_args: Any) -> None:
            pass  # keep the console clean; the action log is the audit trail

    return ToolHandler


class ToolServer:
    """Threaded HTTP server on an ephemeral port unless one is given."""

    def __init__(self, bundle: Any, *, host: str = "127.0.0.1", port: int = 0,
                 secret: Optional[bytes] = None,
                 execution_secret: Optional[str] = None,
                 persistence: Optional[Any] = None) -> None:
        self._httpd = ThreadingHTTPServer(
            (host, port), make_handler(bundle, secret=secret,
                                       execution_secret=execution_secret,
                                       persistence=persistence))
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
    """Reaches the tools across a boundary. Holds no signing material.

    `execution_secret`, when given, is sent as a bearer token so the client can
    reach a server that enforces `HELIOS_EXECUTION_SECRET`. This is the HTTP
    caller's credential, NOT the intent signing secret — a client with it still
    cannot forge an auth code, because that material stays server-side.
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0,
                 execution_secret: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._execution_secret = execution_secret

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = dict(extra or {})
        if self._execution_secret:
            headers["Authorization"] = f"Bearer {self._execution_secret}"
        return headers

    def _get(self, path: str) -> Dict[str, Any]:
        return self._request(Request(f"{self.base_url}{path}", method="GET",
                                     headers=self._headers()))

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode()
        req = Request(f"{self.base_url}{path}", data=body, method="POST",
                      headers=self._headers({"Content-Type": "application/json"}))
        return self._request(req)

    def _request(self, req: Request) -> Dict[str, Any]:
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as exc:
            # A 401/403 is an auth failure, not an unreachable service: surface
            # it distinctly so the caller knows to present a token rather than
            # retrying a dead endpoint.
            if exc.code in (401, 403):
                raise PermissionError(
                    f"tool service rejected the request ({exc.code}); supply the "
                    f"correct {EXECUTION_SECRET_ENV} as execution_secret") from exc
            raise ServiceUnavailable(f"tool service error: HTTP {exc.code}") from exc
        except URLError as exc:
            # Fail closed: an unreachable service is not a licence to proceed.
            raise ServiceUnavailable(f"tool service unreachable: {exc.reason}") from exc
        except socket.timeout as exc:
            raise ServiceUnavailable("tool service timed out") from exc

    def health(self) -> Dict[str, Any]:
        return self._get("/health")

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

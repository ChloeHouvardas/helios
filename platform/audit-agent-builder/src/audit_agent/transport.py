"""Transports: how an action agent talks to a standalone audit agent.

The audit agent shares no code with the action agent at runtime. They
exchange the models in `contracts.py`. This module provides the wires, and
each wire is interchangeable because both ends speak the same protocol:

- `InProcessTransport` — one process, direct calls. Tests and single-app
  demos.
- `FileTransport` — two processes on one host, JSON files in a spool
  directory. No dependencies, works across containers sharing a volume,
  and easy to inspect by hand during a demo.
- `HttpTransport` — two services, stdlib HTTP only. This is the deployment
  shape: the audit agent runs beside the app as its own service.

Why this matters for the architecture rather than being plumbing: because
the action agent only ever sees `AuditClient`, swapping in-process
auditing for an independently deployed auditor is a configuration change,
not a refactor. That is what makes the auditor separable — and separability
is what lets it be trusted, since an app cannot modify the code that
audits it.

Every transport is fail-closed. A request that cannot be delivered, parsed
or answered yields a deny with the transport failure recorded as the
reason, never a default allow.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .audit_log import AuditLog, EventType
from .contracts import (
    ActionDecision,
    ActionRequest,
    AuthorizationRequest,
    Decision,
    Finding,
    HoldRecord,
    Severity,
    TaskAuthorization,
)
from .errors import TransportError


def _transport_deny(
    request_id: str,
    app_id: str,
    agent_id: str,
    tool: str,
    task_type: str,
    message: str,
) -> ActionDecision:
    """A deny produced by the transport, not by policy.

    Distinguishable in the log by its rule id, so an operator can tell
    "the auditor refused this" from "the auditor was unreachable" — the
    second is an outage to fix, the first is a control working.
    """
    return ActionDecision(
        request_id=request_id,
        authorization_id=None,
        app_id=app_id,
        agent_id=agent_id,
        tool=tool,
        task_type=task_type,
        decision=Decision.DENY,
        findings=[
            Finding(
                rule_id="transport.unavailable",
                effect="deny",
                severity=Severity.CRITICAL,
                message=message,
                source="baseline",
            )
        ],
        reason=message,
    )


@runtime_checkable
class AuditClient(Protocol):
    """What an action agent sees. This is the whole client-side surface.

    An action agent depends only on these four methods, so it cannot reach
    into audit internals — it cannot read policy, cannot forge a grant, and
    cannot resolve its own facts.
    """

    def authorize(self, request: AuthorizationRequest) -> TaskAuthorization:
        """Ask which tools may be used for a task."""
        ...

    def adjudicate(self, request: ActionRequest) -> ActionDecision:
        """Ask whether one concrete call may proceed."""
        ...

    def capabilities(self) -> dict[str, Any]:
        """Read the task types and tools the auditor governs."""
        ...

    def resolve_hold(
        self, hold_id: str, *, approve: bool, decided_by: str, note: str = ""
    ) -> HoldRecord:
        """Record an owner's decision so a held agent can resume."""
        ...


class InProcessTransport:
    """Direct calls into a local `AuditAgent`.

    Still goes through the client interface, so code written against it
    behaves identically when the auditor moves out of process.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def authorize(self, request: AuthorizationRequest) -> TaskAuthorization:
        return self.agent.authorize(request)

    def adjudicate(self, request: ActionRequest) -> ActionDecision:
        return self.agent.adjudicate(request)

    def capabilities(self) -> dict[str, Any]:
        return self.agent.describe_capabilities()

    def pending_holds(self, principal: str | None = None) -> list[HoldRecord]:
        return self.agent.pending_holds(principal)

    def resolve_hold(
        self, hold_id: str, *, approve: bool, decided_by: str, note: str = ""
    ) -> HoldRecord:
        return self.agent.resolve_hold(
            hold_id, approve=approve, decided_by=decided_by, note=note
        )


# --- File spool ----------------------------------------------------------


class FileTransportServer:
    """Watches a spool directory and answers requests left there.

    Protocol: the client writes `<id>.request.json`, the server writes
    `<id>.response.json`. Atomic writes via a temp file plus rename, so a
    reader never sees a half-written document.

    Single-threaded by design: one auditor process owns the spool, which
    keeps the action log's hash chain linear. Two auditors on one spool
    would interleave log entries and break the chain.
    """

    def __init__(self, agent: Any, spool: str | Path) -> None:
        self.agent = agent
        self.spool = Path(spool)
        (self.spool / "requests").mkdir(parents=True, exist_ok=True)
        (self.spool / "responses").mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- Paths ------------------------------------------------------------

    @property
    def request_dir(self) -> Path:
        return self.spool / "requests"

    @property
    def response_dir(self) -> Path:
        return self.spool / "responses"

    # -- Handling ---------------------------------------------------------

    def handle_one(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one decoded request to the audit agent."""
        kind = payload.get("kind")
        try:
            if kind == "authorize":
                result: Any = self.agent.authorize(
                    AuthorizationRequest.model_validate(payload["request"])
                )
            elif kind == "adjudicate":
                result = self.agent.adjudicate(
                    ActionRequest.model_validate(payload["request"])
                )
            elif kind == "capabilities":
                return {"ok": True, "result": self.agent.describe_capabilities()}
            elif kind == "pending_holds":
                holds = self.agent.pending_holds(payload.get("principal"))
                return {
                    "ok": True,
                    "result": [h.model_dump(mode="json") for h in holds],
                }
            elif kind == "resolve_hold":
                result = self.agent.resolve_hold(
                    payload["hold_id"],
                    approve=bool(payload["approve"]),
                    decided_by=str(payload["decided_by"]),
                    note=str(payload.get("note", "")),
                )
            else:
                return {"ok": False, "error": f"unknown request kind '{kind}'"}
        except Exception as exc:
            # A raised error becomes an error response, never a crash of the
            # server loop and never a silent allow.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "result": result.model_dump(mode="json")}

    def process_pending(self) -> int:
        """Answer every request currently in the spool. Returns the count."""
        handled = 0
        for path in sorted(self.request_dir.glob("*.request.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Unreadable request: answer with an error so the client
                # stops waiting, then quarantine the file for inspection.
                self._write_response(
                    path.stem.replace(".request", ""),
                    {"ok": False, "error": "request file was unreadable"},
                )
                self._quarantine(path)
                handled += 1
                continue
            response = self.handle_one(payload)
            self._write_response(
                path.stem.replace(".request", ""), response
            )
            path.unlink(missing_ok=True)
            handled += 1
        return handled

    def _write_response(self, correlation_id: str, payload: dict[str, Any]) -> None:
        target = self.response_dir / f"{correlation_id}.response.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
        os.replace(temp, target)

    def _quarantine(self, path: Path) -> None:
        bad = self.spool / "quarantine"
        bad.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(path, bad / path.name)
        except OSError:
            path.unlink(missing_ok=True)

    # -- Lifecycle --------------------------------------------------------

    def run(self, poll_interval: float = 0.05, max_seconds: float | None = None) -> None:
        """Serve until stopped, or until `max_seconds` elapses."""
        deadline = None if max_seconds is None else time.monotonic() + max_seconds
        while not self._stop.is_set():
            self.process_pending()
            if deadline is not None and time.monotonic() >= deadline:
                break
            self._stop.wait(poll_interval)
        self.process_pending()

    def start(self, poll_interval: float = 0.05) -> "FileTransportServer":
        """Serve on a background thread. Returns self for chaining."""
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run,
            kwargs={"poll_interval": poll_interval},
            daemon=True,
            name="audit-file-transport",
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


class FileTransport:
    """Client end of the file spool."""

    def __init__(
        self,
        spool: str | Path,
        *,
        timeout_seconds: float = 10.0,
        poll_interval: float = 0.02,
    ) -> None:
        self.spool = Path(spool)
        self.request_dir = self.spool / "requests"
        self.response_dir = self.spool / "responses"
        self.request_dir.mkdir(parents=True, exist_ok=True)
        self.response_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval

    def _call(self, payload: dict[str, Any]) -> Any:
        correlation_id = payload["id"]
        request_path = self.request_dir / f"{correlation_id}.request.json"
        response_path = self.response_dir / f"{correlation_id}.response.json"
        temp = request_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(temp, request_path)

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    raw = response_path.read_text(encoding="utf-8")
                except OSError:
                    time.sleep(self.poll_interval)
                    continue
                response_path.unlink(missing_ok=True)
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise TransportError(f"corrupt response {correlation_id}: {exc}") from exc
                if not data.get("ok", False):
                    raise TransportError(
                        f"audit agent rejected the request: {data.get('error')}"
                    )
                return data["result"]
            time.sleep(self.poll_interval)
        raise TransportError(
            f"audit agent did not respond within {self.timeout_seconds}s "
            f"(request {correlation_id})"
        )

    def _payload(self, kind: str, **fields: Any) -> dict[str, Any]:
        return {"id": f"req_{uuid.uuid4().hex[:16]}", "kind": kind, **fields}

    # -- AuditClient ------------------------------------------------------

    def authorize(self, request: AuthorizationRequest) -> TaskAuthorization:
        result = self._call(
            self._payload("authorize", request=request.model_dump(mode="json"))
        )
        return TaskAuthorization.model_validate(result)

    def adjudicate(self, request: ActionRequest) -> ActionDecision:
        try:
            result = self._call(
                self._payload("adjudicate", request=request.model_dump(mode="json"))
            )
        except TransportError as exc:
            return _transport_deny(
                request.request_id,
                request.app_id,
                request.agent_id,
                request.tool,
                request.task_type,
                f"the audit agent could not be reached: {exc}",
            )
        return ActionDecision.model_validate(result)

    def capabilities(self) -> dict[str, Any]:
        return self._call(self._payload("capabilities"))

    def pending_holds(self, principal: str | None = None) -> list[HoldRecord]:
        result = self._call(self._payload("pending_holds", principal=principal))
        return [HoldRecord.model_validate(item) for item in result]

    def resolve_hold(
        self, hold_id: str, *, approve: bool, decided_by: str, note: str = ""
    ) -> HoldRecord:
        result = self._call(
            self._payload(
                "resolve_hold",
                hold_id=hold_id,
                approve=approve,
                decided_by=decided_by,
                note=note,
            )
        )
        return HoldRecord.model_validate(result)


# --- HTTP ----------------------------------------------------------------


def make_http_handler(agent: Any) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to one audit agent.

    A closure rather than a class attribute so the server can be
    instantiated per agent without global state.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "HeliosAuditAgent/0.1"
        #: Silence per-request stderr logging; the audit log is the record.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return None

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path.rstrip("/") in ("/capabilities", "/v1/capabilities"):
                self._send(200, {"ok": True, "result": agent.describe_capabilities()})
                return
            if self.path.rstrip("/") in ("/health", "/v1/health"):
                self._send(200, {"ok": True, "result": {"status": "healthy"}})
                return
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            route = self.path.rstrip("/")
            length = int(self.headers.get("Content-Length") or 0)
            # A size cap: an unbounded read is a trivial way to exhaust an
            # auditor that every app in the platform depends on.
            if length > 1_000_000:
                self._send(413, {"ok": False, "error": "request body too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send(400, {"ok": False, "error": f"invalid JSON: {exc}"})
                return

            try:
                if route.endswith("/authorize"):
                    result = agent.authorize(
                        AuthorizationRequest.model_validate(payload)
                    ).model_dump(mode="json")
                elif route.endswith("/adjudicate"):
                    result = agent.adjudicate(
                        ActionRequest.model_validate(payload)
                    ).model_dump(mode="json")
                elif route.endswith("/holds"):
                    result = [
                        h.model_dump(mode="json")
                        for h in agent.pending_holds(payload.get("principal"))
                    ]
                elif "/holds/" in route and route.endswith(("/approve", "/reject")):
                    hold_id = route.rsplit("/", 2)[-2]
                    approve = route.endswith("/approve")
                    result = agent.resolve_hold(
                        hold_id,
                        approve=approve,
                        decided_by=str(payload.get("decided_by", "")),
                        note=str(payload.get("note", "")),
                    ).model_dump(mode="json")
                else:
                    self._send(404, {"ok": False, "error": "not found"})
                    return
            except Exception as exc:
                # 422 for a malformed request, 500 for an internal failure.
                # Neither ever becomes an implicit allow.
                status = 422 if "validation" in str(exc).lower() else 500
                self._send(status, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            self._send(200, {"ok": True, "result": result})

    return Handler


class HttpTransportServer:
    """Serves the audit agent over HTTP using only the standard library."""

    def __init__(self, agent: Any, host: str = "127.0.0.1", port: int = 0) -> None:
        self.agent = agent
        handler = make_http_handler(agent)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self.host, self.port = self._httpd.server_address[:2]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> "HttpTransportServer":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
                name="audit-http-transport",
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class HttpTransport:
    """Client end of the HTTP transport. Stdlib only, no external deps."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        body = (
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            if payload is not None
            else None
        )
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except (HTTPError, URLError, socket.timeout, OSError) as exc:
            raise TransportError(f"audit agent request failed: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TransportError(f"corrupt response from audit agent: {exc}") from exc
        if not data.get("ok", False):
            raise TransportError(f"audit agent error: {data.get('error')}")
        return data["result"]

    # -- AuditClient ------------------------------------------------------

    def authorize(self, request: AuthorizationRequest) -> TaskAuthorization:
        return TaskAuthorization.model_validate(
            self._request("POST", "/authorize", request.model_dump(mode="json"))
        )

    def adjudicate(self, request: ActionRequest) -> ActionDecision:
        try:
            result = self._request(
                "POST", "/adjudicate", request.model_dump(mode="json")
            )
        except TransportError as exc:
            return _transport_deny(
                request.request_id,
                request.app_id,
                request.agent_id,
                request.tool,
                request.task_type,
                f"the audit agent could not be reached: {exc}",
            )
        return ActionDecision.model_validate(result)

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/capabilities")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def pending_holds(self, principal: str | None = None) -> list[HoldRecord]:
        result = self._request("POST", "/holds", {"principal": principal})
        return [HoldRecord.model_validate(item) for item in result]

    def resolve_hold(
        self, hold_id: str, *, approve: bool, decided_by: str, note: str = ""
    ) -> HoldRecord:
        verb = "approve" if approve else "reject"
        return HoldRecord.model_validate(
            self._request(
                "POST",
                f"/holds/{hold_id}/{verb}",
                {"decided_by": decided_by, "note": note},
            )
        )


__all__ = [
    "AuditClient",
    "FileTransport",
    "FileTransportServer",
    "HttpTransport",
    "HttpTransportServer",
    "InProcessTransport",
]

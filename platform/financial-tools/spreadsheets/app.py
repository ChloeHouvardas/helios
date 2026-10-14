"""FastAPI service: the HTTP half of the Spreadsheet Tools.

Guide § Overview: apps call these tools "through the Helios SDK as
`helios.sheets.<tool>()`, or over HTTP at `POST /v1/sheets/<tool>`". Both entry
points share the same `Sheets` facade, so tenancy, permissions and audit logging
stay in one place.

Conventions implemented here:
* No credentials in apps — a caller passes `connection_id`; secrets stay in the
  Helios vault and never appear in a request body or a log line.
* Write calls accept `idempotency_key`; repeating a call returns the first result.
* Every error returns `{code, message, retryable}` with the mapped HTTP status.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import HeliosError, permission_denied, validation
from .filestore import redact_for_logs
from .jobs import JobRunner
from .models import DEFAULT_TARGET
from .qwen import QwenClient
from .tools import Sheets

# Tool names an app may list in `permissions.sheets`.
TOOLS = ("render_sheet", "import_sheet", "reconcile", "watch")


class CallBody(BaseModel):
    """Envelope for every `POST /v1/sheets/<tool>` call.

    `business_id` normally comes from the authenticated connection; it is
    accepted here so the dev server can exercise tenancy without auth.
    """

    business_id: str = "dev"
    app: str | None = None
    actor: str | None = None
    connection_id: str | None = None
    manifest: dict | None = Field(
        default=None, description="the app's permissions block")
    idempotency_key: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


def create_app(sheets: Sheets | None = None,
               jobs: JobRunner | None = None,
               base_url: str = "") -> FastAPI:
    """Build the Spreadsheet Tools service."""
    app = FastAPI(
        title="Helios Spreadsheet Tools",
        version="0.1.0",
        description="Live sheets, import, reconcile and watch over the "
                    "Helios knowledge graph. Sheets are views, never the "
                    "source of truth.")
    app.state.sheets = sheets or Sheets(base_url=base_url)
    app.state.jobs = jobs or JobRunner()

    @app.exception_handler(HeliosError)
    async def _helios_error(_request: Request, exc: HeliosError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.payload())

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals or credentials.
        return JSONResponse(status_code=500, content={
            "code": "internal_error",
            "message": "the spreadsheet tool hit an unexpected error",
            "retryable": True})

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "tools": list(TOOLS)}

    @app.get("/v1/sheets/tools")
    async def list_tools() -> dict:
        """Discovery: what an app may put in `permissions.sheets`."""
        return {"tools": list(TOOLS),
                "events": ["created", "status_changed", "overdue", "matched",
                           "match_suggested", "cell_edited",
                           "proposal_created", "proposal_resolved"]}

    def svc(body: CallBody):
        return app.state.sheets.for_business(body.business_id)

    @app.post("/v1/sheets/render_sheet")
    async def render_sheet(body: CallBody) -> dict:
        p = dict(body.params)
        return svc(body).render_sheet(
            query=p.get("query") or {},
            columns=p.get("columns") or [],
            editable_columns=p.get("editable_columns"),
            protected_columns=p.get("protected_columns"),
            access=p.get("access"),
            target=p.get("target", DEFAULT_TARGET),
            group_by=p.get("group_by"),
            sort=p.get("sort"),
            view_id=p.get("view_id"),
            idempotency_key=body.idempotency_key,
            manifest=body.manifest,
            actor=body.actor)

    @app.post("/v1/sheets/import_sheet")
    async def import_sheet(body: CallBody) -> dict:
        p = dict(body.params)
        if not p.get("file_id"):
            raise validation("file_id is required")
        return svc(body).import_sheet(
            file_id=p["file_id"],
            record_type=p.get("record_type"),
            column_map=p.get("column_map"),
            dry_run=bool(p.get("dry_run", False)),
            archive_password=p.get("archive_password"),
            idempotency_key=body.idempotency_key,
            manifest=body.manifest,
            actor=body.actor)

    @app.post("/v1/sheets/reconcile")
    async def reconcile(body: CallBody) -> dict:
        p = dict(body.params)
        return svc(body).reconcile(
            scope=p.get("scope"),
            max_step=p.get("max_step", 5),
            amount_drift=p.get("amount_drift", 0),
            date_window_days=p.get("date_window_days", 7),
            dry_run=bool(p.get("dry_run", False)),
            idempotency_key=body.idempotency_key,
            manifest=body.manifest,
            actor=body.actor)

    @app.post("/v1/sheets/watch")
    async def watch(body: CallBody) -> dict:
        p = dict(body.params)
        return svc(body).watch(
            query=p.get("query") or {},
            events=p.get("events") or [],
            handler=p.get("handler") or "",
            manifest=body.manifest,
            actor=body.actor)

    @app.post("/v1/sheets/unwatch")
    async def unwatch(body: CallBody) -> dict:
        p = dict(body.params)
        return svc(body).unwatch(p.get("subscription_id") or "",
                                 body.manifest, body.actor)

    @app.post("/v1/sheets/resolve_match")
    async def resolve_match(body: CallBody) -> dict:
        p = dict(body.params)
        return svc(body).resolve_match(
            p.get("match_id") or "", p.get("decision") or "",
            body.actor or "user", body.manifest)

    @app.post("/v1/sheets/list_proposals")
    async def list_proposals(body: CallBody) -> dict:
        p = dict(body.params)
        return svc(body).list_proposals(p.get("view_id"), p.get("status"),
                                        body.manifest)

    @app.post("/v1/sheets/resolve_proposal")
    async def resolve_proposal(body: CallBody) -> dict:
        p = dict(body.params)
        return svc(body).resolve_proposal(
            p.get("proposal_id") or "", p.get("decision") or "",
            body.actor or "user", p.get("actor_kind", "user"), body.manifest)

    @app.post("/v1/sheets/write_cell")
    async def write_cell(body: CallBody) -> dict:
        p = dict(body.params)
        return svc(body).write_cell(
            p.get("view_id") or "", p.get("record_id") or "",
            p.get("field") or "", p.get("new_value"),
            body.actor or "user", p.get("actor_kind", "user"),
            p.get("reason", ""), body.manifest)

    @app.get("/v1/sheets/views/{view_id}")
    async def get_view(view_id: str, business_id: str = "dev",
                       role: str | None = None) -> dict:
        """The live sheet itself, with that role's access rule applied.

        Rows are masked on render; there is no parameter that unmasks them.
        """
        service = app.state.sheets.for_business(business_id)
        eng = service.engine
        if not hasattr(eng, "views"):
            raise validation(f"view '{view_id}' is not served by this engine")
        view = service.kg.by_id.get(view_id)
        if view is None:
            raise validation(f"unknown view_id '{view_id}'")
        return {"view_id": view_id, "rows": eng.get_rows(view_id, role)}

    @app.get("/v1/sheets/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        """Poll a long-running call."""
        return app.state.jobs.get(job_id)

    @app.get("/v1/sheets/audit")
    async def audit(business_id: str = "dev") -> dict:
        """Audit trail for the Audit Agent, secrets already stripped."""
        return {"entries": redact_for_logs(
            app.state.sheets.audit.for_business(business_id))}

    return app


app = create_app()

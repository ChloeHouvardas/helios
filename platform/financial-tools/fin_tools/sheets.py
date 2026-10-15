"""Statement reconciliation (`sheets.*`) over the imported `spreadsheets` package.

Why this family exists separately from `spreadsheet.*`: those three tools are
standalone stdlib CSV helpers. This one wraps the teammates' reconciliation engine
— import a bank or wallet statement, match its transactions against known
obligations, and render the result. That is the "which payment settles which
invoice?" question an SME bookkeeper does by hand, and §E of the objective asks
for it to be reachable from the registry like the other teammate packages.

Three properties are load-bearing and deliberate:

**Offline and model-free by default.** The package can call a Qwen model in three
places (column-mapping fallback, a reconcile suggestion step, and formula
generation from a description). This facade always injects `NullQwen`, so no tool
here can trigger a model call. Column mapping is resolved by the deterministic
`infer_mapping`; when it cannot decide, the tool reports `mapping_ambiguous` with
the headers it saw rather than guessing. `configured` is False under `NullQwen`,
so the reconcile suggestion step is skipped entirely rather than attempted.

**Integer fen throughout, no conversion.** Unlike `helios_forecast`, this package
already stores money as integer minor units, matching decision B.6. Amounts pass
through untouched — there is no float boundary here to get wrong.

**Tenant is explicit.** Every record carries a `business_id` and the filestore
refuses cross-tenant reads. `require_tenant` fails closed when the bundle has no
tenant, so a statement can never land in a shared default bucket.

Note the filestore is in-memory, so uploaded statement files do not survive a
restart even when `SqlitePersistence` is on — only the fin_tools `World` is
snapshotted. Records imported into the knowledge graph have the same lifetime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .contracts import ToolSpec, require_tenant

#: Tools in this family. Read-only ones are safe to call without a gate.
FAMILY = "sheets"

#: Record types the imported engine will accept.
RECORD_TYPES = ("Obligation", "Transaction", "Counterparty")

#: Output targets that work without live spreadsheet credentials. The package's
#: own default is "feishu", which is a live target; this facade defaults to a file
#: target so nothing silently depends on credentials being present.
OFFLINE_TARGETS = ("csv", "xlsx")


#: Query keys the engine's matcher understands beyond real record fields.
_QUERY_PSEUDO_KEYS = frozenset({"type", "record_type"})

#: Cache of every filterable field name across the record models, derived from
#: the models themselves so this cannot drift when a field is added upstream.
_query_field_names: Optional[frozenset] = None


def _valid_query_keys() -> frozenset:
    """Every key a sheet query may use: the pseudo-keys plus all record fields.

    Derived from the pydantic models on first use. The engine's matcher does
    `dump.get(key)` for any key that is not `type` or `status`, so a key that is
    not a real field silently matches nothing rather than raising — which is
    precisely the failure worth preventing here.
    """
    global _query_field_names
    if _query_field_names is None:
        from spreadsheets.tools import RECORD_TYPES as MODELS

        names: set = set()
        for model in MODELS.values():
            names.update(getattr(model, "model_fields", {}))
        _query_field_names = frozenset(names) | _QUERY_PSEUDO_KEYS
    return _query_field_names


def _normalise_query(query: Any) -> Dict[str, Any]:
    """Validate a sheet query and accept `record_type` as an alias for `type`.

    The engine filters on `query["type"]`, but every other tool in this family
    calls the same concept `record_type`. Passing the wrong one is not an error
    to the engine — it silently matches nothing and renders a sheet with zero
    rows, which reads as "this tenant has no obligations" rather than "your query
    was misspelled". That is the kind of quiet wrong answer a financial tool must
    not give, so an unknown key or an unknown type is refused with the field
    named and the fix spelled out.
    """
    if not isinstance(query, dict):
        raise ValueError(
            f"query must be an object like {{'record_type': 'Obligation'}}, got "
            f"{type(query).__name__}")
    normalised = dict(query)
    if "record_type" in normalised and "type" not in normalised:
        normalised["type"] = normalised.pop("record_type")
    elif "record_type" in normalised:
        # Both present: keep them consistent rather than guessing which wins.
        if normalised["record_type"] != normalised["type"]:
            raise ValueError(
                f"query has both 'record_type' ({normalised['record_type']!r}) "
                f"and 'type' ({normalised['type']!r}) with different values; "
                f"pass only one of them.")
        normalised.pop("record_type")
    rtype = normalised.get("type")
    if rtype is not None and rtype not in RECORD_TYPES:
        raise ValueError(
            f"query type must be one of {list(RECORD_TYPES)} or omitted to "
            f"search every type, got {rtype!r}")
    unknown = set(normalised) - _valid_query_keys()
    if unknown:
        raise ValueError(
            f"query has unknown key(s) {sorted(unknown)}; the record type key is "
            f"'type' (alias 'record_type') and any other key must be a real "
            f"record field such as 'status', 'counterparty' or 'direction'. A "
            f"misspelled key matches nothing and would silently render an empty "
            f"sheet rather than raising.")
    return normalised


#: The engine speaks verbs ("confirm", "approve"); callers and models often write
#: the past participle ("confirmed", "approved"). Both are accepted and mapped to
#: what the engine expects, because rejecting a correct decision on tense alone
#: wastes a turn and the error would look like a real refusal.
_DECISION_ALIASES = {
    "confirmed": "confirm", "approving": "approve", "approved": "approve",
    "rejecting": "reject", "rejected": "reject", "declined": "reject",
    "deny": "reject", "denied": "reject",
}


def _decision(decision: Any, allowed: tuple) -> str:
    """Normalise a decision string to one the engine accepts, or refuse.

    The error names the field, the values that work, and what was given, so a
    caller can fix it in one pass.
    """
    if not isinstance(decision, str):
        raise ValueError(
            f"decision must be one of {list(allowed)}, got {decision!r} "
            f"({type(decision).__name__})")
    value = decision.strip().lower()
    value = _DECISION_ALIASES.get(value, value)
    if value not in allowed:
        raise ValueError(
            f"decision must be one of {list(allowed)}, got {decision!r}")
    return value


def _translate(exc: Any) -> ValueError:
    """Turn a `HeliosError` into a model-actionable `ValueError`.

    Same shape `fin_tools.collect` uses, so a caller sees one error convention
    across both teammate-backed families. The service envelope already marks a
    raised ValueError as a rejection.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if code:
        return ValueError(f"[{code}] {message}")
    return ValueError(message)


def _guard(fn, *args, **kwargs):
    """Call `fn`, converting package errors into actionable rejections."""
    try:
        return fn(*args, **kwargs)
    except ValueError:
        raise
    except Exception as exc:                      # noqa: BLE001 - package errors
        raise _translate(exc) from exc


@dataclass
class Sheets:
    """Facade over one tenant's reconciliation service, bound to a bundle.

    The service is built lazily so importing this module needs no pydantic. Pass
    `service` to inject one (seeded records, a fake engine) in tests.
    """

    tenant: Optional[str]
    _service: Optional[Any] = field(default=None, repr=False)

    @property
    def service(self) -> Any:
        """The tenant-scoped `SheetsService`, constructed on first use.

        `NullQwen` is injected unconditionally: these tools must not reach a
        model, and must not read `QWEN_API_KEY` out of the environment.

        The knowledge graph's `proposal_sink` is wired here because the imported
        package never wires it. Without a sink, approving a proposal flips its
        status to "approved" and applies nothing — the record keeps its old value
        while the UI reports success. For a protected column (money, dates, payee)
        that is a silently wrong financial answer, so the sink is what makes
        "approved" mean "applied". See `_apply_approved_proposal`.
        """
        if self._service is None:
            from spreadsheets.qwen import NullQwen
            from spreadsheets.tools import Sheets as TenantSheets

            self._service = TenantSheets(qwen=NullQwen()).for_business(
                require_tenant(self, tool=f"{FAMILY}.*"))
            self._service.kg.proposal_sink = self._apply_approved_proposal
        return self._service

    def _apply_approved_proposal(self, proposal: Any) -> None:
        """Write an approved proposal's value into the record it targets.

        Runs only from `resolve_proposal` after the engine's own checks have
        passed — including "an agent may not approve a proposal that needs a
        human" — so this does not bypass the control, it completes it. The write
        mirrors what `write_cell` does for an unprotected column, and emits
        `cell_edited` so any subscription refreshes.

        Deliberately tolerant of a missing record: the proposal is already marked
        approved by the engine before this runs, and raising here would leave the
        caller with an error for a decision that succeeded. There is nothing left
        to apply to, so it is a no-op.
        """
        service = self._service
        if service is None:
            return
        kg = service.kg
        record = kg.by_id.get(proposal.record_id)
        if record is None:
            return
        previous = record.model_dump()
        setattr(record, proposal.field, proposal.new_value)
        kg.emit("cell_edited", record, previous)

    # -- reads -------------------------------------------------------------

    def records(self, record_type: str, limit: int = 200) -> Dict[str, Any]:
        """List one tenant's records of a type. Read-only."""
        if record_type not in RECORD_TYPES:
            raise ValueError(
                f"record_type must be one of {list(RECORD_TYPES)}, got "
                f"{record_type!r}")
        svc = self.service
        from spreadsheets.tools import RECORD_TYPES as MODELS

        out = _guard(svc.kg.list_records, MODELS[record_type], limit=limit)
        return {
            "record_type": record_type,
            "count": len(out.get("items", [])),
            "items": [_record_dict(r) for r in out.get("items", [])],
            "cursor": out.get("cursor"),
        }

    def list_proposals(self, view_id: Optional[str] = None,
                       status: Optional[str] = None) -> Dict[str, Any]:
        """Pending (or filtered) edit proposals. Read-only.

        A proposal is the engine's answer to "an agent wanted to change a
        protected column": money, dates and payee fields cannot be written
        directly, so the change is queued for a human. Reading that queue must
        not itself be gated.
        """
        return _guard(self.service.list_proposals, view_id=view_id, status=status)

    # -- writes ------------------------------------------------------------

    def upload(self, filename: str, content: str,
               encoding: str = "utf-8") -> Dict[str, Any]:
        """Put a statement file into the tenant's filestore.

        Takes text rather than bytes because the tool schema is flat and a model
        cannot produce a byte string. `encoding` covers the GB-family encodings
        real Chinese bank exports use.

        Side-effecting: it writes state another call can read. It does not touch
        the ledger or send anything.
        """
        svc = self.service
        tenant = require_tenant(self, tool=f"{FAMILY}.upload")
        try:
            data = content.encode(encoding)
        except LookupError as exc:
            raise ValueError(
                f"encoding must be a Python codec name such as 'utf-8', "
                f"'utf-8-sig', 'gb18030' or 'gbk', got {encoding!r}") from exc
        stored = _guard(svc.filestore.put, filename, data, tenant)
        return {"file_id": stored.file_id, "filename": stored.filename,
                "size": stored.size, "sha": stored.sha}

    def import_sheet(self, file_id: str, record_type: str,
                     column_map: Optional[Dict[str, str]] = None,
                     dry_run: bool = False,
                     archive_password: Optional[str] = None,
                     idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Parse an uploaded statement into typed records.

        `dry_run=True` returns the inferred mapping and a preview without writing
        anything — the way to check a mapping before committing to it. When the
        headers are ambiguous the call fails with `mapping_ambiguous`; pass an
        explicit `column_map` (`{"金额": "amount", ...}`) to resolve it. This
        facade never falls back to a model call.
        """
        if record_type not in RECORD_TYPES:
            raise ValueError(
                f"record_type must be one of {list(RECORD_TYPES)}, got "
                f"{record_type!r}")
        return _guard(self.service.import_sheet, file_id,
                      record_type=record_type, column_map=column_map,
                      dry_run=dry_run, archive_password=archive_password,
                      idempotency_key=idempotency_key)

    def reconcile(self, max_step: int = 5, amount_drift: int = 0,
                  date_window_days: int = 7, dry_run: bool = False,
                  scope: Optional[Dict[str, Any]] = None,
                  idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Match transactions against obligations.

        The rule ladder is deterministic and ordered: exact reference first, then
        counterparty/alias, amount and date proximity, partial payments, and
        combinations. `applied` matches settle obligations immediately, which is
        why this is gated — it changes financial state. `suggested` matches do
        not; confirm or reject one with `sheets.resolve_match`.

        `max_step` caps how far down the ladder to go (1-5); a lower value is
        more conservative. `amount_drift` and `date_window_days` widen steps 3-4
        and default to exact. `dry_run=True` reports what would happen without
        applying it.

        With `NullQwen` the model-assisted step is skipped, so step 5 never fires.
        That is a deliberate loss of recall in exchange for a tool that behaves
        identically with and without credentials.
        """
        return _guard(self.service.reconcile, scope=scope, max_step=max_step,
                      amount_drift=amount_drift,
                      date_window_days=date_window_days, dry_run=dry_run,
                      idempotency_key=idempotency_key)

    def resolve_match(self, match_id: str, decision: str,
                      actor: str = "owner") -> Dict[str, Any]:
        """Confirm or reject a suggested match.

        `decision` is `"confirm"` or `"reject"` (the past participles
        `"confirmed"`/`"rejected"` are accepted as aliases). Confirming applies
        the payment, so this is gated. An agent may suggest; only an actor with
        authority should confirm.
        """
        return _guard(self.service.resolve_match, match_id,
                      _decision(decision, ("confirm", "reject")), actor=actor)

    def render_sheet(self, query: Dict[str, Any], columns: List[Any],
                     target: str = "csv",
                     editable_columns: Optional[List[str]] = None,
                     protected_columns: Optional[List[str]] = None,
                     group_by: Optional[str] = None,
                     sort: Optional[str] = None,
                     view_id: Optional[str] = None,
                     idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Materialise a query as a sheet view, defaulting to a file target.

        `query` filters records: `{"type": "Obligation", "status": ["open",
        "overdue"]}`. `record_type` is accepted as an alias for `type` so this
        family speaks one vocabulary; a key that is neither is refused, because
        the engine would otherwise match nothing and render a silently empty
        sheet. Omit `type` to search every record type.

        `target` defaults to `"csv"` rather than the package's own `"feishu"`,
        because a live target needs credentials this offline path does not have.
        Pass `"xlsx"` for a spreadsheet file; a live target is still accepted if
        an app has configured the engine for it.

        `rows` in the result is a **count**, not the row data; the rows go to the
        engine's view or exported file, addressed by `view_id` and `file_id`.

        The rendered view is **read-only unless `editable_columns` names a
        column** — the engine refuses a direct write to any field not in that
        list with `protected_column`. Money, date and payee columns should also
        be listed in `protected_columns`: a write to those creates a pending
        proposal instead of applying, which is the human checkpoint the engine
        exists to enforce. This facade preserves that behaviour rather than
        wrapping it away. See `sheets.list_proposals` and
        `sheets.resolve_proposal`.
        """
        return _guard(self.service.render_sheet, _normalise_query(query), columns,
                      editable_columns=editable_columns,
                      protected_columns=protected_columns, target=target,
                      group_by=group_by, sort=sort, view_id=view_id,
                      idempotency_key=idempotency_key)

    def write_cell(self, view_id: str, record_id: str, field: str,
                   new_value: Any, actor: str = "agent",
                   actor_kind: str = "agent", reason: str = "") -> Dict[str, Any]:
        """Edit one cell, or queue the edit as a proposal if the column is protected.

        This is the only way a proposal gets created, so without it
        `list_proposals` and `resolve_proposal` would have nothing to act on.

        The engine decides, not this facade: a column must appear in the view's
        `editable_columns` to be written at all (otherwise `protected_column`),
        and if it is also in `protected_columns` the write is queued as a pending
        proposal and returns `applied: False`. Money, dates and payee details are
        the columns an app should protect, so an agent cannot quietly restate an
        invoice amount — a human has to approve it.
        """
        return _guard(self.service.write_cell, view_id, record_id, field,
                      new_value, actor=actor, actor_kind=actor_kind, reason=reason)

    def resolve_proposal(self, proposal_id: str, decision: str,
                         resolved_by: str = "owner",
                         resolved_by_kind: str = "agent") -> Dict[str, Any]:
        """Approve or reject a queued edit proposal.

        `decision` is `"approve"` or `"reject"` (past participles accepted as
        aliases). Approving writes the new value into the record, so this is
        gated: it is the human decision the protected column exists to force.

        `resolved_by_kind` is passed through to the engine, which refuses to let
        an agent approve a proposal that `needs_human`. It defaults to `"agent"`
        rather than `"user"` on purpose: a caller must assert human authority
        explicitly, because defaulting the other way would let an agent approve
        its own edit to an invoice amount — the exact control the proposal queue
        exists to enforce.
        """
        return _guard(self.service.resolve_proposal, proposal_id,
                      _decision(decision, ("approve", "reject")),
                      resolved_by=resolved_by,
                      resolved_by_kind=resolved_by_kind)


def _record_dict(record: Any) -> Dict[str, Any]:
    """A pydantic record as a plain dict, datetimes as ISO strings."""
    try:
        return record.model_dump(mode="json")
    except AttributeError:                        # not pydantic
        return {k: v for k, v in vars(record).items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

_INPUT_SCHEMAS: Dict[str, Dict[str, str]] = {
    "records": {"record_type": "str", "limit": "int"},
    "list_proposals": {"view_id": "str|None", "status": "str|None"},
    "upload": {"filename": "str", "content": "str", "encoding": "str"},
    "import_sheet": {"file_id": "str", "record_type": "str",
                     "column_map": "dict|None", "dry_run": "bool",
                     "archive_password": "str|None",
                     "idempotency_key": "str|None"},
    "reconcile": {"max_step": "int", "amount_drift": "int",
                  "date_window_days": "int", "dry_run": "bool",
                  "scope": "dict|None", "idempotency_key": "str|None"},
    "resolve_match": {"match_id": "str", "decision": "str", "actor": "str"},
    "render_sheet": {"query": "dict", "columns": "list", "target": "str",
                     "editable_columns": "list|None",
                     "protected_columns": "list|None",
                     "group_by": "str|None", "sort": "str|None",
                     "view_id": "str|None", "idempotency_key": "str|None"},
    "write_cell": {"view_id": "str", "record_id": "str", "field": "str",
                   "new_value": "str|int|float|None", "actor": "str",
                   "actor_kind": "str", "reason": "str"},
    "resolve_proposal": {"proposal_id": "str", "decision": "str",
                         "resolved_by": "str", "resolved_by_kind": "str"},
}

#: Which of these move money or write records, and so must be gated.
_SIDE_EFFECTS = {
    "records": False,
    "list_proposals": False,
    "upload": True,
    "import_sheet": True,
    "reconcile": True,
    "resolve_match": True,
    "render_sheet": True,
    "write_cell": True,
    "resolve_proposal": True,
}

_DESCRIPTIONS = {
    "records": "List this tenant's reconciliation records of one type.",
    "list_proposals": "List queued edit proposals for protected columns.",
    "upload": "Upload a bank or wallet statement file for this tenant.",
    "import_sheet": "Parse an uploaded statement into typed records.",
    "reconcile": "Match transactions against obligations and apply the matches.",
    "resolve_match": "Confirm or reject one suggested match.",
    "render_sheet": "Materialise a query as a sheet view or exported file.",
    "write_cell": "Edit one cell; a protected column becomes a pending proposal.",
    "resolve_proposal": "Approve or reject one queued edit proposal.",
}


#: Tool name suffix -> the `Sheets` method that implements it.
_TOOLS = (
    ("records", "records"),
    ("list_proposals", "list_proposals"),
    ("upload", "upload"),
    ("import_sheet", "import_sheet"),
    ("reconcile", "reconcile"),
    ("resolve_match", "resolve_match"),
    ("render_sheet", "render_sheet"),
    ("write_cell", "write_cell"),
    ("resolve_proposal", "resolve_proposal"),
)


def register(registry: Any = None, sheets: Optional[Sheets] = None) -> None:
    """Register the `sheets.*` tools bound to one `Sheets` facade.

    `sheets` binds every closure to THIS bundle's tenant and service, so two
    worlds in one process keep separate reconciliation state — the same fix
    Task 1 applied to the payments, email and simulation singletons. Without a
    facade the tools refuse at call time rather than reaching a shared default.
    """
    if registry is None:
        from .contracts import REGISTRY

        registry = REGISTRY

    def _bound() -> Sheets:
        if sheets is None:
            raise ValueError(
                "sheets tools were registered without a bound Sheets facade; "
                "call build_world() (which supplies one) rather than register() "
                "directly.")
        return sheets

    for name, method_name in _TOOLS:
        def _handler(__method=method_name, **kwargs: Any) -> Any:
            return getattr(_bound(), __method)(**kwargs)

        registry.register(ToolSpec(
            name=f"{FAMILY}.{name}",
            description=_DESCRIPTIONS[name],
            input_schema=dict(_INPUT_SCHEMAS[name]),
            output_schema={"result": "dict"},
            side_effects=_SIDE_EFFECTS[name],
            handler=_handler,
            tags=(FAMILY,),
            tenant_scoped=True,
        ))

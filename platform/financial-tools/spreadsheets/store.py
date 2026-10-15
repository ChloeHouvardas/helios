"""In-memory knowledge graph store.

The KG holds the records; a sheet only displays them (guide § Overview: "sheets
are views, never the source of truth"). This store is a **development stub** for
the centrally hosted Shared State & Knowledge Graph service — it is deliberately
behind this one class so the real service can be swapped in without callers
changing.

Responsibilities: per-business record collections, idempotency, dedupe on
channel_ref / document hash, counterparty resolution, overdue derivation,
match application with trust levels, edit proposals, and at-least-once event
delivery to `watch` subscriptions.

Two invariants worth knowing before editing:

* **Reads never mutate.** `list_records` and `query_records` derive overdue
  status virtually. Persisting that transition and emitting `overdue` is an
  explicit `refresh_overdue()` sweep, called by the service layer.
* **Only trusted links move money.** A `Match` applies payment only when
  `applies_payment` is true (auto / confirmed / manual). `suggested` matches
  from reconcile steps 3-5 wait for a human `resolve_match`.
"""

from __future__ import annotations

import hashlib
import itertools
import secrets
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from .errors import (
    HeliosError,
    permission_denied,
    protected_column,
    rate_limited,
    validation,
)
from .models import (
    DEFAULT_PROTECTED_FIELDS,
    LIVE_TARGETS,
    Counterparty,
    EditProposal,
    Match,
    Obligation,
    Record,
    SheetView,
    Transaction,
)

PAGE_LIMIT = 200

# Guide § watch `events`. The four extra codes below are emitted internally and
# are also subscribable; they do not replace the guide's list.
GUIDE_EVENTS = frozenset({
    "created", "status_changed", "overdue", "matched",
    "match_suggested", "cell_edited", "proposal_created", "proposal_resolved",
})
EXTRA_EVENTS = frozenset({"needs_review", "payment_details_changed"})
VALID_EVENTS = GUIDE_EVENTS | EXTRA_EVENTS

# Statuses that can still transition to overdue.
OVERDUE_ELIGIBLE = frozenset({"open", "partial"})

_ids = itertools.count(1)


def new_id(prefix: str) -> str:
    return f"{prefix}_{next(_ids):06d}_{secrets.token_hex(3)}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def doc_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class Subscription:
    def __init__(self, query: dict, events: list[str], handler: str):
        self.subscription_id = new_id("sub")
        self.query = query
        self.events = set(events)
        self.handler = handler
        # at-least-once delivery: failures retry, then park
        self.pending: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []


class KnowledgeGraph:
    """Per-business in-memory KG with idempotency and event delivery."""

    WATCH_LIMIT = 100   # guide § limits: 100 active watch subscriptions
    VIEW_LIMIT = 50     # guide § limits: 50 live views
    IMPORT_ROW_LIMIT = 10_000  # guide § limits: 10,000 rows per import_sheet

    def __init__(self, business_id: str):
        self.business_id = business_id
        self.records: dict[str, list[Record]] = defaultdict(list)
        self.by_id: dict[str, Record] = {}
        self.by_channel_ref: dict[str, str] = {}
        self.by_doc_ref: dict[str, str] = {}
        self.idempotency: dict[str, dict] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.handler_registry: dict[str, Callable[[dict], None]] = {}
        # Intent Verification hook: (proposal) -> bool. Defaults to "agents
        # always need a person", i.e. nothing auto-approves.
        self.intent_verifier: Callable[[EditProposal], bool] | None = None
        # Applied by the tool that owns the record once a proposal is approved.
        self.proposal_sink: Callable[[EditProposal], None] | None = None

    # ------------------------------------------------------------- permissions

    def check_permission(self, tool: str, manifest: dict | None) -> None:
        """Scoped access — guide § Conventions.

        `manifest` is the app's `permissions.sheets` list. Absent manifest means
        an internal/dev caller and is allowed.
        """
        if manifest is None:
            return
        allowed = manifest.get("sheets") if isinstance(manifest, dict) else manifest
        if allowed is None or tool not in allowed:
            raise permission_denied(tool, list(allowed or []))

    # ------------------------------------------------------------------ writes

    def _store(self, record: Record) -> Record:
        self.records[type(record).__name__].append(record)
        self.by_id[record.id] = record
        return record

    def add_transaction(self, tx: Transaction, emit: bool = True) -> tuple[Transaction, bool]:
        """Returns (record, created). Dedupes on channel_ref."""
        existing_id = self.by_channel_ref.get(tx.channel_ref)
        if existing_id:
            return self.by_id[existing_id], False
        self._store(tx)
        self.by_channel_ref[tx.channel_ref] = tx.id
        if emit:
            self._emit("created", tx, None)
        return tx, True

    def add_obligation(self, ob: Obligation, doc_key: str | None = None,
                       emit: bool = True) -> tuple[Obligation, bool]:
        """Returns (record, created). Dedupes on document hash when doc_key given.

        Extraction never marks anything paid — status starts at 'open'.
        """
        existing_id = self.by_doc_ref.get(doc_key) if doc_key else None
        if existing_id:
            existing = self.by_id[existing_id]
            if isinstance(existing, Obligation):
                for field in ("amount", "due_at", "issued_at", "counterparty",
                              "invoice_number"):
                    new = getattr(ob, field)
                    if new is not None and new != getattr(existing, field):
                        setattr(existing, field, new)
                existing.confidence = ob.confidence
                existing.needs_review = ob.needs_review or existing.needs_review
            return existing, False
        self._store(ob)
        if doc_key:
            self.by_doc_ref[doc_key] = ob.id
        if emit:
            self._emit("created", ob, None)
            if ob.needs_review:
                self._emit("needs_review", ob, None)
        return ob, True

    def add_sheet_view(self, view: SheetView) -> SheetView:
        """Stores a live view, defaulting protected_columns per the guide.

        Guide § render_sheet: `protected_columns` "defaults to any money, date
        or payee column". Enforced against the 50 live view limit.
        """
        live = [v for v in self.records.get(SheetView.__name__, [])
                if isinstance(v, SheetView) and v.target in LIVE_TARGETS]
        is_new = view.id not in self.by_id
        if is_new and len(live) >= self.VIEW_LIMIT:
            raise rate_limited(
                f"live view limit ({self.VIEW_LIMIT}) reached for this business")
        if not view.protected_columns:
            view.protected_columns = self.default_protected_columns(view)
        if is_new:
            self._store(view)
        return view

    @staticmethod
    def default_protected_columns(view: SheetView) -> list[str]:
        """Editable columns that hold money, a date or a payee."""
        out = []
        for col in view.editable_columns:
            name = col if isinstance(col, str) else str(col.get("name", ""))
            if name in DEFAULT_PROTECTED_FIELDS:
                out.append(name)
        return out

    def add_counterparty(self, cp: Counterparty, emit: bool = True) -> Counterparty:
        resolved = self.resolve_counterparty(cp.name)
        if resolved is not None:
            for alias in cp.aliases:
                if alias not in resolved.aliases:
                    resolved.aliases.append(alias)
            resolved.payment_details.update(
                {k: v for k, v in cp.payment_details.items() if v != "****"})
            return resolved
        self._store(cp)
        if emit:
            self._emit("created", cp, None)
        return cp

    def set_counterparty_payment_details(self, cp: Counterparty,
                                         details: dict[str, str]) -> None:
        """Record known payment details; emits payment_details_changed on change.

        Values are masked on the way in — a full account number is never stored
        (guide § security rules: masked in every sheet, cannot be unmasked).
        """
        from .filestore import mask_account
        previous = cp.model_dump()
        cp.payment_details.update({k: mask_account(v) for k, v in details.items()})
        if previous != cp.model_dump():
            self._emit("payment_details_changed", cp, previous)

    # ------------------------------------------------------------- counterparties

    def resolve_counterparty(self, name: str) -> Counterparty | None:
        """One entity per real customer or supplier — match by name or alias."""
        for cp in self.records[Counterparty.__name__]:
            if isinstance(cp, Counterparty) and (name == cp.name or name in cp.aliases):
                return cp
        return None

    # ----------------------------------------------------- matches (reconcile)

    def add_match(self, match: Match, emit: bool = True) -> Match:
        """Records a link and, only if trusted, applies the payment.

        Validation happens **before** the record is stored so a rejected match
        never persists. A `suggested` match (reconcile steps 3-5) does not move
        obligation status; it waits for `resolve_match`.
        """
        ob = self.by_id.get(match.obligation_id)
        if not isinstance(ob, Obligation):
            raise validation(f"unknown obligation_id '{match.obligation_id}'")
        if match.amount_applied < 0:
            raise validation("amount_applied cannot be negative")
        if match.amount_applied == 0:
            raise validation("amount_applied must be greater than zero")
        tx = self.by_id.get(match.transaction_id)
        if tx is not None and match.amount_applied > getattr(tx, "amount", 0):
            raise validation(
                f"amount_applied {match.amount_applied} exceeds transaction "
                f"amount {tx.amount}")

        applied_total = self._applied_total(ob.id) + match.amount_applied \
            if match.applies_payment else self._applied_total(ob.id)
        if applied_total > ob.amount:
            raise validation(
                f"applied total {applied_total} exceeds obligation amount "
                f"{ob.amount}")

        # Validated: safe to persist.
        self._store(match)
        if not emit:
            return match

        if match.applies_payment:
            previous = ob.model_dump()
            self._recompute_status(ob)
            self._emit("matched", match, None)
            self._emit("status_changed", ob, previous)
        else:
            self._emit("match_suggested", match, None)
        return match

    def _applied_total(self, obligation_id: str) -> int:
        """Sum of trusted, non-rejected links against an obligation."""
        return sum(m.amount_applied for m in self.records[Match.__name__]
                   if isinstance(m, Match) and m.obligation_id == obligation_id
                   and m.applies_payment)

    def _recompute_status(self, ob: Obligation) -> None:
        """Derive status from trusted applied total; never over-apply."""
        paid = self._applied_total(ob.id)
        if paid <= 0:
            ob.status = "overdue" if self._is_past_due(ob) else "open"
        elif paid >= ob.amount:
            ob.status = "paid"
        else:
            ob.status = "partial"

    def resolve_match(self, match_id: str, decision: str,
                      actor: str = "user") -> Match:
        """Confirm or reject a suggested match — guide § "What happens after".

        `confirm` applies the payment. `reject` reverses any status change it
        caused. Nothing is deleted; the history stays for the Audit Agent.
        """
        m = self.by_id.get(match_id)
        if not isinstance(m, Match):
            raise validation(f"unknown match_id '{match_id}'")
        if decision not in ("confirm", "reject"):
            raise validation(f"decision must be 'confirm' or 'reject', got '{decision}'")

        ob = self.by_id.get(m.obligation_id)
        if not isinstance(ob, Obligation):
            raise validation(f"unknown obligation_id '{m.obligation_id}'")

        previous = ob.model_dump()
        if decision == "confirm":
            if m.is_rejected:
                raise validation(f"match '{match_id}' was already rejected")
            m.method = "confirmed"
            if self._applied_total(ob.id) > ob.amount:
                raise validation(
                    f"confirming would over-apply: {self._applied_total(ob.id)} "
                    f"> {ob.amount}")
            self._recompute_status(ob)
        else:
            m.meta["rejected"] = True
            m.meta["rejected_by"] = actor
            m.meta["rejected_at"] = now().isoformat()
            self._recompute_status(ob)

        if previous != ob.model_dump():
            self._emit("status_changed", ob, previous)
        return m

    # ------------------------------------------------------------ edit proposals

    def propose_edit(self, view_id: str, record_id: str, field: str,
                     new_value: Any, proposed_by: str,
                     proposed_by_kind: str = "user", reason: str = "") -> EditProposal:
        """Turn an edit to a protected column into an approval.

        The cell keeps its old value; the proposal shows as pending. Guide §
        "Edit proposals": an agent's proposal always needs a person to approve.
        """
        view = self.by_id.get(view_id)
        if not isinstance(view, SheetView):
            raise validation(f"unknown view_id '{view_id}'")
        rec = self.by_id.get(record_id)
        if rec is None:
            raise validation(f"unknown record_id '{record_id}'")
        if not hasattr(rec, field):
            raise validation(f"'{field}' is not a field on {type(rec).__name__}")
        if proposed_by_kind not in ("user", "agent"):
            raise validation(
                f"proposed_by_kind must be 'user' or 'agent', got '{proposed_by_kind}'")

        proposal = EditProposal(
            id=new_id("prop"), business_id=self.business_id, source="sheets",
            created_at=now(), view_id=view_id, record_id=record_id, field=field,
            old_value=getattr(rec, field), new_value=new_value,
            proposed_by=proposed_by, proposed_by_kind=proposed_by_kind,
            reason=reason)
        self._store(proposal)
        self._emit("proposal_created", proposal, None)
        return proposal

    def write_cell(self, view_id: str, record_id: str, field: str,
                   new_value: Any, actor: str = "user",
                   actor_kind: str = "user", reason: str = "") -> dict:
        """The sheet's write path. Protected columns route to a proposal.

        Non-protected editable columns write through to the record and fire
        `cell_edited`. A protected column raises `protected_column` when the
        caller insists on a direct write (`route_to_proposal=False` semantics),
        otherwise it returns the created proposal.
        """
        view = self.by_id.get(view_id)
        if not isinstance(view, SheetView):
            raise validation(f"unknown view_id '{view_id}'")
        rec = self.by_id.get(record_id)
        if rec is None:
            raise validation(f"unknown record_id '{record_id}'")
        if field not in view.editable_columns:
            raise protected_column(field, view_id)

        if field in view.protected_columns:
            proposal = self.propose_edit(view_id, record_id, field, new_value,
                                         actor, actor_kind, reason)
            return {"applied": False, "proposal_id": proposal.id,
                    "status": "pending"}

        previous = rec.model_dump()
        setattr(rec, field, new_value)
        self._emit("cell_edited", rec, previous)
        return {"applied": True, "record_id": record_id, "field": field}

    def direct_write(self, record_id: str, field: str, new_value: Any,
                     view_id: str | None = None) -> None:
        """Low-level write. Refuses protected columns outright.

        There is no way to bypass a proposal — guide § security rules.
        """
        rec = self.by_id.get(record_id)
        if rec is None:
            raise validation(f"unknown record_id '{record_id}'")
        if field in DEFAULT_PROTECTED_FIELDS:
            raise protected_column(field, view_id)
        previous = rec.model_dump()
        setattr(rec, field, new_value)
        self._emit("cell_edited", rec, previous)

    def list_proposals(self, view_id: str | None = None,
                       status: str | None = None) -> list[EditProposal]:
        """Pending proposals, optionally filtered by view and status."""
        out = [p for p in self.records.get(EditProposal.__name__, [])
               if isinstance(p, EditProposal)]
        if view_id is not None:
            out = [p for p in out if p.view_id == view_id]
        if status is not None:
            out = [p for p in out if p.status == status]
        return out

    def resolve_proposal(self, proposal_id: str, decision: str,
                         resolved_by: str = "user",
                         resolved_by_kind: str = "user") -> EditProposal:
        """Approve or reject a proposal — guide § "Edit proposals" steps 3-5.

        On approval the proposal is handed to the tool that owns the record,
        which applies it; the sheet then refreshes from the KG. On rejection
        the cell returns to its old value. An agent's proposal can never be
        approved by an agent.
        """
        p = self.by_id.get(proposal_id)
        if not isinstance(p, EditProposal):
            raise validation(f"unknown proposal_id '{proposal_id}'")
        if p.status != "pending":
            raise validation(f"proposal '{proposal_id}' is already {p.status}")
        if decision not in ("approve", "reject"):
            raise validation(f"decision must be 'approve' or 'reject', got '{decision}'")

        # Intent Verification: an agent's proposal always needs a person.
        if decision == "approve":
            if p.needs_human and resolved_by_kind == "agent":
                raise permission_denied(
                    "resolve_proposal",
                    ["agent proposals require a human approver"])
            if self.intent_verifier is not None and not self.intent_verifier(p):
                raise permission_denied(
                    "resolve_proposal",
                    ["Intent Verification did not approve this edit"])

        previous = p.model_dump()
        p.status = "approved" if decision == "approve" else "rejected"
        p.resolved_by = resolved_by
        p.resolved_at = now()

        if p.status == "approved" and self.proposal_sink is not None:
            self.proposal_sink(p)

        # Every proposal, approved or not, is kept for the Audit Agent.
        self._emit("proposal_resolved", p, previous)
        return p

    # ------------------------------------------------------------------- reads

    def list_records(self, record_type: type[Record],
                     where: Callable[[Record], bool] | None = None,
                     cursor: str | None = None, limit: int = PAGE_LIMIT) -> dict:
        """Paginated read: at most 200 items plus next_cursor. Never mutates."""
        rows = [r for r in self.records.get(record_type.__name__, [])
                if where is None or where(r)]
        start = 0
        if cursor:
            try:
                start = int(cursor)
            except ValueError:
                raise validation("invalid cursor") from None
        if start < 0:
            raise validation("cursor must not be negative")
        limit = max(1, min(limit, PAGE_LIMIT))
        page = rows[start:start + limit]
        next_cursor = str(start + limit) if start + limit < len(rows) else None
        return {"items": page, "next_cursor": next_cursor}

    def query_records(self, query: dict) -> list[Record]:
        """Guide-shaped query, e.g. {type: "Obligation", status: ["open", "overdue"]}.

        Never mutates: overdue is derived, not written. Call `refresh_overdue()`
        to persist the transition and emit `overdue`.
        """
        rtype = query.get("type")
        if rtype is not None:
            if rtype not in self.records:
                return []
            pool = list(self.records[rtype])
        else:
            pool = [r for rows in self.records.values() for r in rows]
        return [r for r in pool if self._record_matches_query(r, query)]

    # --------------------------------------------------------- overdue handling

    @staticmethod
    def _is_past_due(rec: Record) -> bool:
        due = getattr(rec, "due_at", None)
        if not isinstance(rec, Obligation) or due is None:
            return False
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return due < now()

    def effective_status(self, rec: Record) -> str:
        """Status as a reader should see it, without mutating the record.

        A past-due open/partial obligation reads as overdue even before the
        sweep has persisted it, so guide queries for `status: ["open",
        "overdue"]` return the right rows.
        """
        status = getattr(rec, "status", None)
        if not isinstance(rec, Obligation) or status is None:
            return status
        if status in OVERDUE_ELIGIBLE and self._is_past_due(rec):
            return "overdue"
        return status

    def refresh_overdue(self) -> list[Obligation]:
        """Persist open/partial -> overdue and emit `overdue`.

        Explicit sweep, called by the service layer (on a timer and before
        reconcile/render), not by reads. Returns the obligations that changed.
        """
        changed = []
        for rec in list(self.records.get(Obligation.__name__, [])):
            if not isinstance(rec, Obligation):
                continue
            if rec.status in OVERDUE_ELIGIBLE and self._is_past_due(rec):
                previous = rec.model_dump()
                rec.status = "overdue"
                changed.append(rec)
                self._emit("overdue", rec, previous)
        return changed

    # ------------------------------------------------------------- idempotency

    def idempotent(self, key: str | None, fn: Callable[[], dict]) -> dict:
        """Same idempotency_key returns the first result instead of duplicating.

        A call that raises is not cached, so retrying with the same key after a
        transient failure still does the work.
        """
        if key is None:
            return fn()
        if key in self.idempotency:
            return self.idempotency[key]
        result = fn()
        self.idempotency[key] = result
        return result

    # ------------------------------------------------------------------ events

    def subscribe(self, query: dict, events: list[str], handler: str) -> Subscription:
        if len(self.subscriptions) >= self.WATCH_LIMIT:
            raise rate_limited(
                f"watch subscription limit ({self.WATCH_LIMIT}) reached")
        unknown = set(events) - VALID_EVENTS
        if unknown:
            raise validation(f"unknown events: {sorted(unknown)}")
        if not events:
            raise validation("events must not be empty")
        if handler not in self.handler_registry and not str(handler).startswith("http"):
            raise validation(
                f"handler '{handler}' is not a registered agent endpoint or an HTTP URL")
        sub = Subscription(query, events, handler)
        self.subscriptions[sub.subscription_id] = sub
        return sub

    def unsubscribe(self, subscription_id: str) -> bool:
        return self.subscriptions.pop(subscription_id, None) is not None

    def register_handler(self, name: str, fn: Callable[[dict], None]) -> None:
        self.handler_registry[name] = fn

    def _record_matches_query(self, record: Record, query: dict) -> bool:
        """Match a guide-shaped query against a record.

        Handles the `type` pseudo-field, list membership (e.g. `status: [...]`),
        and compares dates as ISO 8601 strings so API-supplied filters work
        against datetime fields. `status` is compared via `effective_status`.
        """
        dump = record.model_dump(mode="json")
        for key, value in query.items():
            if key == "type":
                actual: Any = type(record).__name__
            elif key == "status":
                actual = self.effective_status(record)
            else:
                actual = dump.get(key)
            if isinstance(value, list):
                if actual not in value:
                    return False
            elif actual != value:
                return False
        return True

    def emit(self, event: str, record: Record, previous: dict | None = None) -> None:
        """Public emit for the service layer (e.g. cell_edited from a webhook)."""
        if event not in VALID_EVENTS:
            raise validation(f"unknown event '{event}'")
        self._emit(event, record, previous)

    def _emit(self, event: str, record: Record, previous: dict | None) -> None:
        for sub in self.subscriptions.values():
            if event in sub.events and self._record_matches_query(record, sub.query):
                sub.pending.append({
                    "event": event,
                    "record": record.model_dump(mode="json"),
                    "previous": previous,
                    "attempts": 0,
                })
        self._flush()

    def _flush(self) -> None:
        """At-least-once delivery for in-process handlers; URL handlers are
        drained by the service layer."""
        for sub in list(self.subscriptions.values()):
            fn = self.handler_registry.get(sub.handler)
            if fn is None:
                continue
            still: list[dict[str, Any]] = []
            for msg in sub.pending:
                msg["attempts"] += 1
                try:
                    fn(msg)
                    sub.delivered.append(msg)
                except Exception:
                    if msg["attempts"] < 3:
                        still.append(msg)
                    else:
                        sub.failed.append(msg)
            sub.pending = still

    def drain_http(self, poster: Callable[[str, dict], None]) -> None:
        """Service layer: deliver pending messages to URL handlers via poster(url, msg)."""
        for sub in list(self.subscriptions.values()):
            if sub.handler in self.handler_registry:
                continue
            if not str(sub.handler).startswith("http"):
                continue
            still: list[dict[str, Any]] = []
            for msg in sub.pending:
                msg["attempts"] += 1
                try:
                    poster(sub.handler, msg)
                    sub.delivered.append(msg)
                except Exception:
                    if msg["attempts"] < 3:
                        still.append(msg)
                    else:
                        sub.failed.append(msg)
            sub.pending = still


class MultiTenantGraph:
    """One KG per business_id."""

    def __init__(self) -> None:
        self._graphs: dict[str, KnowledgeGraph] = {}

    def for_business(self, business_id: str) -> KnowledgeGraph:
        if not business_id:
            raise validation("business_id is required")
        if business_id not in self._graphs:
            self._graphs[business_id] = KnowledgeGraph(business_id)
        return self._graphs[business_id]

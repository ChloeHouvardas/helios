"""invoice-payer — a generated Helios application (worked example).

Run:  python3 app.py            (offline, no API key needed)
      python3 app.py --json     (machine-readable result)
      python3 app.py --invoice INV-1003

What this file is
-----------------
A complete, runnable Helios app that answers the user request:

    "Make me an app that automatically manages and pays my invoices."

It is the reference for `entry-point/`: read it alongside BUILD.md to see what
those instructions actually produce. It is deliberately a prototype — a few
hundred lines, no web UI, no database server — because the point is to show the
wiring, not to ship a product.

What it composes (nothing here is invented)
-------------------------------------------
1. `fin_tools` (platform/financial-tools) — the real financial tool registry:
   `payments.balance`, `payments.send`, `email.send`, plus the Intent
   Verification service that produces the auth code a payment requires.
2. `audit_agent` (platform/audit-agent-builder) — the standalone audit agent,
   built from `agents/invoice-payer-audit.yaml`. It issues a signed grant for
   the task and adjudicates every side-effect call.
3. `agent_toolkit` (platform/action-agent-builder) — the action-side manifest
   format, the append-only ActionLog, and the run directory it writes to.

The design principle, enforced below rather than asserted: **the model
proposes, code decides.**

Why the gate is the standalone auditor
--------------------------------------
`agent_toolkit`'s in-process `AuditGate` implements receivables semantics
(extension days, contact frequency) and its `MandateLimits` accepts only four
collect-shaped keys. This is a payables app, so that gate cannot express its
policy. The authoritative gate here is therefore the standalone audit agent,
whose manifest declares payables rules.

That is an *unresolved architectural seam*, not a settled design: the two gates
overlap and the team has not agreed which is authoritative. This example picks
"the app-local gate becomes a client of the standalone auditor", because an app
cannot be trusted to police itself. See `entry-point/references/platform-status.md`
and the open question in `platform/audit-agent-builder/README.md`.

Who holds authority
-------------------
Two separate human checkpoints appear below, and they are not the same thing:

- **Intent confirmation** (`Owner.confirm_intent`) happens *before* a payment is
  proposed. The auth code it produces is an argument the audit rules require. If
  the agent could mint that code itself it would be self-authorizing, so the
  confirm step belongs to the owner, not the agent.
- **Hold resolution** (`Owner.decide_hold`) happens *after* the auditor holds a
  proposal that exceeded a limit.

Keeping them distinct is what makes `app.requires_verified_intent` a real
control instead of a formality the agent satisfies on its own.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Protocol

HERE = Path(__file__).resolve().parent
# Repo root for source-on-path runs. Derived defensively, NOT as parents[2]:
# in a deployed container this file is at /app/app.py, where Path("/app") has
# only one parent, so parents[2] raises IndexError at IMPORT time. That kills
# the process before uvicorn can serve /health, and the deploy smoke test fails
# with an opaque traceback instead of a readable missing-route error.
REPO = HERE.parents[2] if len(HERE.parents) > 2 else HERE

# The three platform packages. A generated app would declare these as
# dependencies; this example imports them in place so it runs with no install.
# Each entry is guarded by is_dir() because inside the golden base image the
# packages are pip-installed and these directories do not exist at all.
for _p in (
    REPO / "platform" / "audit-agent-builder" / "src",
    REPO / "platform" / "action-agent-builder" / "src",
    REPO / "platform" / "financial-tools",
):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from audit_agent import (  # noqa: E402
    ActionRequest,
    AuthorizationRequest,
    Decision,
    build_audit_agent,
)
from audit_agent.manifest import AuditManifest  # noqa: E402
from agent_toolkit.action_log import ActionLog  # noqa: E402
from agent_toolkit.manifest import load_manifest as load_action_manifest  # noqa: E402
from fin_tools import build_world  # noqa: E402

# Local development only. A real deployment sets a proper key and leaves
# allow_unsigned_grants=false; see references/platform-status.md.
os.environ.setdefault("HELIOS_AUDIT_SIGNING_KEY", "0" * 64)

OPERATING_ACCOUNT = "acme-operating"

# --- credential redaction -------------------------------------------------
# `audit_agent.redact()` masks a fixed set of credential-shaped keys, but that
# set does not include `auth_code` — the single-use payment credential produced
# by the Intent Verification service. Verified 2026-10-14: an auth_code passed
# as a tool argument was written in full to both the audit log and the action
# log. That is a platform gap, not a property of this example, and it is
# recorded in `entry-point/references/platform-status.md` for the team.
#
# Until it is fixed upstream, a generated app must not log payment credentials.
# Redaction happens here, at the point of writing, because this example owns
# what it writes. It extends the platform's own key set rather than
# substituting for it, so anything `audit_agent` already masks stays masked.
from audit_agent.contracts import REDACTED, REDACTED_KEYS  # noqa: E402

_CREDENTIAL_KEYS = frozenset(REDACTED_KEYS) | {
    "auth_code", "authcode", "authorization_code", "otp", "cvv", "cvc",
    "card_number", "pan", "signing_key", "nonce",
}


def redact_arguments(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Shallow-redact credential-shaped keys before anything is logged.

    Shallow on purpose, matching `audit_agent.redact()`: a deep walk would
    rewrite nested keys named `token` that mean something else entirely.
    """
    if not arguments:
        return {}
    return {k: (REDACTED if k.lower() in _CREDENTIAL_KEYS else v)
            for k, v in dict(arguments).items()}


# ---------------------------------------------------------------------------
# 1. The app's own data — what a generated app's database would hold
# ---------------------------------------------------------------------------

@dataclass
class InvoiceBook:
    """The app's data layer.

    In a generated app this is the database the schema tool created from the
    spec's `data.local.entities`; here it is the JSON file, because the point is
    the wiring, not the storage engine.
    """

    suppliers: dict[str, dict[str, Any]]
    invoices: dict[str, dict[str, Any]]
    balances_cents: dict[str, int]

    @classmethod
    def load(cls, path: Path) -> "InvoiceBook":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            suppliers=raw["suppliers"],
            invoices={i["invoice_id"]: i for i in raw["invoices"]},
            balances_cents=raw["opening_balances_cents"],
        )

    def get(self, invoice_id: str) -> dict[str, Any] | None:
        return self.invoices.get(invoice_id)

    # --- the trusted values the audit rules read -------------------------
    def amount_due(self, invoice_id: str) -> Decimal | None:
        inv = self.invoices.get(invoice_id)
        return Decimal(inv["amount"]) if inv else None

    def amount_due_cents(self, invoice_id: str) -> int | None:
        amount = self.amount_due(invoice_id)
        return int((amount * 100).to_integral_value()) if amount is not None else None

    def is_duplicate(self, invoice_id: str) -> bool | None:
        inv = self.invoices.get(invoice_id)
        return bool(inv["duplicate"]) if inv else None

    def payee_details_changed(self, invoice_id: str) -> bool | None:
        inv = self.invoices.get(invoice_id)
        if inv is None:
            return None
        supplier = self.suppliers.get(inv["supplier_id"])
        return bool(supplier["payment_details_changed"]) if supplier else None


# ---------------------------------------------------------------------------
# 2. The fact source — the single most important class in this file
# ---------------------------------------------------------------------------

class InvoiceFactSource:
    """Trusted facts for the audit agent, read from the app's own data.

    This is what makes "facts are not claims" true rather than a slogan. The
    audit agent resolves every value a rule depends on through this object. It
    never reads the action agent's arguments for those values — the agent gets
    to *name* which invoice it means, and nothing more.

    Consequence: an agent that tries to pay **more** than an invoice is worth is
    held, because the ceiling comes from here. An agent that quietly omits a
    changed-bank-details flag is still denied, because the flag comes from here.
    An agent that lies in `resolved_facts` about the recorded amount changes
    nothing, because those values are never merged into the facts.

    One asymmetry worth being precise about, because it is easy to overstate:
    the `argument` vs `max_from_fact` shape used by `app.amount_matches_invoice`
    constrains what the agent **asked to pay** against a trusted ceiling. It
    therefore catches overpayment, not underpayment — paying $1.00 against a
    $48,000 invoice is inside that ceiling and this rule allows it. Understating
    is stopped by a different mechanism: `payments.send` will not execute without
    an `auth_code` bound to that exact (amount, currency, payee), so a smaller
    payment requires the owner to have confirmed that smaller amount. Two
    directions, two controls. See tests/test_controls.py.

    A rule that binds on the fact instead of the argument
    (`fact: amount_due, max: ...`, as collect-reminder.yaml does) does catch a
    lie about the value, because it never reads the agent's number at all.

    Satisfies `audit_agent.FactSource`. `StaticFactSource` is the test/demo
    equivalent; a deployed app swaps in one of these backed by its database.
    """

    def __init__(self, book: InvoiceBook) -> None:
        self._book = book

    def amount_for(self, subject_id: str) -> Decimal | None:
        return self._book.amount_due(subject_id)

    def contact_count_for(self, subject_id: str, within_days: int) -> int | None:
        return None  # this app has no contact-frequency rule

    def counterparty_for(self, subject_id: str) -> str | None:
        inv = self._book.get(subject_id)
        return inv["supplier_id"] if inv else None

    def payment_details_changed_for(self, subject_id: str) -> bool | None:
        return self._book.payee_details_changed(subject_id)

    def extra_facts_for(self, subject_id: str) -> dict[str, Any]:
        """App-specific signals, keyed by the names the manifest's rules use."""
        cents = self._book.amount_due_cents(subject_id)
        duplicate = self._book.is_duplicate(subject_id)
        facts: dict[str, Any] = {}
        if cents is not None:
            facts["invoice_amount_cents"] = cents
        if duplicate is not None:
            facts["duplicate_invoice"] = duplicate
        return facts


# ---------------------------------------------------------------------------
# 3. The gate — proposals in, verdicts out
# ---------------------------------------------------------------------------

@dataclass
class GatedRuntime:
    """The app's tool runtime: a registry plus the audit agent that gates it.

    Read-only calls execute directly. Side-effect calls become proposals and are
    adjudicated against the manifest. This is `interfaces.md` §3 implemented.
    """

    registry: Any            # fin_tools ToolRegistry
    intents: Any             # fin_tools IntentService — the OWNER uses this
    auditor: Any             # audit_agent AuditAgent
    book: InvoiceBook
    log: ActionLog
    app_id: str = "invoice-payer"
    agent_id: str = "invoice-payer"
    principal: str = "owner"
    task_type: str = "payables.pay_invoice"
    _grant: Any = None
    _counter: int = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    # --- tools exposed to the agent -------------------------------------
    def tool_specs(self) -> list[dict[str, Any]]:
        """Model-facing tool declarations (Qwen-Agent function-calling shape).

        A real Qwen-Agent loop is handed this list. `lookup_invoice` is app-local
        (it reads this app's database) and is appended to the platform's catalog.
        """
        specs = [
            {"name": t.name, "description": t.description,
             "parameters": dict(t.input_schema)}
            for t in self.registry.list()
        ]
        specs.append({
            "name": "lookup_invoice",
            "description": ("Look up one supplier invoice and the supplier it "
                            "belongs to, including days overdue. Read-only."),
            "parameters": {"type": "object", "required": ["invoice_id"],
                           "properties": {"invoice_id": {"type": "string"}}},
        })
        return specs

    # --- authority: what may this agent do for this task? ----------------
    def authorize(self, summary: str, invoice_id: str) -> Any:
        self._grant = self.auditor.authorize(AuthorizationRequest(
            request_id=self._next_id("auth"),
            app_id=self.app_id, agent_id=self.agent_id, principal=self.principal,
            task_type=self.task_type, task_summary=summary,
            requested_tools=["payments.balance", "lookup_invoice",
                             "payments.send", "email.send"],
            context={"invoice_id": invoice_id},
        ))
        self.log.append("authorization", request_id=self._grant.request_id,
                        decision=self._grant.decision.value,
                        tools=self._grant.tool_names(),
                        expires_at=str(self._grant.expires_at))
        return self._grant

    # --- the read path: runs directly ------------------------------------
    def read(self, tool: str, **kwargs: Any) -> Any:
        if tool == "lookup_invoice":
            return self._lookup_invoice(**kwargs)
        spec = self.registry.get(tool)
        if spec is None:
            raise KeyError(f"unknown tool: {tool}")
        if spec.side_effects:
            raise ValueError(
                f"{tool} has side effects; it must go through propose(), not read()")
        result = self.registry.call(tool, **kwargs)
        self.log.append("tool_call", tool=tool, arguments=kwargs, gated=False)
        return result

    def _lookup_invoice(self, invoice_id: str) -> dict[str, Any]:
        inv = self.book.get(invoice_id)
        if inv is None:
            raise ValueError(f"unknown invoice: {invoice_id}")
        supplier = self.book.suppliers.get(inv["supplier_id"], {})
        result = {
            "invoice": inv,
            # `payment_details_changed` is deliberately withheld from the agent.
            # It is a control input, not business information the agent needs;
            # the auditor reads it from the fact source.
            "supplier": {k: v for k, v in supplier.items()
                         if k != "payment_details_changed"},
            "days_overdue": max(
                0, (date.today() - date.fromisoformat(inv["due_date"])).days),
        }
        self.log.append("tool_call", tool="lookup_invoice",
                        arguments={"invoice_id": invoice_id}, gated=False)
        return result

    # --- the write path: propose, adjudicate, maybe execute ---------------
    def propose(self, tool: str, arguments: Mapping[str, Any], *,
                invoice_id: str | None = None) -> dict[str, Any]:
        """Turn a side-effect call into a proposal and ask the auditor.

        `invoice_id` is passed as a *subject name*, not as evidence. The auditor
        uses it only to decide which record to look up; every value it then
        judges by comes from the fact source. Naming a subject is a claim, and
        claims are not trusted — but they do select the record.
        """
        proposal_id = self._next_id("prop")
        self.log.append("proposal", proposal_id=proposal_id, tool=tool,
                        arguments=redact_arguments(arguments))

        resolved = {"invoice_id": invoice_id} if invoice_id else {}
        decision = self.auditor.adjudicate(ActionRequest(
            request_id=proposal_id,
            app_id=self.app_id, agent_id=self.agent_id, principal=self.principal,
            task_type=self.task_type, tool=tool, arguments=dict(arguments),
            authorization=self._grant, resolved_facts=resolved,
        ))

        self.log.append(
            "verdict", proposal_id=proposal_id, tool=tool,
            decision=decision.decision.value, reason=decision.reason,
            findings=[{"rule": f.rule_id, "severity": f.severity.value}
                      for f in decision.findings],
            hold_id=decision.hold_id,
        )

        outcome: dict[str, Any] = {
            "proposal_id": proposal_id, "tool": tool,
            "decision": decision.decision.value, "reason": decision.reason,
            "arguments": dict(arguments),
        }
        if decision.decision is Decision.HOLD:
            outcome["hold_id"] = decision.hold_id
            outcome["owner_question"] = decision.owner_question
        elif decision.decision is Decision.ALLOW:
            outcome["result"] = self.registry.call(tool, **dict(arguments))
        return outcome

    def execute_held(self, outcome: Mapping[str, Any]) -> dict[str, Any]:
        """Execute a proposal the owner has just approved.

        Called only after `resolve_hold(approve=True)`. Re-uses the exact
        arguments that were held — an approval applies to that action and no
        other, so the agent cannot swap in a different amount afterwards.
        """
        result = self.registry.call(outcome["tool"], **outcome["arguments"])
        self.log.append("held_executed", proposal_id=outcome["proposal_id"],
                        tool=outcome["tool"],
                        state=(result or {}).get("state"))
        return result

    # --- the owner's side -------------------------------------------------
    def pending_holds(self) -> list[Any]:
        return self.auditor.pending_holds(self.principal)

    def resolve_hold(self, hold_id: str, *, approve: bool,
                     decided_by: str = "owner") -> Any:
        record = self.auditor.resolve_hold(hold_id, approve=approve,
                                           decided_by=decided_by)
        self.log.append("hold_resolved", hold_id=hold_id, approve=approve,
                        decided_by=decided_by, status=record.status)
        return record


# ---------------------------------------------------------------------------
# 4. Building the app
# ---------------------------------------------------------------------------

def build_app(run_dir: Path | str | None = None) -> GatedRuntime:
    """Assemble a running invoice-payer app from its manifests and data.

    This is the wiring a scaffolder would generate: read the spec, load both
    manifests, build the auditor over the app's fact source, expose the
    financial tools, and run both agents inside the app process.
    """
    book = InvoiceBook.load(HERE / "data" / "invoices.json")

    # The financial tools registry, seeded with this app's opening balances.
    bundle = build_world()
    for account, cents in book.balances_cents.items():
        bundle.world.balances[account] = cents

    # Both manifests load and are validated as they load.
    action = load_action_manifest(HERE / "agents" / "invoice-payer-action.json")
    audit = AuditManifest.load(HERE / "agents" / "invoice-payer-audit.yaml")

    # The cross-file invariant the app-builder validator is meant to check:
    # every tool the action agent may use must be declared by the audit agent.
    # Enforced here by hand, because that validator does not exist yet.
    declared = {t.name for t in audit.tools}
    undeclared = sorted(set(action.mandate.allowed_tools) - declared)
    if undeclared:
        raise ValueError(
            f"action manifest offers tool(s) the audit manifest has not declared: "
            f"{undeclared}. An ungated side effect is not allowed — declare them "
            f"in {audit.app_id}'s audit manifest.")

    run_dir = Path(run_dir) if run_dir is not None else (HERE / "runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(run_dir.glob("run-*")))
    log = ActionLog(run_dir, run_id=f"run-{existing + 1}", agent=action.name)

    auditor = build_audit_agent(
        audit,
        fact_source=InvoiceFactSource(book),
        log_path=log.run_dir / "audit.jsonl",
    )

    return GatedRuntime(registry=bundle.registry, intents=bundle.intents,
                        auditor=auditor, book=book, log=log, agent_id=action.name)


# ---------------------------------------------------------------------------
# 5. The owner — a human checkpoint, not part of the agent
# ---------------------------------------------------------------------------

class Owner(Protocol):
    """The human in the loop. A real app surfaces these through the
    `approval_queue` UI pattern (specs/app-builder.md)."""

    def confirm_intent(self, request: dict[str, Any]) -> bool:
        """True to authorise a payment; the auth code is then minted."""

    def decide_hold(self, hold: dict[str, Any]) -> bool:
        """Approve or decline a proposal the auditor held."""


class ScriptedOwner:
    """A stand-in owner so the example runs offline and deterministically.

    Deliberately **inattentive about the fraud case**, and that is the point of
    the example rather than an oversight. A busy owner rubber-stamps the payment
    to the supplier whose banking details just changed. The payment still does
    not go out, because `app.payee_details_changed` denies it in code. That is
    the platform's central claim demonstrated rather than asserted: the control
    does not depend on the human noticing, and it does not depend on the agent
    behaving.

    The owner's other decisions encode ordinary business judgement:
    - A held large payment is approved (it matched the invoice exactly and the
      owner knows the supplier).
    - A held duplicate is declined (only the owner can tell two genuine
      same-amount invoices from one billed twice, and this one looks wrong).
    """

    def __init__(self, book: InvoiceBook, *, verbose: bool = True) -> None:
        self.book = book
        self.verbose = verbose
        self.decisions: list[dict[str, Any]] = []

    def _say(self, text: str) -> None:
        if self.verbose:
            print(text)

    def confirm_intent(self, request: dict[str, Any]) -> bool:
        amount = Decimal(request["amount_cents"]) / 100
        self._say(f"    [owner asked] confirm payment of "
                  f"{amount} {request['currency']} to {request['payee']} "
                  f"for {request['purpose']}?")
        # A busy owner confirms without checking the payee's details. The
        # auditor is what actually stops the fraudulent one — see run output.
        self._say("    [owner confirms]")
        self.decisions.append({"intent": request["purpose"], "confirmed": True})
        return True

    def decide_hold(self, hold: dict[str, Any]) -> bool:
        question = hold["question"]
        approve = "duplicate" not in question.lower()
        self._say(f"    [owner asked] {question}")
        self._say(f"    [owner decides] {'approve' if approve else 'decline'}")
        self.decisions.append({"hold_id": hold["hold_id"], "approved": approve})
        return approve


def _add_supplier_lookup(book: InvoiceBook) -> None:
    """Attach a supplier-keyed lookup the scripted owner needs."""
    def by_supplier(supplier_id: str) -> bool | None:
        supplier = book.suppliers.get(supplier_id)
        return bool(supplier["payment_details_changed"]) if supplier else None
    book.payee_details_changed_by_supplier = by_supplier  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 6. The action agent
# ---------------------------------------------------------------------------

class ScriptedPayerAgent:
    """A stand-in for the LLM action agent, so this example runs offline.

    It follows exactly the instructions in the action manifest: look up the
    invoice, check the balance, ask the owner to confirm an intent, propose the
    payment, notify the supplier. Substituting a real Qwen-Agent loop means
    replacing `run()` with a model call over `runtime.tool_specs()` — the gate
    underneath does not change, which is the whole point of the split.

    A Qwen-Agent-backed agent is not wired here because `qwen_agent` is not
    installed in this checkout and no API key is configured. See
    `entry-point/references/platform-status.md`.
    """

    def __init__(self, runtime: GatedRuntime, owner: Owner) -> None:
        self.runtime = runtime
        self.owner = owner

    def run(self, invoice_id: str) -> dict[str, Any]:
        r = self.runtime
        trace: list[dict[str, Any]] = []

        # Step 1 — read before acting. Never invent invoice details.
        inv = r.read("lookup_invoice", invoice_id=invoice_id)
        invoice, supplier = inv["invoice"], inv["supplier"]
        amount = Decimal(invoice["amount"])
        amount_cents = int((amount * 100).to_integral_value())
        trace.append({"step": "lookup", "amount": str(amount),
                      "supplier": supplier["name"],
                      "days_overdue": inv["days_overdue"]})

        # Step 2 — is there money to pay it with?
        balance = r.read("payments.balance", account=OPERATING_ACCOUNT)["balance_cents"]
        trace.append({"step": "balance", "balance_cents": balance})
        if balance < amount_cents:
            return {"invoice_id": invoice_id, "status": "insufficient_funds",
                    "balance_cents": balance, "needed_cents": amount_cents,
                    "trace": trace}

        # Step 3 — authority for this task, then a human-confirmed intent.
        # The auth code binds (amount, currency, payee), and only the owner can
        # produce it. An agent that skipped this step cannot pay at all.
        r.authorize(f"Pay invoice {invoice_id} to {supplier['name']}", invoice_id)
        trace.append({"step": "authorize", "granted": r._grant.tool_names()})

        intent = r.intents.capture_intent(
            principal=r.principal, amount_cents=amount_cents,
            currency=invoice["currency"], payee=invoice["supplier_id"],
            purpose=f"invoice {invoice_id} ({invoice['reference']})",
        )
        # The agent captures the intent and then stops. Confirming it is the
        # owner's act, so the credential is minted only on the owner's say-so.
        wants_to_pay = self.owner.confirm_intent({
            "amount_cents": amount_cents, "currency": invoice["currency"],
            "payee": invoice["supplier_id"], "purpose": f"invoice {invoice_id}",
        })
        if not wants_to_pay:
            r.intents.revoke(intent.intent_id)
            trace.append({"step": "intent", "intent_id": intent.intent_id,
                          "confirmed": False})
            return {"invoice_id": invoice_id, "status": "refused_by_owner",
                    "reason": "the owner declined to confirm this payment",
                    "trace": trace}

        auth_code = r.intents.confirm(intent.intent_id, by=r.principal).code
        trace.append({"step": "intent", "intent_id": intent.intent_id,
                      "confirmed": True})

        # Step 4 — propose the payment. The gate decides.
        payment_args = {
            "payer": OPERATING_ACCOUNT,
            "payee": invoice["supplier_id"],
            "amount_cents": amount_cents,
            "currency": invoice["currency"],
            "auth_code": auth_code,
            "idempotency_key": f"pay-{invoice_id}",
            "memo": f"Settlement of {invoice_id} ({invoice['reference']})",
        }
        proposal = r.propose("payments.send", payment_args, invoice_id=invoice_id)
        trace.append({"step": "payment", "decision": proposal["decision"],
                      "reason": proposal["reason"]})

        if proposal["decision"] == "hold":
            # The agent pauses. It does not reword the request and retry,
            # because that would be circumventing a control.
            approved = self.owner.decide_hold({
                "invoice_id": invoice_id, "tool": "payments.send",
                "amount_cents": amount_cents,
                "question": proposal.get("owner_question") or proposal["reason"],
                "hold_id": proposal["hold_id"],
            })
            record = r.resolve_hold(proposal["hold_id"], approve=approved)
            trace.append({"step": "owner", "approved": approved,
                          "status": record.status})
            if not approved:
                return {"invoice_id": invoice_id, "status": "declined_by_owner",
                        "reason": proposal["reason"], "trace": trace}
            # Approved: the held action executes with the exact same arguments.
            result = r.execute_held(proposal)
            proposal = {"decision": "allow", "reason": "owner approved the hold",
                        "result": result}
            trace.append({"step": "payment_after_approval",
                          "state": (result or {}).get("state")})

        if proposal["decision"] == "deny":
            return {"invoice_id": invoice_id, "status": "refused",
                    "reason": proposal["reason"], "trace": trace}

        state = (proposal.get("result") or {}).get("state")
        if state != "settled":
            return {"invoice_id": invoice_id, "status": f"payment_{state}",
                    "reason": (proposal.get("result") or {}).get("reject_reason"),
                    "trace": trace}

        # Step 5 — notify the supplier. Also gated, also logged.
        note = r.propose("email.send", {
            "to": supplier["email"],
            "subject": f"Payment sent for {invoice_id}",
            "body": (f"We have paid invoice {invoice_id} ({invoice['reference']}) "
                     f"in the amount of {amount} {invoice['currency']} to "
                     f"{supplier['name']}. Thank you."),
        }, invoice_id=invoice_id)
        trace.append({"step": "notify", "decision": note["decision"]})

        return {"invoice_id": invoice_id, "status": "paid",
                "amount": str(amount), "currency": invoice["currency"],
                "supplier": supplier["name"], "trace": trace}


# ---------------------------------------------------------------------------
# 7. Demo
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="invoice-payer: a runnable Helios app example")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable results")
    parser.add_argument("--invoice", action="append", default=None,
                        help="run one invoice id (repeatable); default: all")
    parser.add_argument("--trace", action="store_true",
                        help="print the per-step trace for each invoice")
    args = parser.parse_args(argv)

    runtime = build_app()
    _add_supplier_lookup(runtime.book)
    owner = ScriptedOwner(runtime.book, verbose=not args.json)
    agent = ScriptedPayerAgent(runtime, owner=owner)

    if not args.json:
        manifest = runtime.auditor.manifest
        print(f"invoice-payer — app_id={runtime.app_id} task={runtime.task_type}")
        print(f"tools: {', '.join(t.name for t in manifest.tools)}")
        print(f"rules: {', '.join(r.id for r in manifest.rules)}")
        print()

    ids = args.invoice or list(runtime.book.invoices)
    results = []
    for invoice_id in ids:
        if not args.json:
            inv = runtime.book.get(invoice_id)
            supplier = runtime.book.suppliers[inv["supplier_id"]]["name"]
            print(f"  {invoice_id}  {inv['amount']:>10} {inv['currency']}  {supplier}")
        outcome = agent.run(invoice_id)
        results.append(outcome)
        if not args.json:
            print(f"    -> {outcome['status'].upper()}"
                  + (f"  ({outcome['reason']})" if outcome.get("reason") else ""))
            if args.trace:
                for step in outcome["trace"]:
                    print(f"       {step}")
            print()

    entries = runtime.log.entries()
    summary = {
        "app_id": runtime.app_id,
        "task_type": runtime.task_type,
        "invoices": len(results),
        "by_status": {s: sum(1 for r in results if r["status"] == s)
                      for s in sorted({r["status"] for r in results})},
        "action_log_entries": len(entries),
        "kinds_logged": sorted({e.get("kind") for e in entries}),
        "audit_log": str(runtime.log.run_dir / "audit.jsonl"),
        "owner_decisions": owner.decisions,
        "results": results,
    }

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"  status: {summary['by_status']}")
        print(f"  action log: {len(entries)} entries -> {runtime.log.run_dir}")
        print(f"  audit log (hash-chained): {summary['audit_log']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
